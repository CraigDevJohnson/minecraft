#!/usr/bin/env python3
"""Restore a completed backup onto a distinct verified retained volume."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import uuid

from minecraft_backup import (BackupError, budget_checker, check_budget, digest_file,
                              load_recovery, load_receipt_recovery, read_json, tree_snapshot)
from minecraft_content import manifest_digest
from minecraft_instance import LinuxMachine, OperationError, private_json, write_json, verify_restore_control_directories


def extract_recovery(metadata, archive_path, destination, *, check_deadline=None):
    """Extract the validated inventory; ignore tar ownership and permissions."""
    expected = {entry["path"]: entry for entry in metadata["files"]}
    if check_deadline is not None:
        check_deadline()
    destination.mkdir(mode=0o700)
    seen = set()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if check_deadline is not None:
                    check_deadline()
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
                        if check_deadline is not None:
                            check_deadline()
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
    if check_deadline is not None:
        check_deadline()


def apply_restore_ownership(data, *, check_deadline=None):
    for directory, _, files in os.walk(data):
        if check_deadline is not None:
            check_deadline()
        os.chown(directory, 1000, 1000)
        for filename in files:
            if check_deadline is not None:
                check_deadline()
            os.chown(Path(directory) / filename, 1000, 1000)


def verify_restored_receipt(target, metadata, receipt, deployment, *, metadata_reader=os.lstat, check_deadline=None):
    """Recognize only a complete, untouched install of this receipt on this target."""
    expected_storage = {'volume_id': deployment['data_volume_id'], 'filesystem_uuid': deployment['data_filesystem_uuid']}
    try:
        if (target.is_symlink() or not target.is_dir() or set(path.name for path in target.iterdir()) != {'data', 'runtime', 'operation.json'}
                or (target / 'runtime').is_symlink() or not (target / 'runtime').is_dir()
                or set(path.name for path in (target / 'runtime').iterdir()) != {'manifest.json', 'definition.json'}):
            raise BackupError('occupied restore target is ambiguous; preserve it for repair')
        for name in ('operation.json', 'runtime/manifest.json', 'runtime/definition.json'):
            if not stat.S_ISREG((target / name).lstat().st_mode):
                raise BackupError('occupied restore target has untrusted metadata; preserve it for repair')
        verify_restore_control_directories(target)
        state = private_json(target / 'operation.json')
        fields = {'phase', 'backup', 'revision', 'session', 'restored_from', 'restore_receipt_id', 'restore_target', 'bootstrap_sha256'}
        if (set(state) not in (fields, fields | {'restore_certified'})
                or ('restore_certified' in state and type(state['restore_certified']) is not bool)
                or state['phase'] != 'prepared' or state['backup'] != 'not-required'
                or state['revision'] != receipt['manifest_sha256'] or state['restored_from'] != receipt['recovery_point']
                or state['restore_receipt_id'] != receipt['receipt_id'] or state['restore_target'] != expected_storage
                or state['bootstrap_sha256'] != receipt['bootstrap_sha256']
                or not isinstance(state['session'], str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,128}', state['session'])
                or read_json(target / 'runtime/manifest.json') != metadata['manifest']
                or read_json(target / 'runtime/definition.json') != metadata['definition']):
            raise BackupError('occupied restore target does not belong to the same complete receipt; preserve it for repair')
        before = tree_snapshot(target / 'data', check_deadline=check_deadline)
        if set(before) != {entry['path'] for entry in metadata['files']}:
            raise BackupError('restored target inventory differs from its receipt; preserve it for repair')
        for entry in metadata['files']:
            if check_deadline is not None:
                check_deadline()
            path = target / entry['path']
            information = path.lstat()
            permissions = metadata_reader(path)
            expected_mode = 0o700 if entry['type'] == 'directory' else 0o600
            if (permissions.st_uid != 1000 or permissions.st_gid != 1000
                    or stat.S_IMODE(permissions.st_mode) != expected_mode):
                raise BackupError('restored target ownership or permissions differ from its receipt; preserve it for repair')
            if entry['type'] == 'directory':
                if not stat.S_ISDIR(information.st_mode):
                    raise BackupError('restored target directory differs from its receipt')
            elif information.st_size != entry['size_bytes'] or digest_file(path, check_deadline=check_deadline) != entry['sha256']:
                raise BackupError('restored target checkpoint differs from its receipt; preserve it for repair')
        if before != tree_snapshot(target / 'data', check_deadline=check_deadline):
            raise BackupError('restored target changed during verification; preserve it for repair')
    except (OSError, KeyError, TypeError) as error:
        raise BackupError('occupied restore target is partial or unverifiable; preserve it for repair') from error
    if check_deadline is not None:
        check_deadline()


def restore(machine, source_deployment, instance, recovery_point, store, *, receipt=None,
            bootstrap_sha256=None, resume=False):
    if instance not in machine.definitions:
        raise BackupError("restore requires an explicit configured instance")
    target = machine.root / "instances" / instance
    with machine.lock():
        machine.verify_storage()
        occupied = target.exists() or target.is_symlink()
        if machine.inspect(instance)["exists"] or (occupied and not (receipt is not None and resume)):
            raise BackupError("restore requires an absent target instance directory and container")
        if (occupied and receipt is not None and resume and not target.is_symlink()
                and target.is_dir() and target == target.resolve()):
            verify_restore_control_directories(target)
            state = private_json(target / 'operation.json')
            expected_target = {'volume_id': machine.deployment['data_volume_id'],
                               'filesystem_uuid': machine.deployment['data_filesystem_uuid']}
            info = (target / 'operation.json').lstat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
                    and isinstance(state, dict) and state.get('restore_receipt_id') == receipt.get('receipt_id')
                    and state.get('restore_target') == expected_target and state.get('restored_from') == recovery_point
                    and state.get('phase') == 'prepared'):
                if state.get('restore_certified') is not False:
                    state['restore_certified'] = False
                    write_json(target / 'operation.json', state)
        check_budget(store)
        with tempfile.TemporaryDirectory(prefix=".minecraft-restore-", dir=machine.root) as temporary:
            workspace = Path(temporary)
            if receipt is not None:
                if receipt.get('instance') != instance or receipt.get('recovery_point') != recovery_point or bootstrap_sha256 is None:
                    raise BackupError('restore requires the selected instance, point and matching bootstrap release')
                metadata, archive = load_receipt_recovery(store, receipt, workspace, bootstrap_sha256=bootstrap_sha256)
            else:
                if resume:
                    raise BackupError('restore resume requires an exact recovery receipt')
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
            if occupied:
                verify_restored_receipt(target, metadata, receipt, machine.deployment,
                                        metadata_reader=getattr(machine, 'restore_metadata', os.lstat), check_deadline=budget_checker(store))
                machine.verify_storage()
                if machine.inspect(instance)['exists']:
                    raise BackupError('restore target acquired a container; preserve it for repair')
                check_budget(store)
            else:
                if receipt is not None:
                    capacity = os.statvfs(machine.root)
                    remaining_bytes = receipt['required_capacity_bytes'] - receipt['archive']['size_bytes']
                    if capacity.f_frsize * capacity.f_bavail < remaining_bytes:
                        raise BackupError('restore target has insufficient available capacity for the selected receipt')
                staged = workspace / "instance"
                extract_recovery(metadata, archive, staged, check_deadline=budget_checker(store))
                (staged / 'runtime').mkdir(mode=0o700)
                write_json(staged / "runtime/manifest.json", metadata["manifest"])
                write_json(staged / "runtime/definition.json", metadata["definition"])
                state = {
                    "phase": "prepared", "backup": "not-required", "revision": metadata["manifest_sha256"],
                    "session": str(uuid.uuid4()), "restored_from": recovery_point,
                }
                if receipt is not None:
                    state.update(restore_receipt_id=receipt['receipt_id'], restore_certified=False, bootstrap_sha256=receipt['bootstrap_sha256'],
                                 restore_target={'volume_id': machine.deployment['data_volume_id'],
                                                 'filesystem_uuid': machine.deployment['data_filesystem_uuid']})
                write_json(staged / "operation.json", state)
                ownership = getattr(machine, "restore_ownership", None)
                if ownership is None:
                    apply_restore_ownership(staged / 'data', check_deadline=budget_checker(store))
                else:
                    ownership(staged / 'data')
                check_budget(store)
                for directory, _, _ in os.walk(staged, topdown=False):
                    check_budget(store)
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                machine.verify_storage()
                check_budget(store)
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
            check_budget(store)
        check_budget(store)
        result = {"schema_version": 1, "status": "restored", "instance": instance,
                  "recovery_point": recovery_point, "application": "stopped", "backup": "not-required"}
        if receipt is not None:
            certify_restore(target, private_json(target / 'operation.json'), store)
            return {**result, 'receipt_id': receipt['receipt_id'], 'resumed': occupied}
        return result


def certify_restore(target, state, store):
    """The caller holds the host lock until certification and outcome are decided."""
    check_budget(store)
    state = {**state, 'restore_certified': True}
    try:
        write_json(target / 'operation.json', state)
        check_budget(store)
    except (BackupError, OSError):
        state['restore_certified'] = False
        try:
            write_json(target / 'operation.json', state)
        except OSError:
            # Missing lifecycle state blocks ordinary start while preserving the world.
            (target / 'operation.json').unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private target operator configuration")
    parser.add_argument("--source-deployment", help="private original deployment configuration")
    parser.add_argument("--receipt", help="private exact-version S3-only recovery receipt")
    parser.add_argument("--bootstrap-sha256", help="installed reviewed runtime bundle checksum")
    parser.add_argument("--resume", action="store_true", help="verify an occupied target restored from the same receipt")
    parser.add_argument('--timeout', type=int, default=3000, help='bounded restore operation budget, 1 to 3000 seconds')
    parser.add_argument("instance")
    parser.add_argument("recovery_point")
    args = parser.parse_args(argv)
    try:
        from minecraft_backup_store import S3Store
        machine = LinuxMachine(private_json(args.config))
        receipt = private_json(args.receipt) if args.receipt else None
        if args.source_deployment:
            source = private_json(args.source_deployment)
        elif receipt is not None:
            source = {**receipt['source'], 'data_volume_id': receipt['source']['volume_id'],
                      'data_filesystem_uuid': receipt['source']['filesystem_uuid']}
        else:
            raise BackupError('the original source deployment or an exact recovery receipt is required')
        result = restore(machine, source, args.instance, args.recovery_point,
                         S3Store(source["backup_bucket_name"], region=source.get("region"), timeout=args.timeout), receipt=receipt,
                         bootstrap_sha256=args.bootstrap_sha256, resume=args.resume)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OperationError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, (BackupError, OperationError)) else "invalid restore input or filesystem failure"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
