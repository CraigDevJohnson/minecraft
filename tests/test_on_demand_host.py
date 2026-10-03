from __future__ import annotations

import copy
import hashlib
import http.server
import threading
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import minecraft_host as host
import minecraft_on_demand as lifecycle

HOST_ID = 'i-0123456789abcdef0'
VOLUME_ID = 'vol-0123456789abcdef0'
DIGEST = 'a' * 64
DEPLOYMENT = {'schema_version': 2, 'lifecycle_mode': 'on-demand-host', 'session_present': True,
              'environment': 'family', 'resource_prefix': 'minecraft-family', 'account_id': '123456789012', 'host_id': HOST_ID, 'region': 'us-west-2', 'instances': {'survival': {}},
              'operator_role_arn': 'arn:aws:iam::123456789012:role/operator', 'ami_id': 'ami-0123456789abcdef0',
              'host_instance_profile': 'minecraft-private-host', 'availability_zone': 'us-west-2a',
              'subnet_id': 'subnet-private', 'security_group_id': 'sg-private', 'host_role_arn': 'arn:aws:iam::123456789012:role/minecraft-host',
              'data_volume_id': VOLUME_ID, 'data_filesystem_uuid': '12345678-1234-4234-8234-123456789abc',
              'backup_bucket_name': 'minecraft-private-backups', 'bootstrap_sha256': DIGEST}
HOST_ATTRIBUTES = {'ami': DEPLOYMENT['ami_id'], 'instance_type': 'm7g.large', 'iam_instance_profile': 'minecraft-private-host', 'availability_zone': 'us-west-2a', 'id': HOST_ID, 'subnet_id': 'subnet-private', 'vpc_security_group_ids': ['sg-private'],
                   'associate_public_ip_address': True, 'root_block_device': [{'volume_id': 'vol-root', 'encrypted': True,
                    'delete_on_termination': True, 'volume_size': 20, 'volume_type': 'gp3'}],
                   'metadata_options': [{'http_tokens': 'required', 'http_put_response_hop_limit': 1}]}
ATTACHMENT_ATTRIBUTES = {'id': 'attachment-private', 'volume_id': VOLUME_ID, 'instance_id': HOST_ID, 'force_detach': False, 'skip_destroy': False}
DRAIN = {'draining': True, 'instances': {'survival': {'instance': 'survival', 'application': 'stopped', 'backup': 'complete'}}}


def state(present=True):
    values = {'aws_ebs_volume.data': {'id': VOLUME_ID, 'encrypted': True, 'size': 5},
              'aws_vpc.minecraft': {'id': 'vpc-private'}, 'aws_iam_role.host': {'id': 'role-private'}}
    if present:
        values.update({'aws_instance.host[0]': HOST_ATTRIBUTES, 'aws_volume_attachment.data[0]': ATTACHMENT_ATTRIBUTES})
    result = {'lineage': 'private-lineage', 'serial': 3, 'resources': []}
    for address, attributes in values.items():
        kind, name = address.split('.')
        indexed = name.endswith('[0]')
        instance = {'attributes': copy.deepcopy(attributes), **({'index_key': 0} if indexed else {})}
        result['resources'].append({'mode': 'managed', 'type': kind, 'name': name.removesuffix('[0]'), 'instances': [instance]})
    return result


class FakeAWS:
    def __init__(self):
        self.calls = []
        self.retained_available = True
        self.root_absent = True
        self.inventory = None
        self.unrecorded_host = False
    def environment(self): return {}
    def run(self, service, operation, *arguments, timeout):
        self.calls.append((operation, arguments))
        if operation == 'get-caller-identity': return {'Account': DEPLOYMENT['account_id']}
        if operation == 'describe-instances' and '--filters' in arguments:
            filters = json.loads(arguments[arguments.index('--filters') + 1])
            if any(value['Name'] == 'instance-id' for value in filters):
                return {'Reservations': [] if getattr(self, 'terminated_forgotten', False) else [{'Instances': [{'InstanceId': HOST_ID, 'State': {'Name': 'terminated'}}]}]}
            inventory = lifecycle.resources(self.inventory()) if self.inventory else {}
            recorded = inventory.get('aws_instance.host[0]', {}).get('id')
            hosts = [{'InstanceId': recorded}] if recorded else []
            if self.unrecorded_host: hosts.append({'InstanceId': 'i-unrecorded-created-before-state-write'})
            return {'Reservations': [{'Instances': hosts}]}
        if operation == 'describe-instances': return {'Reservations': [{'Instances': [{'InstanceId': HOST_ID, 'State': {'Name': 'terminated'}}]}]}
        if operation == 'describe-volumes':
            if '--filters' in arguments:
                filters = json.loads(arguments[arguments.index('--filters') + 1])
                if any(value['Name'] == 'tag:Name' for value in filters):
                    inventory = lifecycle.resources(self.inventory()) if self.inventory else {}
                    roots = inventory.get('aws_instance.host[0]', {}).get('root_block_device', [])
                    return {'Volumes': [{'VolumeId': value['volume_id']} for value in roots]}
                return {'Volumes': [] if self.root_absent else [{'VolumeId': 'vol-root'}]}
            return {'Volumes': [{'VolumeId': VOLUME_ID, 'Encrypted': True, 'Attachments': [] if self.retained_available else [{}], 'State': 'available'}]}
        raise AssertionError(operation)


