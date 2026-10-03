from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_on_demand_host import DEPLOYMENT, HOST_ATTRIBUTES, ATTACHMENT_ATTRIBUTES, DIGEST, HOST_ID, VOLUME_ID, state, FakeAWS, FakeSession, pin_fixture_tools
import minecraft_host as host
import minecraft_on_demand as base
import minecraft_s3_session as s3

RECEIPT_ID = 'b' * 64
NEW_HOST = 'i-1123456789abcdef0'
NEW_VOLUME = 'vol-1123456789abcdef0'
DATA = {'id': VOLUME_ID, 'encrypted': True, 'type': 'gp3', 'size': 5, 'availability_zone': 'us-west-2a', 'snapshot_id': ''}
DRAIN = {'draining': True, 'instances': {'survival': {'instance': 'survival', 'application': 'stopped', 'backup': 'complete'}},
         'boot_id':'controlled-source-boot',
         'recovery': {'survival': {'session': 'source-session', 'manifest_sha256': DIGEST, 'recovery_point': 'c' * 64}}}
RECEIPT = {'boot_id':'controlled-source-boot', 'receipt_id': RECEIPT_ID, 'instance': 'survival', 'recovery_point': 'c' * 64, 'bootstrap_sha256': DIGEST,
           'required_capacity_bytes': 2 * 1024 ** 3, 'source': {'volume_id': VOLUME_ID, 'filesystem_uuid': DEPLOYMENT['data_filesystem_uuid'],
           'account_id': DEPLOYMENT['account_id'], 'region': DEPLOYMENT['region'], 'backup_bucket_name': DEPLOYMENT['backup_bucket_name']}}
RECEIPTS = {'survival': {'receipt_id': RECEIPT_ID, 'receipt': RECEIPT, 'selection': {'receipt_id': RECEIPT_ID, 'revision': {'version_id': 'selected-version'}}}}


def s3state(present=True):
    result = state(present)
    result['resources'] = [value for value in result['resources'] if value['type'] != 'aws_ebs_volume']
    if present:
        result['resources'].append({'mode': 'managed', 'type': 'aws_ebs_volume', 'name': 'session_data',
                                   'instances': [{'index_key': 0, 'attributes': copy.deepcopy(DATA)}]})
    return result


class S3AWS(FakeAWS):
    def run(self, service, operation, *arguments, timeout):
        if operation == 'describe-instances' and '--filters' in arguments:
            filters = json.loads(arguments[arguments.index('--filters') + 1])
            requested = next((value['Values'][0] for value in filters if value['Name'] == 'instance-id'), None)
            if requested:
                self.calls.append((operation, arguments))
                return {'Reservations': [{'Instances': [{'InstanceId': requested, 'State': {'Name': 'terminated'}}]}]}
        if operation == 'describe-volumes' and '--filters' in arguments:
            self.calls.append((operation, arguments))
            return {'Volumes': []}
        return super().run(service, operation, *arguments, timeout=timeout)


