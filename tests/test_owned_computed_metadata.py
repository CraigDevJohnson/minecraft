import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import minecraft_on_demand as lifecycle
import minecraft_s3_session as s3
from minecraft_host import HostError
from test_on_demand_host import DEPLOYMENT, HOST_ATTRIBUTES, ATTACHMENT_ATTRIBUTES, state

ROLE = {'id': 'minecraft-host', 'name': 'minecraft-host', 'permissions_boundary': 'reviewed-boundary',
        'assume_role_policy': 'reviewed-trust'}
POLICIES = {'ssm': 'session-manager', 'backups': 'instance-backups', 'bootstrap[0]': 'read-pinned-bootstrap'}
DATA = {'id': DEPLOYMENT['data_volume_id'], 'encrypted': True, 'type': 'gp3', 'size': 5,
        'availability_zone': DEPLOYMENT['availability_zone'], 'iops': 3000, 'throughput': 125,
        'kms_key_id': 'recorded-key', 'snapshot_id': '', 'tags': {'Purpose': 'retained-instance-data'},
        'tags_all': {'Purpose': 'retained-instance-data', 'Project': 'minecraft'}}
BLOCK = {'delete_on_termination': False, 'device_name': '/dev/sdf', 'volume_id': DATA['id'],
         'encrypted': True, 'volume_type': 'gp3', 'volume_size': 5, 'iops': 3000, 'throughput': 125,
         'kms_key_id': DATA['kms_key_id'], 'snapshot_id': '', 'tags': DATA['tags_all'], 'tags_all': DATA['tags_all']}


def document(name):
    return json.dumps({'Version': '2012-10-17', 'Statement': [{'Sid': name, 'Effect': 'Allow',
                       'Action': ['controlled:Read'], 'Resource': 'controlled-resource'}]})


def fixture(*, present=True, session_data=False):
    result = state(present)
    for resource in result['resources']:
        value = resource['instances'][0]['attributes']
        if resource['type'] == 'aws_iam_role':
            value.clear();value.update(copy.deepcopy(ROLE), inline_policy=[{'name': name, 'policy': document(name)} for name in list(POLICIES.values())[:2]])
        elif resource['type'] == 'aws_ebs_volume':
            resource['name'] = 'session_data' if session_data else 'data'
            resource['instances'][0]['index_key'] = 0
            value.clear();value.update(copy.deepcopy(DATA))
        elif resource['type'] == 'aws_instance':value['ebs_block_device'] = []
        elif resource['type'] == 'aws_volume_attachment':value['device_name'] = '/dev/sdf'
    for key, name in POLICIES.items():
        indexed = key.endswith('[0]')
        result['resources'].append({'mode': 'managed', 'type': 'aws_iam_role_policy', 'name': key.removesuffix('[0]'),
            'instances': [{'attributes': {'id': 'owned-' + name, 'role': ROLE['name'], 'name': name, 'policy': document(name)},
                           **({'index_key': 0} if indexed else {})}]})
    return result


def refreshed(before, *, stopped=False):
    after = copy.deepcopy(before);after['serial'] += 1
    inventory = lifecycle.resources(after)
    inventory['aws_iam_role.host']['inline_policy'] = [{'name': name, 'policy': document(name)} for name in POLICIES.values()]
    if 'aws_instance.host[0]' in inventory:
        inventory['aws_instance.host[0]']['ebs_block_device'] = [copy.deepcopy(BLOCK)]
        if stopped:inventory['aws_instance.host[0]'].update(instance_state='stopped', public_ip='', public_dns='', associate_public_ip_address=False)
    return after


def removal_plan(before, after, *, session_data=False):
    old, new = lifecycle.resources(before), lifecycle.resources(after)
    return {'resource_drift': [{'address': address, 'change': {'actions': ['update'], 'before': old[address], 'after': new[address]}}
                              for address in ('aws_iam_role.host', 'aws_instance.host[0]')],
            'resource_changes': [{'address': address, 'mode': 'managed', 'change': {'actions': ['delete'], 'before': new[address], 'after': None}}
                                 for address in sorted(lifecycle.SESSION_ADDRESSES | ({s3.DATA_ADDRESS} if session_data else set()))]}