class FakeTofu:
    def __init__(self, present):
        self.current = state(present)
        self.output = {**DEPLOYMENT, 'session_present': present, 'host_id': HOST_ID if present else None}
        self.plans = []
        self.applies = 0
        self.inject_change = None
        self.fail_apply_once = False
        self.partial_apply_once = False
        self.plan_direction = None
    def state(self): return copy.deepcopy(self.current)
    def plan(self, path, present):
        self.plans.append(present)
        self.plan_direction = present
        path.write_bytes(b'reviewable-plan')
        inventory = lifecycle.resources(self.current)
        changes = []
        for address, attributes in (('aws_instance.host[0]', HOST_ATTRIBUTES), ('aws_volume_attachment.data[0]', ATTACHMENT_ATTRIBUTES)):
            if (present and address not in inventory) or (not present and address in inventory):
                changes.append({'address': address, 'mode': 'managed', 'change': {'actions': ['create' if present else 'delete'],
                            'before': None if present else copy.deepcopy(inventory[address]), 'after': copy.deepcopy(attributes) if present else None}})
        if self.inject_change: changes.append(self.inject_change)
        return {'resource_changes': changes}
    def apply(self, path):
        self.applies += 1
        if self.fail_apply_once:
            self.fail_apply_once = False
            raise host.HostError('infrastructure command interrupted')
        if self.partial_apply_once:
            self.partial_apply_once = False
            current = state(self.plan_direction)
            current['resources'] = [value for value in current['resources'] if value['type'] != 'aws_volume_attachment']
            self.current = current
            self.current['serial'] += 1
            raise host.HostError('infrastructure command interrupted after partial apply')
        old_serial = self.current['serial']
        self.current = state(self.plan_direction)
        self.current['serial'] = old_serial + 1
        self.output.update(session_present=self.plan_direction, host_id=HOST_ID if self.plan_direction else None)
    def deployment(self): return copy.deepcopy(self.output)


class FakeSession:
    instances = []
    failure = None
    drain = DRAIN
    installed_runtime = True
    host_state_value = 'running'
    def __init__(self, deployment, aws, cloudflare):
        self.deployment = deployment
        self.state = self.host_state_value
        self.observed = {}
        self.dns = {}
        self.dns_error = None
        self.commands = []
        self.instances.append(self)
    def host_state(self):
        self.state = self.host_state_value
        return self.state
    def call(self, service, operation, *arguments):
        self.commands.append((operation, arguments))
        if operation == 'stop-instances':
            type(self).host_state_value = 'stopping'
        return {}
    def wait_host(self, expected, transitional, **kwargs):
        type(self).host_state_value = expected
        self.state = expected
    def wait_ssm(self): pass
    def execute_shell(self, command, action):
        self.commands.append((action, command))
        if action == self.failure: raise host.HostError('remote operation failed')
        if action == 'runtime-probe': return {'runtime': 'installed' if self.installed_runtime else 'missing'}
        if action == 'private-input-verification': return {'private_inputs': 'verified'}
        return {}
    def remote(self, action):
        self.commands.append((action, ''))
        return copy.deepcopy(self.drain)
    def instance_statuses(self, response):
        return {name: host.public_instance(value, name) for name, value in response['instances'].items()}
    def start(self, instance):
        self.commands.append(('start', instance))
        if self.failure == 'start': raise host.HostError('instance did not report ready after start')
        self.observed = {'instance': instance, 'application': 'running', 'backup': 'pending', 'ready': True}
        return {'host': 'running', **self.observed}
    def sync_dns(self):
        self.dns = {'dns': 'parked'}
        if self.failure == 'dns':
            self.dns = {'dns': 'failed'}
            self.dns_error = 'DNS record not updated; rerun dns'
    def status(self): return {'host': 'running'}


class OnDemandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.operator = self.root / 'operator.json'
        self.operator.write_text('{}')
        self.operator.chmod(0o600)
        self.secret = self.root / 'rcon.secret'
        self.secret.write_text('CONFIDENTIAL_SECRET_DONT_PRINT')
        self.secret.chmod(0o600)
        self.variables = self.root / 'deployment.tfvars'
        self.variables.write_text('pinned-private-inputs')
        self.variables.chmod(0o600)
        self.source_patch = mock.patch.object(lifecycle, 'source_digest', return_value=DIGEST)
        self.source_mock = self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.policy = {'variables_file': str(self.variables), 'infra_root': str(self.root / 'infra'), 'aws_profile': 'workloads-admin', 'journal_dir': str(self.root / 'journal'), 'operator_file': str(self.operator), 'secret_file': str(self.secret),
                       'backend': {'bucket': 'private-state', 'key': 'infrastructure/terraform.tfstate', 'region': 'us-west-2'},
                       'private_inputs_sha256': __import__('hashlib').sha256(self.operator.read_bytes() + b'\0' + self.secret.read_bytes()).hexdigest(),
                       'source_sha256': DIGEST, 'variables_sha256': lifecycle.digest_file(self.variables), 'bootstrap_sha256': DIGEST}
        self.aws = FakeAWS()
        FakeSession.instances = []
        FakeSession.failure = None
        FakeSession.drain = DRAIN
        FakeSession.installed_runtime = True
        FakeSession.host_state_value = "running"
        self.patch = mock.patch.object(lifecycle, 'HostSession', FakeSession)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.transfers = []
    def session(self, present=True, *, tofu=None):
        deployment = {**DEPLOYMENT, 'session_present': present, 'host_id': HOST_ID if present else None}
        self.tofu = tofu or FakeTofu(present)
        self.aws.inventory = lambda: self.tofu.current
        self.deployment_path = self.root / 'deployment.json'
        lifecycle.save_private(self.deployment_path, deployment)
        return lifecycle.OnDemandSession(deployment, self.aws, None, self.policy, self.deployment_path, tofu=self.tofu,
                        transfer=lambda remote, bundle, digest: self.transfers.append((bundle, digest)))
    def test_stop_drains_before_apply_confirms_host_root_and_retained_data_then_parks_dns(self):
        session = self.session()
        output = session.stop()
        self.assertEqual(output, {'host': 'absent', 'root_disk': 'absent', 'data_disk': 'retained', 'backup': 'complete'})
        self.assertEqual([action for action,_ in FakeSession.instances[0].commands], ['private-verifier-release', 'private-input-verification', 'host-drain'])
        self.assertEqual(self.tofu.plans, [False])
        self.assertEqual(session.dns, {'dns': 'parked'})
        self.assertIsNone(json.loads(self.deployment_path.read_text())['host_id'])
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'complete')
    def test_malformed_journal_identity_refuses_without_writing_outside_journal(self):
        session = self.session(False)
        self.tofu.fail_apply_once = True
        with self.assertRaises(host.HostError): session.start('survival')
        journal = json.loads(session.journal_path.read_text())
        journal['operation_id'] = '../deployment.json'
        lifecycle.save_private(session.journal_path, journal)
        before = self.deployment_path.read_bytes()
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 1)
        self.assertEqual(self.deployment_path.read_bytes(), before)

    def test_variables_and_source_changed_during_plan_refuse_apply(self):
        for changed in ('variables', 'source'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child) / 'journal')
                self.source_mock.return_value = DIGEST
                self.variables.write_text('pinned-private-inputs')
                session = self.session(False)
                original_plan = self.tofu.plan
                def plan(path, present):
                    result = original_plan(path, present)
                    if changed == 'variables': self.variables.write_text('changed-after-plan-start')
                    else: self.source_mock.return_value = 'b' * 64
                    return result
                self.tofu.plan = plan
                with self.assertRaises(host.HostError): session.start('survival')
                self.assertEqual(self.tofu.applies, 0)

    def test_unknown_cloud_create_outcome_refuses_duplicate_creation(self):
        session = self.session(False)
        self.tofu.fail_apply_once = True
        with self.assertRaises(host.HostError): session.start('survival')
        self.aws.unrecorded_host = True
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 1)
        self.assertEqual(self.tofu.plans, [True])

    def test_partial_created_host_configuration_drift_refuses_retry(self):
        tofu = FakeTofu(False)
        tofu.partial_apply_once = True
        session = self.session(False, tofu=tofu)
        with self.assertRaises(host.HostError): session.start('survival')
        for resource in tofu.current['resources']:
            if resource['type'] == 'aws_instance':
                resource['instances'][0]['attributes']['subnet_id'] = 'subnet-unreviewed'
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(tofu.applies, 1)

    def test_resume_failed_apply_requires_fresh_complete_drain_before_delete(self):
        session = self.session()
        self.tofu.fail_apply_once = True
        with self.assertRaises(host.HostError): session.stop()
        FakeSession.host_state_value = 'running'
        FakeSession.drain = {'draining': True, 'instances': {'survival': {'instance': 'survival', 'application': 'stopped', 'backup': 'failed'}}}
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies, 1)

    def test_failed_drain_never_plans_or_removes_any_resource(self):
        FakeSession.drain = {'draining': True, 'instances': {'survival': {'instance': 'survival', 'application': 'stopped', 'backup': 'failed'}}}
        session = self.session()
        with self.assertRaises(host.HostError): session.stop()
        self.assertEqual(self.tofu.applies, 0)
        self.assertEqual(self.tofu.plans, [])
        self.assertIsNotNone(session.deployment['host_id'])
    def test_private_bundle_waits_for_cli_tunnel_bind_before_any_http_payload(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                session = self.session(True)
                existing_path = '/usr/local/bin:/usr/bin:/bin'
                self.aws.environment = lambda: {'PATH': existing_path}
                received = []
                servers = []
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_POST(self):
                        received.append(self.rfile.read(int(self.headers['Content-Length'])))
                        self.send_response(204)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                    def log_message(self, *args): pass
                self_outer = self
                class Process:
                    def __init__(self, args, **kwargs):
                        self_outer.assertEqual(kwargs['env']['PATH'], str(Path(self_outer.policy['infra_root']).parent / '.tools/bin') + os.pathsep + existing_path)
                        parameters = json.loads(args[args.index('--parameters') + 1])
                        port = int(parameters['localPortNumber'][0])
                        server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
                        worker = threading.Thread(target=server.serve_forever, daemon=True)
                        worker.start()
                        servers.append((server, worker))
                        read, write = os.pipe()
                        self.stdout = os.fdopen(read, 'rb')
                        payload = (f'Port {port} opened for sessionId private-session\nWaiting for connections...\n' if ready else 'Starting session only\n').encode()
                        os.write(write, payload)
                        os.close(write)
                        self.returncode = None
                    def poll(self): return self.returncode
                    def terminate(self): self.returncode = 0
                    def wait(self, **kwargs): return 0
                    def kill(self): self.returncode = 0
                bundle = b'PRIVATE_BUNDLE_PAYLOAD'
                remote = FakeSession(session.deployment, self.aws, None)
                try:
                    with mock.patch.object(lifecycle.subprocess, 'Popen', side_effect=Process):
                        if ready:
                            session.transfer_private(remote, bundle, DIGEST)
                            self.assertEqual(received, [bundle])
                        else:
                            with self.assertRaises(host.HostError): session.transfer_private(remote, bundle, DIGEST)
                            self.assertEqual(received, [])
                    self.assertNotIn(bundle.decode(), json.dumps(remote.commands))
                finally:
                    for server, worker in servers:
                        server.shutdown()
                        server.server_close()
                        worker.join(timeout=1)

    def test_start_creates_stages_transfer_installs_then_normal_start_and_measures(self):
        session = self.session(False)
        output = session.start('survival')
        self.assertEqual(self.tofu.plans, [True])
        self.assertEqual(output['ready'], True)
        commands = FakeSession.instances[0].commands
        self.assertEqual([value[0] for value in commands], ['bootstrap-prerequisites', 'bootstrap-release', 'runtime-install', 'private-verifier-release', 'private-input-verification', 'start'])
        self.assertEqual(len(self.transfers), 1)
        self.assertNotIn(self.secret.read_text(), json.dumps(commands))
        self.assertNotIn(self.secret.read_text(), json.dumps(output))
        self.assertIn('start_to_ready_seconds', output)
        self.assertIsNone(output['startup_target_met'])
        self.assertEqual(self.deployment_path.stat().st_mode & 0o777, 0o600)
    def test_migration_created_present_host_without_runtime_gets_automatic_setup(self):
        session = self.session(True)
        FakeSession.installed_runtime = False
        result = session.start('survival')
        self.assertTrue(result['ready'])
        self.assertEqual(self.tofu.applies, 0)
        self.assertEqual(len(self.transfers), 1)
        self.assertEqual([value[0] for value in FakeSession.instances[1].commands],
                         ['bootstrap-prerequisites', 'bootstrap-release', 'runtime-install', 'private-verifier-release', 'private-input-verification', 'start'])

    def test_duplicate_start_existing_host_does_not_reinstall_private_files(self):
        session = self.session(True)
        session.start('survival')
        self.assertEqual(self.tofu.applies, 0)
        self.assertEqual(self.transfers, [])
        self.assertEqual([action for action,_ in FakeSession.instances[0].commands], ['runtime-probe', 'private-verifier-release', 'private-input-verification'])
        self.assertEqual([action for action,_ in FakeSession.instances[1].commands], ['private-verifier-release', 'private-input-verification', 'start'])

    def test_changed_reviewed_inputs_require_reconfiguration_before_reusing_host(self):
        session = self.session(True)
        self.operator.write_text('{"backup_timeout":3000}')
        self.policy['private_inputs_sha256'] = lifecycle.private_inputs_digest(self.policy)
        original = FakeSession.execute_shell
        def verify(remote, command, action):
            if action == 'private-input-verification':
                raise host.HostError('installed input certificate differs')
            return original(remote, command, action)
        with mock.patch.object(FakeSession, 'execute_shell', verify):
            for action in ('start', 'stop'):
                with tempfile.TemporaryDirectory() as child:
                    self.policy['journal_dir'] = str(Path(child).resolve() / 'journal')
                    session = self.session(True)
                    with self.subTest(action=action), self.assertRaises(host.HostError):
                        session.start('survival') if action == 'start' else session.stop()
        self.assertEqual(self.tofu.applies, 0)
        self.assertEqual(self.transfers, [])
        self.assertFalse(any(action in ('start', 'host-drain', 'stop-instances') for remote in FakeSession.instances for action,_ in remote.commands))

    def test_invalid_private_encoding_or_bundle_schema_precedes_journal_and_create(self):
        for target, payload in ((self.operator,b'not JSON'), (self.operator,b'\xff'), (self.operator,b'\xef\xbb\xbf{}'), (self.operator,b'[]'), (self.secret,b'invalid secret')):
            saved = target.read_bytes()
            target.write_bytes(payload)
            self.policy['private_inputs_sha256'] = lifecycle.private_inputs_digest(self.policy)
            session = self.session(False)
            with self.subTest(payload=payload), self.assertRaises(host.HostError): session.start('survival')
            self.assertEqual(self.tofu.applies, 0)
            self.assertEqual(self.tofu.plans, [])
            self.assertFalse(session.journal_path.exists())
            target.write_bytes(saved)
        self.policy['private_inputs_sha256'] = lifecycle.private_inputs_digest(self.policy)
    def test_interrupt_after_partial_create_replans_only_missing_attachment_same_operation(self):
        tofu = FakeTofu(False)
        tofu.partial_apply_once = True
        session = self.session(False, tofu=tofu)
        with self.assertRaises(host.HostError): session.start('survival')
        identity = json.loads(session.journal_path.read_text())['operation_id']
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'applying')
        session.start('survival')
        self.assertEqual(json.loads(session.journal_path.read_text())['operation_id'], identity)
        self.assertEqual(tofu.applies, 2)
        self.assertEqual(tofu.plans, [True, True])
    def test_failed_install_or_readiness_resumes_without_reapplying_infrastructure(self):
        for failure in ('runtime-install', 'start'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child) / 'journal')
                session = self.session(False)
                FakeSession.failure = failure
                with self.assertRaises(host.HostError): session.start('survival')
                FakeSession.failure = None
                session.start('survival')
                self.assertEqual(self.tofu.applies, 1)
    def test_unexpected_plan_changes_or_deletion_identity_refused(self):
        for change in ({'address': 'aws_ebs_volume.data', 'change': {'actions': ['delete']}},
                       {'address': 'aws_iam_role.host', 'change': {'actions': ['update']}},
                       {'address': 'aws_instance.host[0]', 'change': {'actions': ['delete', 'create']}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child) / 'journal')
                session = self.session()
                self.tofu.inject_change = change
                with self.assertRaises(host.HostError): session.stop()
                self.assertEqual(self.tofu.applies, 0)
    def test_concurrent_lock_and_input_change_refuse_work(self):
        session = self.session(False)
        with lifecycle.operation_lock(session.directory):
            with self.assertRaises(host.HostError): session.start('survival')
        self.tofu.fail_apply_once = True
        with self.assertRaises(host.HostError): session.start('survival')
        self.secret.write_text('CHANGED_PRIVATE_SECRET_INPUT')
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 1)
    def test_protected_resource_change_during_partial_operation_refuses_resume(self):
        session = self.session(False)
        self.tofu.fail_apply_once = True
        with self.assertRaises(host.HostError): session.start('survival')
        self.tofu.current['resources'][0]['instances'][0]['attributes']['id'] = 'vol-unexpected'
        self.tofu.current['serial'] += 1
        with self.assertRaises(host.HostError): session.start('survival')
        self.assertEqual(self.tofu.applies, 1)
    def test_absent_terminated_instance_still_completes_removal_with_root_and_data_proof(self):
        session = self.session()
        self.aws.terminated_forgotten = True
        self.assertEqual(session.stop()['host'], 'absent')
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'complete')
        queries = [arguments for operation, arguments in self.aws.calls if operation == 'describe-instances']
        self.assertTrue(any('--filters' in arguments and json.loads(arguments[arguments.index('--filters') + 1]) ==
                            [{'Name': 'instance-id', 'Values': [HOST_ID]}] for arguments in queries))

    def test_root_still_present_and_retained_data_ambiguity_preserve_journal_for_repair(self):
        for attribute in ('root_absent', 'retained_available'):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child) / 'journal')
                self.aws = FakeAWS()
                FakeSession.host_state_value = "running"
                setattr(self.aws, attribute, False)
                session = self.session()
                with self.assertRaises(host.HostError): session.stop()
                self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'applied')
                self.assertIsNotNone(session.deployment['host_id'])
    def test_dns_failure_keeps_confirmed_removal_and_can_be_repaired(self):
        session = self.session()
        FakeSession.failure = 'dns'
        output = session.stop()
        self.assertEqual(output['host'], 'absent')
        self.assertEqual(session.dns, {'dns': 'failed'})
        self.assertEqual(json.loads(session.journal_path.read_text())['phase'], 'complete')
    def test_duplicate_start_cannot_use_an_edited_protected_handoff(self):
        session=self.session(True)
        session.deployment['public_hostname']='other.example.com'
        with self.assertRaises(host.HostError):session.start('survival')
        self.assertEqual(FakeSession.instances,[])

    def test_changed_policy_security_pins_cannot_resume_an_operation(self):
        session=self.session(False)
        self.tofu.fail_apply_once=True
        with self.assertRaises(host.HostError):session.start('survival')
        self.policy['backend_sha256']='unreviewed-new-backend'
        with self.assertRaises(host.HostError):session.start('survival')
        self.assertEqual(self.tofu.applies,1)

    def test_live_unknown_retained_attachment_blocks_partial_create_retry(self):
        tofu=FakeTofu(False); tofu.partial_apply_once=True
        session=self.session(False,tofu=tofu)
        with self.assertRaises(host.HostError):session.start('survival')
        self.aws.retained_available=False
        with self.assertRaises(host.HostError):session.start('survival')
        self.assertEqual(tofu.applies,1)

    def test_clock_discontinuity_never_reports_zero_seconds_or_met_target(self):
        session=self.session(True)
        session.command_started_at=__import__('time').time()+1000
        output=session.start('survival')
        self.assertIsNone(output['start_to_ready_seconds'])
        self.assertIsNone(output['controller_ready_within_target'])
        self.assertIsNone(output['startup_target_met'])
        journal=json.loads(session.journal_path.read_text())
        self.assertTrue(journal['timing_unverified'])

    def test_full_handoff_fields_must_match_authoritative_output_before_dns_or_status(self):
        for present in (False, True):
            for key, value in (('public_hostname','other.example.com'), ('cloudflare_zone_id','f'*32), ('data_volume_id','vol-2123456789abcdef0')):
                with self.subTest(present=present,key=key):
                    session = self.session(present)
                    session.deployment[key] = value
                    with self.assertRaises(host.HostError): session.repair_dns()
                    with self.assertRaises(host.HostError): session.status()
                    self.assertEqual(FakeSession.instances, [])

    def test_journal_start_clock_includes_validation_and_initial_state_pull(self):
        session = self.session(False)
        session.command_started_at = 100.0
        with mock.patch.object(lifecycle.time, 'time', return_value=180.0):
            with lifecycle.operation_lock(session.directory):
                journal = session.journal('start','survival')
        self.assertEqual(journal['started_at'],100.0)

    def test_dns_repair_refuses_stale_handoffs_in_both_directions_and_partial_session(self):
        for declared, actual in ((False, True), (True, False)):
            with self.subTest(declared=declared), tempfile.TemporaryDirectory() as child:
                self.policy['journal_dir'] = str(Path(child) / 'journal')
                session = self.session(declared)
                self.tofu.current = state(actual)
                with self.assertRaises(host.HostError): session.repair_dns()
                self.assertEqual(FakeSession.instances, [])
        session = self.session(False)
        self.tofu.current = state(True)
        self.tofu.current['resources'] = [value for value in self.tofu.current['resources'] if value['type'] != 'aws_volume_attachment']
        with self.assertRaises(host.HostError): session.repair_dns()
        self.assertEqual(FakeSession.instances, [])

    def test_dns_repair_refuses_incomplete_journal_even_if_handoff_matches_current_state(self):
        session = self.session(False)
        with lifecycle.operation_lock(session.directory):
            journal = session.journal('start', 'survival')
        with self.assertRaises(host.HostError): session.repair_dns()
        self.assertEqual(FakeSession.instances, [])
        session.record(journal, 'complete')
        self.assertEqual(session.repair_dns(), {'host': 'absent'})
        self.assertEqual(session.dns, {'dns': 'parked'})

    def test_stale_handoff_refuses_status(self):
        session = self.session()
        self.tofu.current = state(False)
        with self.assertRaises(host.HostError): session.status()
    def test_off_state_schema_acceptance_is_explicit_and_existing_handoff_stays_valid(self):
        path = self.root / 'handoff.json'
        path.write_text(json.dumps({**DEPLOYMENT, 'session_present': False, 'host_id': None}))
        self.assertIsNone(host.load_deployment(path)['host_id'])
        for changed in ({'schema_version': 1}, {'session_present': True}, {'lifecycle_mode': 'retained-host'}):
            path.write_text(json.dumps({**DEPLOYMENT, 'session_present': False, 'host_id': None, **changed}))
            with self.assertRaises(host.HostError): host.load_deployment(path)



class PolicyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.directory.chmod(0o700)
        self.infra = self.directory / 'infra'
        self.infra.mkdir()
        (self.infra / '.terraform').mkdir()
        self.backend = {'bucket': 'private-state', 'key': 'infrastructure/terraform.tfstate', 'region': 'us-west-2', 'encrypt': True, 'use_lockfile': True,
                        'allowed_account_ids': [DEPLOYMENT['account_id']], 'assume_role': {'role_arn': DEPLOYMENT['operator_role_arn']}}
        self.metadata = self.infra / '.terraform/terraform.tfstate'
        self.metadata.write_text(json.dumps({'backend': {'type': 's3', 'config': self.backend}}))
        self.variables = self.directory / 'deployment.tfvars'
        self.variables.write_text('private-inputs')
        self.variables.chmod(0o600)
        self.operator = self.directory / 'operator.json'
        self.operator.write_text('{}')
        self.operator.chmod(0o600)
        self.secret = self.directory / 'rcon.secret'
        self.secret.write_text('PRIVATE_SECRET_DONT_DISCLOSE')
        self.secret.chmod(0o600)
        self.policy = {'schema_version': 2, 'workstation': socket.gethostname(), 'aws_profile': 'workloads-admin',
                       'infra_root': str(self.infra), 'variables_file': str(self.variables),
                       'variables_sha256': lifecycle.digest_file(self.variables), 'source_sha256': DIGEST,
                       'backend': {name: self.backend[name] for name in ('bucket', 'key', 'region')},
                       'bootstrap_sha256': DIGEST, 'operator_file': str(self.operator), 'secret_file': str(self.secret),
                       'journal_dir': str(self.directory / 'journal')}
        binary = self.directory / '.tools/bin/tofu'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'reviewed-project-executable')
        binary.chmod(0o755)
        info = binary.stat()
        self.policy.update(tofu_sha256=lifecycle.digest_file(binary), tofu_file_identity={'device': info.st_dev, 'inode': info.st_ino},
                           backend_sha256=lifecycle.backend_digest(self.infra, self.policy),
                           private_inputs_sha256=lifecycle.private_inputs_digest(self.policy))
        self.path = self.directory / 'lifecycle.json'
        self.mock_digest = mock.patch.object(lifecycle, 'source_digest', return_value=DIGEST)
        self.mock_digest.start()
        self.addCleanup(self.mock_digest.stop)
    def validate(self):
        self.path.write_text(json.dumps(self.policy))
        self.path.chmod(0o600)
        return lifecycle.load_policy(self.path, lifecycle.digest_file(self.path), DEPLOYMENT)
    def test_reviewed_private_bytes_are_pinned_across_completed_operations(self):
        self.validate()
        self.secret.write_text('changed-private-secret-content')
        with self.assertRaises(host.HostError): self.validate()

    def test_executable_digest_identity_and_symlink_are_checked_before_subprocess(self):
        self.validate()
        client = lifecycle.TofuCLI(self.policy)
        binary = self.directory / '.tools/bin/tofu'
        binary.write_bytes(b'unreviewed-replacement')
        with mock.patch.object(lifecycle.subprocess,'run') as run:
            with self.assertRaises(host.HostError): client.run('state','pull')
            run.assert_not_called()
        binary.write_bytes(b'reviewed-project-executable')
        target = self.directory / 'same-bytes'
        target.write_bytes(binary.read_bytes()); target.chmod(0o755)
        binary.unlink(); binary.symlink_to(target)
        with self.assertRaises(host.HostError): self.validate()

    def test_backend_custom_endpoints_credentials_and_insecure_settings_are_refused(self):
        for field,value in (('endpoints',{'s3':'https://unreviewed.invalid'}),('endpoint','https://unreviewed.invalid'),
                            ('insecure',True),('access_key','unreviewed'),('shared_credentials_files',['/other']),('skip_credentials_validation',True)):
            with self.subTest(field=field):
                original = copy.deepcopy(self.backend)
                self.backend[field] = value
                self.metadata.write_text(json.dumps({'backend':{'type':'s3','config':self.backend}}))
                with self.assertRaises(host.HostError): self.validate()
                self.backend = original
        self.metadata.write_text(json.dumps({'backend':{'type':'s3','config':self.backend}}))
        self.validate()
        client = lifecycle.TofuCLI(self.policy)
        changed = {**self.backend,'endpoints':{'s3':'https://unreviewed.invalid'}}
        self.metadata.write_text(json.dumps({'backend':{'type':'s3','config':changed}}))
        with mock.patch.object(lifecycle.subprocess,'run') as run:
            with self.assertRaises(host.HostError): client.run('state','pull')
            run.assert_not_called()

    def test_executed_root_rejects_other_directory_and_symlink_components(self):
        original = self.policy['infra_root']
        other = self.directory / 'unreviewed'
        other.mkdir()
        link_parent = self.directory / 'linked-parent'
        link_parent.symlink_to(self.directory, target_is_directory=True)
        for candidate in (other, link_parent / 'infra'):
            self.policy['infra_root'] = str(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(host.HostError): self.validate()
        self.policy['infra_root'] = original
        self.assertEqual(self.validate()['infra_root'], original)

    def test_root_identity_is_rechecked_before_each_tofu_command(self):
        client = lifecycle.TofuCLI(self.policy)
        original = self.directory / 'original-infra'
        self.infra.rename(original)
        self.infra.mkdir()
        with mock.patch.object(lifecycle.subprocess, 'run') as run:
            with self.assertRaises(host.HostError): client.run('state', 'pull')
            run.assert_not_called()
        self.infra.rmdir()
        self.infra.symlink_to(original, target_is_directory=True)
        with mock.patch.object(lifecycle.subprocess, 'run') as run:
            with self.assertRaises(host.HostError): client.run('state', 'pull')
            run.assert_not_called()

    def test_reviewed_policy_checks_exact_workstation_inputs_backend_and_release(self):
        self.assertEqual(self.validate(), self.policy)
        for field, value in (('workstation', 'another-workstation'), ('variables_sha256', 'b' * 64),
                             ('source_sha256', 'b' * 64), ('bootstrap_sha256', 'b' * 64)):
            original = self.policy[field]
            self.policy[field] = value
            with self.subTest(field=field), self.assertRaises(host.HostError): self.validate()
            self.policy[field] = original
    def test_wrong_policy_digest_unreviewed_auto_inputs_backend_and_workspace_refused(self):
        self.validate()
        with self.assertRaises(host.HostError): lifecycle.load_policy(self.path, 'b' * 64, DEPLOYMENT)
        extra = self.infra / 'unexpected.auto.tfvars'
        extra.write_text('unexpected=true')
        with self.assertRaises(host.HostError): self.validate()
        extra.unlink()
        workspace = self.infra / '.terraform/environment'
        workspace.write_text('another-state')
        with self.assertRaises(host.HostError): self.validate()
        workspace.unlink()
        self.backend['key'] = 'wrong-state'
        self.metadata.write_text(json.dumps({'backend': {'type': 's3', 'config': self.backend}}))
        with self.assertRaises(host.HostError): self.validate()
    def test_terraform_json_source_changes_and_overrides_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary).resolve() / 'repo'
            (checkout / 'infra').mkdir(parents=True)
            (checkout / 'scripts').mkdir()
            for name in ('host-bootstrap.sh.tftpl', 'on-demand-bootstrap.sh.tftpl', 'minecraft_storage.py', 'install_aws_cli.sh',
                         'minecraft_bootstrap_release.py', 'minecraft_private_transfer.py', 'minecraft_on_demand.py', 'minecraft_host.py', 'install_runtime.sh'):
                (checkout / 'scripts' / name).write_text('fixture')
            (checkout / 'infra/.terraform.lock.hcl').write_text('fixture')
            self.mock_digest.stop()
            before = lifecycle.source_digest(checkout)
            extra = checkout / 'infra/additional.tf.json'
            extra.write_text('{}')
            self.assertNotEqual(lifecycle.source_digest(checkout), before)
            for name in ('override.tf.json', 'custom_override.tf.json', 'override.tf', 'custom_override.tf', 'override.tofu', 'custom_override.tofu', 'override.tofu.json', 'custom_override.tofu.json'):
                candidate = checkout / 'infra' / name
                candidate.write_text('{}')
                with self.subTest(name=name), self.assertRaises(host.HostError): lifecycle.source_digest(checkout)
                candidate.unlink()
            self.mock_digest.start()

    def test_tofu_process_strips_injected_arguments_secret_token_and_ambient_credentials(self):
        policy = {**self.policy}
        client = lifecycle.TofuCLI(policy)
        with mock.patch.dict(os.environ, {'TF_CLI_ARGS': '-destroy', 'TF_VAR_session_present': 'false', 'CLOUDFLARE_API_TOKEN': 'PRIVATE_TOKEN',
                                         'AWS_ACCESS_KEY_ID': 'PRIVATE_AWS_ACCESS', 'AWS_SECRET_ACCESS_KEY': 'PRIVATE_AWS_SECRET',
                                         'AWS_CONFIG_FILE': '/unreviewed/config', 'AWS_SHARED_CREDENTIALS_FILE': '/unreviewed/credentials',
                                        'AWS_ROLE_ARN':'arn:aws:iam::123456789012:role/unreviewed', 'AWS_WEB_IDENTITY_TOKEN_FILE':'/unreviewed/token', 'AWS_ROLE_SESSION_NAME':'unreviewed',
                                        'AWS_CA_BUNDLE':'/unreviewed/ca', 'HTTP_PROXY':'http://unreviewed.invalid', 'HTTPS_PROXY':'http://unreviewed.invalid', 'ALL_PROXY':'http://unreviewed.invalid',
                                        'http_proxy':'http://unreviewed.invalid', 'https_proxy':'http://unreviewed.invalid', 'all_proxy':'http://unreviewed.invalid',
                                         'AWS_ENDPOINT_URL': 'https://unreviewed.invalid', 'AWS_ENDPOINT_URL_S3': 'https://other.invalid'}), \
                mock.patch.object(lifecycle.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}')) as run:
            client.run('state', 'pull')
        environment = run.call_args.kwargs['env']
        for name in ('TF_CLI_ARGS', 'TF_VAR_session_present', 'CLOUDFLARE_API_TOKEN', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_SESSION_NAME', 'AWS_CA_BUNDLE', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_S3'):
            self.assertNotIn(name, environment)
        self.assertEqual(environment['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
        self.assertEqual(environment['AWS_PROFILE'], 'workloads-admin')
    def test_removal_accepts_only_matching_launch_lookup_state_removal(self):
        current = state(True)
        plan = {'resource_changes': [
            {'address': 'aws_instance.host[0]', 'mode': 'managed', 'change': {'actions': ['delete'], 'before': HOST_ATTRIBUTES}},
            {'address': 'aws_volume_attachment.data[0]', 'mode': 'managed', 'change': {'actions': ['delete'], 'before': ATTACHMENT_ATTRIBUTES}},
            {'address': 'data.aws_ami.host[0]', 'mode': 'data', 'change': {'actions': ['delete'], 'before': {'id': DEPLOYMENT['ami_id']}}}]}
        lifecycle.validate_plan(plan, present=False, state=current, deployment=DEPLOYMENT)
        plan['resource_changes'][2]['change']['before']['id'] = 'ami-unreviewed'
        with self.assertRaises(host.HostError): lifecycle.validate_plan(plan, present=False, state=current, deployment=DEPLOYMENT)

    def test_stopped_host_refresh_drift_is_narrow_and_retained_disk_drift_still_refused(self):
        current = state(True)
        before = copy.deepcopy(HOST_ATTRIBUTES)
        after = {**before, 'instance_state': 'stopped', 'public_ip': '', 'public_dns': '', 'associate_public_ip_address': False}
        before.update(instance_state='running', public_ip='192.0.0.9', public_dns='private', associate_public_ip_address=True)
        for resource in current['resources']:
            if resource['type'] == 'aws_instance': resource['instances'][0]['attributes'] = before
        plan = {'resource_drift': [{'address': 'aws_instance.host[0]', 'change': {'actions': ['update'], 'before': before, 'after': after}}],
                'resource_changes': [
                    {'address': 'aws_instance.host[0]', 'change': {'actions': ['delete'], 'before': after}},
                    {'address': 'aws_volume_attachment.data[0]', 'change': {'actions': ['delete'], 'before': ATTACHMENT_ATTRIBUTES}}]}
        lifecycle.validate_plan(plan, present=False, state=current, deployment=DEPLOYMENT, stopped_host=True)
        with self.assertRaises(host.HostError): lifecycle.validate_plan(plan, present=False, state=current, deployment=DEPLOYMENT)
        bad = copy.deepcopy(plan)
        bad['resource_drift'][0]['change']['after']['subnet_id'] = 'subnet-unreviewed'
        with self.assertRaises(host.HostError): lifecycle.validate_plan(bad, present=False, state=current, deployment=DEPLOYMENT, stopped_host=True)

    def test_saved_plan_gate_rejects_drift_deferred_actions_wrong_network_and_root(self):
        prior = state(False)
        base = {'resource_changes': [
            {'address': 'aws_instance.host[0]', 'change': {'actions': ['create'], 'after': HOST_ATTRIBUTES}},
            {'address': 'aws_volume_attachment.data[0]', 'change': {'actions': ['create'], 'after': ATTACHMENT_ATTRIBUTES}}]}
        lifecycle.validate_plan(base, present=True, state=prior, deployment=DEPLOYMENT)
        for kind in ('drift', 'deferred', 'network', 'root', 'detach'):
            plan = copy.deepcopy(base)
            if kind == 'drift': plan['resource_drift'] = [{'change': {'actions': ['update']}}]
            if kind == 'deferred': plan['deferred_changes'] = [{}]
            if kind == 'network': plan['resource_changes'][0]['change']['after']['subnet_id'] = 'subnet-elsewhere'
            if kind == 'root': plan['resource_changes'][0]['change']['after']['root_block_device'][0]['encrypted'] = False
            if kind == 'detach': plan['resource_changes'][1]['change']['after']['force_detach'] = True
            with self.subTest(kind=kind), self.assertRaises(host.HostError):
                lifecycle.validate_plan(plan, present=True, state=prior, deployment=DEPLOYMENT)

if __name__ == '__main__': unittest.main()