class S3Tofu:
    def __init__(self, present):
        self.current = s3state(present)
        self.output = {**DEPLOYMENT, 'lifecycle_mode': 's3-only', 'data_ownership': 'session', 'data_volume_size_gib': 5,
                       'recovery_receipt_ids': {'survival': RECEIPT_ID}, 'session_present': present,
                       'host_id': HOST_ID if present else None, 'data_volume_id': VOLUME_ID if present else None,
                       'data_filesystem_uuid': DEPLOYMENT['data_filesystem_uuid'] if present else None}
        self.applies = 0
        self.plans = []
        self.extra = None
        self.fail_apply = False
    def state(self): return copy.deepcopy(self.current)
    def deployment(self): return copy.deepcopy(self.output)
    def plan(self, path, present, *, variables):
        self.plans.append((present, copy.deepcopy(variables)))
        self.variables = variables
        self.present = present
        path.write_bytes(b'exact-s3-plan')
        inventory = base.resources(self.current)
        changes = []
        host_after = {**copy.deepcopy(HOST_ATTRIBUTES), 'id': NEW_HOST}
        attach_after = {**copy.deepcopy(ATTACHMENT_ATTRIBUTES), 'instance_id': NEW_HOST, 'volume_id': None}
        data_after = {**DATA, 'id': NEW_VOLUME, 'size': variables.get('data_volume_size_gib', 5)}
        for address, after in (('aws_instance.host[0]', host_after), ('aws_volume_attachment.data[0]', attach_after), (s3.DATA_ADDRESS, data_after)):
            if present and address not in inventory or not present and address in inventory:
                changes.append({'address': address, 'mode': 'managed', 'change': {'actions': ['create' if present else 'delete'],
                                'before': None if present else inventory[address], 'after': after if present else None,
                                'after_unknown': {'volume_id': True} if address == 'aws_volume_attachment.data[0]' and present else {}}})
        if self.extra: changes.append(self.extra)
        return {'resource_changes': changes}
    def apply(self, path):
        self.applies += 1
        if self.fail_apply: raise host.HostError('uncertain apply outcome')
        old = self.current['serial']
        self.current = s3state(self.present)
        self.current['serial'] = old + 1
        self.output.update(session_present=self.present, recovery_receipt_ids=self.variables['recovery_receipt_ids'])
        if self.present:
            for value in self.current['resources']:
                attrs = value['instances'][0]['attributes']
                if value['type'] == 'aws_instance': attrs['id'] = NEW_HOST
                elif value['type'] == 'aws_volume_attachment': attrs.update(instance_id=NEW_HOST, volume_id=NEW_VOLUME)
                elif value['type'] == 'aws_ebs_volume': attrs.update(id=NEW_VOLUME, size=self.variables['data_volume_size_gib'])
            self.output.update(host_id=NEW_HOST, data_volume_id=NEW_VOLUME, data_volume_size_gib=self.variables['data_volume_size_gib'],
                               data_filesystem_uuid=self.variables['data_filesystem_uuid'])
        else:
            self.output.update(host_id=None, data_volume_id=None, data_filesystem_uuid=None)


class S3Remote(FakeSession):
    failure = None
    def execute_shell(self, command, action):
        self.commands.append((action, command))
        if action == self.failure: raise host.HostError('failed controlled remote phase')
        if action == 'runtime-attestation': return {'runtime': 'installed', 'bootstrap_sha256': DIGEST}
        if action == 'private-input-verification': return {'private_inputs': 'verified'}
        if action == 'receipt-restore': return {'status': 'restored', 'receipt_id': RECEIPT_ID, 'application': 'stopped', 'instance': 'survival'}
        return {}
    def remote(self, action):
        self.commands.append((action, ''))
        return copy.deepcopy(DRAIN)