class OwnedComputedMetadataTests(unittest.TestCase):
    def test_both_exact_computed_drifts_and_stopped_metadata_authorize_normal_removal(self):
        before = fixture();after = refreshed(before, stopped=True)
        lifecycle.validate_plan(removal_plan(before, after), present=False, state=before, deployment=DEPLOYMENT, stopped_host=True)
        self.assertEqual(lifecycle.resources(before)['aws_instance.host[0]']['ebs_block_device'], [])

    def test_role_cache_refresh_during_genuinely_absent_host_creation(self):
        before = fixture(present=False);after = refreshed(before)
        old, new = lifecycle.resources(before), lifecycle.resources(after)
        plan = {'resource_drift': [{'address': 'aws_iam_role.host', 'change': {'actions': ['update'], 'before': old['aws_iam_role.host'], 'after': new['aws_iam_role.host']}}],
                'resource_changes': [{'address': 'aws_instance.host[0]', 'change': {'actions': ['create'], 'after': HOST_ATTRIBUTES}},
                                     {'address': 'aws_volume_attachment.data[0]', 'change': {'actions': ['create'], 'after': ATTACHMENT_ATTRIBUTES}}]}
        lifecycle.validate_plan(plan, present=True, state=before, deployment=DEPLOYMENT)

    def test_stale_owned_before_document_refreshes_only_to_actual_full_managed_documents(self):
        before = fixture();lifecycle.resources(before)['aws_iam_role.host']['inline_policy'][1]['policy'] = document('previous-approved-backups')
        after = refreshed(before, stopped=True)
        lifecycle.validate_plan(removal_plan(before, after), present=False, state=before, deployment=DEPLOYMENT, stopped_host=True)
        lifecycle.reconcile_state(before, after)

    def test_unknown_changed_missing_duplicate_and_foreign_inline_policy_are_refused(self):
        before = fixture();after = refreshed(before, stopped=True)
        for kind in ('unknown', 'changed', 'missing', 'duplicate', 'other-role', 'boundary', 'trust', 'cached-unknown'):
            old, new = copy.deepcopy(before), copy.deepcopy(after)
            role = lifecycle.resources(new)['aws_iam_role.host']
            if kind == 'unknown':role['inline_policy'].append({'name': 'unowned-policy', 'policy': document('unowned')})
            elif kind == 'changed':role['inline_policy'][0]['policy'] = document('unreviewed-change')
            elif kind == 'missing':role['inline_policy'].pop()
            elif kind == 'duplicate':role['inline_policy'].append(copy.deepcopy(role['inline_policy'][0]))
            elif kind == 'other-role':role['id'] = role['name'] = 'another-role'
            elif kind == 'boundary':role['permissions_boundary'] = 'another-boundary'
            elif kind == 'trust':role['assume_role_policy'] = 'another-trust'
            else:lifecycle.resources(old)['aws_iam_role.host']['inline_policy'][0]['name'] = 'unowned-cache'
            with self.subTest(kind=kind), self.assertRaises(HostError):
                lifecycle.validate_plan(removal_plan(old, new), present=False, state=old, deployment=DEPLOYMENT, stopped_host=True)

    def test_managed_policy_changes_and_unowned_managed_policy_cannot_hide_in_cache(self):
        before = fixture();after = refreshed(before)
        lifecycle.resources(after)['aws_iam_role_policy.backups']['policy'] = document('unreviewed-change')
        with self.assertRaises(HostError):lifecycle.reconcile_state(before, after)
        before = fixture();before['resources'].append({'mode': 'managed', 'type': 'aws_iam_role_policy', 'name': 'unowned',
            'instances': [{'attributes': {'name': 'unowned', 'role': ROLE['name'], 'policy': document('unowned')}}]})
        after = refreshed(before, stopped=True)
        with self.assertRaises(HostError):lifecycle.validate_plan(removal_plan(before, after), present=False, state=before, deployment=DEPLOYMENT, stopped_host=True)

    def test_every_recorded_data_block_attribute_and_root_configuration_remains_pinned(self):
        before = fixture();after = refreshed(before, stopped=True)
        for field in BLOCK:
            bad = copy.deepcopy(after);block = lifecycle.resources(bad)['aws_instance.host[0]']['ebs_block_device'][0]
            block[field] = not block[field] if isinstance(block[field], bool) else 'unreviewed-value'
            with self.subTest(field=field), self.assertRaises(HostError):
                lifecycle.validate_plan(removal_plan(before, bad), present=False, state=before, deployment=DEPLOYMENT, stopped_host=True)
        for kind in ('extra-volume', 'unknown-attribute', 'root-delete', 'unrecorded-attachment'):
            bad = copy.deepcopy(after);inventory = lifecycle.resources(bad);host = inventory['aws_instance.host[0]']
            if kind == 'extra-volume':host['ebs_block_device'].append(copy.deepcopy(BLOCK))
            elif kind == 'unknown-attribute':host['ebs_block_device'][0]['unreviewed'] = True
            elif kind == 'root-delete':host['root_block_device'][0]['delete_on_termination'] = False
            else:lifecycle.resources(before)['aws_volume_attachment.data[0]']['instance_id'] = 'another-host'
            with self.subTest(kind=kind), self.assertRaises(HostError):
                lifecycle.validate_plan(removal_plan(before, bad), present=False, state=before, deployment=DEPLOYMENT, stopped_host=True)

    def test_reconciliation_survives_applied_metadata_refresh_and_host_removal(self):
        before = fixture();after = refreshed(before)
        lifecycle.reconcile_state(before, after)
        after['resources'] = [row for row in after['resources'] if row['type'] not in ('aws_instance', 'aws_volume_attachment')]
        lifecycle.reconcile_state(before, after)
        lifecycle.verify_resumed_sessions({'initial_state': before, 'session_inventory': {}}, after)

    def test_original_and_recorded_session_resume_accept_exact_owned_ebs_refresh(self):
        before = fixture();after = refreshed(before, stopped=True)
        recorded = {key: value for key, value in lifecycle.resources(before).items() if key in lifecycle.SESSION_ADDRESSES}
        lifecycle.verify_resumed_sessions({'initial_state': before, 'host_stop_requested': True}, after)
        lifecycle.verify_resumed_sessions({'initial_state': before, 'session_inventory': recorded}, refreshed(before))
        with self.assertRaises(HostError):lifecycle.verify_resumed_sessions({'initial_state': before, 'session_inventory': {}}, after)
        bad = refreshed(before);lifecycle.resources(bad)['aws_instance.host[0]']['ebs_block_device'][0]['volume_id'] = 'another-volume'
        with self.assertRaises(HostError):lifecycle.verify_resumed_sessions({'initial_state': before, 'session_inventory': recorded}, bad)

    def test_cold_create_resume_reconciles_new_host_computed_block_from_its_recorded_attachment(self):
        initial = fixture(present=False)
        created = fixture();recorded = {key: value for key, value in lifecycle.resources(created).items() if key in lifecycle.SESSION_ADDRESSES}
        current = refreshed(created)
        journal = {'initial_state': initial, 'session_inventory': recorded,
                   'planned_changes': [{'address': 'aws_instance.host[0]', 'change': {'actions': ['create'],
                       'after': {**HOST_ATTRIBUTES, 'ebs_block_device': []}, 'after_unknown': {'ebs_block_device': True}}}]}
        lifecycle.reconcile_state(initial, current)
        lifecycle.verify_resumed_sessions(journal, current)

    def test_s3_plan_and_resume_keep_full_exact_session_data_ownership_context(self):
        before = fixture(session_data=True);after = refreshed(before, stopped=True)
        journal = {'initial_state': before, 'host_stop_requested': True, 'receipt_ids': {'survival': 'a' * 64}, 'receipts': {'survival': {'verified': True}}}
        s3.validate_s3_plan(removal_plan(before, after, session_data=True), present=False, state=before, deployment=DEPLOYMENT, journal=journal)
        s3.reconcile(before, after)
        s3.S3Session.verify_recorded_sessions(None, journal, after)
        journal['session_inventory'] = {key: value for key, value in lifecycle.resources(before).items() if key in s3.S3_ADDRESSES}
        s3.S3Session.verify_recorded_sessions(None, journal, refreshed(before))
        bad = copy.deepcopy(after);lifecycle.resources(bad)['aws_instance.host[0]']['ebs_block_device'][0]['delete_on_termination'] = True
        with self.assertRaises(HostError):s3.validate_s3_plan(removal_plan(before, bad, session_data=True), present=False, state=before, deployment=DEPLOYMENT, journal=journal)

    def test_s3_cold_create_resume_reconciles_only_its_owned_new_data_block(self):
        initial = fixture(present=False, session_data=True)
        initial['resources'] = [row for row in initial['resources'] if row['type'] != 'aws_ebs_volume']
        created = fixture(session_data=True);current = refreshed(created)
        journal = {'initial_state': initial, 'session_inventory': {key: value for key, value in lifecycle.resources(created).items() if key in s3.S3_ADDRESSES}}
        s3.reconcile(initial, current)
        s3.S3Session.verify_recorded_sessions(None, journal, current)
