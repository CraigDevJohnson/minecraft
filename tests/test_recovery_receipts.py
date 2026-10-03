"""S3-only recovery proofs at the versioned object and restore boundaries."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_restore as restore_fixture
import minecraft_backup as backup
import minecraft_restore as restore


class VersionedStore:
    def __init__(self, objects):
        self.bucket = 'source-backups'
        self.objects = {}
        self.versions = {}
        self.current = {}
        self.reads = []
        self.fail_selection = False
        for key, value in objects.items():
            self.put(key, value)

    def put(self, key, value):
        version = f'version-{len(self.versions) + 1}'
        self.versions[(key, version)] = value
        self.objects[key] = value
        self.current[key] = version
        return version

    def upload(self, key, source):
        if key not in self.objects:
            self.put(key, Path(source).read_bytes())

    def download_revision(self, key, target, *, max_bytes, version_id=None):
        selected = version_id or self.current.get(key)
        self.reads.append((key, selected))
        value = self.versions.get((key, selected))
        if value is None:
            return None
        if len(value) > max_bytes:
            raise backup.BackupError('object exceeds safety limit')
        Path(target).write_bytes(value)
        return {'key': key, 'version_id': selected, 'size_bytes': len(value),
                'etag': '"' + hashlib.sha256(value).hexdigest() + '"'}

    def conditional_write(self, key, source, *, expected_etag):
        if self.fail_selection:
            raise backup.BackupError('interrupted selection publication')
        current = self.objects.get(key)
        actual = '"' + hashlib.sha256(current).hexdigest() + '"' if current else None
        if expected_etag != actual:
            raise backup.BackupError('selection changed concurrently')
        value = Path(source).read_bytes()
        version = self.put(key, value)
        return {'key': key, 'version_id': version, 'size_bytes': len(value),
                'etag': '"' + hashlib.sha256(value).hexdigest() + '"'}


class RecoveryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = restore_fixture.RestoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = self.fixture.directory
        self.workspace = self.directory / 'receipt-workspace'
        self.workspace.mkdir(mode=0o700)
        self.journal = self.directory / 'journal'
        self.journal.mkdir(mode=0o700)
        self.store = VersionedStore(self.fixture.store.objects)
        self.source = {**self.fixture.source_deployment, 'account_id': '123456789012',
                       'region': 'us-west-2', 'host_id': 'i-00000000000000000',
                       'instances': {'survival': self.fixture.definition}}
        self.bootstrap = 'a' * 64
        self.evidence = {'application': 'stopped', 'backup': 'complete',
                         'session': self.fixture.metadata['session'], 'manifest_sha256': self.fixture.metadata['manifest_sha256'],
                         'recovery_point': self.fixture.recovery_point}

    def receipt(self, *, previous=None):
        return backup.create_recovery_receipt(self.store, self.source, 'survival', self.evidence,
                                             self.bootstrap, self.workspace, previous=previous)

    def publish(self, receipt, *, previous_selection=None):
        return backup.publish_recovery_receipt(self.store, receipt, self.workspace,
                                               previous_selection=previous_selection)

    def drain(self):
        return {'draining': True, 'instances': {'survival': {'instance': 'survival', 'application': 'stopped', 'backup': 'complete'}},
                'recovery': {'survival': {key: self.evidence[key] for key in ('session', 'manifest_sha256', 'recovery_point')}}}

    def next_session(self):
        self.fixture.metadata['session'] = 'final-session-two'
        self.fixture.recovery_point = backup.point_identity('survival', 'final-session-two', self.fixture.metadata['manifest_sha256'])
        self.fixture.metadata['recovery_point'] = self.fixture.recovery_point
        self.fixture.metadata['created_at'] = '2026-09-08T19:00:00Z'
        self.fixture.marker_key = backup.marker_key('survival', self.fixture.recovery_point)
        self.fixture.archive_key = f'instances/survival/objects/{self.fixture.recovery_point}/data.tar.gz'
        self.fixture.publish()
        self.store.put(self.fixture.marker_key, self.fixture.store.objects[self.fixture.marker_key])
        self.store.put(self.fixture.archive_key, self.fixture.store.objects[self.fixture.archive_key])
        self.evidence['session'] = 'final-session-two'
        self.evidence['recovery_point'] = self.fixture.recovery_point

    def restore(self, receipt, *, resume=False):
        return restore.restore(self.fixture.machine, self.source, 'survival', receipt['recovery_point'], self.store,
                               receipt=receipt, bootstrap_sha256=self.bootstrap, resume=resume)

    def test_receipt_records_exact_final_session_versions_and_content_source_capacity(self):
        receipt = self.receipt()
        self.assertEqual(receipt['session'], self.evidence['session'])
        self.assertEqual(receipt['source']['volume_id'], self.source['data_volume_id'])
        self.assertEqual(receipt['source']['host_id'], self.source['host_id'])
        self.assertEqual(receipt['source']['deployment_sha256'], hashlib.sha256(backup.encoded(self.source)).hexdigest())
        self.assertEqual(receipt['bootstrap_sha256'], self.bootstrap)
        self.assertEqual(receipt['marker']['version_id'], self.store.current[self.fixture.marker_key])
        self.assertEqual(receipt['archive']['version_id'], self.store.current[self.fixture.archive_key])
        self.assertEqual(receipt['data_size_bytes'], sum(len(value) for value in self.fixture.checkpoints.values()))
        self.assertEqual(receipt['required_capacity_bytes'], receipt['data_size_bytes'] + receipt['archive']['size_bytes'] + 1024 ** 3)
        self.assertFalse((self.fixture.target_root / 'instances').exists())

    def test_final_backup_failure_wrong_session_or_source_cannot_create_a_receipt(self):
        for scenario in ('failed', 'wrong-session', 'wrong-content', 'wrong-volume'):
            with self.subTest(scenario=scenario):
                evidence, source = copy.deepcopy(self.evidence), copy.deepcopy(self.source)
                if scenario == 'failed':
                    evidence['backup'] = 'failed'
                elif scenario == 'wrong-session':
                    evidence['session'] = 'earlier-session'
                elif scenario == 'wrong-content':
                    evidence['manifest_sha256'] = 'b' * 64
                else:
                    source['data_volume_id'] = 'vol-22222222222222222'
                with self.assertRaises(backup.BackupError):
                    backup.create_recovery_receipt(self.store, source, 'survival', evidence, self.bootstrap, self.workspace)
        self.assertFalse(any('/receipts/' in key for key in self.store.objects))

    def test_receipt_verifies_all_archived_checkpoints_before_any_selection_publication(self):
        checkpoint = next(item for item in self.fixture.metadata['files'] if item['path'] == 'data/world/level.dat')
        checkpoint['sha256'] = '0' * 64
        self.fixture.save_metadata()
        self.store.put(self.fixture.marker_key, self.fixture.store.objects[self.fixture.marker_key])
        with self.assertRaisesRegex(backup.BackupError, 'checksum'):
            self.receipt()
        self.assertFalse(any('/receipts/' in key for key in self.store.objects))

    def test_published_selection_is_versioned_idempotent_and_cannot_regress(self):
        first = self.receipt()
        selection = self.publish(first)
        versions = len(self.store.versions)
        self.assertEqual(self.publish(first), selection)
        self.assertEqual(len(self.store.versions), versions)
        self.next_session()
        second = self.receipt(previous=first)
        second_selection = self.publish(second, previous_selection=selection)
        self.assertEqual(second_selection['sequence'], 2)
        with self.assertRaises(backup.BackupError):
            self.publish(first, previous_selection=selection)
        loaded, current = backup.load_current_receipt(self.store, 'survival', self.workspace, expected_receipt_id=second['receipt_id'])
        self.assertEqual(loaded, second)
        self.assertEqual(current, second_selection)

    def test_missing_or_corrupt_authoritative_selection_never_falls_back_to_catalog(self):
        with self.assertRaisesRegex(backup.BackupError, 'selection'):
            backup.load_current_receipt(self.store, 'survival', self.workspace)
        receipt = self.receipt()
        self.publish(receipt)
        self.store.put('instances/survival/receipts/current.json', b'{broken')
        with self.assertRaises(backup.BackupError):
            backup.load_current_receipt(self.store, 'survival', self.workspace)

    def test_concurrent_selection_change_refuses_publication_and_preserves_current_point(self):
        first = self.receipt()
        selection = self.publish(first)
        self.next_session()
        second = self.receipt(previous=first)
        original = self.store.conditional_write
        key = 'instances/survival/receipts/current.json'
        def change_after_observation(selected_key, source, *, expected_etag):
            self.store.put(key, self.store.objects[key] + b'\n')
            return original(selected_key, source, expected_etag=expected_etag)
        with mock.patch.object(self.store, 'conditional_write', side_effect=change_after_observation), self.assertRaisesRegex(backup.BackupError, 'concurrently'):
            self.publish(second, previous_selection=selection)
        current, _ = backup.load_current_receipt(self.store, 'survival', self.workspace)
        self.assertEqual(current['receipt_id'], first['receipt_id'])

    def test_rollback_of_s3_selection_is_rejected_against_durable_local_identity(self):
        initial = backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        old_selection = {key: value for key, value in initial['survival']['selection'].items() if key != 'revision'}
        self.next_session()
        final = backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        self.store.put('instances/survival/receipts/current.json', backup.encoded(old_selection))
        with self.assertRaises(backup.BackupError):
            backup.load_current_receipt(self.store, 'survival', self.workspace, expected_receipt_id=final['survival']['receipt_id'])
        with self.assertRaisesRegex(backup.BackupError, 'durable local'):
            backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        self.assertEqual(json.loads((self.journal / 'current-survival.json').read_text()), final['survival']['selection'])

    def test_selection_failure_retains_recorded_candidate_and_retry_completes_same_receipt(self):
        self.store.fail_selection = True
        with self.assertRaises(backup.BackupError):
            backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        candidates = list(self.journal.glob('receipt-*.json'))
        self.assertEqual(len(candidates), 1)
        candidate = json.loads(candidates[0].read_text())
        self.assertFalse((self.journal / 'current-survival.json').exists())
        self.store.fail_selection = False
        final = backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        self.assertEqual(final['survival']['receipt_id'], candidate['receipt_id'])

    def test_missing_recorded_archive_version_does_not_substitute_current_bytes(self):
        receipt = self.receipt()
        del self.store.versions[(receipt['archive']['key'], receipt['archive']['version_id'])]
        self.store.put(receipt['archive']['key'], self.fixture.store.objects[self.fixture.archive_key])
        with self.assertRaisesRegex(backup.BackupError, 'version'):
            backup.load_receipt_recovery(self.store, receipt, self.workspace)

    def test_stop_wrapper_publishes_durable_private_files_and_is_idempotent(self):
        result = backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        receipt = result['survival']['receipt']
        current = self.journal / 'current-survival.json'
        saved = self.journal / f"receipt-{receipt['receipt_id']}.json"
        self.assertEqual(json.loads(saved.read_text()), receipt)
        self.assertEqual(json.loads(current.read_text()), result['survival']['selection'])
        self.assertEqual(current.stat().st_mode & 0o777, 0o600)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        versions = len(self.store.versions)
        self.assertEqual(backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store), result)
        self.assertEqual(len(self.store.versions), versions)
        failed = self.drain()
        failed['instances']['survival']['backup'] = 'failed'
        with self.assertRaises(backup.BackupError):
            backup.verify_stop_receipt(self.source, failed, self.journal, self.bootstrap, store=self.store)
        inconsistent = self.drain()
        inconsistent['recovery']['survival']['manifest_sha256'] = 'b' * 64
        with self.assertRaises(backup.BackupError):
            backup.verify_stop_receipt(self.source, inconsistent, self.journal, self.bootstrap, store=self.store)
        self.assertEqual(json.loads(current.read_text()), result['survival']['selection'])

    def test_stop_wrapper_resumes_same_recorded_candidate_after_selection_commit_interruption(self):
        backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        self.next_session()
        from minecraft_instance import write_json
        def fail_current(path, value):
            if path.name == 'current-survival.json':
                raise OSError('interruption after S3 commit')
            return write_json(path, value)
        with mock.patch('minecraft_instance.write_json', side_effect=fail_current), self.assertRaises(OSError):
            backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        result = backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store)
        self.assertEqual(result['survival']['receipt']['sequence'], 2)
        self.assertEqual(json.loads((self.journal / 'current-survival.json').read_text()), result['survival']['selection'])

    def test_restore_reads_recorded_versions_after_current_keys_change(self):
        receipt = self.receipt()
        self.store.put(self.fixture.marker_key, b'corrupt current marker')
        self.store.put(self.fixture.archive_key, b'corrupt current archive')
        result = self.restore(receipt)
        self.assertEqual(result['receipt_id'], receipt['receipt_id'])
        target = self.fixture.target_root / 'instances/survival'
        for name, content in self.fixture.checkpoints.items():
            self.assertEqual((target / name).read_bytes(), content)

    def test_fully_installed_same_receipt_resumes_without_rewriting_data(self):
        receipt = self.receipt()
        self.restore(receipt)
        target = self.fixture.target_root / 'instances/survival'
        before = {str(path.relative_to(target)): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in target.rglob('*') if path.is_file()}
        result = self.restore(receipt, resume=True)
        self.assertTrue(result['resumed'])
        after = {str(path.relative_to(target)): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in target.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_partial_wrong_receipt_changed_target_or_checkpoint_are_preserved_for_repair(self):
        for scenario in ('missing-file', 'changed-checkpoint', 'wrong-receipt', 'wrong-target', 'extra-file', 'symlink'):
            with self.subTest(scenario=scenario):
                self.setUp()
                receipt = self.receipt()
                self.restore(receipt)
                target = self.fixture.target_root / 'instances/survival'
                checkpoint = target / 'data/world/level.dat'
                if scenario == 'missing-file':
                    checkpoint.unlink()
                elif scenario == 'changed-checkpoint':
                    checkpoint.write_bytes(b'different world')
                elif scenario == 'extra-file':
                    (target / 'data/untracked').write_bytes(b'keep')
                elif scenario == 'symlink':
                    checkpoint.unlink()
                    checkpoint.symlink_to(target / 'data/ops.json')
                else:
                    state = json.loads((target / 'operation.json').read_text())
                    if scenario == 'wrong-receipt':
                        state['restore_receipt_id'] = 'b' * 64
                    else:
                        state['restore_target']['filesystem_uuid'] = '22222222-2222-4222-8222-222222222222'
                    (target / 'operation.json').write_text(json.dumps(state))
                before = [(str(path.relative_to(target)), path.lstat().st_mode, path.readlink() if path.is_symlink()
                           else path.read_bytes() if path.is_file() else None) for path in target.rglob('*')]
                with self.assertRaises(backup.BackupError):
                    self.restore(receipt, resume=True)
                after = [(str(path.relative_to(target)), path.lstat().st_mode, path.readlink() if path.is_symlink()
                          else path.read_bytes() if path.is_file() else None) for path in target.rglob('*')]
                self.assertEqual(before, after)

    def test_capacity_bootstrap_and_receipt_corruption_fail_without_installing_an_empty_world(self):
        receipt = self.receipt()
        with mock.patch.object(restore.os, 'statvfs', return_value=type('Capacity', (), {'f_frsize': 4096, 'f_bavail': 1})()), self.assertRaisesRegex(backup.BackupError, 'capacity'):
            self.restore(receipt)
        self.assertFalse((self.fixture.target_root / 'instances').exists())
        for field, value in (('receipt_id', 'b' * 64), ('bootstrap_sha256', 'b' * 64), ('required_capacity_bytes', 0),
                             ('sequence', True), ('previous_receipt_id', 'b' * 64)):
            bad = {**receipt, field: value}
            with self.subTest(field=field), self.assertRaises(backup.BackupError):
                self.restore(bad)
            self.assertFalse((self.fixture.target_root / 'instances').exists())
        self.bootstrap = 'b' * 64
        with self.assertRaisesRegex(backup.BackupError, 'bootstrap'):
            self.restore(receipt)

    def test_restore_accepts_exact_remaining_capacity_after_archive_download(self):
        receipt = self.receipt()
        remaining = receipt['required_capacity_bytes'] - receipt['archive']['size_bytes']
        def capacity_after_download(path):
            archives = list(self.fixture.target_root.glob('.minecraft-restore-*/recovery.tar.gz'))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].stat().st_size, receipt['archive']['size_bytes'])
            return type('Capacity', (), {'f_frsize': 1, 'f_bavail': remaining})()
        with mock.patch.object(restore.os, 'statvfs', side_effect=capacity_after_download):
            self.assertEqual(self.restore(receipt)['status'], 'restored')
        for name, content in self.fixture.checkpoints.items():
            self.assertEqual((self.fixture.target_root / 'instances/survival' / name).read_bytes(), content)

    def test_one_byte_below_remaining_capacity_preserves_target_and_backup(self):
        receipt = self.receipt()
        remaining = receipt['required_capacity_bytes'] - receipt['archive']['size_bytes'] - 1
        checkpoint = self.fixture.target_root / 'existing-volume-checkpoint'
        checkpoint.write_bytes(b'preserve this unrelated volume data')
        backup_before = dict(self.store.objects)
        with mock.patch.object(restore.os, 'statvfs', return_value=type('Capacity', (), {'f_frsize': 1, 'f_bavail': remaining})()), self.assertRaisesRegex(backup.BackupError, 'capacity'):
            self.restore(receipt)
        self.assertEqual(list(self.fixture.target_root.iterdir()), [checkpoint])
        self.assertEqual(checkpoint.read_bytes(), b'preserve this unrelated volume data')
        self.assertEqual(self.store.objects, backup_before)

    def test_resume_rejects_changed_normalized_owner_group_or_modes_without_repair(self):
        for changed in ('file-mode', 'directory-mode', 'owner', 'group'):
            with self.subTest(changed=changed):
                self.setUp()
                receipt = self.receipt()
                self.restore(receipt)
                target = self.fixture.target_root / 'instances/survival'
                checkpoint = target / 'data/world/level.dat'
                if changed == 'file-mode':
                    checkpoint.chmod(0o640)
                elif changed == 'directory-mode':
                    (target / 'data/world').chmod(0o755)
                else:
                    original = self.fixture.machine.restore_metadata
                    def changed_metadata(path):
                        info = original(path)
                        if path == checkpoint:
                            setattr(info, 'st_uid' if changed == 'owner' else 'st_gid', 2000)
                        return info
                    self.fixture.machine.restore_metadata = changed_metadata
                before = {str(path.relative_to(target)): (path.lstat().st_mode, path.read_bytes())
                          for path in target.rglob('*') if path.is_file()}
                with self.assertRaisesRegex(backup.BackupError, 'ownership or permissions'):
                    self.restore(receipt, resume=True)
                after = {str(path.relative_to(target)): (path.lstat().st_mode, path.read_bytes())
                         for path in target.rglob('*') if path.is_file()}
                self.assertEqual(before, after)

    def test_receipt_default_store_uses_explicit_bounded_budget_and_invalid_budget_fails_before_io(self):
        with mock.patch.object(backup, 'S3Store', return_value=self.store) as factory:
            backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, timeout=3000)
        factory.assert_called_once_with('source-backups', region='us-west-2', timeout=3000)
        for timeout in (0, 3001, True, '3000'):
            with self.subTest(timeout=timeout), mock.patch.object(backup, 'S3Store') as factory, self.assertRaises(backup.BackupError):
                backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, timeout=timeout)
            factory.assert_not_called()

    def test_injected_store_begins_a_fresh_configured_verification_operation(self):
        factory = mock.Mock(return_value=self.store)
        self.store.for_operation = factory
        backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store, timeout=2500)
        backup.verify_stop_receipt(self.source, self.drain(), self.journal, self.bootstrap, store=self.store, timeout=2500)
        self.assertEqual(factory.call_args_list, [mock.call(timeout=2500), mock.call(timeout=2500)])

    def test_multiple_instances_do_not_share_one_receipt_verification_deadline(self):
        metadata = copy.deepcopy(self.fixture.metadata)
        metadata['instance'] = 'creative'
        metadata['definition']['id'] = 'creative'
        metadata['definition_sha256'] = backup.manifest_digest(metadata['definition'])
        metadata['recovery_point'] = backup.point_identity('creative', metadata['session'], metadata['manifest_sha256'])
        metadata['archive']['key'] = f"instances/creative/objects/{metadata['recovery_point']}/data.tar.gz"
        self.store.put(metadata['archive']['key'], self.store.objects[self.fixture.archive_key])
        self.store.put(backup.marker_key('creative', metadata['recovery_point']), backup.encoded(metadata))
        source = copy.deepcopy(self.source)
        source['instances']['creative'] = metadata['definition']
        drain = self.drain()
        drain['instances']['creative'] = {'instance': 'creative', 'application': 'stopped', 'backup': 'complete'}
        drain['recovery']['creative'] = {field: metadata[field] for field in ('session', 'manifest_sha256', 'recovery_point')}
        factory = mock.Mock(return_value=self.store)
        self.store.for_operation = factory
        result = backup.verify_stop_receipt(source, drain, self.journal, self.bootstrap, store=self.store, timeout=3000)
        self.assertEqual(set(result), {'survival', 'creative'})
        self.assertEqual(factory.call_args_list, [mock.call(timeout=3000), mock.call(timeout=3000)])


if __name__ == '__main__':
    unittest.main()
