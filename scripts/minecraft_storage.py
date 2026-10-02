#!/usr/bin/env python3
"""Establish or verify the retained EBS mount; formatting is operator-only."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MOUNTPOINT = "/srv/minecraft"
WHOLE_DEVICE_FLOOR_BYTES_PER_SECOND = 50 * 1024 * 1024
WHOLE_DEVICE_BASE_SECONDS = 300


class StorageError(Exception):
    pass


class LinuxHost:
    """Linux command and filesystem boundary, replaceable by offline test fakes."""

    def require_root(self) -> None:
        if os.geteuid() != 0:
            raise StorageError("run this command as root")
        if str(Path(MOUNTPOINT).resolve()) != MOUNTPOINT:
            raise StorageError("mountpoint or its parent is a symlink")

    @contextlib.contextmanager
    def lock(self):
        with open("/run/lock/minecraft-storage.lock", "a", encoding="utf-8") as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StorageError("another storage operation holds the lock")
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def prepare_mountpoint(self) -> None:
        path = Path(MOUNTPOINT)
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        if not path.is_dir() or next(path.iterdir(), None) is not None:
            raise StorageError("mountpoint is occupied; refusing to hide root-disk data")

    def run(self, command: list[str], *, timeout: int, allowed=(0,)) -> str:
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=timeout,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise StorageError(f"{command[0]} failed or timed out") from error
        if result.returncode not in allowed:
            raise StorageError(f"{command[0]} failed (exit {result.returncode}); storage remains unavailable")
        return result.stdout


def discover(host, volume_id: str) -> dict:
    inventory = json.loads(host.run(
        ["lsblk", "--json", "--paths", "--output", "NAME,TYPE,SERIAL,FSTYPE,UUID,PTTYPE,MOUNTPOINTS,MAJ:MIN"],
        timeout=10,
    ))
    disks = [disk for disk in inventory["blockdevices"]
             if (disk.get("serial") or "").strip().replace("-", "") == volume_id.replace("-", "")]
    if len(disks) != 1:
        raise StorageError("expected EBS volume is missing or ambiguous; retry after attachment")
    disk = disks[0]
    if disk["type"] != "disk" or not re.fullmatch(r"/dev/nvme\d+n\d+", disk["name"]):
        raise StorageError("expected a whole NVMe EBS disk")
    if disk.get("children") or disk.get("pttype"):
        raise StorageError("disk has partitions; refusing to alter it")
    return disk


def mounted(host) -> list[dict]:
    result = host.run(
        ["findmnt", "--json", "--mountpoint", MOUNTPOINT, "--output", "TARGET,SOURCE,FSTYPE,UUID,MAJ:MIN,OPTIONS,FSROOT"],
        timeout=10, allowed=(0, 1),
    )
    return json.loads(result).get("filesystems", []) if result.strip() else []


def verify_filesystem(host, disk: dict, filesystem_uuid: str) -> None:
    properties = dict(line.split("=", 1) for line in host.run(
        ["blkid", "--probe", "--output", "export", disk["name"]], timeout=10,
    ).splitlines() if "=" in line)
    if properties.get("TYPE") != "ext4" or properties.get("UUID") != filesystem_uuid:
        raise StorageError("filesystem type or UUID mismatch; no formatting was attempted")
    other_mounts = [point for point in disk.get("mountpoints", []) if point and point != MOUNTPOINT]
    if other_mounts:
        raise StorageError("data disk is already mounted at another location")


def verify_mount(host, disk: dict, filesystem_uuid: str) -> None:
    mounts = mounted(host)
    if len(mounts) != 1:
        raise StorageError("expected data mount is absent; runtime startup is forbidden")
    mount = mounts[0]
    if (mount.get("target") != MOUNTPOINT or mount.get("maj:min") != disk["maj:min"]
            or mount.get("fstype") != "ext4" or mount.get("uuid") != filesystem_uuid
            or mount.get("fsroot") != "/"
            or "rw" not in mount.get("options", "").split(",")):
        raise StorageError("mount is not the expected writable EBS filesystem")


def whole_device_timeout(size: int) -> int:
    """Bound a whole-device pass: five minutes plus the device at a 50 MiB/s floor."""
    return WHOLE_DEVICE_BASE_SECONDS + -(-size // WHOLE_DEVICE_FLOOR_BYTES_PER_SECOND)


def device_size(host, disk: dict) -> int:
    size = int(host.run(["blockdev", "--getsize64", disk["name"]], timeout=10).strip())
    if size <= 0:
        raise StorageError("cannot establish disk size")
    return size


def zero_fill(host, disk: dict, volume_id: str) -> None:
    """Overwrite a newly created, unrecognized volume with zeros before initialization.

    New encrypted EBS volumes can read back nonzero bytes. The operator proves the
    volume's AWS provenance first; this command only refuses recognizable data.
    """
    if disk.get("fstype") or disk.get("uuid"):
        raise StorageError("disk already has a filesystem; refusing to zero-fill")
    if any(disk.get("mountpoints", [])) or mounted(host):
        raise StorageError("disk or target is mounted; refusing to zero-fill")
    signatures = json.loads(host.run(["wipefs", "--no-act", "--json", disk["name"]], timeout=10))
    if signatures.get("signatures"):
        raise StorageError("disk has existing signatures; refusing to zero-fill")
    size = device_size(host, disk)
    current = discover(host, volume_id)
    if (current["name"] != disk["name"] or current["maj:min"] != disk["maj:min"]
            or current.get("fstype") or current.get("uuid") or any(current.get("mountpoints", []))):
        raise StorageError("disk state changed during checks; refusing to zero-fill")
    try:
        host.run(["blkdiscard", "--zeroout", disk["name"]], timeout=whole_device_timeout(size))
    except StorageError as error:
        raise StorageError(f"{error}; the disk may be partially zeroed. Do not initialize it "
                           "until zero-fill is rerun to completion") from error


def initialize(host, disk: dict, volume_id: str, filesystem_uuid: str) -> None:
    if disk.get("fstype") or disk.get("uuid"):
        verify_filesystem(host, disk, filesystem_uuid)
        return
    if any(disk.get("mountpoints", [])) or mounted(host):
        raise StorageError("disk or target is mounted; refusing initialization")
    host.prepare_mountpoint()
    signatures = json.loads(host.run(["wipefs", "--no-act", "--json", disk["name"]], timeout=10))
    if signatures.get("signatures"):
        raise StorageError("disk has existing signatures; refusing initialization")
    size = device_size(host, disk)
    # Signatures alone cannot distinguish an empty disk from unrecognized data.
    try:
        host.run(["cmp", "--silent", "--bytes", str(size), disk["name"], "/dev/zero"],
                 timeout=whole_device_timeout(size))
    except StorageError as error:
        raise StorageError("disk is not proven entirely zero-filled; refusing initialization") from error
    current = discover(host, volume_id)
    if (current["name"] != disk["name"] or current["maj:min"] != disk["maj:min"]
            or current.get("fstype") or current.get("uuid")
            or any(current.get("mountpoints", [])) or mounted(host)):
        raise StorageError("disk state changed during blank-disk check; refusing initialization")
    host.run(["mkfs.ext4", "-q", "-U", filesystem_uuid, "-E", "lazy_itable_init=0,lazy_journal_init=0", disk["name"]], timeout=120)
    verify_filesystem(host, discover(host, volume_id), filesystem_uuid)


def main(argv=None, *, host=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("mount", "verify", "zero-fill", "initialize"))
    parser.add_argument("--volume-id", required=True)
    parser.add_argument("--confirm-volume-id", help="required for the explicit zero-fill and blank-disk initialization operations")
    parser.add_argument("--filesystem-uuid", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"vol-[0-9a-f]{17}", args.volume_id):
        parser.error("volume ID must be an EBS vol- identifier with 17 lowercase hex digits")
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", args.filesystem_uuid):
        parser.error("filesystem UUID must be canonical lowercase hexadecimal")
    host = host or LinuxHost()
    try:
        host.require_root()
        with host.lock():
            disk = discover(host, args.volume_id)
            if args.action in ("zero-fill", "initialize") and args.confirm_volume_id != args.volume_id:
                raise StorageError(f"{args.action} requires --confirm-volume-id matching the intended volume")
            if args.action == "zero-fill":
                zero_fill(host, disk, args.volume_id)
                print("disk zero-filled; run initialize separately")
                return 0
            if args.action == "initialize":
                initialize(host, disk, args.volume_id, args.filesystem_uuid)
                print("filesystem initialized or already matches; run mount separately")
                return 0
            verify_filesystem(host, disk, args.filesystem_uuid)
            if args.action == "mount" and not mounted(host):
                host.prepare_mountpoint()
                host.run(["mount", "--types", "ext4", "--options", "rw,nosuid,nodev", disk["name"], MOUNTPOINT], timeout=30)
                disk = discover(host, args.volume_id)
                verify_filesystem(host, disk, args.filesystem_uuid)
            verify_mount(host, disk, args.filesystem_uuid)
        print(f"storage {args.action} succeeded: {MOUNTPOINT}")
        return 0
    except (StorageError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"storage unavailable: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
