from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_runtime_install as guard


class RuntimeInstallationGuardTests(unittest.TestCase):
    def fixture(self, root):
        deployment = root / "deployment.json"
        deployment.write_text(json.dumps({
            "data_volume_id": "vol-0123456789abcdef0",
            "data_filesystem_uuid": "01234567-89ab-4cde-8fab-0123456789ab",
            "instances": {"survival": {"data_path": "/srv/minecraft/instances/survival/data",
                                        "backup_prefix": "instances/survival/"}},
        }))
        deployment.chmod(0o600)
        retained = root / "retained"
        retained.mkdir()
        return deployment, retained

    def journal(self, retained, *, instance="survival", phase="stopped", backup="complete"):
        directory = retained / "instances" / instance
        (directory / "data").mkdir(parents=True)
        path = directory / "operation.json"
        path.write_text(json.dumps({"phase": phase, "backup": backup, "recovery_point": "fixture-success"}))
        path.chmod(0o600)
        return path

    def run_guard(self, deployment, retained, *, containers=None, storage_error=None):
        calls = []

        def external(argv):
            calls.append(argv)
            if argv[0] == "/usr/local/sbin/minecraft-storage":
                if storage_error:
                    raise storage_error
                return "verified"
            if argv[:2] == ["docker", "ps"]:
                return "fixture-container" if containers else ""
            if argv[:2] == ["docker", "inspect"]:
                return json.dumps(containers)
            self.fail(f"unexpected external command: {argv}")

        with mock.patch.object(guard, "ROOT", retained), mock.patch.object(guard, "command", side_effect=external), \
                mock.patch.object(guard, "verify_retained_paths") as subtree:
            guard.check_installation(deployment)
            subtree.assert_called_once_with(retained)
        return calls

    def test_pristine_install_and_completed_retained_session_are_read_only(self):
        for pristine in (True, False):
            with self.subTest(pristine=pristine), tempfile.TemporaryDirectory() as temporary:
                deployment, retained = self.fixture(Path(temporary))
                journal = None if pristine else self.journal(retained)
                before = journal.read_bytes() if journal else None
                calls = self.run_guard(deployment, retained)
                self.assertEqual(calls[0][0], "/usr/local/sbin/minecraft-storage")
                self.assertFalse((retained / "host-drain.json").exists())
                if journal:
                    self.assertEqual(journal.read_bytes(), before)
                else:
                    self.assertEqual(list(retained.iterdir()), [])

    def test_pending_failed_or_incomplete_stop_blocks_without_old_release(self):
        for phase, backup in (("stopped", "pending"), ("stopped", "failed"), ("stopping", "complete"),
                              ("running", "complete"), ("prepared", "not-required")):
            with self.subTest(phase=phase, backup=backup), tempfile.TemporaryDirectory() as temporary:
                deployment, retained = self.fixture(Path(temporary))
                journal = self.journal(retained, phase=phase, backup=backup)
                before = journal.read_bytes()
                with self.assertRaisesRegex(guard.OperationError, "completed backup"):
                    self.run_guard(deployment, retained)
                self.assertEqual(journal.read_bytes(), before)

    def test_unconfigured_retained_session_cannot_escape_backup_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            deployment, retained = self.fixture(Path(temporary))
            self.journal(retained, instance="retired", backup="failed")
            with self.assertRaisesRegex(guard.OperationError, "completed backup"):
                self.run_guard(deployment, retained)

    def test_untracked_data_or_stopped_container_is_not_pristine(self):
        for scenario in ("data", "container"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                deployment, retained = self.fixture(Path(temporary))
                containers = None
                if scenario == "data":
                    (retained / "instances/survival/data").mkdir(parents=True)
                else:
                    containers = [{"Config": {"Labels": {"com.docker.compose.project": "minecraft-survival"}},
                                   "State": {"Running": False, "ExitCode": 0}}]
                with self.assertRaisesRegex(guard.OperationError, "untracked"):
                    self.run_guard(deployment, retained, containers=containers)

    def test_storage_failure_prevents_any_retained_state_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            deployment, retained = self.fixture(Path(temporary))
            (retained / "instances").write_text("unverified root-disk decoy")
            with mock.patch.object(guard, "verify_retained_paths") as subtree:
                with self.assertRaisesRegex(guard.OperationError, "mount unavailable"):
                    self.run_guard(deployment, retained, storage_error=guard.OperationError("mount unavailable"))
                subtree.assert_not_called()

    def test_nested_mount_failure_precedes_journal_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            deployment, retained = self.fixture(Path(temporary))
            journal = self.journal(retained)
            journal.write_text("not trusted before subtree verification")
            with mock.patch.object(guard, "ROOT", retained), mock.patch.object(guard, "command", return_value="verified"), \
                    mock.patch.object(guard, "verify_retained_paths", side_effect=guard.OperationError("nested mount")):
                with self.assertRaisesRegex(guard.OperationError, "nested mount"):
                    guard.check_installation(deployment)


if __name__ == "__main__":
    unittest.main()
