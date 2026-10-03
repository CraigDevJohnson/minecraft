"""Operator-owned protection and bounded retirement of exact recovery versions."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import stat
import tempfile

from minecraft_backup import (BackupError, MAX_METADATA_BYTES, RETAIN, _validate_object_receipt,
                              MAX_ARCHIVE_BYTES, check_digest, digest_file, encoded,
                              budget_checker, check_budget,
                              load_current_receipt, load_receipt_recovery, marker_key,
                              read_json, validate_metadata, validate_receipt)
from minecraft_backup_store import valid_version
from minecraft_instance import private_json, write_json
from minecraft_restore import extract_recovery


def identity(reference):
    return reference['key'], reference['version_id']


def _journal_directory(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise BackupError('retention journal must be operator-owned mode 0700')
    return path


def _read_version(store, key, version, path, limit):
    result = store.download_revision(key, path, max_bytes=limit, version_id=version)
    if result is None or result['key'] != key or result['version_id'] != version:
        raise BackupError('reviewed legacy version is unavailable')
    reference = {**result, 'sha256': digest_file(path, check_deadline=budget_checker(store))}
    _validate_object_receipt(reference, key, limit)
    check_budget(store)
    return reference


def _validate_legacy(record, instance):
    fields = {'schema_version', 'status', 'instance', 'recovery_point', 'created_at', 'published_at',
              'manifest_sha256', 'definition_sha256', 'source', 'marker', 'archive', 'inventory_sha256'}
    if (not isinstance(record, dict) or set(record) != fields or type(record['schema_version']) is not int
            or record['schema_version'] != 1 or record['status'] != 'verified-legacy' or record['instance'] != instance):
        raise BackupError('legacy inventory record is malformed')
    for field in ('recovery_point', 'manifest_sha256', 'definition_sha256', 'inventory_sha256'):
        check_digest(record[field])
    _validate_object_receipt(record['marker'], marker_key(instance, record['recovery_point']), MAX_METADATA_BYTES)
    _validate_object_receipt(record['archive'], f"instances/{instance}/objects/{record['recovery_point']}/data.tar.gz", MAX_ARCHIVE_BYTES)
    from minecraft_backup import timestamp
    timestamp(record['created_at'])
    timestamp(record['published_at'])
    core = {key: value for key, value in record.items() if key != 'inventory_sha256'}
    if hashlib.sha256(encoded(core)).hexdigest() != record['inventory_sha256']:
        raise BackupError('legacy inventory checksum differs from its recorded versions')
    return record


def _load_legacy(store, record, workspace):
    marker = record['marker']
    actual = _read_version(store, marker['key'], marker['version_id'], workspace / 'legacy-marker.json', MAX_METADATA_BYTES)
    if actual != marker:
        raise BackupError('legacy marker differs from the reviewed inventory')
    metadata = validate_metadata(read_json(workspace / 'legacy-marker.json'), record['instance'], record['recovery_point'])
    if (any(metadata[field] != record[field] for field in ('created_at', 'manifest_sha256', 'definition_sha256', 'source'))
            or any(metadata['archive'][field] != record['archive'][field] for field in ('key', 'sha256', 'size_bytes'))):
        raise BackupError('legacy metadata differs from its reviewed source/content inventory')
    archive = workspace / 'legacy-archive.tar.gz'
    actual = _read_version(store, record['archive']['key'], record['archive']['version_id'], archive, record['archive']['size_bytes'])
    if actual != record['archive']:
        raise BackupError('legacy archive differs from the reviewed inventory')
    return metadata, archive


def import_legacy_points(store, instance, journal_dir, *, reviewed_versions):
    """Owner-invoked local import of an explicitly reviewed version list, without invented receipts."""
    journal_dir = _journal_directory(journal_dir)
    inventory = store.list_versions(f'instances/{instance}/')
    available = {identity(item): item for item in inventory if not item['delete_marker']}
    if not isinstance(reviewed_versions, list) or not reviewed_versions or len(reviewed_versions) > 10000:
        raise BackupError('a bounded reviewed legacy version inventory is required')
    reviewed = set()
    for item in reviewed_versions:
        if not isinstance(item, dict) or set(item) != {'key', 'version_id'}:
            raise BackupError('reviewed legacy version inventory is malformed')
        valid_version(item['version_id'])
        pair = identity(item)
        if pair not in available or pair in reviewed or not re.fullmatch(f'instances/{instance}/(?:completed/[a-f0-9]{{64}}.json|objects/[a-f0-9]{{64}}/data.tar.gz)', pair[0]):
            raise BackupError('reviewed legacy version is missing, duplicated or outside the supported point paths')
        reviewed.add(pair)
    consumed, records = set(), []
    with tempfile.TemporaryDirectory(prefix='.minecraft-legacy-import-', dir=journal_dir) as directory:
        workspace = Path(directory)
        for key, version in sorted(reviewed):
            if '/completed/' not in key:
                continue
            point = key.rsplit('/', 1)[1][:-5]
            marker = _read_version(store, key, version, workspace / 'legacy-marker.json', MAX_METADATA_BYTES)
            metadata = validate_metadata(read_json(workspace / 'legacy-marker.json'), instance, point)
            candidates = [pair for pair in reviewed if pair[0] == metadata['archive']['key']]
            if len(candidates) != 1:
                raise BackupError('legacy import requires one reviewed archive version for each point')
            archive_key, archive_version = candidates[0]
            archive = _read_version(store, archive_key, archive_version, workspace / 'legacy-archive.tar.gz', metadata['archive']['size_bytes'])
            if any(archive[field] != metadata['archive'][field] for field in ('key', 'sha256', 'size_bytes')):
                raise BackupError('reviewed legacy archive is corrupt')
            with tempfile.TemporaryDirectory(prefix='.minecraft-legacy-check-', dir=workspace) as verify:
                extract_recovery(metadata, workspace / 'legacy-archive.tar.gz', Path(verify) / 'verified', check_deadline=budget_checker(store))
            core = {'schema_version': 1, 'status': 'verified-legacy', 'instance': instance,
                    'recovery_point': point, 'created_at': metadata['created_at'], 'published_at': available[(key, version)]['last_modified'],
                    'manifest_sha256': metadata['manifest_sha256'], 'definition_sha256': metadata['definition_sha256'],
                    'source': metadata['source'], 'marker': marker, 'archive': archive}
            record = _validate_legacy({**core, 'inventory_sha256': hashlib.sha256(encoded(core)).hexdigest()}, instance)
            if any(item['recovery_point'] == point for item in records):
                raise BackupError('conflicting legacy point versions require explicit investigation')
            records.append(record)
            consumed.update(((key, version), (archive_key, archive_version)))
        if consumed != reviewed:
            raise BackupError('reviewed legacy versions contain an orphan or unclassified object')
    for record in records:
        path = journal_dir / f"legacy-{record['recovery_point']}.json"
        if path.exists() and private_json(path) != record:
            raise BackupError('legacy point already has a different reviewed inventory')
    for record in records:
        check_budget(store)
        write_json(journal_dir / f"legacy-{record['recovery_point']}.json", record)
    check_budget(store)
    return {'instance': instance, 'imported_points': len(records), 'status': 'verified'}


def _assert_current(store, instance, workspace, expected):
    receipt, selection = load_current_receipt(store, instance, workspace,
                                             expected_receipt_id=expected['receipt_id'])
    if selection != expected:
        raise BackupError('authoritative recovery selection changed during retention')
    return receipt


def _protect(store, references):
    for key, version in sorted(set(references)):
        tags = store.version_tags(key, version)
        if tags.get('minecraft-retention') == 'retired':
            raise BackupError('required recovery version is already retired; preserve resources for repair')
        store.set_retention(key, version, 'keep')


def verify_protection_lifecycle(store, instance):
    """Reject live expiry drift before authorizing removal of the only local copy."""
    from minecraft_backup import check_instance
    check_instance(instance)
    configuration = store.lifecycle_configuration()
    rules = configuration.get('Rules') if isinstance(configuration, dict) else None
    if not isinstance(rules, list) or not 0 < len(rules) <= 1000:
        raise BackupError('live recovery lifecycle is unavailable or malformed')
    prefix = f'instances/{instance}/'
    seen, applicable = set(), []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get('Status') not in ('Enabled', 'Disabled'):
            raise BackupError('live recovery lifecycle has an unknown rule status')
        if 'ID' in rule and not isinstance(rule['ID'], str):
            raise BackupError('live recovery lifecycle has an invalid rule identity')
        if rule['Status'] == 'Disabled':
            continue
        filter_value = rule.get('Filter', {'Prefix': rule.get('Prefix', '')})
        if not isinstance(filter_value, dict):
            raise BackupError('live recovery lifecycle has an unsupported filter')
        tags = []
        if 'And' in filter_value:
            conjunction = filter_value['And']
            if (set(filter_value) != {'And'} or not isinstance(conjunction, dict)
                    or set(conjunction) - {'Prefix', 'Tags'}):
                raise BackupError('live recovery lifecycle has an unsupported filter')
            selected_prefix = conjunction.get('Prefix', '')
            tags = conjunction.get('Tags', [])
        elif set(filter_value).issubset({'Prefix', 'Tag'}):
            selected_prefix = filter_value.get('Prefix', '')
            tags = [filter_value['Tag']] if 'Tag' in filter_value else []
        else:
            raise BackupError('live recovery lifecycle has an unsupported filter')
        if not isinstance(selected_prefix, str) or not isinstance(tags, list):
            raise BackupError('live recovery lifecycle filter is malformed')
        if not prefix.startswith(selected_prefix) and not selected_prefix.startswith(prefix):
            continue
        if selected_prefix != prefix:
            raise BackupError('live recovery lifecycle has a conflicting blanket or partial-prefix rule')
        actions = set(rule) - {'ID', 'Status', 'Filter', 'Prefix'}
        if (actions == {'Expiration', 'NoncurrentVersionExpiration'}
                and tags == [{'Key': 'minecraft-retention', 'Value': 'retired'}]
                and rule['Expiration'] == {'Days': 14} and rule['NoncurrentVersionExpiration'] == {'NoncurrentDays': 14}
                and type(rule['Expiration']['Days']) is int and type(rule['NoncurrentVersionExpiration']['NoncurrentDays']) is int):
            kind = 'retired'
        elif (actions == {'AbortIncompleteMultipartUpload'} and not tags
                and rule['AbortIncompleteMultipartUpload'] == {'DaysAfterInitiation': 1}
                and type(rule['AbortIncompleteMultipartUpload']['DaysAfterInitiation']) is int):
            kind = 'multipart'
        elif (actions == {'Expiration'} and not tags
                and rule['Expiration'] == {'ExpiredObjectDeleteMarker': True}
                and type(rule['Expiration'].get('ExpiredObjectDeleteMarker')) is bool):
            kind = 'markers'
        else:
            raise BackupError('live recovery lifecycle can expire kept versions or differs from the reviewed retirement policy')
        if kind in seen:
            raise BackupError('live recovery lifecycle contains duplicate applicable rules')
        seen.add(kind)
        applicable.append(rule)
    if seen != {'retired', 'multipart', 'markers'}:
        raise BackupError('live recovery lifecycle is missing required bounded cleanup rules')
    result = hashlib.sha256(encoded(sorted(applicable, key=lambda rule: rule.get('ID', '')))).hexdigest()
    check_budget(store)
    return result


def protect_current_selection(store, instance, workspace, *, expected_selection):
    """Verify and protect the selected sole copy before the controller removes its disk."""
    receipt = _assert_current(store, instance, workspace, expected_selection)
    lifecycle = verify_protection_lifecycle(store, instance)
    metadata, archive = load_receipt_recovery(store, receipt, workspace)
    with tempfile.TemporaryDirectory(prefix='.minecraft-protection-', dir=workspace) as temporary:
        extract_recovery(metadata, archive, Path(temporary) / 'verified', check_deadline=budget_checker(store))
    references = [identity(receipt['marker']), identity(receipt['archive']),
                  identity(expected_selection['receipt']), identity(expected_selection['revision'])]
    _protect(store, references)
    if verify_protection_lifecycle(store, instance) != lifecycle:
        raise BackupError('live recovery lifecycle changed during selected-version protection')
    _assert_current(store, instance, workspace, expected_selection)
    check_budget(store)
    return {'instance': instance, 'receipt_id': receipt['receipt_id'], 'protection': 'verified',
            'lifecycle_sha256': lifecycle}


def verify_current_selection_protection(store, instance, workspace, *, expected_selection):
    """Read-only replay/start gate; missing protection is never silently repaired."""
    receipt = _assert_current(store, instance, workspace, expected_selection)
    lifecycle = verify_protection_lifecycle(store, instance)
    references = {identity(receipt['marker']), identity(receipt['archive']),
                  identity(expected_selection['receipt']), identity(expected_selection['revision'])}
    def require_tags():
        for key, version in sorted(references):
            check_budget(store)
            if store.version_tags(key, version) != {'minecraft-retention': 'keep'}:
                raise BackupError('selected recovery version lost its keep protection; preserve resources for repair')
    require_tags()
    metadata, archive = load_receipt_recovery(store, receipt, workspace)
    with tempfile.TemporaryDirectory(prefix='.minecraft-replay-check-', dir=workspace) as verify:
        extract_recovery(metadata, archive, Path(verify) / 'verified', check_deadline=budget_checker(store))
    require_tags()
    if verify_protection_lifecycle(store, instance) != lifecycle:
        raise BackupError('live recovery lifecycle changed during selected-version verification')
    _assert_current(store, instance, workspace, expected_selection)
    check_budget(store)
    return {'instance': instance, 'receipt_id': receipt['receipt_id'], 'protection': 'verified',
            'lifecycle_sha256': lifecycle}


def reconcile_retention(store, instance, journal_dir, *, current_selection, active_receipt_ids):
    """Called only under the controller's complete workstation operation lock."""
    journal_dir = _journal_directory(journal_dir)
    if isinstance(active_receipt_ids, dict):
        value = active_receipt_ids.get(instance)
        active = {value} if isinstance(value, str) else set(value or ())
    else:
        active = set(active_receipt_ids)
    for receipt_id in active:
        check_digest(receipt_id)
    receipts = {}
    paths = list(journal_dir.glob('receipt-*.json'))
    if len(paths) > 10000:
        raise BackupError('private receipt inventory exceeds its safety limit')
    for path in paths:
        check_budget(store)
        receipt = validate_receipt(private_json(path))
        if path.name != f"receipt-{receipt['receipt_id']}.json":
            raise BackupError('private receipt filename differs from its identity')
        if receipt['instance'] == instance:
            receipts[receipt['receipt_id']] = receipt
    if not active.issubset(receipts) or current_selection['receipt_id'] not in receipts:
        raise BackupError('active or selected receipt is missing from the private journal')
    if len({receipt['sequence'] for receipt in receipts.values()}) != len(receipts):
        raise BackupError('private receipt sequence contains conflicting candidates')
    legacy = {}
    for path in journal_dir.glob('legacy-*.json'):
        check_budget(store)
        record = _validate_legacy(private_json(path), instance)
        if path.name != f"legacy-{record['recovery_point']}.json":
            raise BackupError('legacy inventory filename differs from its point')
        legacy[record['recovery_point']] = record
        if len(legacy) > 10000:
            raise BackupError('legacy inventory exceeds its safety limit')
    inventory = store.list_versions(f'instances/{instance}/')
    objects = {identity(item): item for item in inventory if not item['delete_marker']}
    ledger_path = journal_dir / f'retired-points-{instance}.json'
    ledger = private_json(ledger_path) if ledger_path.exists() else {'schema_version': 1, 'instance': instance, 'points': []}
    if (set(ledger) != {'schema_version', 'instance', 'points'} or ledger['schema_version'] != 1
            or ledger['instance'] != instance or not isinstance(ledger['points'], list) or len(ledger['points']) > 10000):
        raise BackupError('retired point ledger is malformed')
    retired_points = set(ledger['points'])
    for point in retired_points:
        check_digest(point)
    rows = {}
    all_records = list(legacy.values()) + list(receipts.values())
    for record in all_records:
        pair = identity(record['marker'])
        archive_pair = identity(record['archive'])
        point = record['recovery_point']
        if pair not in objects or archive_pair not in objects:
            if point not in retired_points:
                raise BackupError('a catalog point disappeared without completed operator retirement')
            continue
        published = objects[pair]['last_modified']
        rows[point] = max(rows.get(point, ''), published)
    points = set(sorted(rows, key=lambda point: (rows[point], point), reverse=True)[:RETAIN])
    kept = {receipt_id for receipt_id, receipt in receipts.items() if receipt['recovery_point'] in points} | active | {current_selection['receipt_id']}
    kept_legacy = {point for point in legacy if point in points}
    references = {identity(current_selection['revision']), identity(current_selection['receipt'])}
    known = {}
    for receipt in all_records:
        is_kept = (receipt.get('receipt_id') in kept if 'receipt_id' in receipt else receipt['recovery_point'] in kept_legacy)
        for field in ('marker', 'archive'):
            reference = receipt[field]
            pair = identity(reference)
            if pair in known and known[pair] != reference:
                raise BackupError('private receipts disagree about an exact recovery version')
            known[pair] = reference
            if is_kept:
                references.add(pair)
    journal_path = journal_dir / f'retention-{instance}.json'
    with tempfile.TemporaryDirectory(prefix='.minecraft-retention-', dir=journal_dir) as temporary:
        workspace = Path(temporary)
        selected = _assert_current(store, instance, workspace, current_selection)
        lifecycle = verify_protection_lifecycle(store, instance)
        if selected != receipts[current_selection['receipt_id']]:
            raise BackupError('private selected receipt differs from authoritative S3 evidence')
        if not references.issubset(objects):
            raise BackupError('required recovery object version is missing from the complete inventory')
        classified = set(known)
        pointer = f'instances/{instance}/receipts/current.json'
        for pair, item in objects.items():
            key, version = pair
            if key == pointer:
                reference = store.download_revision(key, workspace / 'historical-selection.json', max_bytes=MAX_METADATA_BYTES, version_id=version)
                if reference is None:
                    raise BackupError('selection history disappeared during inventory')
                value = read_json(workspace / 'historical-selection.json')
                if (not isinstance(value, dict) or set(value) != {'schema_version', 'instance', 'receipt_id', 'sequence', 'previous_receipt_id', 'receipt'}
                        or type(value['schema_version']) is not int or value['schema_version'] != 1
                        or value['instance'] != instance or type(value['sequence']) is not int
                        or value['receipt_id'] not in receipts):
                    raise BackupError('unclassified selection history blocks retirement')
                recorded = receipts[value['receipt_id']]
                if any(value[field] != recorded[field] for field in ('sequence', 'previous_receipt_id')):
                    raise BackupError('selection history differs from its private receipt')
                _validate_object_receipt(value['receipt'], f"instances/{instance}/receipts/{value['receipt_id']}.json", MAX_METADATA_BYTES)
                classified.add(pair)
                if value['receipt_id'] in kept:
                    references.add(pair)
            elif re.fullmatch(f'instances/{instance}/receipts/[a-f0-9]{{64}}.json', key):
                receipt_id = key.rsplit('/', 1)[1][:-5]
                if receipt_id not in receipts:
                    raise BackupError('unclassified immutable receipt blocks retirement')
                revision = store.download_revision(key, workspace / 'inventory-receipt.json', max_bytes=MAX_METADATA_BYTES, version_id=version)
                if revision is None or validate_receipt(read_json(workspace / 'inventory-receipt.json')) != receipts[receipt_id]:
                    raise BackupError('immutable receipt differs from the private inventory')
                classified.add(pair)
                if receipt_id in kept:
                    references.add(pair)
        if not set(objects).issubset(classified):
            raise BackupError('unclassified recovery versions block retirement; preserve them for repair')
        for receipt_id in kept:
            metadata, archive = load_receipt_recovery(store, receipts[receipt_id], workspace)
            with tempfile.TemporaryDirectory(prefix='.minecraft-keep-check-', dir=workspace) as verify:
                extract_recovery(metadata, archive, Path(verify) / 'verified', check_deadline=budget_checker(store))
        for point in kept_legacy:
            metadata, archive = _load_legacy(store, legacy[point], workspace)
            with tempfile.TemporaryDirectory(prefix='.minecraft-legacy-keep-', dir=workspace) as verify:
                extract_recovery(metadata, archive, Path(verify) / 'verified', check_deadline=budget_checker(store))
        retired = sorted(set(objects) - references)
        plan = {'schema_version': 1, 'instance': instance, 'receipt_id': current_selection['receipt_id'],
                'selection': current_selection, 'active_receipt_ids': sorted(active), 'keep': [list(pair) for pair in sorted(references)],
                'retire': [list(pair) for pair in retired], 'status': 'planned'}
        if journal_path.exists() or journal_path.is_symlink():
            prior = private_json(journal_path)
            if prior.get('status') != 'completed':
                if (not isinstance(prior.get('retire'), list) or any(not isinstance(pair, list) or len(pair) != 2
                        or not all(isinstance(value, str) for value in pair) for pair in prior['retire'])):
                    raise BackupError('interrupted retirement contains malformed object identities')
                expected = {**plan, 'retire': prior.get('retire')}
                if prior != expected or not set(map(tuple, prior['retire'])).isdisjoint(references):
                    raise BackupError('interrupted retention journal differs from the current operation')
                if {pair for pair in map(tuple, prior['retire']) if pair in objects} != set(retired):
                    raise BackupError('interrupted retirement omits a currently verified obsolete version')
                for pair in map(tuple, prior['retire']):
                    if (len(pair) != 2 or not all(isinstance(value, str) for value in pair)
                            or not re.fullmatch(f'instances/{instance}/(?:completed/[a-f0-9]{{64}}.json|objects/[a-f0-9]{{64}}/data.tar.gz|receipts/(?:[a-f0-9]{{64}}|current).json)', pair[0])):
                        raise BackupError('interrupted retirement contains an unsafe object identity')
                    valid_version(pair[1])
                    if pair in objects and pair not in set(retired):
                        raise BackupError('interrupted retirement contains a version outside the verified obsolete inventory')
                    known_keys = {key for key, _ in known} | {pointer} | {
                        f'instances/{instance}/receipts/{receipt_id}.json' for receipt_id in receipts}
                    if pair not in objects and pair[0] not in known_keys:
                        raise BackupError('interrupted retirement contains an unclassified absent object')
                plan = prior
        write_json(journal_path, plan)
        _protect(store, references)
        _assert_current(store, instance, workspace, current_selection)
        retire_pairs = set(map(tuple, plan['retire']))
        retired_points.update(record['recovery_point'] for record in all_records
                              if identity(record['marker']) in retire_pairs and identity(record['archive']) in retire_pairs)
        if len(retired_points) > 10000:
            raise BackupError('retired point ledger exceeds its safety limit')
        # Save the verified retirement intent before tags can make old versions expire.
        write_json(ledger_path, {'schema_version': 1, 'instance': instance, 'points': sorted(retired_points)})
        for key, version in plan['retire']:
            check_budget(store)
            _assert_current(store, instance, workspace, current_selection)
            if (key, version) in objects:
                store.set_retention(key, version, 'retired')
        _assert_current(store, instance, workspace, current_selection)
        if verify_protection_lifecycle(store, instance) != lifecycle:
            raise BackupError('live recovery lifecycle changed during retirement')
        plan['status'] = 'completed'
        write_json(journal_path, plan)
        check_budget(store)
    return {'instance': instance, 'receipt_id': current_selection['receipt_id'],
            'keep_versions': len(references), 'retired_versions': len(retired), 'retention': 'verified'}


