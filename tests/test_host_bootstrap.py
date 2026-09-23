from __future__ import annotations

import base64
import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HostBootstrapTests(unittest.TestCase):
    def fixture(self, root: Path, *, ready: bool, public_ip_after: int = 1, wait_seconds: float = 300):
        binaries = root / "bin"
        binaries.mkdir()
        log = root / "commands.jsonl"

        metadata_reads = 0

        class MetadataHandler(http.server.BaseHTTPRequestHandler):
            def respond(self, status: int, payload: bytes = b"") -> None:
                with log.open("a") as commands:
                    commands.write(json.dumps(["metadata", self.command, self.path, status]) + "\n")
                self.send_response(status)
                self.end_headers()
                self.wfile.write(payload)

            def do_PUT(self) -> None:
                if (self.path == "/latest/api/token"
                        and self.headers.get("X-aws-ec2-metadata-token-ttl-seconds") == "60"):
                    self.respond(200, b"fixture-token")
                else:
                    self.respond(400)

            def do_GET(self) -> None:
                nonlocal metadata_reads
                if (self.path != "/latest/meta-data/public-ipv4"
                        or self.headers.get("X-aws-ec2-metadata-token") != "fixture-token"):
                    self.respond(401)
                    return
                metadata_reads += 1
                if metadata_reads < public_ip_after:
                    self.respond(404)
                else:
                    self.respond(200, b"203.0.113.10")

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MetadataHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.addCleanup(stop_server)
        fake = binaries / "fake.py"
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "id": print("0")
elif name == "uname": print("aarch64")
elif name == "timeout":
    assert args[0] == "--kill-after=10s"
    assert args[1] in ("30s", "120s", "300s", "600s")
    sys.exit(subprocess.call(args[2:]))
elif name == "systemctl":
    if args == ["start", "minecraft-storage.service"] and os.environ["STORAGE_READY"] == "no":
        sys.exit(1)
''')
        fake.chmod(0o755)
        for command in ("id", "uname", "timeout", "apt-get", "systemctl", "docker"):
            (binaries / command).symlink_to(fake)
        release = root / "os-release"
        release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        payload = b'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(["minecraft-storage", *sys.argv[1:]]) + "\\n")
sys.exit(0 if os.environ["STORAGE_" + sys.argv[1].upper() + "_READY"] == "yes" else 1)
'''
        script = (ROOT / "scripts/host-bootstrap.sh.tftpl").read_text()
        script = script.replace("${storage_script_base64}", base64.b64encode(payload).decode())
        script = script.replace("${data_volume_id}", "vol-0123456789abcdef0")
        script = script.replace("${data_filesystem_uuid}", "01234567-89ab-4cde-8fab-0123456789ab")
        script = script.replace("169.254.169.254", f"127.0.0.1:{server.server_port}")
        script = script.replace("time.monotonic() + 300", f"time.monotonic() + {wait_seconds}")
        script = script.replace("time.sleep(min(2,", "time.sleep(min(0.01,")
        script = script.replace("/etc/os-release", str(release))
        for target in ("/etc/minecraft", "/etc/systemd/system", "/usr/local/sbin"):
            script = script.replace(target, str(root / target.lstrip("/")))
        rendered = root / "host-bootstrap.sh"
        rendered.write_text(script)
        environment = os.environ.copy()
        environment.update(PATH=f"{binaries}:{environment['PATH']}", COMMAND_LOG=str(log), STORAGE_READY="yes" if ready else "no", STORAGE_MOUNT_READY="yes", STORAGE_VERIFY_READY="yes")
        return rendered, environment, log, payload

    def test_repeat_bootstrap_installs_identical_storage_gate_and_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log, payload = self.fixture(root, ready=True)
            for _ in range(2):
                result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "usr/local/sbin/minecraft-storage").read_bytes(), payload)
            self.assertEqual((root / "etc/minecraft").stat().st_mode & 0o777, 0o700)
            gate = (root / "etc/systemd/system/docker.service.d/10-minecraft-storage.conf").read_text()
            self.assertIn("Requires=minecraft-storage.service", gate)
            self.assertIn("After=minecraft-storage.service", gate)
            self.assertIn("ExecStartPre=", gate)
            self.assertIn("minecraft-storage verify", gate)
            service = (root / "etc/systemd/system/minecraft-storage.service").read_text()
            self.assertIn("minecraft-storage mount", service)
            self.assertNotIn("initialize", service)
            self.assertIn("TimeoutStartSec=120", service)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(sum(call == ["systemctl", "start", "docker.service"] for call in calls), 2)
            install = next(call for call in calls if call[0] == "apt-get" and "install" in call)
            self.assertIn("docker.io", install)
            self.assertIn("docker-compose-v2", install)
            self.assertIn("time", install)

    def test_package_commands_wait_for_public_ip_association(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log, _ = self.fixture(root, ready=True, public_ip_after=3)
            result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            pending = [index for index, call in enumerate(calls) if call == [
                "metadata", "GET", "/latest/meta-data/public-ipv4", 404]]
            assigned = calls.index(["metadata", "GET", "/latest/meta-data/public-ipv4", 200])
            update = next(index for index, call in enumerate(calls) if call[0] == "apt-get")
            self.assertEqual(len(pending), 2)
            self.assertLess(max(pending), assigned)
            self.assertLess(assigned, update)

    def test_missing_public_ip_fails_before_package_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log, _ = self.fixture(
                root, ready=True, public_ip_after=1_000_000, wait_seconds=0.05)
            result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Timed out waiting for the host's public IPv4 association", result.stderr)
            self.assertIn("bootstrap failed", result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertTrue(any(call[0] == "metadata" for call in calls))
            self.assertFalse(any(call[0] == "apt-get" for call in calls))

    def test_storage_failure_stops_bootstrap_before_docker_can_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log, _ = self.fixture(root, ready=False)
            result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertNotIn(["systemctl", "start", "docker.service"], calls)
            self.assertIn("bootstrap failed", result.stderr)

    def test_active_units_cannot_hide_storage_failure_on_a_repeat_bootstrap(self) -> None:
        for failed_operation in ("mount", "verify"):
            with self.subTest(failed_operation=failed_operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script, environment, log, _ = self.fixture(root, ready=True)
                result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # systemctl still succeeds: the oneshot and idle Docker are already active.
                environment[f"STORAGE_{failed_operation.upper()}_READY"] = "no"
                log.write_text("")
                result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, "active units hid a failed fresh storage check")
                self.assertNotIn("Host prerequisites and retained storage are ready", result.stdout)
                self.assertIn("bootstrap failed", result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertTrue(any(call[:2] == ["minecraft-storage", failed_operation] for call in calls))


if __name__ == "__main__":
    unittest.main()
