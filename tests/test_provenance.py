from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from minecraft_host import HostError
from minecraft_provenance import first_setup_command, moment, verify_fresh_volume

EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
DEPLOYMENT = {'account_id': '123456789012', 'region': 'us-west-2', 'availability_zone': 'us-west-2a',
              'host_id': 'i-' + '1' * 17, 'data_volume_id': 'vol-' + '2' * 17,
              'data_filesystem_uuid': '22222222-2222-4222-8222-222222222222', 'data_volume_size_gib': 2,
              'operator_role_arn': 'arn:aws:iam::123456789012:role/fixture-operator'}
JOURNAL = {'successful_apply_window': {'started_at_utc': EPOCH.isoformat(),
                                     'completed_at_utc': (EPOCH + dt.timedelta(seconds=30)).isoformat()},
           'source_storage': [{'volume_id': 'vol-' + '3' * 17,
                               'filesystem_uuid': '33333333-3333-4333-8333-333333333333'}],
           'receipt_ids': {'survival': 'a' * 64}}
FLAGS = {'storage_config_matches': True, 'first_boot_only': True, 'first_boot_matches_launch': True,
         'prior_storage_use_absent': True, 'runtime_absent': True, 'mount_absent': True}


def event(name, seconds, request, response, number):
    identity = f'00000000-0000-4000-8000-{number:012d}'
    when = (EPOCH + dt.timedelta(seconds=seconds)).isoformat()
    raw = {'eventID': identity, 'eventName': name, 'eventTime': when, 'awsRegion': 'us-west-2',
           'eventSource': 'ec2.amazonaws.com', 'recipientAccountId': DEPLOYMENT['account_id'],
           'userIdentity': {'type': 'AssumedRole', 'accountId': DEPLOYMENT['account_id'],
                            'sessionContext': {'sessionIssuer': {'arn': DEPLOYMENT['operator_role_arn']}}},
           'requestParameters': request, 'responseElements': response}
    return {'EventId': identity, 'EventName': name, 'EventTime': when, 'ReadOnly': 'false',
            'Username': 'fixture-creator', 'CloudTrailEvent': json.dumps(raw)}


