from __future__ import annotations

import base64
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_crafty  # noqa: E402

TEMPLATE = ROOT / "scripts/world-bootstrap.sh.tftpl"
INPUTS = {"world_id": "creative", "backup_bucket": "minecraft-test-example-backups", "region": "us-west-2"}
HOST_PATHS = ("/etc/os-release", "/etc/minecraft", "/etc/systemd/system", "/usr/local/sbin", "/opt/minecraft-crafty",
              "/srv/minecraft", "/var/lib/minecraft", "/var/lib/cloud")


def render(aws_cli_script: bytes) -> str:
    script = TEMPLATE.read_text().replace("${aws_cli_script_base64}", base64.b64encode(aws_cli_script).decode())
    for name, value in INPUTS.items():
        script = script.replace("${" + name + "}", value)
    return script


class WorldBootstrapTests(unittest.TestCase):
    def fixture(self, root: Path):
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
    sys.exit(subprocess.call(args[2:]))
elif name == os.environ.get("FAILING"):
    sys.exit(1)
''')
        fake.chmod(0o755)
        for command in ("id", "uname", "timeout", "apt-get", "systemctl", "docker", "shutdown", "sync"):
            (binaries / command).symlink_to(fake)
        (root / "etc/systemd/system").mkdir(parents=True)
        (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        script = render(b'#!/bin/sh\necho \'["install-aws-cli"]\' >>"$COMMAND_LOG"\n')
        for target in HOST_PATHS:
            script = script.replace(target, str(root / target.lstrip("/")))
        rendered = root / "part-001"
        rendered.write_text(script)
        environment = os.environ.copy()
        environment.update(PATH=f"{binaries}:{environment['PATH']}", COMMAND_LOG=str(log))
        return rendered, environment, log

    def run_script(self, script: Path, environment: dict, log: Path, failing: str = ""):
        log.write_text("")
        result = subprocess.run(["bash", str(script)], env={**environment, "FAILING": failing},
                                capture_output=True, text=True, timeout=10)
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_first_boot_prepares_the_host_and_powers_it_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log = self.fixture(root)
            result, calls = self.run_script(script, environment, log)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            written = root / "etc/minecraft/operator.json"
            self.assertEqual(written.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "etc/minecraft").stat().st_mode & 0o777, 0o700)
            # The paths the fixture moved under its own folder are the host's again before the adapter reads it.
            operator = root / "operator.json"
            operator.write_text(written.read_text().replace(str(root), ""))
            self.assertEqual(minecraft_crafty.load_config(operator), {
                "schema_version": 1, "runtime": "crafty", "crafty_url": "https://127.0.0.1:8443",
                "token_file": "/etc/minecraft/crafty-api.token", "data_root": "/srv/minecraft",
                "instances": {"creative": {"server_name": "creative", "java_port": 25565}},
                "backup": {"bucket": "minecraft-test-example-backups", "region": "us-west-2"}})

            # The launcher's adapter install refuses a folder its group or others may write.
            for folder in ("opt/minecraft-crafty", "opt/minecraft-crafty/scripts"):
                self.assertEqual((root / folder).stat().st_mode & 0o777, 0o755, folder)
            self.assertEqual(list((root / "opt/minecraft-crafty/scripts").iterdir()), [])
            self.assertFalse((root / "opt/minecraft-crafty/compose.yaml").exists())
            crafty = root / "srv/minecraft/crafty"
            self.assertEqual({path.name: path.stat().st_mode & 0o777 for path in crafty.iterdir()}, {
                "backups": 0o770, "logs": 0o770, "servers": 0o770, "config": 0o770, "import": 0o770,
                "state": 0o700, "staging": 0o700})
            self.assertFalse(crafty.is_mount())

            unit = (root / "etc/systemd/system/minecraft-crafty.service").read_text().replace(str(root), "")
            self.assertIn("Requires=docker.service\n", unit)
            self.assertIn("ConditionPathExists=/opt/minecraft-crafty/compose.yaml\n", unit)
            self.assertIn("minecraft-instance --config /etc/minecraft/operator.json host-drain; drained=$$?", unit)
            self.assertNotIn("minecraft-storage", unit)
            wrapper = root / "usr/local/sbin/minecraft-instance"
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)
            self.assertIn("/opt/minecraft-crafty/scripts/minecraft_crafty.py", wrapper.read_text())

            install = next(call for call in calls if call[0] == "apt-get" and "install" in call)
            self.assertIn("docker.io", install)
            self.assertIn("docker-compose-v2", install)
            self.assertIn(["install-aws-cli"], calls)
            self.assertIn(["systemctl", "enable", "minecraft-crafty.service"], calls)
            self.assertFalse(any(call[0] == "systemctl" and "start" in call for call in calls))
            self.assertFalse(any(call[0] == "systemctl" and "--now" in call and "minecraft-crafty.service" in call
                                 for call in calls))

            self.assertTrue((root / "var/lib/minecraft/bootstrapped").exists())
            again = root / "var/lib/cloud/scripts/per-boot/minecraft-world-bootstrap"
            self.assertEqual(again.read_text(), script.read_text())
            self.assertEqual(again.stat().st_mode & 0o777, 0o700)
            self.assertEqual(calls[-1], ["shutdown", "-h", "now"])
            self.assertEqual(sum(call[0] == "shutdown" for call in calls), 1)

    def test_a_host_that_is_set_up_does_nothing_at_a_later_boot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log = self.fixture(root)
            result, _ = self.run_script(script, environment, log)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            again = root / "var/lib/cloud/scripts/per-boot/minecraft-world-bootstrap"
            result, calls = self.run_script(again, environment, log)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(calls, [])

    def test_a_failed_first_boot_powers_off_and_the_next_boot_finishes_and_stays_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, log = self.fixture(root)
            marker = root / "var/lib/minecraft/bootstrapped"
            # The marker is written last: a run that fails at any step, even the final check, leaves none.
            for failing in ("apt-get", "docker"):
                with self.subTest(failing=failing):
                    result, calls = self.run_script(script, environment, log, failing=failing)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("bootstrap failed", result.stderr)
                    self.assertFalse(marker.exists())
                    self.assertEqual(calls[-1], ["shutdown", "-h", "now"])

            again = root / "var/lib/cloud/scripts/per-boot/minecraft-world-bootstrap"
            result, calls = self.run_script(again, environment, log, failing="apt-get")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertEqual(calls[-1], ["shutdown", "-h", "now"])

            # A start is waiting for this boot, so the run that finishes the setup leaves the host up.
            result, calls = self.run_script(again, environment, log)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(marker.exists())
            self.assertFalse(any(call[0] == "shutdown" for call in calls))

    def test_the_template_carries_only_its_inputs_and_fits_ec2_user_data(self) -> None:
        template = TEMPLATE.read_text()
        # OpenTofu reads "${" and "%{" as its own; the shell around the inputs never uses either.
        self.assertEqual(set(re.findall(r"\$\{([^}]*)\}", template)), {"aws_cli_script_base64", *INPUTS})
        self.assertNotIn("%{", template)
        # Nothing from the pinned server content, so changing it never replaces a host.
        code = "\n".join(line for line in template.splitlines() if not line.startswith("#"))
        for absent in ("server.json", "minecraft-storage", "minecraft_storage"):
            self.assertNotIn(absent, code)
        rendered = render((ROOT / "scripts/install_aws_cli.sh").read_bytes())
        # EC2 takes 16 KiB of user data, and OpenTofu sends this gzipped.
        self.assertLess(len(gzip.compress(rendered.encode())), 8 * 1024)


if __name__ == "__main__":
    unittest.main()