def main(argv=None):
    import argparse
    import json
    import sys
    from minecraft_backup_store import S3Store
    from minecraft_instance import OperationError, command
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('import-legacy',))
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--journal-dir', required=True)
    parser.add_argument('--reviewed-inventory', required=True)
    parser.add_argument('--inventory-sha256', required=True)
    parser.add_argument('--profile')
    parser.add_argument('instance')
    args = parser.parse_args(argv)
    try:
        deployment = private_json(args.deployment)
        document = private_json(args.reviewed_inventory)
        check_digest(args.inventory_sha256)
        if (digest_file(args.reviewed_inventory) != args.inventory_sha256 or args.instance not in deployment['instances']
                or set(document) != {'schema_version', 'instance', 'bucket', 'versions'} or document['schema_version'] != 1
                or document['instance'] != args.instance or document['bucket'] != deployment['backup_bucket_name']):
            raise BackupError('legacy import does not match the reviewed inventory and deployment')
        store = S3Store(deployment['backup_bucket_name'], region=deployment['region'], profile=args.profile, timeout=3000)
        arguments = ['aws', 'sts', 'get-caller-identity', '--query', 'Account', '--output', 'text',
                     '--region', deployment['region'], '--no-cli-pager', '--no-cli-auto-prompt']
        if args.profile:
            arguments.extend(('--profile', args.profile))
        if command(arguments).strip() != deployment['account_id']:
            raise BackupError('active AWS account differs from the reviewed legacy deployment')
        result = import_legacy_points(store, args.instance, args.journal_dir, reviewed_versions=document['versions'])
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OperationError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'failed', 'error': 'legacy inventory import failed; no version is certified'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
