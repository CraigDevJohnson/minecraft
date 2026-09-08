from __future__ import annotations

import contextlib
import io
import json
import sys
import subprocess
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_storage  # noqa: E402

VOLUME = "vol-0123456789abcdef0"
UUID = "01234567-89ab-4cde-8fab-0123456789ab"


class FakeHost:
    """The command/OS boundary: no test can reach a real device or mount."""

    def __init__(self) -> None:
        self.disk = {
            "name": "/dev/nvme1n1",
            "type": "disk",
            "serial": VOLUME.replace("-", ""),
            "fstype": "ext4",
            "uuid": UUID,
            "pttype": None,
            "mountpoints": [None],
            "maj:min": "259:1",
        }
        self.devices = [self.disk]
        self.mounts: list[dict] = []
        self.signatures: list[dict] = []
        self.all_zero = True
        self.mountpoint_empty = True
        self.calls: list[list[str]] = []
        self.formatted = False
        self.mount_works = True

    def require_root(self) -> None:
        pass

    @contextlib.contextmanager
    def lock(self):
        yield

    def prepare_mountpoint(self) -> None:
        if not self.mountpoint_empty:
            raise minecraft_storage.StorageError("mountpoint is occupied")

    def run(self, command: list[str], *, timeout: int, allowed=(0,)) -> str:
        self.assert_bounded(timeout)
        self.calls.append(command)
        tool = command[0]
        if tool == "lsblk":
            return json.dumps({"blockdevices": self.devices})
        if tool == "findmnt":
            return json.dumps({"filesystems": self.mounts})
        if tool == "blkid":
            return f"DEVNAME={self.disk['name']}\nTYPE={self.disk['fstype']}\nUUID={self.disk['uuid']}\n"
        if tool == "wipefs":
            return json.dumps({"signatures": self.signatures})
        if tool == "blockdev":
            return "42949672960\n"
        if tool == "cmp":
            if not self.all_zero:
                raise minecraft_storage.StorageError("disk contains nonzero bytes")
            return ""
        if tool == "mkfs.ext4":
            self.formatted = True
            self.disk.update(fstype="ext4", uuid=UUID)
            return ""
        if tool == "mount":
            if self.mount_works:
                self.mounts = [{"target": "/srv/minecraft", "source": self.disk["name"], "fstype": "ext4", "uuid": UUID, "maj:min": "259:1", "options": "rw,relatime", "fsroot": "/"}]
                self.disk["mountpoints"] = ["/srv/minecraft"]
            return ""
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def assert_bounded(timeout: int) -> None:
        if not 0 < timeout <= 900:
            raise AssertionError(f"unbounded command timeout: {timeout}")