class S3SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.vars = self.root / 'variables.tfvars'; self.vars.write_text('pinned'); self.vars.chmod(0o600)
        self.operator = self.root / 'operator.json'; self.operator.write_text('{}'); self.operator.chmod(0o600)
        self.secret = self.root / 'rcon.secret'; self.secret.write_text('PRIVATE_SECRET_DO_NOT_PRINT'); self.secret.chmod(0o600)
        self.policy = {'aws_profile': 'workloads-admin', 'infra_root': str(self.root / 'infra'), 'variables_file': str(self.vars),
                       'variables_sha256': base.digest_file(self.vars), 'source_sha256': DIGEST, 'bootstrap_sha256': DIGEST,
                       'operator_file': str(self.operator), 'secret_file': str(self.secret), 'journal_dir': str(self.root / 'journal'),
                       'backend': {'bucket': 'private-state', 'key': 'state', 'region': 'us-west-2'},
                       'private_inputs_sha256': __import__('hashlib').sha256(self.operator.read_bytes()+b'\0'+self.secret.read_bytes()).hexdigest()}
        self.patch_source = mock.patch.object(base, 'source_digest', return_value=DIGEST); self.patch_source.start(); self.addCleanup(self.patch_source.stop)
        pin_fixture_tools(self.root,self.policy)
        (self.root/'scripts').mkdir()
        (self.root/'scripts/minecraft_bootstrap_release.py').write_bytes((ROOT/'scripts/minecraft_bootstrap_release.py').read_bytes())
        self.patch_remote = mock.patch.object(s3, 'HostSession', S3Remote); self.patch_remote.start(); self.addCleanup(self.patch_remote.stop)
        self.patch_running = mock.patch.object(base, 'HostSession', S3Remote); self.patch_running.start(); self.addCleanup(self.patch_running.stop)
        S3Remote.instances = []; S3Remote.failure = None; S3Remote.host_state_value = 'running'
        self.transfers = []; self.verifications = []; self.loads = []; self.proofs = []; self.marker_scans = []; self.retirements = []
    def create(self, present=True):
        self.tofu = S3Tofu(present)
        self.aws = S3AWS()
        self.aws.cli_path=self.policy['aws_cli_path']
        self.path = self.root / 'deployment.json'
        base.save_private(self.path, self.tofu.output)
        def verify(deployment, drain):
            self.verifications.append((copy.deepcopy(deployment), copy.deepcopy(drain)))
            return copy.deepcopy(RECEIPTS)
        def load(ids):
            self.loads.append(copy.deepcopy(ids))
            if ids != {'survival': RECEIPT_ID}: raise host.HostError('bad exact selection')
            return copy.deepcopy(RECEIPTS)
        def provenance(aws, deployment, journal, **kwargs):
            self.proofs.append((copy.deepcopy(deployment), copy.deepcopy(journal)))
            return {'provenance_ready': True, 'mandatory_wait_seconds': 900,
                    'volume_id': deployment['data_volume_id'], 'host_id': deployment['host_id'],
                    'filesystem_uuid': deployment['data_filesystem_uuid'], 'receipt_ids': journal['receipt_ids'],
                    'successful_apply_window': journal['successful_apply_window'],
                    'provenance_expires_at_utc': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()}
        def scan():
            self.marker_scans.append(True)
            return {'target_volume_unused': True, 'completed_marker_versions_checked': 14}
        return s3.S3Session(copy.deepcopy(self.tofu.output), self.aws, None, self.policy, self.path, tofu=self.tofu,
                            transfer=lambda remote,bundle,digest: self.transfers.append(json.loads(bundle)),
                            receipt_verifier=verify, receipt_loader=load, provenance=provenance, marker_scanner=scan,
                            retention=lambda journal: self.retirements.append(copy.deepcopy(journal['receipt_ids'])))
    def test_failed_retirement_preserves_active_disk_and_host_for_repair(self):
        session=self.create(True)
        session.retention=mock.Mock(side_effect=host.HostError('unknown historical version'))
        with self.assertRaises(host.HostError):session.stop()
        self.assertEqual(self.tofu.applies,0)
        self.assertFalse(any(action=='stop-instances' for remote in S3Remote.instances for action,_ in remote.commands))

    def test_private_input_certificate_failure_refuses_existing_start_and_receipt_capture(self):
        for action in ('start', 'stop'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child).resolve() / 'journal')
                session = self.create(True)
                S3Remote.failure = 'private-input-verification'
                with self.assertRaises(host.HostError):
                    session.start('survival') if action == 'start' else session.stop()
                self.assertEqual(self.tofu.applies, 0)
                self.assertEqual(self.verifications, [])
                self.assertFalse(any(name in ('start', 'host-drain-private', 'stop-instances')
                                     for remote in S3Remote.instances for name,_ in remote.commands))
                S3Remote.instances = []

    def test_invalid_local_private_bundle_never_creates_cold_target_or_journal(self):
        for operator in ('invalid operator JSON', '\ufeff{}'):
            with self.subTest(operator=operator):
                session = self.create(False)
                self.operator.write_text(operator)
                self.policy['private_inputs_sha256'] = base.private_inputs_digest(self.policy)
                with self.assertRaises(host.HostError): session.start('survival')
                self.assertEqual(self.tofu.applies, 0)
                self.assertEqual(self.tofu.plans, [])
                self.assertFalse(session.journal_path.exists())

    def test_conflicting_live_retention_policy_refuses_final_deletion(self):
        session = self.create(True)
        session.receipt_verifier = mock.Mock(side_effect=host.HostError('live lifecycle expires selected versions'))
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies,0)
        self.assertFalse(any(action == 'stop-instances' for remote in S3Remote.instances for action,_ in remote.commands))
        self.assertEqual(session.deployment['data_volume_id'], VOLUME_ID)

    def protected_loader(self, session):
        session.store_factory = mock.Mock(return_value=object())
        session.receipt_loader = session.load_receipts
        metadata = mock.patch('minecraft_backup.load_current_receipt', return_value=(copy.deepcopy(RECEIPT), copy.deepcopy(RECEIPTS['survival']['selection'])))
        protection = mock.patch('minecraft_retention.verify_current_selection_protection', return_value={'instance':'survival', 'receipt_id':RECEIPT_ID, 'protection':'verified'})
        self.addCleanup(metadata.stop)
        self.addCleanup(protection.stop)
        metadata.start()
        return protection.start()

    def test_interrupted_delete_requires_fresh_keep_tags_and_lifecycle_before_retry(self):
        session = self.create(True)
        protector = self.protected_loader(session)
        self.tofu.fail_apply = True
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies, 1)
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'applying')
        captured = len(self.verifications)
        checked = protector.call_count
        self.tofu.fail_apply = False
        protector.side_effect = host.HostError('selected archive retired or live lifecycle changed')
        with self.assertRaises(host.HostError): session.stop()
        self.assertGreater(protector.call_count, checked)
        self.assertEqual(len(self.verifications), captured)
        self.assertEqual(self.tofu.applies, 1)
        self.assertEqual(session.deployment['data_volume_id'], VOLUME_ID)

    def test_interrupted_three_resource_delete_drift_resumes_with_protection_and_disk_proof(self):
        session=self.create(True); self.tofu.fail_apply=True
        with self.assertRaises(host.HostError):session.stop()
        self.assertTrue(json.loads(session.journal_path.read_text())['removal_started'])
        self.tofu.fail_apply=False
        before=base.resources(self.tofu.current)
        original=self.tofu.plan
        def refreshed(path,present,**kwargs):
            original(path,present,**kwargs)
            return {'resource_drift':[{'address':address,'change':{'actions':['delete'],'before':before[address],'after':None}}
                    for address in sorted(s3.S3_ADDRESSES)],'resource_changes':[]}
        self.tofu.plan=refreshed
        calls=len(self.loads)
        result=session.stop()
        self.assertEqual(result['data_disk'],'absent')
        self.assertEqual(self.tofu.applies,2)
        self.assertGreater(len(self.loads),calls)
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'],'complete')

    def test_data_deletion_drift_requires_exact_prior_intent_stop_and_receipts(self):
        session=self.create(True)
        inventory=base.resources(self.tofu.current)
        plan={'resource_drift':[{'address':address,'change':{'actions':['delete'],'before':inventory[address],'after':None}}
              for address in sorted(s3.S3_ADDRESSES)],'resource_changes':[]}
        journal={'removal_started':True,'host_stop_requested':True,'receipts':copy.deepcopy(RECEIPTS),'receipt_ids':{'survival':RECEIPT_ID}}
        s3.validate_s3_plan(plan,present=False,state=self.tofu.current,deployment=session.deployment,journal=journal)
        for field,value in (('removal_started',False),('host_stop_requested',False),('receipts',{})):
            with self.subTest(field=field),self.assertRaises(host.HostError):
                s3.validate_s3_plan(plan,present=False,state=self.tofu.current,deployment=session.deployment,journal={**journal,field:value})
        changed=copy.deepcopy(plan)
        next(row for row in changed['resource_drift'] if row['address']==s3.DATA_ADDRESS)['change']['before']['id']=NEW_VOLUME
        with self.assertRaises(host.HostError):s3.validate_s3_plan(changed,present=False,state=self.tofu.current,deployment=session.deployment,journal=journal)

    def test_fresh_drain_callback_queries_the_current_same_host_and_store_uses_execution_pin(self):
        session=self.create(True)
        with mock.patch('minecraft_backup.verify_stop_receipt',return_value=copy.deepcopy(RECEIPTS)) as verify:
            session.verify_receipts(session.deployment,DRAIN)
            callback=verify.call_args.kwargs['fresh_drain']
            self.assertEqual(callback(),DRAIN); self.assertEqual(callback(),DRAIN)
        self.assertEqual(sum(action=='host-drain-private' for remote in S3Remote.instances for action,_ in remote.commands),2)
        with mock.patch('minecraft_backup_store.S3Store') as store:
            session.make_store()
            self.assertEqual(store.call_args.kwargs['executable_provider'],self.aws.command_path)
            self.assertEqual(store.call_args.kwargs['environment_provider'],self.aws.environment)

    def test_protection_failure_after_plan_refuses_the_first_removal_apply(self):
        session = self.create(True)
        protector = self.protected_loader(session)
        def plan(*args, **kwargs):
            result = original(*args, **kwargs)
            protector.side_effect = host.HostError('keep tag disappeared during planning')
            return result
        original = self.tofu.plan
        self.tofu.plan = plan
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(len(self.tofu.plans), 1)
        self.assertEqual(self.tofu.applies, 0)
        self.assertEqual(session.deployment['data_volume_id'], VOLUME_ID)

    def test_off_state_start_refuses_missing_or_retired_selected_protection(self):
        session = self.create(False)
        protector = self.protected_loader(session)
        protector.side_effect = host.HostError('selected marker has no keep protection')
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 0)
        self.assertEqual(self.tofu.plans, [])
        self.assertEqual(S3Remote.instances, [])

    def test_real_base_binding_survives_authorized_cold_start_and_stop(self):
        session = self.create(False)
        binding = session.binding()
        self.assertTrue(binding['policy_sha256'])
        self.assertEqual(session.start('survival')['ready'], True)
        self.assertEqual(session.binding(), binding)
        self.assertEqual(session.deployment['data_volume_id'], NEW_VOLUME)
        S3Remote.host_state_value = 'running'
        self.assertEqual(session.stop()['data_disk'], 'absent')
        self.assertEqual(session.binding(), binding)
        self.assertIsNone(session.deployment['data_volume_id'])
        self.assertEqual(self.tofu.applies, 2)
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'complete')

    def test_wrong_provenance_target_and_expired_proof_leave_disk_without_initialization(self):
        for failure in ('wrong-target','expired'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child).resolve()/'journal')
                session = self.create(False)
                original = session.provenance
                def proof(*args,**kwargs):
                    value = original(*args,**kwargs)
                    if failure == 'wrong-target': value['volume_id'] = VOLUME_ID
                    else: value['provenance_expires_at_utc'] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1)).isoformat()
                    return value
                session.provenance = proof
                with self.assertRaises(host.HostError): session.start('survival')
                self.assertEqual(self.tofu.applies,1)
                self.assertFalse(any(action in ('storage-initialize','start') for remote in S3Remote.instances for action,_ in remote.commands))
                S3Remote.instances=[]

    def test_capacity_or_bootstrap_ownership_failure_happens_before_provisioning(self):
        for changed in ('capacity','bootstrap','account'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir']=str(Path(child).resolve()/'journal')
                session=self.create(False)
                rows=copy.deepcopy(RECEIPTS)
                if changed=='capacity': rows['survival']['receipt']['required_capacity_bytes']=129*1024**3
                if changed=='bootstrap': rows['survival']['receipt']['bootstrap_sha256']='f'*64
                if changed=='account': rows['survival']['receipt']['source']['account_id']='999999999999'
                session.receipt_loader=mock.Mock(return_value=rows)
                with self.assertRaises(host.HostError): session.start('survival')
                self.assertEqual(self.tofu.applies,0)

    def test_start_existing_host_requires_pinned_runtime_attestation_without_restore(self):
        session=self.create(True)
        result=session.start('survival')
        self.assertTrue(result['ready'])
        self.assertEqual(self.tofu.applies,0)
        self.assertEqual(self.proofs,[])
        self.assertFalse(any(action=='receipt-restore' for remote in S3Remote.instances for action,_ in remote.commands))

    def test_dns_failure_keeps_confirmed_no_session_resources(self):
        session=self.create(True)
        S3Remote.failure='dns'
        result=session.stop()
        self.assertEqual(result['data_disk'],'absent')
        self.assertEqual(session.dns,{'dns':'failed'})
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'],'complete')

    def test_verified_stop_publishes_receipts_before_graceful_stop_then_removes_every_session_disk(self):
        session = self.create(True)
        result = session.stop()
        self.assertEqual(result, {'host': 'absent', 'root_disk': 'absent', 'data_disk': 'absent', 'recovery': 'verified'})
        self.assertEqual(len(self.verifications), 1)
        self.assertEqual(self.tofu.applies, 1)
        self.assertEqual(self.tofu.plans[0], (False, {'recovery_receipt_ids': {'survival': RECEIPT_ID}}))
        command_names = [action for action,_ in S3Remote.instances[0].commands]
        self.assertLess(command_names.index('host-drain-private'), command_names.index('stop-instances'))
        final = json.loads(self.path.read_text())
        self.assertIsNone(final['data_volume_id']); self.assertIsNone(final['data_filesystem_uuid'])
    def test_failed_receipt_leaves_host_and_data_available_without_plan_or_ec2_stop(self):
        session = self.create(True)
        session.receipt_verifier = mock.Mock(side_effect=host.HostError('receipt corrupt'))
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies, 0); self.assertEqual(self.tofu.plans, [])
        self.assertFalse(any(action == 'stop-instances' for remote in S3Remote.instances for action,_ in remote.commands))
    def test_start_proves_fresh_storage_transfers_exact_receipt_restores_then_starts(self):
        session = self.create(False)
        result = session.start('survival')
        self.assertTrue(result['ready']); self.assertIsNone(result['startup_target_seconds'])
        self.assertEqual(len(self.proofs), 1); self.assertEqual(len(self.marker_scans), 1)
        journal = json.loads(session.journal_path.read_text())
        self.assertNotEqual(journal['target_storage']['filesystem_uuid'], DEPLOYMENT['data_filesystem_uuid'])
        self.assertEqual(journal['target_storage']['size_gib'], 3)
        commands = [action for remote in S3Remote.instances for action,_ in remote.commands]
        self.assertLess(commands.index('storage-initialize'), commands.index('receipt-restore'))
        self.assertLess(commands.index('receipt-restore'), commands.index('start'))
        self.assertEqual(json.loads(self.transfers[-1]['files']['recovery-receipt.json']), RECEIPT)
        self.assertNotIn(self.secret.read_text(), json.dumps([remote.commands for remote in S3Remote.instances]))
    def test_missing_archive_or_wrong_selection_prevents_provisioning(self):
        session = self.create(False)
        session.receipt_loader = mock.Mock(side_effect=host.HostError('archive unavailable'))
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 0); self.assertEqual(S3Remote.instances, [])
    def test_failed_provenance_or_marker_scan_never_initializes_or_starts_game(self):
        for boundary in ('provenance', 'marker_scanner'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child).resolve() / 'journal')
                session = self.create(False)
                setattr(session, boundary, mock.Mock(side_effect=host.HostError('prior use or missing evidence')))
                with self.assertRaises(host.HostError): session.start('survival')
                self.assertFalse(any(action in ('storage-initialize','start') for remote in S3Remote.instances for action,_ in remote.commands))
                S3Remote.instances = []
    def test_failed_restore_preserves_new_target_and_resumes_same_receipt_without_recreate(self):
        session = self.create(False)
        S3Remote.failure = 'receipt-restore'
        with self.assertRaises(host.HostError): session.start('survival')
        identity = json.loads(session.journal_path.read_text())['operation_id']
        self.assertEqual(self.tofu.applies, 1)
        self.assertFalse(any(action == 'start' for remote in S3Remote.instances for action,_ in remote.commands))
        S3Remote.failure = None
        session.start('survival')
        self.assertEqual(self.tofu.applies, 1)
        self.assertEqual(json.loads(session.journal_path.read_text())['operation_id'], identity)
    def test_resume_refuses_edited_dynamic_target_or_receipt_handoff(self):
        session = self.create(False)
        S3Remote.failure = 'receipt-restore'
        with self.assertRaises(host.HostError): session.start('survival')
        valid = copy.deepcopy(session.deployment)
        S3Remote.failure = None
        commands = sum(len(remote.commands) for remote in S3Remote.instances)
        for field, value in (('data_filesystem_uuid', DEPLOYMENT['data_filesystem_uuid']),
                             ('recovery_receipt_ids', {'survival':'e' * 64})):
            session.deployment = {**valid, field:value}
            with self.assertRaises(host.HostError): session.start('survival')
            self.assertEqual(self.tofu.applies, 1)
            self.assertEqual(sum(len(remote.commands) for remote in S3Remote.instances), commands)
    def test_failed_readiness_retry_skips_restore_and_provenance(self):
        session = self.create(False)
        S3Remote.failure = 'start'
        with self.assertRaises(host.HostError): session.start('survival')
        before = sum(action == 'receipt-restore' for remote in S3Remote.instances for action,_ in remote.commands)
        S3Remote.failure = None
        session.start('survival')
        self.assertEqual(sum(action == 'receipt-restore' for remote in S3Remote.instances for action,_ in remote.commands), before)
        self.assertEqual(len(self.proofs), 1)
    def test_uncertain_create_outcome_never_creates_another_disk(self):
        session = self.create(False); self.tofu.fail_apply = True
        with self.assertRaises(host.HostError): session.start('survival')
        self.tofu.fail_apply = False
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 1)
    def test_unexpected_protected_plan_change_refuses_removal(self):
        session = self.create(True)
        self.tofu.extra = {'address':'aws_s3_bucket.backups','change':{'actions':['delete']}}
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies, 0)

    def test_transport_rules_remain_protected_in_s3_only_session_plans(self):
        session = self.create(True)
        plan = self.tofu.plan(self.path.parent / 'transport-plan', False,
                              variables={'recovery_receipt_ids': {'survival': RECEIPT_ID}})
        journal = {'receipt_ids': {'survival': RECEIPT_ID}, 'receipts': RECEIPTS}
        for address in ('aws_vpc_security_group_egress_rule.webrtc[0]',
                        'aws_vpc_security_group_ingress_rule.game["signaling"]',
                        'aws_vpc_security_group_ingress_rule.game["webrtc"]'):
            selected = copy.deepcopy(plan)
            selected['resource_changes'].append({'address': address, 'mode': 'managed', 'change': {'actions': ['no-op']}})
            s3.validate_s3_plan(selected, present=False, state=self.tofu.current, deployment=session.deployment, journal=journal)
            for actions in (['create'], ['update'], ['delete']):
                selected['resource_changes'][-1]['change']['actions'] = actions
                with self.subTest(address=address, actions=actions), self.assertRaises(host.HostError):
                    s3.validate_s3_plan(selected, present=False, state=self.tofu.current, deployment=session.deployment, journal=journal)
    def test_retained_data_ownership_refuses_session_mutation(self):
        session = self.create(True); session.deployment['data_ownership'] = 'retained'
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies, 0)
    def test_absent_data_handoff_requires_every_exact_receipt(self):
        session = self.create(False)
        self.assertIsNone(host.load_deployment(self.path)['data_volume_id'])
        for delta in ({'recovery_receipt_ids':{}}, {'data_ownership':'retained'}, {'data_filesystem_uuid':DEPLOYMENT['data_filesystem_uuid']}):
            base.save_private(self.path, {**self.tofu.output, **delta})
            with self.assertRaises(host.HostError): host.load_deployment(self.path)
    def test_absent_data_handoff_validates_instance_map_before_receipt_inventory(self):
        self.create(False)
        for instances in (None, [], {}, {'invalid/name': {}}, {'survival': None}):
            with self.subTest(instances=instances):
                base.save_private(self.path, {**self.tofu.output, 'instances':instances})
                with self.assertRaisesRegex(host.HostError, 'instances must contain valid'):
                    host.load_deployment(self.path)


