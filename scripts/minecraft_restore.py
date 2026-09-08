#!/usr/bin/env python3
"""Restore a completed backup onto a distinct verified retained volume."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import uuid

from minecraft_backup import BackupError, load_recovery
from minecraft_content import manifest_digest
from minecraft_instance import LinuxMachine, OperationError, private_json, write_json


def extract_recovery(metadata, archive_path, destination):
    """Extract the validated inventory; ignore tar ownership and permissions."""
    expected = {entry["path"]: entry for entry in metadata["files"]}
    destination.mkdir(mode=0o700)
    seen = set()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                entry = expected.get(member.name)
                if not entry or member.name in seen:
                    raise BackupError("archive and recovery inventory differ")
                seen.add(member.name)
                output = destination / member.name
                if entry["type"] == "directory":
                    if not member.isdir() or member.size != 0:
                        raise BackupError("archive directory has an unexpected type or size")
                    output.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if (not member.isreg() or member.issparse() or member.size != entry["size_bytes"]):
                    raise BackupError("archive file has an unexpected type or size")
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied = 0
                with archive.extractfile(member) as source, output.open("xb") as stream:
                    os.chmod(output, 0o600)
                    while chunk := source.read(1024 * 1024):
                        stream.write(chunk)
                        copied += len(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if copied != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise BackupError("recovery file checksum or size mismatch")
    except (tarfile.TarError, EOFError) as error:
        raise BackupError("recovery archive is invalid or incomplete") from error
    if seen != set(expected):
        raise BackupError("archive and recovery inventory differ")


def apply_restore_ownership(data):
    for directory, _, files in os.walk(data):
        os.chown(directory, 1000, 1000)
        for filename in files:
            os.chown(Path(directory) / filename, 1000, 1000)


def restore(machine, source_deployment, instance, recovery_point, store):
    if instance not in machine.definitions:
        raise BackupError("restore requires an explicit configured instance")
    target = machine.root / "instances" / instance
    with machine.lock():
        machine.verify_storage()
        if target.exists() or target.is_symlink() or machine.inspect(instance)["exists"]:
            raise BackupError("restore requires an absent target instance directory and container")
        with tempfile.TemporaryDirectory(prefix=".minecraft-restore-", dir=machine.root) as temporary:
            workspace = Path(temporary)
            metadata, archive = load_recovery(store, instance, recovery_point, workspace)
            source = metadata["source"]
            if (source["volume_id"] != source_deployment["data_volume_id"]
                    or source["filesystem_uuid"] != source_deployment["data_filesystem_uuid"]):
                raise BackupError("recovery point does not belong to the supplied source deployment")
            if (source["volume_id"] == machine.deployment["data_volume_id"]
                    or source["filesystem_uuid"] == machine.deployment["data_filesystem_uuid"]):
                raise BackupError("restore requires a distinct target volume and filesystem")
            if (manifest_digest(machine.manifests[instance]) != metadata["manifest_sha256"]
                    or manifest_digest(machine.definitions[instance]) != metadata["definition_sha256"]):
                raise BackupError("target content manifest or definition is incompatible with the recovery point")
            staged = workspace / "instance"
            extract_recovery(metadata, archive, staged)
            write_json(staged / "runtime/manifest.json", metadata["manifest"])
            write_json(staged / "runtime/definition.json", metadata["definition"])
            write_json(staged / "operation.json", {
                "phase": "prepared", "backup": "not-required", "revision": metadata["manifest_sha256"],
                "session": str(uuid.uuid4()), "restored_from": recovery_point,
            })
            ownership = getattr(machine, "restore_ownership", apply_restore_ownership)
            ownership(staged / "data")
            for directory, _, _ in os.walk(staged, topdown=False):
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            machine.verify_storage()
            if target.exists() or target.is_symlink() or machine.inspect(instance)["exists"]:
                raise BackupError("restore target became occupied; no recovery data was installed")
            created_parent = not target.parent.exists()
            target.parent.mkdir(mode=0o700, exist_ok=True)
            published = False
            try:
                os.rename(staged, target)
                published = True
                descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                if published:
                    os.rename(target, staged)
                if created_parent:
                    with contextlib.suppress(OSError):
                        target.parent.rmdir()
                raise
    return {"schema_version": 1, "status": "restored", "instance": instance,
            "recovery_point": recovery_point, "application": "stopped", "backup": "not-required"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private target operator configuration")
    parser.add_argument("--source-deployment", required=True, help="private original deployment configuration")
    parser.add_argument("instance")
    parser.add_argument("recovery_point")
    args = parser.parse_args(argv)
    try:
        from minecraft_backup_store import S3Store
        machine = LinuxMachine(private_json(args.config))
        source = private_json(args.source_deployment)
        result = restore(machine, source, args.instance, args.recovery_point,
                         S3Store(source["backup_bucket_name"], region=source.get("region")))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OperationError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, (BackupError, OperationError)) else "invalid restore input or filesystem failure"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
