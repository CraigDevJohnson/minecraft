"""Recovery-point behavior against a private in-memory S3 boundary."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from minecraft_content import manifest_digest
import minecraft_backup as backup


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.fail_upload = False
        self.fail_delete = False

    def keys(self, prefix):
        return sorted(key for key in self.objects if key.startswith(prefix))

    def download(self, key, target, *, max_bytes):
        if key not in self.objects:
            return False
        value = self.objects[key]
        if len(value) > max_bytes:
            raise backup.BackupError('object exceeds limit')
        target.write_bytes(value)
        return True

    def upload(self, key, source):
        if self.fail_upload:
            raise backup.BackupError('interrupted upload')
        self.objects.setdefault(key, source.read_bytes())

    def delete(self, key):
        if self.fail_delete:
            raise backup.BackupError('delete failed')
        self.objects.pop(key, None)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.instance = self.root / 'instances/survival'
        data = self.instance / 'data'
        checkpoints = {'world/level.dat': b'world checkpoint',
                       'world/playerdata/fixture.dat': b'inventory checkpoint',
                       'world_nether/DIM-1/region/r.0.0.mca': b'nether checkpoint',
                       'world_the_end/DIM1/region/r.0.0.mca': b'end checkpoint',
                       'whitelist.json': b'[]', 'ops.json': b'[]',
                       'server.properties': b'white-list=true\n',
                       'plugins/floodgate/key.pem': b'private fixture key',
                       'plugins/plugin-state.db': b'plugin checkpoint'}
        for name, content in checkpoints.items():
            path = data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        runtime = self.instance / 'runtime'
        runtime.mkdir()
        repo = Path(__file__).resolve().parents[1]
        manifest = json.loads((repo / 'runtime/content.json').read_text())
        definition = json.loads((repo / 'runtime/survival.json').read_text())
        (runtime / 'manifest.json').write_text(json.dumps(manifest))
        (runtime / 'definition.json').write_text(json.dumps(definition))
        self.request = {'schema_version': 1, 'instance': 'survival', 'session': 'session-1',
                        'data_path': str(data), 'manifest_path': str(runtime / 'manifest.json'),
                        'definition_path': str(runtime / 'definition.json'),
                        'manifest_sha256': manifest_digest(manifest),
                        'destination': {'bucket': 'fixture-private-bucket', 'prefix': 'instances/survival/'},
                        'source': {'volume_id': 'vol-0123456789abcdef0', 'filesystem_uuid': '11111111-1111-4111-8111-111111111111'}}
        self.record_session()
        self.store = MemoryStore()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)

    def record_session(self):
        (self.instance / 'operation.json').write_text(json.dumps({
            'phase': 'stopped', 'backup': 'pending', 'session': self.request['session'],
            'revision': self.request['manifest_sha256']}))

    def publish(self, **kwargs):
        return backup.publish(self.request, self.store, self.workspace, **kwargs)

    def test_clean_session_publishes_verified_complete_recovery_point(self):
        before = {str(p.relative_to(self.instance)): p.read_bytes()
                  for p in self.instance.rglob('*') if p.is_file()}
        receipt = self.publish(now='2026-09-08T10:00:00Z')
        self.assertEqual(receipt['status'], 'complete')
        status = backup.status(self.store, 'survival', self.workspace, now='2026-09-08T11:00:00Z')
        self.assertEqual(status['completion'], 'available')
        self.assertEqual(status['recovery_points'][0]['age_seconds'], 3600)
        metadata, archive = backup.load_recovery(self.store, 'survival', receipt['recovery_point'], self.workspace)
        self.assertEqual(metadata['manifest_sha256'], self.request['manifest_sha256'])
        self.assertIn('data/plugins/floodgate/key.pem', [item['path'] for item in metadata['files']])
        self.assertGreater(archive.stat().st_size, 0)
        self.assertEqual(before, {str(p.relative_to(self.instance)): p.read_bytes()
                                 for p in self.instance.rglob('*') if p.is_file()})

    def test_interrupted_upload_preserves_previous_point_and_retry_is_idempotent(self):
        first = self.publish(now='2026-09-08T10:00:00Z')
        previous = copy.deepcopy(self.store.objects)
        self.request['session'] = 'session-2'
        self.record_session()
        self.store.fail_upload = True
        with self.assertRaises(backup.BackupError):
            self.publish()
        self.assertEqual(previous, self.store.objects)
        self.store.fail_upload = False
        second = self.publish(now='2026-09-08T11:00:00Z')
        after = copy.deepcopy(self.store.objects)
        retry = self.publish(now='2026-09-09T11:00:00Z')
        self.assertEqual(second['recovery_point'], retry['recovery_point'])
        self.assertEqual(after, self.store.objects)
        self.assertNotEqual(first['recovery_point'], second['recovery_point'])
        self.assertEqual(len(self.store.keys('instances/survival/completed/')), 2)

    def test_archive_verification_failure_does_not_publish_completion(self):
        upload = self.store.upload
        def corrupt(key, source):
            upload(key, source)
            if key.endswith('.tar.gz'):
                self.store.objects[key] = b'interrupted bytes'
        self.store.upload = corrupt
        with self.assertRaises(backup.BackupError):
            self.publish()
        self.assertEqual(self.store.keys('instances/survival/completed/'), [])

    def test_receipt_loss_retries_verified_point_without_duplicate(self):
        upload = self.store.upload
        def lose_receipt(key, source):
            upload(key, source)
            if '/completed/' in key:
                raise backup.BackupError('completion response lost')
        self.store.upload = lose_receipt
        with self.assertRaises(backup.BackupError):
            self.publish()
        self.store.upload = upload
        result = self.publish()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.store.keys('instances/survival/completed/')), 1)

    def test_unclean_session_missing_required_files_and_links_cannot_publish(self):
        journal = self.instance / 'operation.json'
        journal.write_text(json.dumps({'phase': 'running', 'session': 'session-1'}))
        with self.assertRaises(backup.BackupError):
            self.publish()
        self.record_session()
        path = self.instance / 'data/world/level.dat'
        path.unlink()
        with self.assertRaises(backup.BackupError):
            self.publish()
        path.symlink_to(self.instance / 'data/server.properties')
        with self.assertRaises(backup.BackupError):
            self.publish()
        self.assertFalse(self.store.objects)

    def test_corrupt_missing_and_incompatible_points_are_unavailable(self):
        result = self.publish()
        good = copy.deepcopy(self.store.objects)
        marker = backup.marker_key('survival', result['recovery_point'])
        archive = next(key for key in good if key.endswith('.tar.gz'))
        for failure in ('metadata', 'archive', 'missing', 'version'):
            with self.subTest(failure=failure):
                self.store.objects = copy.deepcopy(good)
                if failure == 'metadata':
                    self.store.objects[marker] = b'invalid json'
                elif failure == 'archive':
                    self.store.objects[archive] = b'corrupt'
                elif failure == 'missing':
                    del self.store.objects[archive]
                else:
                    value = json.loads(self.store.objects[marker]); value['schema_version'] = 2
                    self.store.objects[marker] = json.dumps(value).encode()
                result = backup.status(self.store, 'survival', self.workspace)
                self.assertEqual(result['completion'], 'unavailable')
                self.assertEqual(result['invalid_points'], 1)

    def test_retention_keeps_fourteen_and_latest_after_long_inactivity(self):
        points = []
        for day in range(1, 17):
            self.request['session'] = f'session-{day}'
            self.record_session()
            points.append(self.publish(now=f'2026-08-{day:02d}T10:00:00Z')['recovery_point'])
        current = backup.status(self.store, 'survival', self.workspace, now='2027-08-16T10:00:00Z')
        retained = [point['recovery_point'] for point in current['recovery_points']]
        self.assertEqual(len(retained), 14)
        self.assertEqual(retained[0], points[-1])
        self.assertNotIn(points[0], retained)
        self.assertEqual(current['recovery_points'][0]['age_seconds'], 365 * 86400)

    def test_partial_pruning_is_visible_and_retries_orphan_deletion(self):
        for day in range(1, 15):
            self.request['session'] = f'session-{day}'
            self.record_session()
            self.publish(now=f'2026-08-{day:02d}T10:00:00Z')
        delete = self.store.delete
        def fail_archive(key):
            if key.endswith('.tar.gz'):
                raise backup.BackupError('archive deletion failed')
            delete(key)
        self.store.delete = fail_archive
        self.request['session'] = 'session-15'
        self.record_session()
        result = self.publish(now='2026-08-15T10:00:00Z')
        self.assertEqual(result['retention']['outcome'], 'pending')
        state = backup.status(self.store, 'survival', self.workspace)
        self.assertEqual(state['retention']['pending_deletions'], 1)
        self.store.delete = delete
        self.assertEqual(backup.prune(self.store, 'survival', self.workspace)['outcome'], 'complete')
        self.assertEqual(len([key for key in self.store.objects if key.endswith('.tar.gz')]), 14)

    def test_standalone_hook_refuses_capture_without_lifecycle_exclusion(self):
        from minecraft_instance import operation_lock
        lock = self.root / 'runtime.lock'
        with self.assertRaises(backup.BackupError):
            backup.require_controller_lock(lock)
        with operation_lock(lock):
            backup.require_controller_lock(lock)

    def test_prune_preserves_all_copies_when_catalog_integrity_is_uncertain(self):
        result = self.publish()
        marker = backup.marker_key('survival', result['recovery_point'])
        self.store.objects[marker] = b'corrupted metadata'
        before = copy.deepcopy(self.store.objects)
        result = backup.prune(self.store, 'survival', self.workspace)
        self.assertEqual(result['outcome'], 'pending')
        self.assertEqual(self.store.objects, before)

    def test_retention_listing_failure_keeps_verified_completion_receipt(self):
        def fail_listing(prefix):
            raise backup.BackupError('catalog listing unavailable')
        self.store.keys = fail_listing
        result = self.publish()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['retention']['outcome'], 'pending')
        metadata, _ = backup.load_recovery(self.store, 'survival', result['recovery_point'], self.workspace)
        self.assertEqual(metadata['status'], 'complete')
