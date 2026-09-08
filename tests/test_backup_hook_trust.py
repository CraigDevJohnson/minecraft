from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import minecraft_instance as runtime  # noqa: E402
from minecraft_content import manifest_digest  # noqa: E402

HOOK = "/opt/minecraft-backup/bin/backup"


class BackupHookTrustTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        deployment = root / "deployment.json"
        deployment.write_text(json.dumps({"backup_bucket_name": "fixture-bucket", "instances": {
            "survival": {"data_path": "/srv/minecraft/instances/survival/data", "backup_prefix": "instances/survival/"},
        }}))
        deployment.chmod(0o600)
        self.machine = runtime.LinuxMachine({"deployment": str(deployment),
            "definitions": {"survival": str(ROOT / "runtime/survival.json")}, "backup_hook": [HOOK]})
        self.machine.root = root / "retained"
        manifest_path = self.machine.runtime("survival") / "manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest = json.loads((ROOT / "runtime/content.json").read_text())
        manifest_path.write_text(json.dumps(manifest))
        self.state = {"session": "fixture-session", "revision": manifest_digest(manifest)}
        self.metadata = {str(path): SimpleNamespace(st_uid=os.geteuid(), st_mode=stat.S_IFDIR | 0o755)
                         for path in reversed(Path(HOOK).parents)}
        self.metadata[HOOK] = SimpleNamespace(st_uid=os.geteuid(), st_mode=stat.S_IFREG | 0o755)

    def test_backup_refuses_a_hook_replaceable_by_the_game_user(self):
        self.metadata[HOOK].st_uid = os.geteuid() + 1
        with mock.patch.object(runtime, "command", return_value="{}") as execute:
            # Path metadata is the controlled OS boundary, not a live permission change.
            with mock.patch.object(runtime.os, "lstat", side_effect=lambda path: self.metadata[str(path)]):
                with self.assertRaises(runtime.OperationError):
                    self.machine.backup("survival", self.state)
        execute.assert_not_called()

    def test_trusted_hook_preserves_the_bounded_backup_protocol(self):
        receipt = {"schema_version": 1, "status": "complete", "instance": "survival",
                   "session": self.state["session"], "manifest_sha256": self.state["revision"],
                   "recovery_point": "fixture-recovery-point"}
        arguments = [HOOK, "--private-option", "operator-reviewed-argument"]
        self.machine.config["backup_hook"] = arguments
        with mock.patch.object(runtime.os, "lstat", side_effect=lambda path: self.metadata[str(path)]) as inspect, \
                mock.patch.object(runtime, "command", return_value=json.dumps(receipt)) as execute:
            self.assertEqual(self.machine.backup("survival", self.state), receipt)
        self.assertEqual([str(call.args[0]) for call in inspect.call_args_list],
                         ["/", "/opt", "/opt/minecraft-backup", "/opt/minecraft-backup/bin", HOOK])
        self.assertEqual(execute.call_args.args[0], arguments)
        self.assertEqual(execute.call_args.kwargs["timeout"], 900)
        request = json.loads(execute.call_args.kwargs["input_text"])
        self.assertEqual(request["session"], "fixture-session")
        self.assertEqual(request["manifest_sha256"], self.state["revision"])
        self.assertEqual(request["destination"], {"bucket": "fixture-bucket", "prefix": "instances/survival/"})

    def test_untrusted_ancestor_blocks_an_otherwise_trusted_executable(self):
        for ancestor in ("/", "/opt", "/opt/minecraft-backup", "/opt/minecraft-backup/bin"):
            with self.subTest(ancestor=ancestor):
                metadata = self.metadata[ancestor]
                metadata.st_uid += 1
                try:
                    with mock.patch.object(runtime.os, "lstat", side_effect=lambda path: self.metadata[str(path)]), \
                            mock.patch.object(runtime, "command") as execute:
                        with self.assertRaises(runtime.OperationError):
                            self.machine.backup("survival", self.state)
                    execute.assert_not_called()
                finally:
                    metadata.st_uid -= 1

    def test_group_or_world_write_on_any_component_blocks_execution(self):
        for component in self.metadata:
            for write_bit in (stat.S_IWGRP, stat.S_IWOTH):
                with self.subTest(component=component, write_bit=write_bit):
                    metadata = self.metadata[component]
                    original = metadata.st_mode
                    metadata.st_mode |= write_bit
                    try:
                        with mock.patch.object(runtime.os, "lstat", side_effect=lambda path: self.metadata[str(path)]), \
                                mock.patch.object(runtime, "command") as execute:
                            with self.assertRaises(runtime.OperationError):
                                self.machine.backup("survival", self.state)
                        execute.assert_not_called()
                    finally:
                        metadata.st_mode = original

    def test_symlink_file_or_ancestor_is_rejected_without_resolving_it(self):
        for component in self.metadata:
            with self.subTest(component=component):
                metadata = self.metadata[component]
                original = metadata.st_mode
                metadata.st_mode = stat.S_IFLNK | 0o777
                try:
                    with mock.patch.object(runtime.os, "lstat", side_effect=lambda path: self.metadata[str(path)]) as inspect, \
                            mock.patch.object(runtime, "command") as execute:
                        with self.assertRaisesRegex(runtime.OperationError, "symlink"):
                            self.machine.backup("survival", self.state)
                    self.assertEqual(str(inspect.call_args.args[0]), component)
                    execute.assert_not_called()
                finally:
                    metadata.st_mode = original

    def test_nonexecutable_special_file_and_nondirectory_paths_are_rejected(self):
        for component, mode in ((HOOK, stat.S_IFREG | 0o644), (HOOK, stat.S_IFDIR | 0o755),
                                (HOOK, stat.S_IFIFO | 0o700), ("/opt/minecraft-backup", stat.S_IFREG | 0o755)):
            with self.subTest(component=component, mode=mode):
                metadata = self.metadata[component]
                original = metadata.st_mode
                metadata.st_mode = mode
                try:
                    with mock.patch.object(runtime.os, "lstat", side_effect=lambda path: self.metadata[str(path)]), \
                            mock.patch.object(runtime, "command") as execute:
                        with self.assertRaises(runtime.OperationError):
                            self.machine.backup("survival", self.state)
                    execute.assert_not_called()
                finally:
                    metadata.st_mode = original

    def test_missing_or_unreadable_executable_path_has_private_failure(self):
        for failure in (FileNotFoundError("private-hook-path"), PermissionError("private-hook-path")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(runtime.os, "lstat", side_effect=failure), \
                        mock.patch.object(runtime, "command") as execute:
                    with self.assertRaises(runtime.OperationError) as result:
                        self.machine.backup("survival", self.state)
                self.assertNotIn("private-hook-path", str(result.exception))
                execute.assert_not_called()

    def test_parent_traversal_does_not_bypass_ancestor_checks(self):
        self.machine.config["backup_hook"] = ["/opt/minecraft-backup/bin/../bin/backup"]
        with mock.patch.object(runtime.os, "lstat") as inspect, mock.patch.object(runtime, "command") as execute:
            with self.assertRaises(runtime.OperationError):
                self.machine.backup("survival", self.state)
        inspect.assert_not_called()
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
