from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import minecraft_instance


class FakeMachine:
    """Controlled storage/container/backup boundary; never touches Docker or AWS."""

    def __init__(self, root):
        self.root = root
        self.running = False
        self.exists = False
        self.exit_code = 0
        self.storage_ok = True
        self.accepted = True
        self.events = []
        self.revision = "a" * 64
        self.boot = "boot-one"
        self.backup_works = False
        self.stop_works = True
        self.ready = True

    @contextlib.contextmanager
    def lock(self):
        yield

    def verify_storage(self):
        if not self.storage_ok:
            raise minecraft_instance.OperationError("wrong or missing retained mount")

    def boot_id(self):
        return self.boot

    def verify_inventory(self):
        pass

    def desired_revision(self, instance):
        return self.revision

    def inspect(self, instance):
        return {"exists": self.exists, "running": self.running, "exit_code": self.exit_code}

    def prepare(self, instance):
        if not self.accepted:
            raise minecraft_instance.OperationError("EULA acceptance is required")
        self.events.append("prepare")

    def launch(self, instance):
        self.events.append("launch")
        self.running = self.exists = True

    def wait_ready(self, instance):
        if not self.ready:
            raise minecraft_instance.OperationError("readiness timed out")
        self.events.append("ready")

    def save(self, instance):
        self.events.append("save")

    def shutdown(self, instance):
        self.events.append("shutdown")
        if not self.stop_works:
            raise minecraft_instance.OperationError("shutdown timed out")
        self.running = False

    def backup(self, instance, state):
        self.events.append("backup")
        if self.running:
            raise AssertionError("backup observed a writer")
        if not self.backup_works:
            raise minecraft_instance.OperationError("required backup hook missing or failed")
        return {"schema_version": 1, "status": "complete", "instance": instance,
                "session": state["session"], "manifest_sha256": state["revision"],
                "recovery_point": "instances/survival/session-1"}


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.machine = FakeMachine(self.root)
        self.manager = minecraft_instance.Manager(self.root, self.machine, ["survival"])

    def test_start_requires_verified_storage_and_duplicate_start_keeps_one_writer(self):
        self.machine.storage_ok = False
        with self.assertRaises(minecraft_instance.OperationError):
            self.manager.execute("start", "survival")
        self.assertFalse((self.root / "instances").exists())
        self.machine.storage_ok = True
        self.assertEqual(self.manager.execute("start", "survival")["application"], "running")
        self.assertEqual(self.manager.execute("start", "survival")["application"], "running")
        self.assertEqual(self.machine.events.count("launch"), 1)

    def test_update_requires_backup_before_launching_new_content(self):
        self.manager.execute("start", "survival")
        self.machine.revision = "b" * 64
        with self.assertRaisesRegex(minecraft_instance.OperationError, "backup"):
            self.manager.execute("update", "survival")
        self.assertEqual(self.machine.events.count("prepare"), 1)
        self.machine.backup_works = True
        self.assertEqual(self.manager.execute("update", "survival")["application"], "running")
        self.assertEqual(self.machine.events.count("prepare"), 2)

    def test_readiness_and_shutdown_timeouts_never_back_up_running_world(self):
        self.machine.ready = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "readiness"):
            self.manager.execute("start", "survival")
        self.machine.stop_works = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "shutdown"):
            self.manager.execute("stop", "survival")
        self.assertNotIn("backup", self.machine.events)

    def test_storage_is_reverified_after_preparation_before_container_start(self):
        original = self.machine.prepare
        def lose_mount(instance):
            original(instance)
            self.machine.storage_ok = False
        self.machine.prepare = lose_mount
        with self.assertRaisesRegex(minecraft_instance.OperationError, "mount"):
            self.manager.execute("start", "survival")
        self.assertNotIn("launch", self.machine.events)

    def test_untracked_existing_world_is_not_silently_adopted_or_reported_backed_up(self):
        data = self.root / "instances" / "survival" / "data"
        data.mkdir(parents=True)
        with self.assertRaisesRegex(minecraft_instance.OperationError, "untracked"):
            self.manager.execute("start", "survival")
        self.assertEqual(self.manager.execute("status", "survival")["backup"], "pending")

    def test_missing_acceptance_and_invalid_ids_never_launch(self):
        self.machine.accepted = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "EULA"):
            self.manager.execute("start", "survival")
        for instance in (None, "../survival", "creative"):
            with self.assertRaises(minecraft_instance.OperationError):
                self.manager.execute("start", instance)
        self.assertNotIn("launch", self.machine.events)

    def test_incomplete_or_wrong_session_backup_cannot_complete_stop(self):
        self.manager.execute("start", "survival")
        for response in ({}, {"status": "complete", "recovery_point": "fake"}):
            self.machine.backup = lambda instance, state: response
            with self.assertRaisesRegex(minecraft_instance.OperationError, "completion"):
                self.manager.execute("stop", "survival")
            self.assertEqual(self.manager.execute("status", "survival")["backup"], "failed")

    def test_os_lock_prevents_competing_operations_and_releases_after_failure(self):
        lock = self.root / "host.lock"
        with minecraft_instance.operation_lock(lock, timeout=0.01):
            with self.assertRaisesRegex(minecraft_instance.OperationError, "another runtime"):
                with minecraft_instance.operation_lock(lock, timeout=0.01):
                    self.fail("second operation acquired the same host lock")
        with minecraft_instance.operation_lock(lock, timeout=0.01):
            pass

    def test_stop_failure_can_retry_backup_without_restarting_or_resaving_world(self):
        self.manager.execute("start", "survival")
        with self.assertRaisesRegex(minecraft_instance.OperationError, "backup"):
            self.manager.execute("stop", "survival")
        self.assertEqual(self.manager.execute("status", "survival"), {
            "instance": "survival", "application": "stopped", "backup": "failed"})
        self.machine.backup_works = True
        self.assertEqual(self.manager.execute("stop", "survival")["backup"], "complete")
        self.manager.execute("stop", "survival")
        self.assertEqual(self.machine.events.count("save"), 1)
        self.assertEqual(self.machine.events.count("backup"), 2)

    def test_host_drain_retries_stopped_pending_backup_and_blocks_same_boot_start(self):
        self.manager.execute("start", "survival")
        with self.assertRaises(minecraft_instance.OperationError):
            self.manager.execute("host-drain")
        self.assertFalse(self.machine.running)
        with self.assertRaisesRegex(minecraft_instance.OperationError, "draining"):
            self.manager.execute("start", "survival")
        self.machine.backup_works = True
        result = self.manager.execute("host-drain")
        self.assertTrue(result["draining"])
        self.assertEqual(result["instances"]["survival"]["backup"], "complete")
        self.machine.boot = "boot-after-ec2-restart"
        self.assertEqual(self.manager.execute("start", "survival")["application"], "running")


if __name__ == "__main__":
    unittest.main()