class StorageCommandTests(unittest.TestCase):
    def invoke(self, host: FakeHost, action="mount", extra=()) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = minecraft_storage.main(
                [action, "--volume-id", VOLUME, "--filesystem-uuid", UUID, *extra],
                host=host,
            )
        return result, output.getvalue()

    def test_expected_filesystem_mounts_and_repeated_mount_is_safe(self) -> None:
        host = FakeHost()
        result, output = self.invoke(host)
        self.assertEqual(result, 0, output)
        result, output = self.invoke(host)
        self.assertEqual(result, 0, output)
        result, output = self.invoke(host, "verify")
        self.assertEqual(result, 0, output)
        self.assertEqual(sum(command[0] == "mount" for command in host.calls), 1)
        self.assertFalse(host.formatted)

    def test_missing_and_wrong_volume_never_mount_or_format(self) -> None:
        for devices in ([], [{"serial": "volfffffffffffffffff"}]):
            with self.subTest(devices=devices):
                host = FakeHost()
                host.devices = devices
                result, output = self.invoke(host)
                self.assertEqual(result, 1, output)
                self.assertIn("missing", output)
                self.assertFalse(any(call[0] in ("mount", "mkfs.ext4") for call in host.calls))

    def test_wrong_uuid_or_filesystem_is_preserved(self) -> None:
        for change in ({"uuid": "ffffffff-ffff-4fff-8fff-ffffffffffff"}, {"fstype": "xfs"}):
            with self.subTest(change=change):
                host = FakeHost()
                host.disk.update(change)
                result, output = self.invoke(host)
                self.assertEqual(result, 1, output)
                self.assertIn("mismatch", output)
                self.assertFalse(any(call[0] in ("mount", "mkfs.ext4") for call in host.calls))

    def test_verify_cannot_start_runtime_when_mount_is_missing(self) -> None:
        host = FakeHost()
        result, output = self.invoke(host, "verify")
        self.assertEqual(result, 1, output)
        self.assertIn("runtime startup is forbidden", output)
        self.assertFalse(any(call[0] == "mount" for call in host.calls))

    def test_mount_command_success_without_a_real_mount_is_rejected(self) -> None:
        host = FakeHost()
        host.mount_works = False
        result, output = self.invoke(host)
        self.assertEqual(result, 1, output)
        self.assertIn("runtime startup is forbidden", output)

    def test_occupied_root_directory_is_not_hidden_by_mount(self) -> None:
        host = FakeHost()
        host.mountpoint_empty = False
        result, output = self.invoke(host)
        self.assertEqual(result, 1, output)
        self.assertFalse(any(call[0] == "mount" for call in host.calls))

    def test_wrong_or_readonly_actual_mount_blocks_runtime(self) -> None:
        for change in ({"maj:min": "259:0"}, {"uuid": "wrong"}, {"options": "ro"}, {"fsroot": "/wrong-data"}):
            with self.subTest(change=change):
                host = FakeHost()
                self.assertEqual(self.invoke(host)[0], 0)
                host.mounts[0].update(change)
                self.assertEqual(self.invoke(host, "verify")[0], 1)
                self.assertEqual(sum(call[0] == "mount" for call in host.calls), 1)

    def test_explicit_initialize_formats_only_blank_disk_then_mount_can_succeed(self) -> None:
        host = FakeHost()
        host.disk.update(fstype=None, uuid=None)
        result, output = self.invoke(host, "initialize", ["--confirm-volume-id", VOLUME])
        self.assertEqual(result, 0, output)
        self.assertTrue(host.formatted)
        self.assertEqual(self.invoke(host)[0], 0)

    def test_occupied_disks_are_never_reformatted(self) -> None:
        scenarios = ("expected-filesystem", "wrong-filesystem", "signature", "unknown-bytes", "partition", "mounted-elsewhere")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                host = FakeHost()
                if scenario == "wrong-filesystem":
                    host.disk["fstype"] = "xfs"
                elif scenario != "expected-filesystem":
                    host.disk.update(fstype=None, uuid=None)
                    if scenario == "signature":
                        host.signatures = [{"type": "LVM2_member"}]
                    elif scenario == "unknown-bytes":
                        host.all_zero = False
                    elif scenario == "partition":
                        host.disk["children"] = [{"name": "/dev/nvme1n1p1"}]
                    elif scenario == "mounted-elsewhere":
                        host.disk["mountpoints"] = ["/other"]
                result, output = self.invoke(host, "initialize", ["--confirm-volume-id", VOLUME])
                self.assertEqual(result, 0 if scenario == "expected-filesystem" else 1, output)
                self.assertFalse(host.formatted)

    def test_initialize_requires_the_explicit_matching_confirmation(self) -> None:
        for extra in ([], ["--confirm-volume-id", "vol-fffffffffffffffff"]):
            host = FakeHost()
            host.disk.update(fstype=None, uuid=None)
            result, output = self.invoke(host, "initialize", extra)
            self.assertEqual(result, 1, output)
            self.assertFalse(host.formatted)

    def test_boot_mount_never_initializes_a_blank_disk(self) -> None:
        host = FakeHost()
        host.disk.update(fstype=None, uuid=None)
        result, output = self.invoke(host)
        self.assertEqual(result, 1, output)
        self.assertFalse(host.formatted)
        self.assertFalse(any(call[0] == "cmp" for call in host.calls))

    def test_command_timeout_is_a_clear_bounded_failure(self) -> None:
        class TimedOutHost(FakeHost):
            def run(self, command, *, timeout, allowed=(0,)):
                return minecraft_storage.LinuxHost().run(command, timeout=timeout, allowed=allowed)
        with mock.patch.object(minecraft_storage.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsblk", 10)) as command:
            result, output = self.invoke(TimedOutHost())
        self.assertEqual(command.call_args.kwargs["timeout"], 10)
        self.assertEqual(result, 1, output)
        self.assertIn("timed out", output)


if __name__ == "__main__":
    unittest.main()