class FakeAWS:
    def __init__(self):
        self.clock = 30
        self.calls = []
        self.volume_calls = 0
        self.volume = {'VolumeId': DEPLOYMENT['data_volume_id'], 'Encrypted': True, 'Size': 2,
                       'VolumeType': 'gp3', 'SnapshotId': '', 'State': 'in-use',
                       'AvailabilityZone': DEPLOYMENT['availability_zone'],
                       'CreateTime': (EPOCH + dt.timedelta(seconds=1)).isoformat(),
                       'Attachments': [{'InstanceId': DEPLOYMENT['host_id'], 'VolumeId': DEPLOYMENT['data_volume_id'],
                                        'State': 'attached', 'AttachTime': (EPOCH + dt.timedelta(seconds=10)).isoformat()}]}
        self.host = {'InstanceId': DEPLOYMENT['host_id'], 'State': {'Name': 'running'},
                     'Placement': {'AvailabilityZone': 'us-west-2a'}}
        self.create = event('CreateVolume', 1, {'size': '2', 'encrypted': True, 'zone': 'us-west-2a', 'volumeType': 'gp3'},
                            {'volumeId': DEPLOYMENT['data_volume_id'], 'size': '2', 'encrypted': True,
                             'zone': 'us-west-2a', 'volumeType': 'gp3', 'createTime': int((EPOCH + dt.timedelta(seconds=1)).timestamp() * 1000)}, 1)
        self.launch = event('RunInstances', 2, {},
                            {'instancesSet': {'items': [{'instanceId': DEPLOYMENT['host_id']}]}}, 2)
        self.attach = event('AttachVolume', 11, {'volumeId': DEPLOYMENT['data_volume_id'], 'instanceId': DEPLOYMENT['host_id']},
                            {'volumeId': DEPLOYMENT['data_volume_id'], 'instanceId': DEPLOYMENT['host_id'],
                             'attachTime': int((EPOCH + dt.timedelta(seconds=10.873)).timestamp() * 1000)}, 3)
        self.extra_volume = []
        self.host_flags = copy.deepcopy(FLAGS)
        self.delayed = 0
        self.change_attachment_at = None
        self.page_token = None

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def utcnow(self):
        return EPOCH + dt.timedelta(seconds=self.clock)

    def run(self, service, operation, *args, timeout):
        self.calls.append((service, operation, args, self.clock))
        if (service, operation) == ('sts', 'get-caller-identity'):
            return {'Account': DEPLOYMENT['account_id']}
        if (service, operation) == ('ec2', 'describe-volumes'):
            self.volume_calls += 1
            volume = copy.deepcopy(self.volume)
            if self.change_attachment_at and self.volume_calls >= self.change_attachment_at:
                volume['Attachments'][0]['AttachTime'] = (EPOCH + dt.timedelta(seconds=12)).isoformat()
            return {'Volumes': [volume]}
        if (service, operation) == ('ec2', 'describe-instances'):
            return {'Reservations': [{'Instances': [self.host]}]}
        if (service, operation) == ('cloudtrail', 'lookup-events'):
            if self.delayed:
                self.delayed -= 1
                return {'Events': []}
            identifier = args[1].split('AttributeValue=', 1)[1]
            rows = [self.create, self.attach, *self.extra_volume] if identifier == DEPLOYMENT['data_volume_id'] else [self.launch, self.attach]
            return {'Events': rows, **({'NextToken': self.page_token} if self.page_token else {})}
        if (service, operation) == ('ssm', 'send-command'):
            return {'Command': {'CommandId': '11111111-1111-4111-8111-111111111111'}}
        if (service, operation) == ('ssm', 'get-command-invocation'):
            return {'Status': 'Success', 'ResponseCode': 0, 'StandardOutputContent': json.dumps(self.host_flags),
                    'CommandId': '11111111-1111-4111-8111-111111111111', 'InstanceId': DEPLOYMENT['host_id']}
        raise AssertionError('unexpected operation: ' + operation)


