from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_backup_store as backup_store  # noqa: E402


FAKE_AWS = r'''
import base64, json, os, pathlib, sys, time
path = pathlib.Path(os.environ["TEST_AWS_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
operation = args[args.index("s3api") + 1]
step = state["steps"].pop(0)
if operation != step["operation"]:
    sys.stderr.write("unexpected AWS operation: " + operation)
    sys.exit(99)
record = {"args": args, "environment": {key: os.environ.get(key) for key in ("AWS_PAGER", "AWS_CLI_AUTO_PROMPT")}}
if "--body" in args:
    record["body"] = base64.b64encode(pathlib.Path(args[args.index("--body") + 1]).read_bytes()).decode()
if "--multipart-upload" in args:
    record["multipart"] = json.loads(pathlib.Path(args[args.index("--multipart-upload") + 1].removeprefix("file://")).read_text())
state["calls"].append(record)
path.write_text(json.dumps(state))
if "download" in step:
    pathlib.Path(args[-1]).write_bytes(step["download"].encode())
if "sleep" in step:
    time.sleep(step["sleep"])
if "output_bytes" in step:
    sys.stdout.write("x" * step["output_bytes"])
else:
    sys.stdout.write(json.dumps(step.get("response", {})))
sys.stderr.write(step.get("error", ""))
sys.exit(step.get("exit", 0))
'''


class BackupStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        executable = self.directory / "aws"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_AWS)
        executable.chmod(0o700)
        self.state = self.directory / "aws-state.json"
        self.environment = mock.patch.dict(os.environ, {
            "PATH": str(self.directory) + os.pathsep + os.environ["PATH"],
            "TEST_AWS_STATE": str(self.state),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = backup_store.S3Store("family-backups", region="us-west-2")
        self.source = self.directory / "source"
        self.source.write_bytes(b"abc")

    def steps(self, *steps):
        self.state.write_text(json.dumps({"steps": steps, "calls": []}))

    def calls(self):
        return json.loads(self.state.read_text())["calls"]

    def test_small_upload_is_immutable_and_checksums_the_actual_bytes(self):
        self.steps({"operation": "put-object"})
        self.store.upload("survival/one/archive.tar.gz", self.source)
        call = self.calls()[0]
        args = call["args"]
        self.assertEqual(args[args.index("--if-none-match") + 1], "*")
        self.assertEqual(args[args.index("--checksum-sha256") + 1], "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=")
        self.assertEqual(base64.b64decode(call["body"]), b"abc")
        self.assertEqual(args[args.index("--bucket") + 1], "family-backups")
        self.assertEqual(args[args.index("--region") + 1], "us-west-2")
        self.assertEqual(call["environment"], {"AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off"})

    def test_workstation_operations_pin_the_reviewed_profile(self):
        self.store = backup_store.S3Store('family-backups', region='us-west-2', profile='workloads-admin')
        self.steps({'operation': 'put-object'})
        self.store.upload('instances/survival/receipts/one.json', self.source)
        args = self.calls()[0]['args']
        self.assertEqual(args[args.index('--profile') + 1], 'workloads-admin')
        for profile in ('', '--debug', 'secret\nvalue', True):
            with self.subTest(profile=profile), self.assertRaises(backup_store.BackupError):
                backup_store.S3Store('family-backups', profile=profile)

    def test_temporary_operator_environment_is_requested_for_each_call_without_profile(self):
        environment = dict(os.environ, AWS_ACCESS_KEY_ID='fixture-temporary-key')
        provider = mock.Mock(return_value=environment)
        self.store = backup_store.S3Store('family-backups', environment_provider=provider)
        self.steps({'operation': 'put-object'}, {'operation': 'delete-object'})
        self.store.upload('instances/survival/one', self.source)
        self.store.delete('instances/survival/old')
        self.assertEqual(provider.call_count, 2)
        self.assertTrue(all('--profile' not in call['args'] for call in self.calls()))
        with self.assertRaises(backup_store.BackupError):
            backup_store.S3Store('family-backups', profile='source-admin', environment_provider=provider)
        self.store = backup_store.S3Store('family-backups', environment_provider=mock.Mock(side_effect=ValueError('fixture-secret')))
        with self.assertRaises(backup_store.BackupError) as caught:
            self.store.delete('instances/survival/old')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_reviewed_absolute_executable_is_used_after_environment_refresh_for_each_command(self):
        pinned = self.directory / 'reviewed-aws'
        pinned.write_bytes((self.directory / 'aws').read_bytes())
        pinned.chmod(0o700)
        (self.directory / 'aws').write_text(f'#!{sys.executable}\nraise SystemExit(99)\n')
        order = []
        def environment():
            order.append('environment')
            return dict(os.environ)
        def executable():
            order.append('executable')
            return str(pinned)
        self.store = backup_store.S3Store('family-backups', environment_provider=environment, executable_provider=executable)
        self.steps({'operation': 'put-object'}, {'operation': 'delete-object'})
        self.store.upload('instances/survival/one', self.source)
        self.store.delete('instances/survival/old')
        self.assertEqual(order, ['environment', 'executable', 'environment', 'executable'])
        self.assertEqual(len(self.calls()), 2)

    def test_rejected_or_relative_executable_never_spawns_an_aws_process(self):
        self.steps({'operation': 'delete-object'})
        providers = [mock.Mock(side_effect=ValueError('fixture-secret')), lambda: 'aws', lambda: None]
        for provider in providers:
            with self.subTest(provider=provider), mock.patch.object(backup_store.subprocess, 'Popen') as spawn:
                self.store = backup_store.S3Store('family-backups', executable_provider=provider)
                with self.assertRaisesRegex(backup_store.BackupError, 'executable') as caught:
                    self.store.delete('instances/survival/old')
                self.assertNotIn('fixture-secret', str(caught.exception))
                spawn.assert_not_called()
        self.assertEqual(self.calls(), [])
        with self.assertRaises(backup_store.BackupError):
            backup_store.S3Store('family-backups', executable_provider='aws')

    def test_credential_refresh_replacing_pinned_executable_is_rechecked_before_outer_s3_command(self):
        pinned = self.directory / 'aws'
        original = hashlib.sha256(pinned.read_bytes()).hexdigest()
        executed = self.directory / 'replaced-executable-ran'
        order = []
        def environment():
            order.append('environment')
            replacement = self.directory / 'replacement'
            replacement.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(executed)!r}).touch()\n')
            replacement.chmod(0o700)
            replacement.replace(pinned)
            return dict(os.environ)
        def executable():
            order.append('executable')
            if hashlib.sha256(pinned.read_bytes()).hexdigest() != original:
                raise ValueError('pinned executable changed during credential refresh')
            return str(pinned)
        self.steps({'operation': 'delete-object'})
        self.store = backup_store.S3Store('family-backups', environment_provider=environment, executable_provider=executable)
        with mock.patch.object(backup_store.subprocess, 'Popen') as spawn, self.assertRaisesRegex(backup_store.BackupError, 'changed'):
            self.store.delete('instances/survival/old')
        spawn.assert_not_called()
        self.assertEqual(order, ['environment', 'executable'])
        self.assertFalse(executed.exists())
        self.assertEqual(self.calls(), [])

    def test_download_checks_head_size_and_pins_etag_before_fetching_bytes(self):
        self.steps(
            {"operation": "head-object", "response": {"ContentLength": 3, "ETag": '\"revision-one\"'}},
            {"operation": "get-object", "download": "abc", "response": {"ContentLength": 3}},
        )
        target = self.directory / "download"
        self.assertTrue(self.store.download("survival/one/archive.tar.gz", target, max_bytes=3))
        self.assertEqual(target.read_bytes(), b"abc")
        args = self.calls()[1]["args"]
        self.assertEqual(args[args.index("--if-match") + 1], '\"revision-one\"')

    def test_versioned_reads_pin_current_or_explicit_version_and_validate_get_identity(self):
        for supplied in (None, 'version-one'):
            with self.subTest(version=supplied):
                metadata = {'ContentLength': 3, 'ETag': '"revision-one"', 'VersionId': 'version-one'}
                self.steps({'operation': 'head-object', 'response': metadata},
                           {'operation': 'get-object', 'download': 'abc', 'response': metadata})
                result = self.store.download_revision('instances/survival/completed/one.json',
                    self.directory / 'versioned', max_bytes=3, version_id=supplied)
                self.assertEqual(result['version_id'], 'version-one')
                for index, call in enumerate(self.calls()):
                    if index or supplied:
                        args = call['args']
                        self.assertEqual(args[args.index('--version-id') + 1], 'version-one')

    def test_versioned_read_never_substitutes_missing_or_unversioned_objects(self):
        self.steps({'operation': 'head-object', 'exit': 254,
                    'error': 'An error occurred (NoSuchVersion) when calling the HeadObject operation: absent'})
        self.assertIsNone(self.store.download_revision('one', self.directory / 'versioned', max_bytes=3, version_id='old'))
        self.assertEqual(len(self.calls()), 1)
        for version in (None, 'null'):
            self.steps({'operation': 'head-object', 'response': {'ContentLength': 3, 'ETag': 'one', 'VersionId': version}})
            with self.assertRaises(backup_store.BackupError):
                self.store.download_revision('one', self.directory / 'versioned', max_bytes=3)

    def test_changed_get_version_preserves_previous_local_bytes(self):
        self.steps({'operation': 'head-object', 'response': {'ContentLength': 3, 'ETag': 'one', 'VersionId': 'old'}},
                   {'operation': 'get-object', 'download': 'abc', 'response': {'ContentLength': 3, 'ETag': 'one', 'VersionId': 'new'}})
        target = self.directory / 'versioned'
        target.write_bytes(b'keep')
        with self.assertRaises(backup_store.BackupError):
            self.store.download_revision('one', target, max_bytes=3)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_selection_publication_uses_conditional_write_and_requires_versioning(self):
        for expected in (None, '"prior"'):
            self.steps({'operation': 'put-object', 'response': {'ETag': '"next"', 'VersionId': 'next-version'}})
            result = self.store.conditional_write('instances/survival/receipts/current.json', self.source, expected_etag=expected)
            args = self.calls()[0]['args']
            condition = '--if-match' if expected else '--if-none-match'
            self.assertEqual(args[args.index(condition) + 1], expected or '*')
            self.assertEqual(result['version_id'], 'next-version')
        self.steps({'operation': 'put-object', 'response': {'ETag': '"next"', 'VersionId': 'null'}})
        with self.assertRaises(backup_store.BackupError):
            self.store.conditional_write('current', self.source, expected_etag=None)

    def test_oversize_or_unknown_length_is_rejected_without_a_download(self):
        for size in (4, -1, True, "3", None):
            with self.subTest(size=size):
                self.steps({"operation": "head-object", "response": {"ContentLength": size, "ETag": '\"one\"'}})
                target = self.directory / "download"
                with self.assertRaises(backup_store.BackupError):
                    self.store.download("survival/one/archive.tar.gz", target, max_bytes=3)
                self.assertFalse(target.exists())
                self.assertEqual(len(self.calls()), 1)

    def test_download_rejects_partial_or_changed_bytes_and_preserves_existing_target(self):
        for contents in ("ab", "abcd"):
            with self.subTest(contents=contents):
                self.steps(
                    {"operation": "head-object", "response": {"ContentLength": 3, "ETag": '\"one\"'}},
                    {"operation": "get-object", "download": contents, "response": {"ContentLength": len(contents)}},
                )
                target = self.directory / "download"
                target.write_bytes(b"previous")
                with self.assertRaises(backup_store.BackupError):
                    self.store.download("survival/one/archive.tar.gz", target, max_bytes=3)
                self.assertEqual(target.read_bytes(), b"previous")

    def test_only_definitive_missing_object_is_reported_as_absent(self):
        for code in ("404", "NoSuchKey", "AccessDenied", "ExpiredToken", "NoSuchBucket", "500"):
            with self.subTest(code=code):
                steps = [{"operation": "head-object", "exit": 254,
                          "error": f"An error occurred ({code}) when calling the HeadObject operation: secret-token"}]
                if code == "404":
                    steps.append({"operation": "list-objects-v2", "response": {"IsTruncated": False}})
                self.steps(*steps)
                target = self.directory / "download"
                if code in ("404", "NoSuchKey"):
                    self.assertFalse(self.store.download("survival/one/metadata.json", target, max_bytes=16))
                else:
                    with self.assertRaises(backup_store.BackupError) as caught:
                        self.store.download("survival/one/metadata.json", target, max_bytes=16)
                    self.assertNotIn("secret-token", str(caught.exception))
                self.assertFalse(target.exists())

    def test_listing_follows_continuation_pages_with_an_instance_prefix(self):
        self.steps(
            {"operation": "list-objects-v2", "response": {"Contents": [{"Key": "survival/one", "LastModified": "2026-09-08T11:00:00+01:00"}], "IsTruncated": True, "NextContinuationToken": "page-two"}},
            {"operation": "list-objects-v2", "response": {"Contents": [{"Key": "survival/two", "LastModified": "2026-09-08T11:00:00Z"}], "IsTruncated": False}},
        )
        self.assertEqual(self.store.list_objects("survival/"), [
            {"key": "survival/one", "last_modified": "2026-09-08T10:00:00Z"},
            {"key": "survival/two", "last_modified": "2026-09-08T11:00:00Z"},
        ])
        for call in self.calls():
            args = call["args"]
            self.assertEqual(args[args.index("--prefix") + 1], "survival/")
            self.assertIn("--no-paginate", args)
        args = self.calls()[1]["args"]
        self.assertEqual(args[args.index("--continuation-token") + 1], "page-two")

    def test_authority_history_probe_detects_versions_delete_markers_and_rejects_ambiguity(self):
        key = 'instances/survival/receipts/current.json'
        for field in ('Versions', 'DeleteMarkers'):
            self.steps({'operation': 'list-object-versions', 'response': {'IsTruncated': False,
                        field: [{'Key': key, 'VersionId': 'prior'}]}})
            self.assertTrue(self.store.has_version_history(key))
            args = self.calls()[0]['args']
            self.assertEqual(args[args.index('--prefix') + 1], key)
            self.assertEqual(args[args.index('--max-keys') + 1], '1')
        self.steps({'operation': 'list-object-versions', 'response': {'IsTruncated': False}})
        self.assertFalse(self.store.has_version_history(key))
        for page in ({}, {'IsTruncated': True}, {'IsTruncated': False, 'Versions': [{'Key': 'foreign', 'VersionId': 'prior'}]}):
            self.steps({'operation': 'list-object-versions', 'response': page})
            with self.assertRaises(backup_store.BackupError):
                self.store.has_version_history(key)

    def test_listing_metadata_fails_closed_on_missing_or_invalid_last_modified(self):
        for value in (None, "not-a-timestamp", 123):
            with self.subTest(value=value):
                item = {"Key": "survival/one"}
                if value is not None:
                    item["LastModified"] = value
                self.steps({"operation": "list-objects-v2", "response": {
                    "Contents": [item], "IsTruncated": False}})
                with self.assertRaises(backup_store.BackupError):
                    self.store.list_objects("survival/")

    def test_listing_fails_closed_on_missing_token_and_out_of_prefix_keys(self):
        for response in (
            {"IsTruncated": True},
            {"IsTruncated": False, "Contents": [{"Key": "creative/one"}]},
            {"IsTruncated": "false"},
            {"IsTruncated": False, "Contents": [None]},
        ):
            with self.subTest(response=response):
                self.steps({"operation": "list-objects-v2", "response": response})
                with self.assertRaises(backup_store.BackupError):
                    self.store.keys("survival/")

    def test_delete_is_scoped_to_the_exact_selected_object(self):
        self.steps({"operation": "delete-object"})
        self.store.delete("survival/old/archive.tar.gz")
        args = self.calls()[0]["args"]
        self.assertEqual(args[args.index("--key") + 1], "survival/old/archive.tar.gz")

    def test_version_inventory_preserves_exact_ids_and_paginates_both_markers(self):
        key = 'instances/survival/completed/point.json'
        self.steps({'operation': 'list-object-versions', 'response': {'IsTruncated': True,
            'Versions': [{'Key': key, 'VersionId': 'one', 'IsLatest': True, 'LastModified': '2026-10-03T12:00:00Z'}],
            'NextKeyMarker': key, 'NextVersionIdMarker': 'one'}},
            {'operation': 'list-object-versions', 'response': {'IsTruncated': False,
             'DeleteMarkers': [{'Key': key, 'VersionId': 'older-delete', 'IsLatest': False, 'LastModified': '2026-10-02T12:00:00Z'}]}})
        result = self.store.list_versions('instances/survival/')
        self.assertEqual([item['version_id'] for item in result], ['one', 'older-delete'])
        self.assertTrue(result[1]['delete_marker'])
        args = self.calls()[1]['args']
        self.assertEqual(args[args.index('--key-marker') + 1], key)
        self.assertEqual(args[args.index('--version-id-marker') + 1], 'one')

    def test_lifecycle_read_is_bounded_scoped_and_never_substitutes_missing_access(self):
        self.steps({'operation': 'get-bucket-lifecycle-configuration', 'response': {'Rules': []}})
        self.assertEqual(self.store.lifecycle_configuration(), {'Rules': []})
        self.assertEqual(self.calls()[0]['args'][self.calls()[0]['args'].index('--bucket') + 1], 'family-backups')
        self.steps({'operation': 'get-bucket-lifecycle-configuration', 'exit': 254,
                    'error': 'An error occurred (AccessDenied) when calling the GetBucketLifecycleConfiguration operation: secret'})
        with self.assertRaises(backup_store.BackupError) as caught:
            self.store.lifecycle_configuration()
        self.assertNotIn('secret', str(caught.exception))

    def test_incomplete_duplicate_or_foreign_version_inventory_is_rejected(self):
        item = {'Key': 'instances/survival/objects/one', 'VersionId': 'one', 'IsLatest': True, 'LastModified': '2026-10-03T12:00:00Z'}
        for page in ({'IsTruncated': True}, {'IsTruncated': False, 'Versions': [item, item]},
                     {'IsTruncated': False, 'Versions': [{**item, 'Key': 'instances/other/one'}]},
                     {'IsTruncated': False, 'Versions': [{**item, 'VersionId': 'null'}]}):
            self.steps({'operation': 'list-object-versions', 'response': page})
            with self.assertRaises(backup_store.BackupError):
                self.store.list_versions('instances/survival/')

    def test_retention_changes_exact_version_and_requires_readback(self):
        self.steps({'operation': 'get-object-tagging', 'response': {'VersionId': 'one', 'TagSet': []}},
                   {'operation': 'put-object-tagging', 'response': {'VersionId': 'one'}},
                   {'operation': 'get-object-tagging', 'response': {'VersionId': 'one', 'TagSet': [{'Key': 'minecraft-retention', 'Value': 'keep'}]}})
        self.store.set_retention('instances/survival/objects/one', 'one', 'keep')
        for call in self.calls():
            args = call['args']
            self.assertEqual(args[args.index('--version-id') + 1], 'one')
        self.steps({'operation': 'get-object-tagging', 'response': {'VersionId': 'one', 'TagSet': [{'Key': 'foreign-owner', 'Value': 'keep'}]}})
        with self.assertRaises(backup_store.BackupError):
            self.store.set_retention('instances/survival/objects/one', 'one', 'retired')
        self.assertEqual(len(self.calls()), 1)
        self.steps({'operation': 'get-object-tagging', 'response': {'VersionId': 'one', 'TagSet': []}},
                   {'operation': 'put-object-tagging', 'response': {'VersionId': 'one'}},
                   {'operation': 'get-object-tagging', 'response': {'VersionId': 'one', 'TagSet': []}})
        with self.assertRaisesRegex(backup_store.BackupError, 'readback'):
            self.store.set_retention('instances/survival/objects/one', 'one', 'keep')

    def test_large_upload_uses_checksum_parts_and_conditional_completion(self):
        self.source.write_bytes(b"abcabc")
        checksum = "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0="
        self.steps(
            {"operation": "create-multipart-upload", "response": {"UploadId": "upload-one"}},
            {"operation": "upload-part", "response": {"ETag": "part-one", "ChecksumSHA256": checksum}},
            {"operation": "upload-part", "response": {"ETag": "part-two", "ChecksumSHA256": checksum}},
            {"operation": "complete-multipart-upload", "response": {"ETag": "completed"}},
        )
        with mock.patch.object(backup_store, "PART_BYTES", 3):
            self.store.upload("survival/one/archive.tar.gz", self.source)
        calls = self.calls()
        self.assertEqual([base64.b64decode(call["body"]) for call in calls if "body" in call], [b"abc", b"abc"])
        args = calls[-1]["args"]
        self.assertEqual(args[args.index("--if-none-match") + 1], "*")
        self.assertEqual(calls[-1]["multipart"], {"Parts": [
            {"PartNumber": 1, "ETag": "part-one", "ChecksumSHA256": checksum},
            {"PartNumber": 2, "ETag": "part-two", "ChecksumSHA256": checksum},
        ]})

    def test_failed_part_aborts_without_publishing_the_archive(self):
        self.source.write_bytes(b"abcabc")
        self.steps(
            {"operation": "create-multipart-upload", "response": {"UploadId": "upload-one"}},
            {"operation": "upload-part", "exit": 254, "error": "An error occurred (ExpiredToken) when calling the UploadPart operation: secret-token"},
            {"operation": "abort-multipart-upload"},
        )
        with mock.patch.object(backup_store, "PART_BYTES", 3), self.assertRaises(backup_store.BackupError) as caught:
            self.store.upload("survival/one/archive.tar.gz", self.source)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(self.calls()[-1]["args"][1], "abort-multipart-upload")
        self.assertFalse(json.loads(self.state.read_text())["steps"])

    def test_existing_object_is_preserved_but_conflict_and_authentication_fail(self):
        for code in ("PreconditionFailed", "412", "ConditionalRequestConflict", "AccessDenied"):
            with self.subTest(code=code):
                self.steps({"operation": "put-object", "exit": 254,
                            "error": f"An error occurred ({code}) when calling the PutObject operation: secret-token"})
                if code in ("PreconditionFailed", "412"):
                    self.store.upload("survival/one/archive.tar.gz", self.source)
                else:
                    with self.assertRaises(backup_store.BackupError):
                        self.store.upload("survival/one/archive.tar.gz", self.source)
                self.assertEqual(len(self.calls()), 1)

    def test_missing_bucket_is_not_confused_with_a_missing_object(self):
        self.steps(
            {"operation": "head-object", "exit": 254, "error": "An error occurred (404) when calling the HeadObject operation: Not Found"},
            {"operation": "list-objects-v2", "exit": 254, "error": "An error occurred (NoSuchBucket) when calling the ListObjectsV2 operation: missing"},
        )
        with self.assertRaises(backup_store.BackupError):
            self.store.download("survival/one/metadata.json", self.directory / "download", max_bytes=16)

    def test_multipart_completion_preserves_existing_objects_and_aborts_conflicts(self):
        checksum = "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0="
        self.source.write_bytes(b"abcabc")
        for code in ("PreconditionFailed", "ConditionalRequestConflict"):
            with self.subTest(code=code):
                self.steps(
                    {"operation": "create-multipart-upload", "response": {"UploadId": "upload-one"}},
                    {"operation": "upload-part", "response": {"ETag": "one", "ChecksumSHA256": checksum}},
                    {"operation": "upload-part", "response": {"ETag": "two", "ChecksumSHA256": checksum}},
                    {"operation": "complete-multipart-upload", "exit": 254, "error": f"An error occurred ({code}) when calling the CompleteMultipartUpload operation: failed"},
                    {"operation": "abort-multipart-upload"},
                )
                with mock.patch.object(backup_store, "PART_BYTES", 3):
                    if code == "PreconditionFailed":
                        self.store.upload("survival/one/archive.tar.gz", self.source)
                    else:
                        with self.assertRaises(backup_store.BackupError):
                            self.store.upload("survival/one/archive.tar.gz", self.source)
                self.assertFalse(json.loads(self.state.read_text())["steps"])

    def test_command_deadline_kills_a_stalled_cli_and_does_not_expose_its_output(self):
        self.steps({"operation": "list-objects-v2", "sleep": 30})
        with mock.patch.object(backup_store, "COMMAND_TIMEOUT", 0.2), self.assertRaisesRegex(backup_store.BackupError, "timed out"):
            self.store.keys("survival/")

    def test_command_output_is_bounded_before_json_parsing(self):
        self.steps({"operation": "list-objects-v2", "output_bytes": 100000})
        with mock.patch.object(backup_store, "MAX_OUTPUT_BYTES", 1024), self.assertRaisesRegex(backup_store.BackupError, "safety limit"):
            self.store.keys("survival/")

    def test_repeated_pagination_token_is_rejected(self):
        self.steps(
            {"operation": "list-objects-v2", "response": {"IsTruncated": True, "NextContinuationToken": "loop"}},
            {"operation": "list-objects-v2", "response": {"IsTruncated": True, "NextContinuationToken": "loop"}},
        )
        with self.assertRaisesRegex(backup_store.BackupError, "repeated"):
            self.store.keys("survival/")

    def test_operation_deadline_is_shared_across_pagination_calls(self):
        self.steps(
            {"operation": "list-objects-v2", "sleep": 0.6, "response": {"IsTruncated": True, "NextContinuationToken": "next"}},
            {"operation": "list-objects-v2", "sleep": 0.6, "response": {"IsTruncated": False}},
        )
        store = backup_store.S3Store("family-backups", timeout=1)
        with self.assertRaisesRegex(backup_store.BackupError, "timed out"):
            store.keys("survival/")

    def test_expired_operation_cannot_spawn_another_aws_process(self):
        self.steps({"operation": "delete-object"})
        with mock.patch.object(backup_store.time, "monotonic", side_effect=[100, 102]):
            store = backup_store.S3Store("family-backups", timeout=1)
            with self.assertRaisesRegex(backup_store.BackupError, "timed out"):
                store.delete("survival/old/archive.tar.gz")
        self.assertEqual(self.calls(), [])

    def test_invalid_operation_timeouts_are_rejected(self):
        for timeout in (0, -1, 3001, True, "900"):
            with self.subTest(timeout=timeout), self.assertRaises(backup_store.BackupError):
                backup_store.S3Store("family-backups", timeout=timeout)

    def test_new_operation_preserves_temporary_provider_and_gets_fresh_deadline(self):
        provider = mock.Mock(return_value=dict(os.environ))
        executable = mock.Mock(return_value=str(self.directory / 'aws'))
        with mock.patch.object(backup_store.time, 'monotonic', side_effect=[10, 1000]):
            initial = backup_store.S3Store('family-backups', region='us-west-2', environment_provider=provider,
                                          executable_provider=executable, timeout=900)
            later = initial.for_operation(timeout=3000)
        self.assertEqual(initial.deadline, 910)
        self.assertEqual(later.deadline, 4000)
        self.assertIs(later.environment_provider, provider)
        self.assertIs(later.executable_provider, executable)
        self.assertIsNone(later.profile)

    def test_archive_transfer_can_exceed_metadata_deadline_within_operation_budget(self):
        def elapsed_transfer():
            try:
                calls = json.loads(self.state.read_text())['calls']
                return elapsed_after_start if any(call['args'][1] == 'get-object' for call in calls) else 0
            except (OSError, ValueError):
                return 0
        for key, elapsed_after_start, succeeds in (('instances/survival/objects/point/data.tar.gz', 1000, True),
                                                   ('instances/survival/completed/point.json', 1000, False),
                                                   ('instances/survival/objects/point/data.tar.gz', 4000, False)):
            with self.subTest(key=key):
                self.steps({'operation': 'head-object', 'response': {'ContentLength': 3, 'ETag': 'one'}},
                           {'operation': 'get-object', 'download': 'abc', 'sleep': 0.05, 'response': {'ContentLength': 3}})
                target = self.directory / 'long-transfer'
                target.write_bytes(b'preserve')
                with mock.patch.object(backup_store.time, 'monotonic', side_effect=elapsed_transfer):
                    store = backup_store.S3Store('family-backups', timeout=3000)
                    if succeeds:
                        self.assertTrue(store.download(key, target, max_bytes=3))
                        self.assertEqual(target.read_bytes(), b'abc')
                    else:
                        with self.assertRaisesRegex(backup_store.BackupError, 'timed out'):
                            store.download(key, target, max_bytes=3)
                        self.assertEqual(target.read_bytes(), b'preserve')

    def test_a_part_checksum_mismatch_aborts_before_publication(self):
        self.source.write_bytes(b"abcabc")
        self.steps(
            {"operation": "create-multipart-upload", "response": {"UploadId": "upload-one"}},
            {"operation": "upload-part", "response": {"ETag": "one", "ChecksumSHA256": "incorrect"}},
            {"operation": "abort-multipart-upload"},
        )
        with mock.patch.object(backup_store, "PART_BYTES", 3), self.assertRaisesRegex(backup_store.BackupError, "verify"):
            self.store.upload("survival/one/archive.tar.gz", self.source)
        self.assertFalse(json.loads(self.state.read_text())["steps"])


if __name__ == "__main__":
    unittest.main()
