"""Historical marker scan through the real bounded S3Store CLI boundary."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import minecraft_backup_store as backup_store
import minecraft_s3_session as session
from minecraft_host import HostError
import test_backup_store as aws_fixture
import test_restore as recovery_fixture


class FreshMarkerStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.aws = aws_fixture.BackupStoreTests()
        self.aws.setUp()
        self.addCleanup(self.aws.doCleanups)
        self.recovery = recovery_fixture.RestoreTests()
        self.recovery.setUp()
        self.addCleanup(self.recovery.doCleanups)
        self.store = backup_store.S3Store('source-backups', region='us-west-2')
        self.workspace = self.recovery.directory / 'marker-scan'
        self.workspace.mkdir(mode=0o700)
        self.key = self.recovery.marker_key
        self.target = 'vol-22222222222222222'

    def row(self, key, version, *, latest=True):
        return {'Key': key, 'VersionId': version, 'IsLatest': latest, 'LastModified': '2026-10-03T12:00:00Z'}

    def download(self, version, metadata=None):
        value = self.recovery.store.objects[self.key].decode() if metadata is None else json.dumps(metadata)
        response = {'ContentLength': len(value.encode()), 'ETag': '"exact-marker"', 'VersionId': version}
        return ({'operation': 'head-object', 'response': response},
                {'operation': 'get-object', 'download': value, 'response': response})

    def scan(self):
        return session.scan_fresh_marker_versions(self.store, ['survival'], self.target, self.workspace)

    def test_instance_root_inventory_filters_nonmarkers_and_reads_all_historical_marker_versions(self):
        self.aws.steps(
            {'operation': 'list-object-versions', 'response': {'IsTruncated': False, 'Versions': [
                self.row(self.key, 'current-marker'), self.row(self.key, 'prior-marker', latest=False),
                self.row('instances/survival/objects/' + 'a' * 64 + '/data.tar.gz', 'archive'),
                self.row('instances/survival/receipts/current.json', 'selection')],
                'DeleteMarkers': [self.row('instances/survival/completed/' + 'b' * 64 + '.json', 'deleted-marker')]}},
            *self.download('current-marker'), *self.download('prior-marker'))
        self.assertEqual(self.scan(), {'completed_marker_versions_checked': 2, 'target_volume_unused': True})
        calls = self.aws.calls()
        args = calls[0]['args']
        self.assertEqual(args[args.index('--prefix') + 1], 'instances/survival/')
        self.assertEqual(args[args.index('--max-keys') + 1], '1000')
        self.assertEqual(len(calls), 5)
        for call, version in zip(calls[1:], ('current-marker', 'current-marker', 'prior-marker', 'prior-marker')):
            args = call['args']
            self.assertEqual(args[args.index('--key') + 1], self.key)
            self.assertEqual(args[args.index('--version-id') + 1], version)

    def test_target_used_by_an_older_marker_version_refuses_first_setup(self):
        older = copy.deepcopy(self.recovery.metadata)
        older['source']['volume_id'] = self.target
        self.aws.steps(
            {'operation': 'list-object-versions', 'response': {'IsTruncated': False, 'Versions': [
                self.row(self.key, 'current-marker'), self.row(self.key, 'prior-marker', latest=False)]}},
            *self.download('current-marker'), *self.download('prior-marker', older))
        with self.assertRaisesRegex(HostError, 'already used'):
            self.scan()
        self.assertEqual(len(self.aws.calls()), 5)

    def test_malformed_completed_key_and_incomplete_inventory_refuse_before_marker_download(self):
        pages = [
            {'IsTruncated': False, 'Versions': [self.row('instances/survival/completed/not-a-point.json', 'invalid')]},
            {'IsTruncated': True, 'Versions': []},
            {'IsTruncated': False, 'Versions': [self.row('instances/creative/completed/' + 'a' * 64 + '.json', 'foreign')]},
        ]
        for page in pages:
            with self.subTest(page=page):
                self.aws.steps({'operation': 'list-object-versions', 'response': page})
                with self.assertRaises((HostError, backup_store.BackupError)):
                    self.scan()
                self.assertEqual(len(self.aws.calls()), 1)

    def test_unavailable_recorded_marker_version_never_substitutes_current(self):
        self.aws.steps(
            {'operation': 'list-object-versions', 'response': {'IsTruncated': False,
                'Versions': [self.row(self.key, 'missing-version', latest=False)]}},
            {'operation': 'head-object', 'exit': 1,
             'error': 'An error occurred (NoSuchVersion) when calling the HeadObject operation'})
        with self.assertRaisesRegex(HostError, 'unavailable'):
            self.scan()
        calls = self.aws.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn('missing-version', calls[1]['args'])


if __name__ == '__main__':
    unittest.main()