class FreshVolumeBoundaryTests(unittest.TestCase):
    def test_waits_for_full_attachment_age_then_checks_actual_boundaries(self):
        aws = FakeAWS()
        result = verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)
        self.assertTrue(result['provenance_ready'])
        self.assertGreaterEqual(aws.clock, 910)
        checks = [when for service, _, _, when in aws.calls if service in ('cloudtrail', 'ssm')]
        self.assertTrue(checks)
        self.assertTrue(all(when >= 910 for when in checks))
        self.assertEqual(result['receipt_ids'], JOURNAL['receipt_ids'])
        allowed = {('sts', 'get-caller-identity'), ('ec2', 'describe-volumes'), ('ec2', 'describe-instances'),
                   ('cloudtrail', 'lookup-events'), ('ssm', 'send-command'), ('ssm', 'get-command-invocation')}
        self.assertTrue(all((service, operation) in allowed for service, operation, _, _ in aws.calls))

    def test_request_completion_next_second_does_not_replace_response_identity(self):
        aws = FakeAWS()
        self.assertTrue(verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)['provenance_ready'])
        raw = json.loads(aws.attach['CloudTrailEvent'])
        raw['responseElements']['attachTime'] += 1000
        aws.attach['CloudTrailEvent'] = json.dumps(raw)
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)

    def test_copied_snapshot_or_other_source_cannot_qualify(self):
        for field in ('SnapshotId', 'SourceVolumeId'):
            aws = FakeAWS()
            aws.volume[field] = 'fixture-source'
            with self.subTest(field=field), self.assertRaises(HostError):
                verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)
            self.assertFalse(any(service == 'ssm' for service, _, _, _ in aws.calls))

    def test_source_volume_and_filesystem_reuse_refuse_before_aws(self):
        for source_field, target_field in (('volume_id', 'data_volume_id'), ('filesystem_uuid', 'data_filesystem_uuid')):
            aws = FakeAWS()
            journal = copy.deepcopy(JOURNAL)
            journal['source_storage'][0][source_field] = DEPLOYMENT[target_field]
            with self.subTest(field=source_field), self.assertRaises(HostError):
                verify_fresh_volume(aws, DEPLOYMENT, journal)
            self.assertEqual(aws.calls, [])

    def test_unknown_apply_window_never_qualifies(self):
        aws = FakeAWS()
        journal = copy.deepcopy(JOURNAL)
        del journal['successful_apply_window']
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, journal)
        self.assertEqual(aws.calls, [])

    def test_other_volume_write_refuses_even_without_direct_volume_id_parameter(self):
        aws = FakeAWS()
        aws.extra_volume = [event('CreateTags', 20, {'resourcesSet': {'items': [{'resourceId': DEPLOYMENT['data_volume_id']}]}}, {}, 4)]
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)
        self.assertFalse(any(service == 'ssm' for service, _, _, _ in aws.calls))

    def test_changed_attachment_during_wait_refuses(self):
        aws = FakeAWS()
        aws.change_attachment_at = 2
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)

    def test_wrong_creator_or_response_host_cannot_qualify(self):
        for mutation in ('creator', 'role', 'host'):
            aws = FakeAWS()
            raw = json.loads(aws.attach['CloudTrailEvent'])
            if mutation == 'creator':
                aws.attach['Username'] = 'other-creator'
            elif mutation == 'role':
                raw['userIdentity']['sessionContext']['sessionIssuer']['arn'] = 'arn:aws:iam::123456789012:role/other'
            else:
                raw['responseElements']['instanceId'] = 'i-' + '4' * 17
            aws.attach['CloudTrailEvent'] = json.dumps(raw)
            with self.subTest(mutation=mutation), self.assertRaises(HostError):
                verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)

    def test_cloudtrail_size_encoding_is_exact_and_never_boolean_or_fractional(self):
        for value in (2, '2', True, 2.0, '2.0', '02', '3'):
            aws = FakeAWS()
            raw = json.loads(aws.create['CloudTrailEvent'])
            raw['requestParameters']['size'] = value
            raw['responseElements']['size'] = value
            aws.create['CloudTrailEvent'] = json.dumps(raw)
            with self.subTest(value=value):
                if type(value) is int or value == '2':
                    self.assertTrue(verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)['provenance_ready'])
                else:
                    with self.assertRaises(HostError):
                        verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)

    def test_delayed_expected_history_waits_but_timeout_preserves_disk(self):
        aws = FakeAWS()
        aws.delayed = 2
        self.assertTrue(verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)['provenance_ready'])
        self.assertGreaterEqual(aws.clock, 925)
        aws = FakeAWS()
        aws.delayed = 10000
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL, timeout=120)
        self.assertFalse(any(service == 'ssm' for service, _, _, _ in aws.calls))

    def test_incomplete_first_boot_evidence_refuses(self):
        aws = FakeAWS()
        aws.host_flags['prior_storage_use_absent'] = False
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)

    def test_pagination_cannot_repeat_or_duplicate_events(self):
        aws = FakeAWS()
        aws.page_token = 'repeat-token'
        with self.assertRaises(HostError):
            verify_fresh_volume(aws, DEPLOYMENT, JOURNAL)

    def test_documented_first_setup_script_compiles_and_checks_exact_storage(self):
        command = first_setup_command(DEPLOYMENT, moment((EPOCH + dt.timedelta(seconds=2)).isoformat()))
        result = subprocess.run(['bash', '-n'], input=command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        python = command.split("python3 - <<'VERIFY'\n", 1)[1].split('\nVERIFY\n', 1)[0]
        compile(python, '<first-setup>', 'exec')
        self.assertIn("fields['DATA_VOLUME_ID'] == expected['volume_id']", python)
        self.assertNotIn('zero-fill', command)
        self.assertNotIn("['mkfs", command)


if __name__ == '__main__':
    unittest.main()
