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

from minecraft_backup_store import BackupError, S3Store, valid_version
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
        # A recovery point may record an earlier reviewed content release. Restore still
        # requires the configured manifest to match it exactly (minecraft_restore.restore).
        _validate_manifest(metadata['manifest'], recorded=True)
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


def _object_receipt(store, key, target, max_bytes, *, expected=None):
    if expected is not None:
        _validate_object_receipt(expected, key, max_bytes)
    revision = store.download_revision(key, target, max_bytes=max_bytes,
                                       version_id=expected['version_id'] if expected else None)
    if revision is None:
        raise BackupError('the selected recovery object version is unavailable')
    result = {**revision, 'sha256': digest_file(target)}
    if expected is not None and result != expected:
        raise BackupError('the selected recovery object version differs from its receipt')
    return result


def _validate_object_receipt(item, key, maximum):
    if (not isinstance(item, dict) or set(item) != {'key', 'version_id', 'etag', 'size_bytes', 'sha256'}
            or item['key'] != key or not isinstance(item['etag'], str)
            or not re.fullmatch(r'[\x21-\x7e]{1,1024}', item['etag'])
            or type(item['size_bytes']) is not int or not 0 < item['size_bytes'] <= maximum):
        raise BackupError('invalid receipt object identity')
    valid_version(item['version_id'])
    check_digest(item['sha256'])


