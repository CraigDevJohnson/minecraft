from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HostBootstrapTests(unittest.TestCase):
    def fixture(self, root: Path, *, ready: bool):
        binaries = root / "bin"
        binaries.mkdir()
        log = root / "commands.jsonl"
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
