#!/usr/bin/env python3
"""Cold recovery points: verified immutable publication, catalog and retention."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tempfile

from minecraft_backup_store import BackupError, S3Store
from minecraft_content import _validate_definition, _validate_manifest, manifest_digest

MAX_ARCHIVE_BYTES = 100 * 1024 ** 3
MAX_METADATA_BYTES = 16 * 1024 ** 2
RETAIN = 14
REQUIRED_FILES = {'data/world/level.dat', 'data/whitelist.json', 'data/ops.json',
                  'data/server.properties', 'data/plugins/floodgate/key.pem'}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError('duplicate metadata key')
            result[key] = value
        return result
    try:
        if Path(path).stat().st_size > MAX_METADATA_BYTES:
            raise BackupError('metadata exceeds size limit')
        return json.loads(Path(path).read_bytes(), object_pairs_hook=unique)
    except (ValueError, OSError) as error:
        raise BackupError('metadata is missing or malformed') from error


def timestamp(value=None):
    if value is None:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise BackupError('invalid recovery timestamp')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise BackupError('invalid recovery timestamp') from error


def check_instance(instance):
    if not isinstance(instance, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', instance):
        raise BackupError('invalid instance identity')


def check_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise BackupError('invalid recovery checksum or identity')


def point_identity(instance, session, revision):
    return hashlib.sha256(encoded({'instance': instance, 'session': session, 'manifest_sha256': revision})).hexdigest()


def marker_key(instance, recovery_point):
    check_instance(instance)
    check_digest(recovery_point)
    return f'instances/{instance}/completed/{recovery_point}.json'


def safe_path(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise BackupError('unsafe archive path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')) or path.parts[0] != 'data':
        raise BackupError('unsafe archive path')
    return path


def validate_metadata(metadata, instance=None, recovery_point=None):
    try:
        if not isinstance(metadata, dict) or type(metadata.get('schema_version')) is not int or metadata['schema_version'] != 1 or metadata.get('status') != 'complete':
            raise BackupError('incompatible recovery metadata')
        check_instance(metadata['instance'])
        check_digest(metadata['manifest_sha256'])
        check_digest(metadata['definition_sha256'])
        check_digest(metadata['recovery_point'])
        if not isinstance(metadata['session'], str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,128}', metadata['session']):
            raise BackupError('invalid session identity')
        if ((instance is not None and metadata['instance'] != instance)
                or (recovery_point is not None and metadata['recovery_point'] != recovery_point)
                or point_identity(metadata['instance'], metadata['session'], metadata['manifest_sha256']) != metadata['recovery_point']):
            raise BackupError('recovery identity mismatch')
        timestamp(metadata['created_at'])
        _validate_manifest(metadata['manifest'])
        _validate_definition(metadata['definition'])
        if (manifest_digest(metadata['manifest']) != metadata['manifest_sha256']
                or manifest_digest(metadata['definition']) != metadata['definition_sha256']
                or metadata['definition']['id'] != metadata['instance']):
            raise BackupError('recorded content or definition checksum mismatch')
        source = metadata['source']
        if (set(source) != {'volume_id', 'filesystem_uuid'}
                or not re.fullmatch(r'vol-[a-f0-9]{8,17}', source['volume_id'])
                or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', source['filesystem_uuid'])):
            raise BackupError('invalid source storage identity')
        archive = metadata['archive']
        expected = f"instances/{metadata['instance']}/objects/{metadata['recovery_point']}/data.tar.gz"
        if archive['key'] != expected or type(archive['size_bytes']) is not int or not 0 < archive['size_bytes'] <= MAX_ARCHIVE_BYTES:
            raise BackupError('invalid archive identity or size')
        check_digest(archive['sha256'])
        files = metadata['files']
        if not isinstance(files, list) or not files or len(files) > 200000:
            raise BackupError('invalid recovery file inventory')
        entries = {}
        total = 0
        for entry in files:
            name = str(safe_path(entry['path']))
            if name in entries:
                raise BackupError('duplicate archive inventory path')
            if entry['type'] == 'file':
                if set(entry) != {'path', 'type', 'size_bytes', 'sha256'} or type(entry['size_bytes']) is not int or not 0 <= entry['size_bytes'] <= MAX_ARCHIVE_BYTES:
                    raise BackupError('invalid recovery file size')
                total += entry['size_bytes']
                check_digest(entry['sha256'])
            elif entry['type'] != 'directory' or set(entry) != {'path', 'type'}:
                raise BackupError('unsupported archive inventory type')
            entries[name] = entry['type']
        if total > MAX_ARCHIVE_BYTES or entries.get('data') != 'directory' or any(entries.get(name) != 'file' for name in REQUIRED_FILES):
            raise BackupError('required recovery files missing or recovery set too large')
        for name in entries:
            if name != 'data' and entries.get(str(PurePosixPath(name).parent)) != 'directory':
                raise BackupError('archive inventory has missing or conflicting parent')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BackupError('malformed or incompatible recovery metadata') from error
    return metadata


def load_recovery(store, instance, recovery_point, workspace):
    """Read and verify a completed point without exposing its private metadata."""
    key = marker_key(instance, recovery_point)
    workspace = Path(workspace)
    metadata_path = workspace / 'recovery.json'
    if not store.download(key, metadata_path, max_bytes=MAX_METADATA_BYTES):
        raise BackupError('completed recovery point is unavailable')
    metadata = validate_metadata(read_json(metadata_path), instance, recovery_point)
    archive_path = workspace / 'recovery.tar.gz'
    archive = metadata['archive']
    if (not store.download(archive['key'], archive_path, max_bytes=archive['size_bytes'])
            or archive_path.stat().st_size != archive['size_bytes']
            or digest_file(archive_path) != archive['sha256']):
        raise BackupError('recovery archive is missing or corrupt')
    return metadata, archive_path


def tree_snapshot(data):
    entries = {}
    def visit(path):
        info = path.lstat()
        if info.st_dev != data.stat().st_dev or (not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode)):
            raise BackupError('recovery data contains a link, mount or unsupported file')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise BackupError('recovery data contains a hard link')
        name = 'data' if path == data else 'data/' + path.relative_to(data).as_posix()
        safe_path(name)
        entries[name] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(entries) > 200000:
            raise BackupError('recovery file inventory exceeds limit')
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)
    visit(data)
    return entries


def capture(data, archive_path):
    before = tree_snapshot(data)
    files = []
    total = 0
    with archive_path.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
        for name, info in before.items():
            entry = {'path': name, 'type': 'directory' if stat.S_ISDIR(info[2]) else 'file'}
            member = tarfile.TarInfo(name)
            member.uid = member.gid = 1000
            member.mode = 0o750 if entry['type'] == 'directory' else 0o640
            if entry['type'] == 'directory':
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                path = data.parent / name
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != info:
                        raise BackupError('recovery data changed during capture')
                    member.size = opened.st_size
                    total += member.size
                    if total > MAX_ARCHIVE_BYTES:
                        raise BackupError('recovery data exceeds size limit')
                    entry.update(size_bytes=member.size, sha256=hashlib.file_digest(source, 'sha256').hexdigest())
                    source.seek(0)
                    archive.addfile(member, source)
            files.append(entry)
    if tree_snapshot(data) != before:
        raise BackupError('recovery data changed during capture')
    if not REQUIRED_FILES.issubset({entry['path'] for entry in files if entry['type'] == 'file'}):
        raise BackupError('required recovery files are missing')
    return files


def validate_request(request):
    try:
        check_instance(request['instance'])
        check_digest(request['manifest_sha256'])
        if request['schema_version'] != 1 or not re.fullmatch(r'[a-zA-Z0-9._-]{1,128}', request['session']):
            raise BackupError('invalid backup request')
        instance = request['instance']
        data = Path(request['data_path'])
        if (not data.is_absolute() or data != data.resolve() or data.name != 'data'
                or data.parent.name != instance or data.parent.parent.name != 'instances' or not data.is_dir()
                or request['destination']['prefix'] != f'instances/{instance}/'):
            raise BackupError('invalid instance backup path or namespace')
        for key, name in [('manifest_path', 'manifest.json'), ('definition_path', 'definition.json')]:
            path = Path(request[key])
            if path != data.parent / 'runtime' / name or path != path.resolve() or not path.is_file():
                raise BackupError('backup requires the active runtime metadata')
        state = read_json(data.parent / 'operation.json')
        if (state.get('phase') != 'stopped' or state.get('backup') not in ('pending', 'failed', 'complete')
                or state.get('session') != request['session'] or state.get('revision') != request['manifest_sha256']):
            raise BackupError('matching cleanly stopped session is required')
        manifest = read_json(request['manifest_path'])
        definition = read_json(request['definition_path'])
        _validate_manifest(manifest)
        _validate_definition(definition)
        if manifest_digest(manifest) != request['manifest_sha256'] or definition['id'] != instance:
            raise BackupError('active content differs from the stopped session')
        return data, manifest, definition
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as error:
        raise BackupError('invalid or unavailable backup request input') from error


def publish(request, store, workspace, *, now=None):
    """Called only by the controller while it holds lifecycle exclusion."""
    data, manifest, definition = validate_request(request)
    workspace = Path(workspace)
    if workspace.resolve().is_relative_to(data.parent.resolve()):
        raise BackupError('backup workspace must be outside the source instance')
    instance = request['instance']
    point = point_identity(instance, request['session'], request['manifest_sha256'])
    marker = marker_key(instance, point)
    existing = workspace / 'existing.json'
    if store.download(marker, existing, max_bytes=MAX_METADATA_BYTES):
        metadata, _ = load_recovery(store, instance, point, workspace)
        if metadata['definition'] != definition or metadata['source'] != request['source']:
            raise BackupError('logical backup already exists with different configuration')
    else:
        archive_path = workspace / 'capture.tar.gz'
        files = capture(data, archive_path)
        metadata = {'schema_version': 1, 'status': 'complete', 'instance': instance,
                    'session': request['session'], 'manifest_sha256': request['manifest_sha256'],
                    'definition_sha256': manifest_digest(definition), 'manifest': manifest, 'definition': definition,
                    'source': request['source'], 'created_at': now or timestamp(), 'recovery_point': point,
                    'archive': {'key': f'instances/{instance}/objects/{point}/data.tar.gz',
                                'size_bytes': archive_path.stat().st_size, 'sha256': digest_file(archive_path)},
                    'files': files}
        validate_metadata(metadata, instance, point)
        metadata_path = workspace / 'publish.json'
        metadata_path.write_bytes(encoded(metadata))
        if metadata_path.stat().st_size > MAX_METADATA_BYTES:
            raise BackupError('recovery metadata exceeds size limit')
        store.upload(metadata['archive']['key'], archive_path)
        verified = workspace / 'verify.tar.gz'
        if (not store.download(metadata['archive']['key'], verified, max_bytes=metadata['archive']['size_bytes'])
                or verified.stat().st_size != metadata['archive']['size_bytes']
                or digest_file(verified) != metadata['archive']['sha256']):
            raise BackupError('published archive verification failed')
        verified.unlink()
        archive_path.unlink()
        store.upload(marker, metadata_path)
        verified_metadata, _ = load_recovery(store, instance, point, workspace)
        if verified_metadata != metadata:
            raise BackupError('logical recovery point already contains different metadata')
    try:
        retention = prune(store, instance, workspace)
    except (BackupError, OSError):
        retention = {'policy': 'latest-14-completed', 'outcome': 'pending', 'failed': 1}
    return {'schema_version': 1, 'status': 'complete', 'instance': instance,
            'session': request['session'], 'manifest_sha256': request['manifest_sha256'],
            'recovery_point': point, 'retention': retention}


def catalog(store, instance, workspace):
    check_instance(instance)
    points, invalid = [], 0
    prefix = f'instances/{instance}/completed/'
    for key in store.keys(prefix):
        point = key.removeprefix(prefix).removesuffix('.json')
        try:
            if key != marker_key(instance, point):
                raise BackupError('invalid completion key')
            metadata, _ = load_recovery(store, instance, point, workspace)
            points.append(metadata)
        except BackupError:
            invalid += 1
    points.sort(key=lambda item: (item['created_at'], item['recovery_point']), reverse=True)
    return points, invalid


def prune(store, instance, workspace):
    points, invalid = catalog(store, instance, workspace)
    failed = 0
    removed = 0
    prefix = f'instances/{instance}/retiring/'
    # A durable deletion intent allows an interrupted two-object prune to resume.
    # Verify all surviving recovery points before any destructive work.
    if not points or invalid:
        return {'policy': 'latest-14-completed', 'removed': 0, 'failed': 0,
                'invalid_points': invalid, 'outcome': 'pending' if invalid else 'unavailable',
                'noncurrent_expiry_days': 14, 'incomplete_multipart_expiry_days': 1}
    protected = {item['recovery_point'] for item in points[:RETAIN]}
    for metadata in points[RETAIN:]:
        try:
            intent = Path(workspace) / 'retiring.json'
            intent.write_bytes(encoded(metadata))
            store.upload(prefix + metadata['recovery_point'] + '.json', intent)
        except BackupError:
            failed += 1
    for key in store.keys(prefix):
        try:
            point = key.removeprefix(prefix).removesuffix('.json')
            check_digest(point)
            if key != prefix + point + '.json' or point in protected:
                raise BackupError('retention refuses to delete a protected recovery point')
            intent = Path(workspace) / 'retiring.json'
            if not store.download(key, intent, max_bytes=MAX_METADATA_BYTES):
                raise BackupError('retention intent disappeared')
            metadata = validate_metadata(read_json(intent), instance, point)
            # Remove discovery first, retaining the intent until both objects are deleted.
            store.delete(marker_key(instance, point))
            store.delete(metadata['archive']['key'])
            store.delete(key)
            removed += 1
        except BackupError:
            failed += 1
    return {'policy': 'latest-14-completed', 'removed': removed, 'failed': failed,
            'invalid_points': invalid, 'outcome': 'pending' if failed else 'complete',
            'noncurrent_expiry_days': 14, 'incomplete_multipart_expiry_days': 1}


def status(store, instance, workspace, *, now=None):
    current = timestamp(now or timestamp())
    points, invalid = catalog(store, instance, workspace)
    return {'instance': instance, 'completion': 'available' if points else 'unavailable',
            'recovery_points': [{'recovery_point': item['recovery_point'], 'manifest_sha256': item['manifest_sha256'],
                                 'created_at': item['created_at'], 'age_seconds': max(0, int((current - timestamp(item['created_at'])).total_seconds())),
                                 'archive_size_bytes': item['archive']['size_bytes']} for item in points],
            'invalid_points': invalid,
            'retention': {'keep_completed': RETAIN, 'excess_completed': max(0, len(points) - RETAIN),
                          'pending_deletions': len(store.keys(f'instances/{instance}/retiring/')),
                          'latest_expires': False, 'noncurrent_expiry_days': 14,
                          'incomplete_multipart_expiry_days': 1}}


def require_controller_lock(path):
    """The internal hook must run with lifecycle exclusion already held by its caller."""
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise BackupError('backup hook requires the lifecycle controller lock') from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise BackupError('backup hook lock is untrusted')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        raise BackupError('invoke backup through minecraft-instance; lifecycle exclusion is absent')
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('hook', 'status', 'prune'))
    parser.add_argument('instance', nargs='?')
    parser.add_argument('--config', type=Path, default=Path('/etc/minecraft/operator.json'))
    args = parser.parse_args(argv)
    try:
        from minecraft_instance import LinuxMachine, OperationError, private_json, ensure_runtime_lock_directory
        if args.action == 'hook':
            if args.instance is not None:
                raise BackupError('hook accepts only the controller request')
            raw = sys.stdin.buffer.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise BackupError('backup request exceeds size limit')
            request = json.loads(raw)
            machine = LinuxMachine(private_json(args.config))
            require_controller_lock(ensure_runtime_lock_directory() / 'runtime.lock')
            instance = request['instance']
            if (instance not in machine.definitions or request['data_path'] != str(machine.root / 'instances' / instance / 'data')
                    or request['destination'] != {'bucket': machine.deployment['backup_bucket_name'], 'prefix': f'instances/{instance}/'}
                    or request['source'] != {'volume_id': machine.deployment['data_volume_id'], 'filesystem_uuid': machine.deployment['data_filesystem_uuid']}):
                raise BackupError('hook request differs from the private deployment')
            machine.verify_storage()
            container = machine.inspect(instance)
            if container['running'] or (container['exists'] and container['exit_code'] != 0):
                raise BackupError('backup requires a cleanly stopped runtime')
            # The parent controller owns the host lock. Never recursively acquire it here.
            with tempfile.TemporaryDirectory(prefix='minecraft-backup-') as directory:
                result = publish(request, S3Store(request['destination']['bucket'], timeout=machine.backup_timeout), Path(directory))
        else:
            config = private_json(args.config)
            machine = LinuxMachine(config)
            if args.instance not in machine.definitions:
                raise BackupError('an explicit configured instance ID is required')
            with machine.lock(), tempfile.TemporaryDirectory(prefix='minecraft-backup-') as directory:
                machine.verify_storage()
                store = S3Store(machine.deployment['backup_bucket_name'], timeout=machine.backup_timeout)
                function = status if args.action == 'status' else prune
                result = function(store, args.instance, Path(directory))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OperationError, ValueError, OSError, KeyError, TypeError):
        print(json.dumps({'status': 'failed', 'error': 'backup operation failed; no completion is certified'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
