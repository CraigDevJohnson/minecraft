"""Selected-version protection and operator retirement at the object boundary."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
import contextlib
import io
from unittest import mock

import test_recovery_receipts as fixture_module
import minecraft_backup as backup
import minecraft_retention as retention


class RetentionStore(fixture_module.VersionedStore):
    def __init__(self, objects):
        super().__init__(objects)
        self.tags = {}
        self.events = []
        self.fail_retirement = False
        self.fail_protection = False
        self.lifecycle = {'Rules': [
            {'ID': 'retire-verified-survival', 'Status': 'Enabled',
             'Filter': {'And': {'Prefix': 'instances/survival/', 'Tags': [{'Key': 'minecraft-retention', 'Value': 'retired'}]}},
             'Expiration': {'Days': 14}, 'NoncurrentVersionExpiration': {'NoncurrentDays': 14}},
            {'ID': 'abort-incomplete-survival', 'Status': 'Enabled', 'Filter': {'Prefix': 'instances/survival/'},
             'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 1}},
            {'ID': 'remove-expired-markers-survival', 'Status': 'Enabled', 'Filter': {'Prefix': 'instances/survival/'},
             'Expiration': {'ExpiredObjectDeleteMarker': True}}]}

    def lifecycle_configuration(self):
        return copy.deepcopy(self.lifecycle)

    def list_versions(self, prefix):
        result = []
        for key, version in self.versions:
            if not key.startswith(prefix):
                continue
            published = (json.loads(self.versions[(key, version)])['created_at']
                         if '/completed/' in key else '2026-10-03T12:00:00Z')
            result.append({'key': key, 'version_id': version, 'is_latest': self.current.get(key) == version,
                           'delete_marker': False, 'last_modified': published})
        return result

    def version_tags(self, key, version):
        if (key, version) not in self.versions:
            raise backup.BackupError('version is missing')
        return dict(self.tags.get((key, version), {}))

    def set_retention(self, key, version, state):
        self.events.append((key, version, state))
        if (state == 'retired' and self.fail_retirement) or (state == 'keep' and self.fail_protection):
            raise backup.BackupError('tag update or readback failed')
        self.tags[(key, version)] = {'minecraft-retention': state}


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.RecoveryReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = RetentionStore(self.fixture.store.objects)
        self.current = None

    def publish(self, number):
        source_fixture = self.fixture.fixture
        metadata = source_fixture.metadata
        metadata['session'] = f'session-{number}'
        metadata['created_at'] = f'2026-10-03T12:{number:02d}:00Z'
        point = backup.point_identity('survival', metadata['session'], metadata['manifest_sha256'])
        metadata['recovery_point'] = point
        source_fixture.recovery_point = point
        source_fixture.marker_key = backup.marker_key('survival', point)
        source_fixture.archive_key = f'instances/survival/objects/{point}/data.tar.gz'
        source_fixture.publish()
        self.store.put(source_fixture.marker_key, source_fixture.store.objects[source_fixture.marker_key])
        self.store.put(source_fixture.archive_key, source_fixture.store.objects[source_fixture.archive_key])
        # The initial fixture point predates the migration and is intentionally unclassified.
        if number == 1 and not getattr(self, 'preserve_legacy', False):
            for key, version in list(self.store.versions):
                if key not in (source_fixture.marker_key, source_fixture.archive_key):
                    del self.store.versions[(key, version)]
                    self.store.objects.pop(key, None)
                    self.store.current.pop(key, None)
        self.fixture.evidence.update(session=metadata['session'], recovery_point=point)
        result = backup.verify_stop_receipt(self.fixture.source, self.fixture.drain(), self.fixture.journal,
                                           self.fixture.bootstrap, store=self.store, protect=True, fresh_drain=self.fixture.drain)
        self.current = result['survival']['selection']
        return result['survival']['receipt']

    def reconcile(self, active=()):
        return retention.reconcile_retention(self.store, 'survival', self.fixture.journal,
                                             current_selection=self.current, active_receipt_ids=active)

    def test_protection_tags_exact_selected_data_receipt_and_pointer_versions(self):
        receipt = self.publish(1)
        expected = [retention.identity(receipt['marker']), retention.identity(receipt['archive']),
                    retention.identity(self.current['receipt']), retention.identity(self.current['revision'])]
        for pair in expected:
            self.assertEqual(self.store.tags[pair], {'minecraft-retention': 'keep'})
        self.assertEqual(self.reconcile({'survival': receipt['receipt_id'], 'creative': 'b' * 64})['retired_versions'], 0)

    def test_more_than_fourteen_sessions_retains_current_and_active_old_point(self):
        first = self.publish(1)
        second = self.publish(2)
        for number in range(3, 17):
            last = self.publish(number)
        result = self.reconcile({first['receipt_id']})
        self.assertGreater(result['retired_versions'], 0)
        for receipt in (first, last):
            for field in ('marker', 'archive'):
                self.assertEqual(self.store.tags[retention.identity(receipt[field])]['minecraft-retention'], 'keep')
        for field in ('marker', 'archive'):
            self.assertEqual(self.store.tags[retention.identity(second[field])]['minecraft-retention'], 'retired')
        journal = json.loads((self.fixture.journal / 'retention-survival.json').read_text())
        self.assertEqual(journal['status'], 'completed')
        self.assertTrue(set(map(tuple, journal['keep'])).isdisjoint(map(tuple, journal['retire'])))

    def test_unknown_version_and_stale_selection_fail_without_any_retirement(self):
        self.publish(1)
        before = dict(self.store.tags)
        self.store.put('instances/survival/objects/unclassified/data.tar.gz', b'preserve unknown data')
        with self.assertRaisesRegex(backup.BackupError, 'unclassified'):
            self.reconcile()
        self.assertEqual(self.store.tags, before)
        self.current = {**self.current, 'receipt_id': 'b' * 64}
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        self.assertFalse(any(state == 'retired' for _, _, state in self.store.events))

    def test_interrupted_retirement_reuses_same_journal_and_preserves_current(self):
        for number in range(1, 16):
            selected = self.publish(number)
        self.store.fail_retirement = True
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        plan = json.loads((self.fixture.journal / 'retention-survival.json').read_text())
        self.assertEqual(plan['status'], 'planned')
        for field in ('marker', 'archive'):
            self.assertEqual(self.store.tags[retention.identity(selected[field])]['minecraft-retention'], 'keep')
        self.store.fail_retirement = False
        self.assertEqual(self.reconcile()['retention'], 'verified')
        completed = json.loads((self.fixture.journal / 'retention-survival.json').read_text())
        self.assertEqual(completed['retire'], plan['retire'])

    def test_already_retired_selected_version_and_tag_failure_block_protection(self):
        receipt = self.publish(1)
        self.store.tags[retention.identity(receipt['archive'])] = {'minecraft-retention': 'retired'}
        with self.assertRaisesRegex(backup.BackupError, 'already retired'):
            retention.protect_current_selection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
        self.store.tags[retention.identity(receipt['archive'])] = {'minecraft-retention': 'keep'}
        self.store.fail_protection = True
        with self.assertRaises(backup.BackupError):
            retention.protect_current_selection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)

    def test_runtime_prune_refuses_s3_only_without_touching_objects(self):
        self.publish(1)
        before = dict(self.store.objects)
        with self.assertRaisesRegex(backup.BackupError, 'operator'):
            backup.prune(self.store, 'survival', self.fixture.workspace, lifecycle_mode='s3-only')
        self.assertEqual(self.store.objects, before)

    def test_missing_selected_exact_version_keeps_journal_and_other_versions_untouched(self):
        receipt = self.publish(1)
        del self.store.versions[retention.identity(receipt['archive'])]
        before = copy.deepcopy(self.store.tags)
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        self.assertEqual(self.store.tags, before)
        self.assertFalse((self.fixture.journal / 'retention-survival.json').exists())

    def test_selection_change_before_retirement_preserves_all_old_and_new_data(self):
        for number in range(1, 16):
            self.publish(number)
        original = self.store.set_retention
        changed = False
        def interfere(key, version, state):
            nonlocal changed
            original(key, version, state)
            if not changed:
                changed = True
                self.store.put('instances/survival/receipts/current.json', self.store.objects['instances/survival/receipts/current.json'] + b'\n')
        self.store.set_retention = interfere
        with self.assertRaisesRegex(backup.BackupError, 'selection changed'):
            self.reconcile()
        self.assertFalse(any(state == 'retired' for _, _, state in self.store.events))

    def legacy_versions(self, count):
        source_fixture = self.fixture.fixture
        for number in range(2, count + 1):
            metadata = source_fixture.metadata
            metadata['session'] = f'legacy-{number}'
            metadata['created_at'] = f'2026-09-08T18:{number:02d}:00Z'
            metadata['recovery_point'] = backup.point_identity('survival', metadata['session'], metadata['manifest_sha256'])
            source_fixture.marker_key = backup.marker_key('survival', metadata['recovery_point'])
            source_fixture.archive_key = f"instances/survival/objects/{metadata['recovery_point']}/data.tar.gz"
            source_fixture.publish()
            self.store.put(source_fixture.marker_key, source_fixture.store.objects[source_fixture.marker_key])
            self.store.put(source_fixture.archive_key, source_fixture.store.objects[source_fixture.archive_key])
        return [{'key': key, 'version_id': version} for key, version in self.store.versions]

    def test_reviewed_legacy_fourteen_points_join_new_receipt_and_bounded_retirement(self):
        reviewed = self.legacy_versions(14)
        self.assertEqual(retention.import_legacy_points(self.store, 'survival', self.fixture.journal,
            reviewed_versions=reviewed)['imported_points'], 14)
        self.assertEqual(self.store.events, [])
        records = [json.loads(path.read_text()) for path in self.fixture.journal.glob('legacy-*.json')]
        self.assertTrue(all('host_id' not in item and 'bootstrap_sha256' not in item for item in records))
        self.preserve_legacy = True
        selected = self.publish(1)
        self.assertEqual(self.reconcile()['retired_versions'], 2)
        kept_data = [pair for pair, tags in self.store.tags.items() if tags['minecraft-retention'] == 'keep'
                     and ('/completed/' in pair[0] or '/objects/' in pair[0])]
        self.assertEqual(len(kept_data), 28)
        self.assertEqual(self.store.tags[retention.identity(selected['archive'])]['minecraft-retention'], 'keep')

    def test_legacy_import_rejects_missing_archive_or_corrupt_bytes_without_records_or_tags(self):
        reviewed = self.legacy_versions(1)
        for versions in (reviewed[:1], [{**item, 'version_id': 'unknown'} for item in reviewed]):
            with self.assertRaises(backup.BackupError):
                retention.import_legacy_points(self.store, 'survival', self.fixture.journal, reviewed_versions=versions)
            self.assertEqual(list(self.fixture.journal.glob('legacy-*.json')), [])
        key, version = next(pair for pair in self.store.versions if '/objects/' in pair[0])
        self.store.versions[(key, version)] = b'corrupt'
        with self.assertRaises(backup.BackupError):
            retention.import_legacy_points(self.store, 'survival', self.fixture.journal, reviewed_versions=reviewed)
        self.assertEqual(list(self.fixture.journal.glob('legacy-*.json')), [])
        self.assertEqual(self.store.events, [])

    def test_modified_interrupted_journal_cannot_retire_an_active_kept_version(self):
        for number in range(1, 16):
            selected = self.publish(number)
        self.store.fail_retirement = True
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        journal_path = self.fixture.journal / 'retention-survival.json'
        plan = json.loads(journal_path.read_text())
        plan['retire'].append(list(retention.identity(selected['archive'])))
        from minecraft_instance import write_json
        write_json(journal_path, plan)
        self.store.fail_retirement = False
        before = dict(self.store.tags)
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        self.assertEqual(before, self.store.tags)

    def test_owner_import_cli_binds_reviewed_file_digest_and_account_before_reading(self):
        versions = self.legacy_versions(1)
        from minecraft_instance import write_json
        deployment = self.fixture.journal / 'deployment.json'
        reviewed = self.fixture.journal / 'reviewed.json'
        write_json(deployment, self.fixture.source)
        write_json(reviewed, {'schema_version': 1, 'instance': 'survival', 'bucket': self.store.bucket, 'versions': versions})
        digest = backup.digest_file(reviewed)
        arguments = ['import-legacy', '--deployment', str(deployment), '--journal-dir', str(self.fixture.journal),
                     '--reviewed-inventory', str(reviewed), '--inventory-sha256', digest, 'survival']
        with mock.patch('minecraft_backup_store.S3Store', return_value=self.store), mock.patch('minecraft_instance.command', return_value='123456789012\n'):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(retention.main(arguments), 0)
        arguments[arguments.index('--inventory-sha256') + 1] = '0' * 64
        with mock.patch('minecraft_instance.command') as identity_call:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(retention.main(arguments), 1)
        identity_call.assert_not_called()

    def test_retirement_resumes_when_lifecycle_removed_a_recorded_obsolete_version(self):
        for number in range(1, 16):
            self.publish(number)
        original = self.store.set_retention
        interrupted = False
        def expire_after_tag(key, version, state):
            nonlocal interrupted
            original(key, version, state)
            if state == 'retired' and not interrupted:
                interrupted = True
                del self.store.versions[(key, version)]
                raise backup.BackupError('lifecycle removed the retired version before readback')
        self.store.set_retention = expire_after_tag
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        self.store.set_retention = original
        self.assertEqual(self.reconcile()['retention'], 'verified')

    def test_live_lifecycle_conflict_or_missing_access_blocks_protection_without_tag_mutation(self):
        self.publish(1)
        valid = copy.deepcopy(self.store.lifecycle)
        for conflict in ({'ID': 'blanket', 'Status': 'Enabled', 'Filter': {}, 'NoncurrentVersionExpiration': {'NoncurrentDays': 14}},
                         {'ID': 'tagless', 'Status': 'Enabled', 'Filter': {'Prefix': 'instances/survival/'}, 'Expiration': {'Days': 14}},
                         {'ID': 'wrong-tag', 'Status': 'Enabled', 'Filter': {'And': {'Prefix': 'instances/survival/', 'Tags': [{'Key': 'minecraft-retention', 'Value': 'keep'}]}}, 'Expiration': {'Days': 14}}):
            self.store.lifecycle = {'Rules': valid['Rules'] + [conflict]}
            before = dict(self.store.tags)
            with self.assertRaises(backup.BackupError):
                retention.protect_current_selection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
            self.assertEqual(self.store.tags, before)
        self.store.lifecycle = valid
        with mock.patch.object(self.store, 'lifecycle_configuration', side_effect=backup.BackupError('access denied')):
            with self.assertRaises(backup.BackupError):
                retention.protect_current_selection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
        self.assertEqual(self.store.tags, before)

    def provider_two_world_lifecycle(self):
        rules = []
        for instance in ('survival', 'creative'):
            for original in RetentionStore({}).lifecycle['Rules']:
                rule = copy.deepcopy(original)
                rule['ID'] = rule['ID'].replace('survival', instance)
                filter_value = rule['Filter']
                if 'And' in filter_value:
                    filter_value = filter_value['And']
                    # AWS provider 6.63 sends this planned zero value to S3.
                    filter_value['ObjectSizeGreaterThan'] = 0
                filter_value['Prefix'] = f'instances/{instance}/'
                rules.append(rule)
        return {'Rules': rules}

    def test_provider_zero_size_six_rules_protect_and_verify_kept_selection(self):
        self.store.lifecycle = self.provider_two_world_lifecycle()
        self.assertEqual(len(self.store.lifecycle['Rules']), 6)
        self.publish(1)
        for instance in ('survival', 'creative'):
            self.assertEqual(len(retention.verify_protection_lifecycle(self.store, instance)), 64)
        before = list(self.store.events)
        result = retention.verify_current_selection_protection(
            self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
        self.assertEqual(result['protection'], 'verified')
        self.assertEqual(self.store.events, before)
        self.assertEqual(len(self.store.tags), 4)
        self.assertTrue(all(tags == {'minecraft-retention': 'keep'} for tags in self.store.tags.values()))

    def test_provider_size_filter_variants_refuse_before_protection_tag_mutation(self):
        self.publish(1)
        before = dict(self.store.tags)
        before_events = list(self.store.events)
        for instance in ('survival', 'creative'):
            for key, value in (('ObjectSizeGreaterThan', 1), ('ObjectSizeGreaterThan', -1),
                               ('ObjectSizeGreaterThan', True), ('ObjectSizeGreaterThan', False),
                               ('ObjectSizeGreaterThan', 0.0), ('ObjectSizeGreaterThan', '0'),
                               ('ObjectSizeGreaterThan', None), ('ObjectSizeLessThan', 0),
                               ('UnknownFilter', 0),
                               ('Tags', [{'Key': 'minecraft-retention', 'Value': 'keep'}]),
                               ('Tags', []), ('Prefix', 'instances/')):
                with self.subTest(instance=instance, key=key, value=value):
                    self.store.lifecycle = self.provider_two_world_lifecycle()
                    rule = next(rule for rule in self.store.lifecycle['Rules']
                                if rule['ID'] == f'retire-verified-{instance}')
                    rule['Filter']['And'][key] = value
                    with self.assertRaises(backup.BackupError):
                        retention.verify_protection_lifecycle(self.store, instance)
                    with self.assertRaises(backup.BackupError):
                        retention.protect_current_selection(
                            self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
                    self.assertEqual(self.store.tags, before)
                    self.assertEqual(self.store.events, before_events)

    def test_zero_size_filter_does_not_admit_cleanup_rule_with_expiry_of_kept_versions(self):
        self.store.lifecycle = self.provider_two_world_lifecycle()
        conflict = {'ID': 'keep-expiry', 'Status': 'Enabled',
                    'Filter': {'And': {'Prefix': 'instances/survival/', 'ObjectSizeGreaterThan': 0,
                                      'Tags': [{'Key': 'minecraft-retention', 'Value': 'keep'}]}},
                    'Expiration': {'Days': 14}, 'NoncurrentVersionExpiration': {'NoncurrentDays': 14}}
        self.store.lifecycle['Rules'].append(conflict)
        with self.assertRaises(backup.BackupError):
            retention.verify_protection_lifecycle(self.store, 'survival')

    def test_missing_cleanup_rule_or_lifecycle_drift_cannot_certify_protection(self):
        self.publish(1)
        valid = copy.deepcopy(self.store.lifecycle)
        self.store.lifecycle['Rules'].pop()
        with self.assertRaises(backup.BackupError):
            retention.protect_current_selection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
        self.store.lifecycle = valid
        changed = copy.deepcopy(valid)
        changed['Rules'][0]['Expiration']['Days'] = 1
        with mock.patch.object(self.store, 'lifecycle_configuration', side_effect=[valid, changed]):
            with self.assertRaises(backup.BackupError):
                retention.protect_current_selection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)

    def test_modified_interrupted_journal_cannot_silently_skip_obsolete_versions(self):
        for number in range(1, 16):
            self.publish(number)
        self.store.fail_retirement = True
        with self.assertRaises(backup.BackupError):
            self.reconcile()
        path = self.fixture.journal / 'retention-survival.json'
        plan = json.loads(path.read_text())
        plan['retire'].pop()
        from minecraft_instance import write_json
        write_json(path, plan)
        self.store.fail_retirement = False
        before = dict(self.store.tags)
        with self.assertRaisesRegex(backup.BackupError, 'omits'):
            self.reconcile()
        self.assertEqual(self.store.tags, before)

    def test_read_only_replay_verifier_requires_keep_on_every_selected_version_without_repair(self):
        receipt = self.publish(1)
        references = [retention.identity(receipt['marker']), retention.identity(receipt['archive']),
                      retention.identity(self.current['receipt']), retention.identity(self.current['revision'])]
        before_events = list(self.store.events)
        self.assertEqual(retention.verify_current_selection_protection(self.store, 'survival', self.fixture.workspace,
            expected_selection=self.current)['protection'], 'verified')
        self.assertEqual(self.store.events, before_events)
        for pair in references:
            for value in ({}, {'minecraft-retention': 'retired'}):
                with self.subTest(pair=pair, value=value):
                    self.store.tags[pair] = value
                    with self.assertRaisesRegex(backup.BackupError, 'lost its keep'):
                        retention.verify_current_selection_protection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
                    self.assertEqual(self.store.tags[pair], value)
                    self.assertEqual(self.store.events, before_events)
                    self.store.tags[pair] = {'minecraft-retention': 'keep'}

    def test_read_only_replay_refuses_tag_drift_during_archive_verification(self):
        receipt = self.publish(1)
        original = self.store.download_revision
        def change_archive_tag(key, target, **kwargs):
            result = original(key, target, **kwargs)
            if key == receipt['archive']['key']:
                self.store.tags[retention.identity(receipt['archive'])] = {}
            return result
        before_events = list(self.store.events)
        with mock.patch.object(self.store, 'download_revision', side_effect=change_archive_tag):
            with self.assertRaisesRegex(backup.BackupError, 'lost its keep'):
                retention.verify_current_selection_protection(self.store, 'survival', self.fixture.workspace, expected_selection=self.current)
        self.assertEqual(self.store.events, before_events)


if __name__ == '__main__':
    unittest.main()
