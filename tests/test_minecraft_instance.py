from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.starts = 0
        self.locked = False
        self.usage = {"size_bytes": 5_000_000_000, "available_bytes": 4_000_000_000, "used_percent": 16}

    @contextlib.contextmanager
    def lock(self):
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def verify_storage(self):
        if not self.storage_ok:
            raise minecraft_instance.OperationError("wrong or missing retained mount")

    def storage_usage(self):
        if not self.locked:
            raise AssertionError("storage usage was read outside lifecycle exclusion")
        self.events.append("storage-usage")
        return dict(self.usage)

    def boot_id(self):
        return self.boot

    def verify_inventory(self):
        pass

    def desired_revision(self, instance):
        return self.revision

    def inspect(self, instance):
        return {"exists": self.exists, "running": self.running, "exit_code": self.exit_code,
                "container_id": "fixture-container", "started_at": f"run-{self.starts}"}

    def prepare(self, instance):
        if not self.accepted:
            raise minecraft_instance.OperationError("EULA acceptance is required")
        self.events.append("prepare")

    def verify_restored(self, instance, state):
        self.events.append("verify-restored")

    def launch(self, instance):
        self.events.append("launch")
        self.starts += 1
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
        if not self.locked:
            raise AssertionError("backup ran outside lifecycle exclusion")
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

    def test_on_demand_backup_cleanly_stops_and_retries_the_same_session(self):
        self.manager.execute("start", "survival")
        session = self.manager.state("survival")["session"]
        with self.assertRaisesRegex(minecraft_instance.OperationError, "backup"):
            self.manager.execute("backup", "survival")
        self.assertFalse(self.machine.running)
        self.assertEqual(self.manager.state("survival")["backup"], "failed")
        self.machine.backup_works = True
        result = self.manager.execute("backup", "survival")
        self.assertEqual(result["backup"], "complete")
        self.assertEqual(self.manager.state("survival")["session"], session)
        self.manager.execute("backup", "survival")
        self.assertEqual(self.machine.events.count("save"), 1)
        self.assertEqual(self.machine.events.count("shutdown"), 1)
        self.assertEqual(self.machine.events.count("backup"), 2)
        self.assertEqual(self.machine.events.count("launch"), 1)

    def test_on_demand_backup_rejects_unexpected_or_unclean_exit(self):
        for tracked_stop, exit_code in ((False, 0), (True, 137)):
            with self.subTest(tracked_stop=tracked_stop, exit_code=exit_code):
                self.setUp()
                self.manager.execute("start", "survival")
                self.machine.running = False
                self.machine.exit_code = exit_code
                if tracked_stop:
                    state = self.manager.state("survival")
                    state["phase"] = "stopping"
                    minecraft_instance.write_json(self.manager.state_path("survival"), state)
                with self.assertRaises(minecraft_instance.OperationError):
                    self.manager.execute("backup", "survival")
                self.assertNotIn("backup", self.machine.events)

    def test_disappeared_container_cannot_prove_an_interrupted_stop_was_clean(self):
        self.manager.execute("start", "survival")
        self.machine.stop_works = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "shutdown"):
            self.manager.execute("stop", "survival")
        self.machine.running = self.machine.exists = False
        self.machine.backup_works = True
        with self.assertRaisesRegex(minecraft_instance.OperationError, "clean shutdown"):
            self.manager.execute("backup", "survival")
        self.assertNotIn("backup", self.machine.events)

    def test_restored_prepared_data_starts_only_with_exact_revision_and_acceptance(self):
        (self.root / "instances/survival/data").mkdir(parents=True)
        restored = {"phase": "prepared", "backup": "not-required", "revision": self.machine.revision,
                    "session": "restored-session", "restored_from": "instances/survival/checkpoint"}
        minecraft_instance.write_json(self.manager.state_path("survival"), restored)
        self.machine.revision = "b" * 64
        with self.assertRaisesRegex(minecraft_instance.OperationError, "content changed"):
            self.manager.execute("start", "survival")
        self.machine.revision = restored["revision"]
        self.machine.accepted = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "EULA"):
            self.manager.execute("start", "survival")
        self.assertNotIn("launch", self.machine.events)
        self.machine.accepted = True
        self.assertTrue(self.manager.execute("start", "survival")["ready"])
        self.assertNotEqual(self.manager.state("survival")["session"], restored["session"])

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

    def test_prelaunch_failure_preserves_completed_backup_and_can_retry_start(self):
        self.machine.backup_works = True
        self.manager.execute("start", "survival")
        self.manager.execute("stop", "survival")
        launch = self.machine.launch
        def fail_before_launch(instance):
            raise minecraft_instance.OperationError("Compose failed before launch")
        self.machine.launch = fail_before_launch
        with self.assertRaisesRegex(minecraft_instance.OperationError, "before launch"):
            self.manager.execute("start", "survival")
        self.assertEqual(self.manager.execute("status", "survival")["backup"], "complete")
        self.machine.launch = launch
        self.assertTrue(self.manager.execute("start", "survival")["ready"])
        self.assertEqual(self.machine.events.count("backup"), 1)

    def test_first_preparation_failure_after_data_creation_can_retry(self):
        prepare = self.machine.prepare
        def interrupt_preparation(instance):
            (self.root / "instances" / instance / "data").mkdir(parents=True)
            raise minecraft_instance.OperationError("preparation interrupted after data creation")
        self.machine.prepare = interrupt_preparation
        with self.assertRaisesRegex(minecraft_instance.OperationError, "preparation interrupted"):
            self.manager.execute("start", "survival")
        self.assertIsNotNone(self.manager.state("survival"))
        self.assertNotIn("launch", self.machine.events)
        self.assertEqual(self.manager.execute("status", "survival")["backup"], "not-required")
        self.machine.prepare = prepare
        self.manager = minecraft_instance.Manager(self.root, self.machine, ["survival"])
        self.assertTrue(self.manager.execute("start", "survival")["ready"])
        self.assertEqual(self.machine.starts, 1)

    def test_failed_intent_write_never_reaches_preparation(self):
        with mock.patch.object(minecraft_instance, "write_json", side_effect=OSError("journal unavailable")):
            with self.assertRaises(OSError):
                self.manager.execute("start", "survival")
        self.assertEqual(self.machine.events, [])

    def test_failed_update_preparation_preserves_previous_recovery_point(self):
        self.machine.backup_works = True
        self.manager.execute("start", "survival")
        self.manager.execute("stop", "survival")
        completed = self.manager.state("survival")
        self.machine.revision = "b" * 64
        prepare = self.machine.prepare
        with mock.patch.object(self.machine, "prepare", side_effect=OSError("interrupted preparation")):
            with self.assertRaises(OSError):
                self.manager.execute("update", "survival")
        self.manager = minecraft_instance.Manager(self.root, self.machine, ["survival"])
        self.assertEqual(self.manager.execute("status", "survival")["backup"], "complete")
        self.assertEqual(self.manager.state("survival"), completed)
        self.machine.prepare = prepare
        self.assertTrue(self.manager.execute("update", "survival")["ready"])
        self.assertEqual(self.machine.events.count("backup"), 1)

    def test_running_container_cannot_reuse_a_completed_or_prepared_session(self):
        self.machine.backup_works = True
        self.manager.execute("start", "survival")
        self.manager.execute("stop", "survival")
        completed = self.manager.state("survival")
        self.machine.running = True
        before_events = self.machine.events.copy()
        for phase, backup in (("stopped", "complete"), ("prepared", "not-required"),
                              ("running", "complete"), ("stopped", "pending")):
            state = {**completed, "phase": phase, "backup": backup}
            minecraft_instance.write_json(self.manager.state_path("survival"), state)
            for action in ("start", "status", "stop"):
                with self.subTest(phase=phase, backup=backup, action=action):
                    with self.assertRaisesRegex(minecraft_instance.OperationError, "untracked container execution"):
                        self.manager.execute(action, "survival")
                    self.assertEqual(self.manager.state("survival"), state)
                    self.assertEqual(self.machine.events, before_events)

    def test_tracked_readiness_and_shutdown_failures_remain_retryable(self):
        self.machine.ready = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "readiness"):
            self.manager.execute("start", "survival")
        session = self.manager.state("survival")["session"]
        self.machine.ready = True
        self.assertTrue(self.manager.execute("start", "survival")["ready"])
        self.assertEqual(self.manager.state("survival")["session"], session)
        self.machine.stop_works = False
        with self.assertRaisesRegex(minecraft_instance.OperationError, "shutdown"):
            self.manager.execute("stop", "survival")
        self.assertEqual(self.manager.execute("status", "survival")["backup"], "pending")
        self.machine.stop_works = self.machine.backup_works = True
        self.assertEqual(self.manager.execute("stop", "survival")["backup"], "complete")
        self.assertEqual(self.manager.state("survival")["session"], session)

    def test_failed_new_execution_is_not_mistaken_for_safe_prelaunch_failure(self):
        self.machine.backup_works = True
        self.manager.execute("start", "survival")
        self.manager.execute("stop", "survival")
        def fail_after_execution(instance):
            self.machine.starts += 1
            raise minecraft_instance.OperationError("new container execution exited")
        self.machine.launch = fail_after_execution
        with self.assertRaises(minecraft_instance.OperationError):
            self.manager.execute("start", "survival")
        with self.assertRaisesRegex(minecraft_instance.OperationError, "unexpectedly"):
            self.manager.execute("start", "survival")
        with self.assertRaisesRegex(minecraft_instance.OperationError, "not proven"):
            self.manager.execute("stop", "survival")

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

    def test_only_start_and_host_status_report_retained_disk_usage(self):
        self.machine.backup_works = True
        started = self.manager.execute("start", "survival")
        self.assertEqual(started, {"instance": "survival", "application": "running", "backup": "pending",
                                   "ready": True, "storage": self.machine.usage})
        # Usage is read after preparation staged content and the instance became ready.
        self.assertEqual(self.machine.events, ["prepare", "launch", "ready", "storage-usage"])
        self.assertEqual(self.manager.execute("host-status"), {
            "draining": False, "storage": self.machine.usage,
            "instances": {"survival": {"instance": "survival", "application": "running", "backup": "pending"}}})
        self.assertEqual(self.machine.events.count("storage-usage"), 2)
        self.machine.revision = "b" * 64
        responses = {
            "status": self.manager.execute("status", "survival"),
            "update": self.manager.execute("update", "survival"),
            "backup": self.manager.execute("backup", "survival"),
            "stop": self.manager.execute("stop", "survival"),
            "host-drain": self.manager.execute("host-drain"),
        }
        for action, response in responses.items():
            with self.subTest(action=action):
                self.assertNotIn("storage", response)
        # Host stop and the idle-stop function validate exactly this drain evidence.
        self.assertEqual(responses["host-drain"], {"draining": True, "instances": {
            "survival": {"instance": "survival", "application": "stopped", "backup": "complete"}}})
        self.assertEqual(responses["update"], {"instance": "survival", "application": "running",
                                               "backup": "pending", "ready": True})
        self.assertEqual(self.machine.events.count("storage-usage"), 2)

    def test_storage_usage_is_never_read_from_unverified_storage(self):
        self.machine.storage_ok = False
        for action, instance in (("start", "survival"), ("host-status", None)):
            with self.subTest(action=action):
                with self.assertRaisesRegex(minecraft_instance.OperationError, "mount"):
                    self.manager.execute(action, instance)
        self.assertNotIn("storage-usage", self.machine.events)

    def test_linux_machine_reports_usage_of_its_own_root_with_the_df_formula(self):
        machine = object.__new__(minecraft_instance.LinuxMachine)
        machine.root = self.root / "any-retained-root"
        cases = (
            # f_blocks, f_bfree, f_bavail -> used percent of the space an unprivileged writer can use
            (1_000_000, 300_000, 250_000, 74),  # 700000 / 950000 = 73.7, rounded up like df
            (1050, 250, 200, 80),               # exactly 80: the root-reserved 50 blocks are excluded
            (1050, 259, 209, 80),               # 79.1 rounds up
            (1050, 260, 210, 79),               # exactly 79
            (1000, 1000, 950, 0),
            (1000, 50, 0, 100),                 # only root-reserved blocks remain
            (1000, 0, 0, 100),
            (0, 0, 0, 100),                     # no capacity at all is full, not a division error
        )
        for blocks, free, available, percent in cases:
            with self.subTest(blocks=blocks, free=free, available=available):
                usage = mock.Mock(f_frsize=4096, f_bsize=1024 ** 2, f_blocks=blocks, f_bfree=free, f_bavail=available)
                with mock.patch.object(minecraft_instance.os, "statvfs", return_value=usage) as statvfs:
                    reported = machine.storage_usage()
                statvfs.assert_called_once_with(machine.root)
                self.assertEqual(reported, {"size_bytes": 4096 * blocks, "available_bytes": 4096 * available,
                                            "used_percent": percent})
                self.assertTrue(all(type(value) is int for value in reported.values()))

    def test_local_validation_machines_inherit_usage_of_their_fixture_root(self):
        import validate_lifecycle
        import validate_recovery
        for machine_type in (validate_lifecycle.LocalMachine, validate_recovery.RecoveryMachine):
            with self.subTest(machine=machine_type.__name__):
                machine = object.__new__(machine_type)
                machine.root = self.root
                reported = machine.storage_usage()
                actual = os.statvfs(self.root)
                self.assertEqual(reported["size_bytes"], actual.f_frsize * actual.f_blocks)
                self.assertLessEqual(0, reported["available_bytes"])
                self.assertLessEqual(reported["available_bytes"], reported["size_bytes"])
                self.assertIn(reported["used_percent"], range(101))

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

    def test_private_drain_reports_exact_final_session_without_changing_public_shape(self):
        self.machine.backup_works = True
        self.manager.execute('start', 'survival')
        session = self.manager.state('survival')['session']
        result = self.manager.execute('host-drain-private')
        self.assertEqual(result['boot_id'], self.machine.boot)
        self.assertEqual(result['recovery']['survival'], {'session': session,
            'manifest_sha256': self.machine.revision, 'recovery_point': 'instances/survival/session-1'})
        self.assertEqual(self.manager.execute('host-drain'), {key: result[key] for key in ('draining', 'instances')})
        self.assertEqual(self.machine.events.count('backup'), 1)

    def test_private_drain_cannot_call_an_unplayed_instance_a_recoverable_session(self):
        with self.assertRaisesRegex(minecraft_instance.OperationError, 'final-session'):
            self.manager.execute('host-drain-private')


if __name__ == "__main__":
    unittest.main()
