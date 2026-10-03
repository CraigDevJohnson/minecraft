from __future__ import annotations

import contextlib
import io
import json
import math
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
MIB = 1024 ** 2
GIB = 1024 ** 3
WHOLE_DEVICE_COMMANDS = ("cmp", "blkdiscard")
PARTIAL_ZERO_GUIDANCE = ("the disk may be partially zeroed. Do not initialize it "
                         "until zero-fill is rerun to completion")


def expected_whole_device_bound(size: int) -> int:
    """Five minutes plus the whole device at a 50 MiB/s floor."""
    return 300 + math.ceil(size / (50 * 1024 * 1024))


def usable_bytes(device_bytes: int) -> int:
    """What statvfs reports for ext4 spanning a device: its size less metadata overhead."""
    return device_bytes - device_bytes // 32


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
        self.zero_filled = False
        self.mount_works = True
        self.size = 40 * GIB
        self.blockdev_output: str | None = None
        self.timeouts: list[tuple[str, int]] = []
        # The device size the filesystem spans; None until a test enlarges the disk under it.
        self.filesystem_spans: int | None = None

    def require_root(self) -> None:
        pass

    @contextlib.contextmanager
    def lock(self):
        yield

    def prepare_mountpoint(self) -> None:
        if not self.mountpoint_empty:
            raise minecraft_storage.StorageError("mountpoint is occupied")

    def filesystem_size(self) -> int:
        if not self.mounts:
            raise AssertionError("filesystem size was read without a mounted filesystem")
        return usable_bytes(self.size if self.filesystem_spans is None else self.filesystem_spans)

    def run(self, command: list[str], *, timeout: int, allowed=(0,)) -> str:
        self.assert_bounded(command, timeout)
        self.calls.append(command)
        self.timeouts.append((command[0], timeout))
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
            if command != ["blockdev", "--getsize64", self.disk["name"]]:
                raise AssertionError(f"unexpected size query: {command}")
            return f"{self.size}\n" if self.blockdev_output is None else self.blockdev_output
        if tool == "cmp":
            if command != ["cmp", "--silent", "--bytes", str(self.size), self.disk["name"], "/dev/zero"]:
                raise AssertionError(f"zero check does not cover exactly the device: {command}")
            if not self.all_zero:
                raise minecraft_storage.StorageError("disk contains nonzero bytes")
            return ""
        if tool == "blkdiscard":
            self.zero_filled = True
            self.all_zero = True
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
        if tool == "resize2fs":
            if command != ["resize2fs", self.disk["name"]]:
                raise AssertionError(f"resize must name only the whole data disk, with no size: {command}")
            if not self.mounts or self.disk["mountpoints"] != ["/srv/minecraft"]:
                raise AssertionError("resize2fs ran against a disk that is not mounted at the data mountpoint")
            self.filesystem_spans = None  # without a size argument it extends to the device and never shrinks
            return ""
        raise AssertionError(f"unexpected command: {command}")

    def assert_bounded(self, command: list[str], timeout: int) -> None:
        if command[0] in WHOLE_DEVICE_COMMANDS:
            if timeout != expected_whole_device_bound(self.size):
                raise AssertionError(f"{command[0]} timeout {timeout} is not derived from device size {self.size}")
        elif not 0 < timeout <= 900:
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
        self.assertFalse(any(call[0] in ("cmp", "blkdiscard") for call in host.calls))

    def test_fresh_nonzero_volume_needs_explicit_zero_fill_before_initialize(self) -> None:
        host = FakeHost()
        host.disk.update(fstype=None, uuid=None)
        host.all_zero = False
        self.assertEqual(self.invoke(host, "initialize", ["--confirm-volume-id", VOLUME])[0], 1)
        self.assertFalse(host.formatted)
        result, output = self.invoke(host, "zero-fill", ["--confirm-volume-id", VOLUME])
        self.assertEqual(result, 0, output)
        self.assertIn(["blkdiscard", "--zeroout", "/dev/nvme1n1"], host.calls)
        self.assertFalse(host.formatted)
        result, output = self.invoke(host, "initialize", ["--confirm-volume-id", VOLUME])
        self.assertEqual(result, 0, output)
        self.assertTrue(host.formatted)

    def test_zero_fill_requires_the_explicit_matching_confirmation(self) -> None:
        for extra in ([], ["--confirm-volume-id", "vol-fffffffffffffffff"]):
            host = FakeHost()
            host.disk.update(fstype=None, uuid=None)
            result, output = self.invoke(host, "zero-fill", extra)
            self.assertEqual(result, 1, output)
            self.assertFalse(host.zero_filled)

    def test_zero_fill_never_erases_recognized_or_attached_data(self) -> None:
        scenarios = ("expected-filesystem", "wrong-filesystem", "signature", "partition", "mounted-elsewhere", "target-mounted", "missing")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                host = FakeHost()
                if scenario == "wrong-filesystem":
                    host.disk["fstype"] = "xfs"
                elif scenario != "expected-filesystem":
                    host.disk.update(fstype=None, uuid=None)
                    if scenario == "signature":
                        host.signatures = [{"type": "crypto_LUKS"}]
                    elif scenario == "partition":
                        host.disk["children"] = [{"name": "/dev/nvme1n1p1"}]
                    elif scenario == "mounted-elsewhere":
                        host.disk["mountpoints"] = ["/other"]
                    elif scenario == "target-mounted":
                        host.mounts = [{"target": "/srv/minecraft", "source": "/dev/nvme2n1"}]
                    elif scenario == "missing":
                        host.devices = []
                result, output = self.invoke(host, "zero-fill", ["--confirm-volume-id", VOLUME])
                self.assertEqual(result, 1, output)
                self.assertFalse(host.zero_filled)
                self.assertFalse(host.formatted)

    def test_whole_device_bound_scales_with_device_size(self) -> None:
        boundaries = {1: 301, 50 * MIB: 301, 50 * MIB + 1: 302, 100 * MIB: 302,
                      GIB: 321, 5 * GIB: 403, 40 * GIB: 1120, 220 * GIB: 4806}
        for size, seconds in boundaries.items():
            with self.subTest(size=size):
                self.assertEqual(minecraft_storage.whole_device_timeout(size), seconds)
        for size in (1, 40 * GIB, 220 * GIB, 220 * GIB + 1):
            self.assertEqual(minecraft_storage.whole_device_timeout(size), expected_whole_device_bound(size))

    def test_zero_fill_and_initialize_use_the_size_derived_bound(self) -> None:
        for size in (40 * GIB, 220 * GIB):
            with self.subTest(size=size):
                host = FakeHost()
                host.size = size
                host.disk.update(fstype=None, uuid=None)
                host.all_zero = False
                result, output = self.invoke(host, "zero-fill", ["--confirm-volume-id", VOLUME])
                self.assertEqual(result, 0, output)
                result, output = self.invoke(host, "initialize", ["--confirm-volume-id", VOLUME])
                self.assertEqual(result, 0, output)
                self.assertTrue(host.formatted)
                bound = expected_whole_device_bound(size)
                self.assertGreater(bound, 900)
                self.assertEqual([t for tool, t in host.timeouts if tool in WHOLE_DEVICE_COMMANDS], [bound, bound])
                self.assertEqual([tool for tool, _ in host.timeouts if tool in WHOLE_DEVICE_COMMANDS], ["blkdiscard", "cmp"])

    def test_failed_zero_fill_warns_of_partial_zeroing_and_never_formats(self) -> None:
        class DiscardFailsHost(FakeHost):
            def run(self, command, *, timeout, allowed=(0,)):
                if command[0] != "blkdiscard":
                    return super().run(command, timeout=timeout, allowed=allowed)
                self.assert_bounded(command, timeout)
                self.calls.append(command)
                return minecraft_storage.LinuxHost().run(command, timeout=timeout, allowed=allowed)
        failures = {
            "timeout": ({"side_effect": subprocess.TimeoutExpired("blkdiscard", 4806)}, "timed out"),
            "nonzero-exit": ({"return_value": subprocess.CompletedProcess(["blkdiscard"], 1, "", "")}, "exit 1"),
            "interrupt": ({"side_effect": KeyboardInterrupt}, "interrupted"),
        }
        for failure, (behavior, cause) in failures.items():
            with self.subTest(failure=failure):
                host = DiscardFailsHost()
                host.size = 220 * GIB
                host.disk.update(fstype=None, uuid=None)
                host.all_zero = False
                with mock.patch.object(minecraft_storage.subprocess, "run", **behavior) as command:
                    try:
                        result, output = self.invoke(host, "zero-fill", ["--confirm-volume-id", VOLUME])
                    except KeyboardInterrupt:
                        self.fail("interrupted zero-fill escaped without the partial-zero warning")
                self.assertEqual(command.call_args.args[0], ["blkdiscard", "--zeroout", "/dev/nvme1n1"])
                self.assertEqual(command.call_args.kwargs["timeout"], expected_whole_device_bound(host.size))
                self.assertEqual(result, 1, output)
                self.assertIn(cause, output)
                self.assertIn(PARTIAL_ZERO_GUIDANCE, output)
                self.assertNotIn("disk zero-filled", output)
                self.assertFalse(any(call[0] == "mkfs.ext4" for call in host.calls))
                result, output = self.invoke(host, "initialize", ["--confirm-volume-id", VOLUME])
                self.assertEqual(result, 1, output)
                self.assertFalse(host.formatted)

    def test_unknown_disk_size_refuses_zero_fill_and_initialize(self) -> None:
        for action in ("zero-fill", "initialize"):
            for blockdev_output in ("0\n", "abc\n", "", "-1\n", "1_000\n"):
                with self.subTest(action=action, blockdev_output=blockdev_output):
                    host = FakeHost()
                    host.blockdev_output = blockdev_output
                    host.disk.update(fstype=None, uuid=None)
                    result, output = self.invoke(host, action, ["--confirm-volume-id", VOLUME])
                    self.assertEqual(result, 1, output)
                    self.assertIn("cannot establish disk size", output)
                    self.assertFalse(any(call[0] in ("blkdiscard", "cmp", "mkfs.ext4") for call in host.calls))
                    self.assertFalse(host.zero_filled)
                    self.assertFalse(host.formatted)

    def enlarged(self, *, device=10 * GIB, filesystem=5 * GIB) -> FakeHost:
        """A mounted, verified filesystem whose EBS disk has since been enlarged."""
        host = FakeHost()
        host.size = filesystem
        self.assertEqual(self.invoke(host)[0], 0)
        host.size, host.filesystem_spans = device, filesystem
        host.calls.clear()
        host.timeouts.clear()
        return host

    def test_grow_extends_the_verified_mounted_filesystem_to_an_enlarged_disk(self) -> None:
        host = self.enlarged()
        result, output = self.invoke(host, "grow")
        self.assertEqual(result, 0, output)
        self.assertIn(f"filesystem grew from {usable_bytes(5 * GIB)} to {usable_bytes(10 * GIB)} bytes", output)
        tools = [call[0] for call in host.calls]
        self.assertEqual(tools.count("resize2fs"), 1)
        resize = tools.index("resize2fs")
        # Disk identity, filesystem identity and the writable mount are proven first, and the mount again after.
        for check in ("lsblk", "blkid", "findmnt"):
            self.assertIn(check, tools[:resize])
        self.assertIn("findmnt", tools[resize + 1:])
        self.assertIn(("resize2fs", 300), host.timeouts)
        self.assertFalse(any(tool in ("mount", "mkfs.ext4", "blkdiscard", "cmp", "wipefs") for tool in tools))
        self.assertEqual(self.invoke(host, "verify")[0], 0)

    def test_grow_on_a_disk_that_is_no_larger_succeeds_and_says_nothing_grew(self) -> None:
        for device in (5 * GIB, 40 * GIB):
            with self.subTest(device=device):
                host = self.enlarged(device=device, filesystem=device)
                result, output = self.invoke(host, "grow")
                self.assertEqual(result, 0, output)
                self.assertIn("filesystem not grown", output)
                self.assertIn(f"{device}-byte disk", output)
                self.assertIn("optimizing", output)
                self.assertNotIn("grew", output)
                self.assertFalse(any(call[0] in ("mount", "mkfs.ext4") for call in host.calls))
                result, output = self.invoke(host, "grow")
                self.assertEqual(result, 0, output)

    def test_grow_refuses_unverified_storage_without_resizing(self) -> None:
        other_uuid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        scenarios = {
            "unmounted": lambda host: (host.mounts.clear(), host.disk.update(mountpoints=[None])),
            "wrong-uuid": lambda host: host.disk.update(uuid=other_uuid),
            "wrong-filesystem": lambda host: host.disk.update(fstype="xfs"),
            "blank": lambda host: host.disk.update(fstype=None, uuid=None),
            "ambiguous": lambda host: host.devices.append(dict(host.disk, name="/dev/nvme2n1")),
            "missing": lambda host: host.devices.clear(),
            "partitioned": lambda host: host.disk.update(children=[{"name": "/dev/nvme1n1p1"}]),
            "read-only": lambda host: host.mounts[0].update(options="ro,relatime"),
            "another-device-mounted": lambda host: host.mounts[0].update({"maj:min": "259:0"}),
            "another-uuid-mounted": lambda host: host.mounts[0].update(uuid=other_uuid),
            "subdirectory-mounted": lambda host: host.mounts[0].update(fsroot="/instances"),
            "also-mounted-elsewhere": lambda host: host.disk.update(mountpoints=["/srv/minecraft", "/other"]),
        }
        for scenario, change in scenarios.items():
            with self.subTest(scenario=scenario):
                host = self.enlarged()
                change(host)
                result, output = self.invoke(host, "grow")
                self.assertEqual(result, 1, output)
                self.assertIn("storage grow failed", output)
                self.assertNotIn("grew", output)
                self.assertFalse(any(call[0] in ("resize2fs", "mount", "mkfs.ext4", "blkdiscard") for call in host.calls))
                self.assertEqual(host.filesystem_spans, 5 * GIB)

    def test_failed_resize_reports_failure_and_can_be_rerun(self) -> None:
        class ResizeFailsHost(FakeHost):
            def run(self, command, *, timeout, allowed=(0,)):
                if command[0] != "resize2fs" or not getattr(self, "resize_fails", True):
                    return super().run(command, timeout=timeout, allowed=allowed)
                self.assert_bounded(command, timeout)
                self.calls.append(command)
                return minecraft_storage.LinuxHost().run(command, timeout=timeout, allowed=allowed)
        failures = {
            "timeout": {"side_effect": subprocess.TimeoutExpired("resize2fs", 300)},
            "nonzero-exit": {"return_value": subprocess.CompletedProcess(["resize2fs"], 1, "", "")},
        }
        for failure, behavior in failures.items():
            with self.subTest(failure=failure):
                host = ResizeFailsHost()
                host.size = 5 * GIB
                self.assertEqual(self.invoke(host)[0], 0)
                host.size, host.filesystem_spans = 10 * GIB, 5 * GIB
                with mock.patch.object(minecraft_storage.subprocess, "run", **behavior) as command:
                    result, output = self.invoke(host, "grow")
                self.assertEqual(command.call_args.args[0], ["resize2fs", "/dev/nvme1n1"])
                self.assertEqual(command.call_args.kwargs["timeout"], 300)
                self.assertEqual(result, 1, output)
                self.assertIn("storage grow failed: resize2fs failed or timed out", output)
                self.assertIn("rerun grow", output)
                self.assertNotIn("grew", output)
                self.assertNotIn("unavailable", output)
                # The filesystem stays mounted and usable, and a rerun finishes the grow.
                self.assertEqual(self.invoke(host, "verify")[0], 0)
                host.resize_fails = False
                result, output = self.invoke(host, "grow")
                self.assertEqual(result, 0, output)
                self.assertIn("filesystem grew", output)

    def test_grow_needs_no_destructive_confirmation_and_other_actions_never_resize(self) -> None:
        host = self.enlarged()
        for action, extra in (("mount", []), ("verify", []), ("initialize", ["--confirm-volume-id", VOLUME])):
            self.assertEqual(self.invoke(host, action, extra)[0], 0)
        self.assertFalse(any(call[0] == "resize2fs" for call in host.calls))
        self.assertEqual(host.filesystem_spans, 5 * GIB)
        self.assertEqual(self.invoke(host, "grow", ["--confirm-volume-id", "vol-fffffffffffffffff"])[0], 0)
        self.assertIsNone(host.filesystem_spans)

    def test_filesystem_size_is_read_from_the_data_mountpoint_in_fragment_units(self) -> None:
        usage = mock.Mock(f_frsize=4096, f_bsize=1024 ** 2, f_blocks=1_270_000, f_bfree=9, f_bavail=7)
        with mock.patch.object(minecraft_storage.os, "statvfs", return_value=usage) as statvfs:
            self.assertEqual(minecraft_storage.LinuxHost().filesystem_size(), 4096 * 1_270_000)
        statvfs.assert_called_once_with("/srv/minecraft")

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