def validate_receipt(receipt):
    """Private evidence required before the world may exist only in S3."""
    try:
        fields = {'schema_version', 'status', 'receipt_id', 'instance', 'session', 'recovery_point',
                  'created_at', 'manifest_sha256', 'definition_sha256', 'bootstrap_sha256', 'source',
                  'marker', 'archive', 'data_size_bytes', 'required_capacity_bytes', 'sequence', 'previous_receipt_id'}
        if (not isinstance(receipt, dict) or set(receipt) != fields
                or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
                or receipt['status'] != 'verified'):
            raise BackupError('malformed or incompatible recovery receipt')
        check_instance(receipt['instance'])
        for field in ('receipt_id', 'recovery_point', 'manifest_sha256', 'definition_sha256', 'bootstrap_sha256'):
            check_digest(receipt[field])
        if (not isinstance(receipt['session'], str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,128}', receipt['session'])
                or point_identity(receipt['instance'], receipt['session'], receipt['manifest_sha256']) != receipt['recovery_point']):
            raise BackupError('recovery receipt final session does not match its point')
        timestamp(receipt['created_at'])
        if type(receipt['sequence']) is not int or receipt['sequence'] < 1:
            raise BackupError('invalid recovery receipt sequence')
        previous = receipt['previous_receipt_id']
        if (receipt['sequence'] == 1) != (previous is None):
            raise BackupError('recovery receipt selection history is inconsistent')
        if previous is not None:
            check_digest(previous)
        source = receipt['source']
        if (set(source) != {'account_id', 'region', 'host_id', 'backup_bucket_name', 'volume_id',
                           'filesystem_uuid', 'deployment_sha256'}
                or not re.fullmatch(r'[0-9]{12}', source['account_id'])
                or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', source['region'])
                or not re.fullmatch(r'i-(?:[a-f0-9]{8}|[a-f0-9]{17})', source['host_id'])
                or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', source['backup_bucket_name'])
                or not re.fullmatch(r'vol-(?:[a-f0-9]{8}|[a-f0-9]{17})', source['volume_id'])
                or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', source['filesystem_uuid'])):
            raise BackupError('invalid receipt source deployment or storage')
        check_digest(source['deployment_sha256'])
        for field, key, maximum in (
                ('marker', marker_key(receipt['instance'], receipt['recovery_point']), MAX_METADATA_BYTES),
                ('archive', f"instances/{receipt['instance']}/objects/{receipt['recovery_point']}/data.tar.gz", MAX_ARCHIVE_BYTES)):
            _validate_object_receipt(receipt[field], key, maximum)
        if (type(receipt['data_size_bytes']) is not int or not 0 < receipt['data_size_bytes'] <= MAX_ARCHIVE_BYTES
                or type(receipt['required_capacity_bytes']) is not int
                or receipt['required_capacity_bytes'] != receipt['data_size_bytes'] + receipt['archive']['size_bytes'] + 1024 ** 3):
            raise BackupError('invalid recovery receipt capacity requirement')
        core = {key: value for key, value in receipt.items() if key != 'receipt_id'}
        if hashlib.sha256(encoded(core)).hexdigest() != receipt['receipt_id']:
            raise BackupError('recovery receipt checksum mismatch')
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise BackupError('malformed or incompatible recovery receipt') from error
    return receipt


def load_receipt_recovery(store, receipt, workspace, *, bootstrap_sha256=None):
    validate_receipt(receipt)
    if bootstrap_sha256 is not None and bootstrap_sha256 != receipt['bootstrap_sha256']:
        raise BackupError('bootstrap release differs from the recorded recovery receipt')
    if getattr(store, 'bucket', receipt['source']['backup_bucket_name']) != receipt['source']['backup_bucket_name']:
        raise BackupError('recovery receipt belongs to a different backup bucket')
    workspace = Path(workspace)
    metadata_path = workspace / 'recovery.json'
    _object_receipt(store, receipt['marker']['key'], metadata_path, MAX_METADATA_BYTES, expected=receipt['marker'])
    metadata = validate_metadata(read_json(metadata_path), receipt['instance'], receipt['recovery_point'])
    if (any(metadata[field] != receipt[field] for field in ('session', 'manifest_sha256', 'definition_sha256', 'created_at'))
            or metadata['source'] != {'volume_id': receipt['source']['volume_id'],
                                      'filesystem_uuid': receipt['source']['filesystem_uuid']}
            or any(metadata['archive'][field] != receipt['archive'][field] for field in ('key', 'sha256', 'size_bytes'))
            or sum(item.get('size_bytes', 0) for item in metadata['files']) != receipt['data_size_bytes']):
        raise BackupError('completed recovery point differs from the final-session receipt')
    archive = workspace / 'recovery.tar.gz'
    _object_receipt(store, receipt['archive']['key'], archive, receipt['archive']['size_bytes'], expected=receipt['archive'])
    return metadata, archive


def create_recovery_receipt(store, source_deployment, instance, evidence, bootstrap_sha256, workspace, *, previous=None):
    """Verify the drained final session independently, outside the disposable volume."""
    check_digest(bootstrap_sha256)
    if getattr(store, 'bucket', source_deployment.get('backup_bucket_name')) != source_deployment.get('backup_bucket_name'):
        raise BackupError('recovery receipt source uses a different backup bucket')
    if (not isinstance(evidence, dict) or evidence.get('application') != 'stopped'
            or evidence.get('backup') != 'complete'):
        raise BackupError('a completed final-session backup is required for S3-only storage')
    workspace = Path(workspace)
    point = evidence.get('recovery_point')
    marker_path = workspace / 'recovery.json'
    marker = _object_receipt(store, marker_key(instance, point), marker_path, MAX_METADATA_BYTES)
    metadata = validate_metadata(read_json(marker_path), instance, point)
    if (metadata['session'] != evidence.get('session') or metadata['manifest_sha256'] != evidence.get('manifest_sha256')
            or metadata['source'] != {'volume_id': source_deployment.get('data_volume_id'),
                                      'filesystem_uuid': source_deployment.get('data_filesystem_uuid')}):
        raise BackupError('final session, content or source storage differs from the completed recovery point')
    archive_path = workspace / 'recovery.tar.gz'
    archive = _object_receipt(store, metadata['archive']['key'], archive_path, metadata['archive']['size_bytes'])
    if any(archive[field] != metadata['archive'][field] for field in ('key', 'size_bytes', 'sha256')):
        raise BackupError('final-session recovery archive is corrupt')
    # Inspect every archived file before this receipt can authorize deletion of the only live copy.
    from minecraft_restore import extract_recovery
    with tempfile.TemporaryDirectory(prefix='.minecraft-receipt-check-', dir=workspace) as temporary:
        extract_recovery(metadata, archive_path, Path(temporary) / 'verified')
    if previous is not None:
        validate_receipt(previous)
        if (previous['instance'] != instance or previous['source']['account_id'] != source_deployment.get('account_id')
                or previous['source']['backup_bucket_name'] != source_deployment.get('backup_bucket_name')
                or timestamp(metadata['created_at']) < timestamp(previous['created_at'])):
            raise BackupError('recovery receipt selection cannot regress or change ownership')
    data_bytes = sum(item.get('size_bytes', 0) for item in metadata['files'])
    core = {'schema_version': 1, 'status': 'verified', 'instance': instance, 'session': metadata['session'],
            'recovery_point': point, 'created_at': metadata['created_at'], 'manifest_sha256': metadata['manifest_sha256'],
            'definition_sha256': metadata['definition_sha256'], 'bootstrap_sha256': bootstrap_sha256,
            'source': {'account_id': source_deployment.get('account_id'), 'region': source_deployment.get('region'),
                       'host_id': source_deployment.get('host_id'), 'backup_bucket_name': source_deployment.get('backup_bucket_name'),
                       **metadata['source'], 'deployment_sha256': hashlib.sha256(encoded(source_deployment)).hexdigest()},
            'marker': marker, 'archive': archive, 'data_size_bytes': data_bytes,
            'required_capacity_bytes': data_bytes + archive['size_bytes'] + 1024 ** 3,
            'sequence': previous['sequence'] + 1 if previous else 1,
            'previous_receipt_id': previous['receipt_id'] if previous else None}
    return validate_receipt({**core, 'receipt_id': hashlib.sha256(encoded(core)).hexdigest()})


def _selection_key(instance):
    check_instance(instance)
    return f'instances/{instance}/receipts/current.json'


def load_current_receipt(store, instance, workspace, *, expected_receipt_id=None, allow_absent=False):
    """The authoritative current selection is mandatory; never choose catalog latest."""
    workspace = Path(workspace)
    path = workspace / 'selection.json'
    revision = store.download_revision(_selection_key(instance), path, max_bytes=MAX_METADATA_BYTES)
    if revision is None:
        if allow_absent and expected_receipt_id is None:
            return None, None
        raise BackupError('authoritative recovery selection is unavailable')
    try:
        selection = read_json(path)
        if (set(selection) != {'schema_version', 'instance', 'receipt_id', 'sequence', 'previous_receipt_id', 'receipt'}
                or type(selection['schema_version']) is not int or selection['schema_version'] != 1
                or type(selection['sequence']) is not int or selection['sequence'] < 1
                or selection['instance'] != instance or (expected_receipt_id is not None and selection['receipt_id'] != expected_receipt_id)):
            raise BackupError('authoritative recovery selection differs from the expected receipt')
        check_digest(selection['receipt_id'])
        reference = selection['receipt']
        if reference['key'] != f"instances/{instance}/receipts/{selection['receipt_id']}.json":
            raise BackupError('authoritative recovery selection has an invalid receipt key')
        receipt_path = workspace / 'selected-receipt.json'
        _object_receipt(store, reference['key'], receipt_path, MAX_METADATA_BYTES, expected=reference)
        receipt = validate_receipt(read_json(receipt_path))
        if any(receipt[field] != selection[field] for field in ('instance', 'receipt_id', 'sequence', 'previous_receipt_id')):
            raise BackupError('authoritative recovery selection and its receipt disagree')
        return receipt, {**selection, 'revision': revision}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BackupError('malformed authoritative recovery selection') from error


def publish_recovery_receipt(store, receipt, workspace, *, previous_selection=None):
    validate_receipt(receipt)
    workspace = Path(workspace)
    current, observed = load_current_receipt(store, receipt['instance'], workspace, allow_absent=True)
    if current and current['receipt_id'] == receipt['receipt_id']:
        return observed
    if observed != previous_selection:
        raise BackupError('authoritative recovery selection changed during publication')
    if (receipt['previous_receipt_id'] != (current['receipt_id'] if current else None)
            or receipt['sequence'] != (current['sequence'] + 1 if current else 1)):
        raise BackupError('recovery receipt cannot replace an unrelated or newer selection')
    if current and (timestamp(receipt['created_at']) < timestamp(current['created_at'])
            or any(receipt['source'][field] != current['source'][field]
                   for field in ('account_id', 'region', 'backup_bucket_name'))):
        raise BackupError('recovery receipt selection cannot regress or change ownership')
    load_receipt_recovery(store, receipt, workspace, bootstrap_sha256=receipt['bootstrap_sha256'])
    path = workspace / 'receipt-publication.json'
    path.write_bytes(encoded(receipt))
    key = f"instances/{receipt['instance']}/receipts/{receipt['receipt_id']}.json"
    store.upload(key, path)
    reference = _object_receipt(store, key, workspace / 'published-receipt.json', MAX_METADATA_BYTES)
    if read_json(workspace / 'published-receipt.json') != receipt:
        raise BackupError('published receipt contains different evidence')
    selection = {field: receipt[field] for field in ('schema_version', 'instance', 'receipt_id', 'sequence', 'previous_receipt_id')}
    selection['receipt'] = reference
    path = workspace / 'selection-publication.json'
    path.write_bytes(encoded(selection))
    store.conditional_write(_selection_key(receipt['instance']), path,
                            expected_etag=previous_selection['revision']['etag'] if previous_selection else None)
    _, verified = load_current_receipt(store, receipt['instance'], workspace, expected_receipt_id=receipt['receipt_id'])
    return verified


def verify_stop_receipt(deployment, drain, journal_dir, bootstrap_sha256, *, store=None):
    """Publish exact final-session receipts and durable private selection before disk deletion."""
    from minecraft_instance import private_json, write_json
    journal_dir = Path(journal_dir)
    if (not journal_dir.is_dir() or journal_dir.is_symlink() or journal_dir.stat().st_uid != os.geteuid()
            or stat.S_IMODE(journal_dir.stat().st_mode) != 0o700):
        raise BackupError('recovery receipt journal must be operator-owned mode 0700')
    instances = deployment.get('instances')
    if (not isinstance(instances, dict) or not instances or not isinstance(drain, dict)
            or drain.get('draining') is not True or set(drain.get('instances', {})) != set(instances)
            or set(drain.get('recovery', {})) != set(instances)):
        raise BackupError('complete private drain evidence is required for every configured instance')
    store = store or S3Store(deployment['backup_bucket_name'], region=deployment.get('region'))
    result = {}
    with tempfile.TemporaryDirectory(prefix='.minecraft-receipts-', dir=journal_dir) as temporary:
        workspace = Path(temporary)
        for instance in sorted(instances):
            check_instance(instance)
            if (set(drain['recovery'][instance]) != {'session', 'manifest_sha256', 'recovery_point'}
                    or drain['instances'][instance].get('instance') != instance
                    or drain['instances'][instance].get('application') != 'stopped'
                    or drain['instances'][instance].get('backup') != 'complete'):
                raise BackupError('a completed final-session backup is required for every configured instance')
            evidence = {**drain['instances'][instance], **drain['recovery'][instance]}
            local_path = journal_dir / f'current-{instance}.json'
            local = private_json(local_path) if local_path.exists() or local_path.is_symlink() else None
            prior, selection = load_current_receipt(store, instance, workspace, allow_absent=local is None)
            if local is not None and (selection is None or local != selection):
                candidate_path = journal_dir / f"receipt-{prior['receipt_id']}.json" if prior else None
                if (prior is None or prior['previous_receipt_id'] != local.get('receipt_id')
                        or candidate_path is None or not candidate_path.exists() or private_json(candidate_path) != prior
                        or prior['session'] != evidence.get('session') or prior['recovery_point'] != evidence.get('recovery_point')
                        or prior['manifest_sha256'] != evidence.get('manifest_sha256')
                        or prior['source']['deployment_sha256'] != hashlib.sha256(encoded(deployment)).hexdigest()
                        or prior['bootstrap_sha256'] != bootstrap_sha256):
                    raise BackupError('durable local recovery selection differs from authoritative S3 selection')
                # The same operation published its recorded candidate before interruption.
                load_receipt_recovery(store, prior, workspace, bootstrap_sha256=bootstrap_sha256)
                write_json(local_path, selection)
            if (prior and prior['session'] == evidence.get('session') and prior['recovery_point'] == evidence.get('recovery_point')
                    and prior['manifest_sha256'] == evidence.get('manifest_sha256')
                    and prior['source']['deployment_sha256'] == hashlib.sha256(encoded(deployment)).hexdigest()
                    and prior['bootstrap_sha256'] == bootstrap_sha256):
                load_receipt_recovery(store, prior, workspace, bootstrap_sha256=bootstrap_sha256)
                receipt = prior
            else:
                receipt = create_recovery_receipt(store, deployment, instance, evidence, bootstrap_sha256, workspace, previous=prior)
                write_json(journal_dir / f"receipt-{receipt['receipt_id']}.json", receipt)
                selection = publish_recovery_receipt(store, receipt, workspace, previous_selection=selection)
            write_json(journal_dir / f"receipt-{receipt['receipt_id']}.json", receipt)
            write_json(local_path, selection)
            result[instance] = {'receipt_id': receipt['receipt_id'], 'selection': selection, 'receipt': receipt}
    return result


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
    return {'schema_version': 1, 'status': 'complete', 'instance': instance,
            'session': request['session'], 'manifest_sha256': request['manifest_sha256'],
            'recovery_point': point,
            'retention': {'policy': 'latest-14-completed', 'outcome': 'pending', 'failed': 0}}


def catalog(store, instance, workspace):
    check_instance(instance)
    points, invalid = [], 0
    published = {}
    prefix = f'instances/{instance}/completed/'
    for item in store.list_objects(prefix):
        key = item['key']
        point = key.removeprefix(prefix).removesuffix('.json')
        try:
            if key != marker_key(instance, point):
                raise BackupError('invalid completion key')
            metadata, _ = load_recovery(store, instance, point, workspace)
            points.append(metadata)
            published[point] = timestamp(item['last_modified'])
        except BackupError:
            invalid += 1
    points.sort(key=lambda item: (published[item['recovery_point']], item['recovery_point']), reverse=True)
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
