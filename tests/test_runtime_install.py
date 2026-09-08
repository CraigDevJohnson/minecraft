from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeInstallTests(unittest.TestCase):
    def fixture(self, root: Path):
        source = root / "source"
        (source / "scripts").mkdir(parents=True)
        (source / "runtime").mkdir()
        for name in ("minecraft_instance.py", "minecraft_content.py", "minecraft_access.py", "minecraft_backup.py",
                     "minecraft_backup_store.py", "minecraft_restore.py", "check_runtime_install.py"):
            shutil.copy2(ROOT / "scripts" / name, source / "scripts" / name)
        for name in ("survival.json", "content.json", "operator.json.example", "minecraft@.service"):
            shutil.copy2(ROOT / "runtime" / name, source / "runtime" / name)
        script = (ROOT / "scripts/install_runtime.sh").read_text()
        # Replace only OS path boundaries; exercise the actual installer logic.
        for target in ("/opt", "/usr/local/sbin", "/etc/systemd/system", "/etc/minecraft", "/run"):
            replacement = root / target.lstrip("/")
            replacement.mkdir(parents=True, exist_ok=True)
            if target == "/run":
                script = re.sub(r"/run(?=/|[\s\"')]|$)", lambda _: str(replacement), script)
            else:
                script = script.replace(target, str(replacement))
        (root / "etc/minecraft").chmod(0o700)
        (root / "run/lock").mkdir(mode=0o1777)
        deployment = root / "etc/minecraft/deployment.json"
        deployment.write_text('{"private": "deployment fixture"}\n')
        deployment.chmod(0o600)
        script = script.replace("/usr/bin/python3", sys.executable)
        installed_script = source / "scripts/install_runtime.sh"
        installed_script.write_text(script)
        storage = root / "usr/local/sbin/minecraft-storage"
        storage.write_text("#!/bin/sh\nexit 0\n")
        storage.chmod(0o755)
        binaries = root / "bin"
        binaries.mkdir()
        log = root / "commands.jsonl"
        fake = binaries / "fake.py"
        fake.write_text('''#!PYTHON_EXECUTABLE
import json, os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "python3":
    if len(args) > 2 and args[0] == "-B" and pathlib.Path(args[1]).name == "check_runtime_install.py":
        assert args[2] == "--deployment"
        assert pathlib.Path(args[3]).name == "deployment.json"
        sys.exit(int(os.environ.get("INSTALL_GUARD_FAILURE", "0")))
    os.execv(sys.executable, [sys.executable, *args])
elif name == "id": print(os.environ.get("INSTALL_UID", "0"))
elif name == "uname": print("Linux" if args == ["-s"] else "aarch64")
elif name == "timeout":
    assert args[0] == "--kill-after=10s" and args[1] in ("30s", "180s")
    sys.exit(subprocess.call(args[2:]))
elif name == "flock":
    assert args == ["-w", "10", "9"]
    sys.exit(int(os.environ.get("LOCK_FAILURE", "0")))
elif name == "docker":
    assert args == ["ps", "--quiet", "--filter", "label=com.docker.compose.service=minecraft"]
    print(os.environ.get("RUNNING_CONTAINER", ""))
elif name == "systemctl":
    assert args == ["daemon-reload"]
    sys.exit(int(os.environ.get("RELOAD_FAILURE", "0")))
elif name == "aws":
    if args == ["--version"]:
        print(os.environ.get("AWS_VERSION", "aws-cli/2.36.34 fixture-only"))
    else:
        assert args[0] == "s3api"
        assert args[2:] == ["--generate-cli-skeleton", "input", "--no-cli-pager", "--no-cli-auto-prompt"]
        skeletons = {
            "put-object": {"IfNoneMatch": "", "ChecksumSHA256": ""},
            "complete-multipart-upload": {"IfNoneMatch": ""},
            "create-multipart-upload": {"ChecksumType": "COMPOSITE", "ChecksumAlgorithm": "SHA256"},
        }
        skeleton = skeletons[args[1]]
        for field in list(skeleton):
            if os.environ.get("AWS_MISSING_FIELD") == args[1] + ":" + field:
                del skeleton[field]
        print(json.dumps(skeleton))
elif name == "stat":
    assert args[:3] == ["-c", "%u:%a", "--"]
    target = pathlib.Path(args[3])
    owner = 1000 if str(target) == os.environ.get("UNTRUSTED_OWNER_PATH") else 0
    print(f"{owner}:{target.stat().st_mode & 0o7777:o}")
'''.replace("PYTHON_EXECUTABLE", sys.executable))
        fake.chmod(0o755)
        for name in ("id", "uname", "timeout", "flock", "docker", "systemctl", "stat", "python3", "aws"):
            (binaries / name).symlink_to(fake)
        environment = os.environ.copy()
        environment.update(PATH=f"{binaries}:{environment['PATH']}", COMMAND_LOG=str(log))
        return source, installed_script, environment, log

    def run_installer(self, script, environment):
        return subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)

    def test_first_install_and_identical_rerun_preserve_private_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, script, environment, log = self.fixture(root)
            private = root / "etc/minecraft/operator.json"
            private.write_text('{"operator-owned": true}\n')
            private.chmod(0o600)
            for _ in range(2):
                result = self.run_installer(script, environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(private.read_text(), '{"operator-owned": true}\n')
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            installed = root / "opt/minecraft"
            for name in ("minecraft_instance.py", "minecraft_content.py", "minecraft_access.py", "minecraft_backup.py",
                         "minecraft_backup_store.py", "minecraft_restore.py"):
                self.assertEqual((installed / "scripts" / name).read_bytes(), (source / "scripts" / name).read_bytes())
            self.assertEqual((installed / "runtime/content.json").read_bytes(), (source / "runtime/content.json").read_bytes())
            example = json.loads((installed / "runtime/operator.json.example").read_text())
            self.assertIs(example["eula_accepted"], False)
            self.assertEqual(example["backup_hook"], ["/usr/local/sbin/minecraft-backup", "hook"])
            self.assertFalse((root / "srv").exists())
            self.assertEqual((root / "etc/minecraft/deployment.json").read_text(), '{"private": "deployment fixture"}\n')
            self.assertFalse((root / "etc/minecraft/rcon.secret").exists())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([call for call in calls if call[0] == "systemctl"], [["systemctl", "daemon-reload"]] * 2)
            launcher = root / "usr/local/sbin/minecraft-instance"
            help_result = subprocess.run(["bash", str(launcher), "--help"], capture_output=True, text=True, timeout=10)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--config", help_result.stdout)
            for name in ("minecraft-backup", "minecraft-restore"):
                command = root / "usr/local/sbin" / name
                self.assertTrue(command.is_file(), f"missing trusted {name} launcher")
                self.assertEqual(command.stat().st_mode & 0o777, 0o755)
                help_result = subprocess.run(["bash", str(command), "--help"], capture_output=True, text=True, timeout=10)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertFalse((installed / "scripts/__pycache__").exists())

    def test_changed_release_or_desired_content_is_not_overwritten(self):
        for changed in ("scripts/minecraft_instance.py", "runtime/content.json", "scripts/minecraft_backup.py"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, script, environment, _ = self.fixture(root)
                first = self.run_installer(script, environment)
                self.assertEqual(first.returncode, 0, first.stderr)
                installed = root / "opt/minecraft" / changed
                before = installed.read_bytes()
                (source / changed).write_bytes(before + b"\n")
                result = self.run_installer(script, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("staged upgrade", result.stderr)
                self.assertEqual(installed.read_bytes(), before)

    def test_incompatible_backup_cli_is_rejected_before_any_installation_mutation(self):
        unsupported = [{"AWS_VERSION": "aws-cli/1.99.0 fixture-only"}]
        unsupported.extend({"AWS_MISSING_FIELD": field} for field in (
            "put-object:IfNoneMatch", "put-object:ChecksumSHA256", "complete-multipart-upload:IfNoneMatch",
            "create-multipart-upload:ChecksumType", "create-multipart-upload:ChecksumAlgorithm"))
        for settings in unsupported:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, script, environment, log = self.fixture(root)
                environment.update(settings)
                result = self.run_installer(script, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AWS CLI v2", result.stderr)
                self.assertFalse((root / "run/minecraft").exists())
                self.assertFalse((root / "opt/minecraft").exists())
                self.assertFalse((root / "usr/local/sbin/minecraft-backup").exists())
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(call[0] in ("docker", "flock", "systemctl") for call in calls))

    def test_running_instance_or_lock_failure_prevents_installation(self):
        for values in ({"RUNNING_CONTAINER": "fixture-container"}, {"LOCK_FAILURE": "1"}):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, script, environment, _ = self.fixture(root)
                environment.update(values)
                result = self.run_installer(script, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "opt/minecraft").exists())
                self.assertFalse((root / "usr/local/sbin/minecraft-instance").exists())

    def test_retained_state_refusal_blocks_install_after_old_release_is_moved_aside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, script, environment, log = self.fixture(root)
            environment["INSTALL_GUARD_FAILURE"] = "1"
            # Absence of /opt/minecraft also represents the documented staged replacement.
            self.assertFalse((root / "opt/minecraft").exists())
            result = self.run_installer(script, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "opt/minecraft").exists())
            self.assertFalse((root / "usr/local/sbin/minecraft-instance").exists())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] == "systemctl" for call in calls))

    def test_non_root_and_symlink_targets_are_rejected(self):
        for condition in ("non-root", "symlink"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, script, environment, _ = self.fixture(root)
                target = root / "opt/minecraft"
                if condition == "non-root":
                    environment["INSTALL_UID"] = "1000"
                else:
                    target.symlink_to(root / "elsewhere")
                result = self.run_installer(script, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "elsewhere").exists())
                self.assertFalse((root / "usr/local/sbin/minecraft-instance").exists())

    def test_reload_failure_can_retry_without_replacing_the_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, script, environment, _ = self.fixture(root)
            environment["RELOAD_FAILURE"] = "1"
            first = self.run_installer(script, environment)
            self.assertNotEqual(first.returncode, 0)
            installed = root / "opt/minecraft/scripts/minecraft_instance.py"
            timestamp = installed.stat().st_mtime_ns
            environment["RELOAD_FAILURE"] = "0"
            second = self.run_installer(script, environment)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(installed.stat().st_mtime_ns, timestamp)

    def test_untrusted_private_directory_is_rejected_without_repair(self):
        for condition in ("non-root-owner", "group-writable", "public-readable"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, script, environment, _ = self.fixture(root)
                private_dir = root / "etc/minecraft"
                if condition == "non-root-owner":
                    environment["UNTRUSTED_OWNER_PATH"] = str(private_dir)
                else:
                    private_dir.chmod(0o770 if condition == "group-writable" else 0o755)
                mode_before = private_dir.stat().st_mode
                marker = private_dir / "operator.json"
                marker.write_text('{"existing": true}\n')
                result = self.run_installer(script, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("root-owned mode 0700", result.stderr)
                self.assertEqual(private_dir.stat().st_mode, mode_before)
                self.assertEqual(marker.read_text(), '{"existing": true}\n')
                self.assertFalse((root / "opt/minecraft").exists())
                self.assertFalse((root / "usr/local/sbin/minecraft-instance").exists())

    def test_shared_runtime_lock_uses_a_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, script, environment, _ = self.fixture(root)
            result = self.run_installer(script, environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            private_lock_dir = root / "run/minecraft"
            self.assertEqual(private_lock_dir.stat().st_mode & 0o777, 0o700)
            self.assertTrue((private_lock_dir / "runtime.lock").is_file())
            self.assertFalse((root / "run/lock/minecraft-runtime.lock").exists())

    def test_untrusted_lock_parent_or_directory_prevents_installation_without_repair(self):
        for condition in ("public-parent", "foreign-parent", "public-child", "foreign-child", "symlink-child"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, script, environment, _ = self.fixture(root)
                parent = root / "run"
                child = parent / "minecraft"
                if condition == "public-parent":
                    parent.chmod(0o777)
                elif condition == "foreign-parent":
                    environment["UNTRUSTED_OWNER_PATH"] = str(parent)
                elif condition == "symlink-child":
                    child.symlink_to(root / "attacker-target")
                else:
                    child.mkdir(mode=0o700)
                    if condition == "public-child":
                        child.chmod(0o755)
                    else:
                        environment["UNTRUSTED_OWNER_PATH"] = str(child)
                result = self.run_installer(script, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "opt/minecraft").exists())
                self.assertFalse((root / "attacker-target").exists())
                if condition == "public-child":
                    self.assertEqual(child.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