class MarkerVersionScanTests(unittest.TestCase):
    def setUp(self):
        from test_recovery_receipts import RecoveryReceiptTests
        self.fixture=RecoveryReceiptTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store
        self.store.list_versions=lambda prefix: [{'key':key,'version_id':version,'delete_marker':False}
                                                  for key,version in self.store.versions if key.startswith(prefix)]
    def test_full_historical_markers_are_read_by_exact_versions_outside_target(self):
        result=s3.scan_fresh_marker_versions(self.store,['survival'],NEW_VOLUME,self.fixture.workspace)
        self.assertTrue(result['target_volume_unused'])
        self.assertEqual(result['completed_marker_versions_checked'],1)
        self.assertEqual(self.store.reads[-1][1],self.store.current[self.fixture.fixture.marker_key])
    def test_prior_target_volume_in_any_completed_version_forbids_initialization(self):
        with self.assertRaises(host.HostError):
            s3.scan_fresh_marker_versions(self.store,['survival'],self.fixture.source['data_volume_id'],self.fixture.workspace)
    def test_malformed_or_unavailable_version_fails_closed(self):
        key=self.fixture.fixture.marker_key
        self.store.put(key,b'not-json')
        with self.assertRaises(Exception):s3.scan_fresh_marker_versions(self.store,['survival'],NEW_VOLUME,self.fixture.workspace)
        self.store.list_versions=lambda prefix:[{'key':key,'version_id':'unavailable','delete_marker':False}]
        with self.assertRaises(host.HostError):s3.scan_fresh_marker_versions(self.store,['survival'],NEW_VOLUME,self.fixture.workspace)

if __name__ == '__main__': unittest.main()
