#!/usr/bin/env python3
"""Receipt-bound S3-only session controller; no game starts before exact restore."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import re
import tempfile
import time
import uuid
from pathlib import Path

from minecraft_host import HostError, HostSession
from minecraft_on_demand import (OnDemandSession, SESSION_ADDRESSES, HOST_STOP_FIELDS, digest_file,
                                 matches_known, operation_lock, resources, save_private, validate_plan,
                                 owned_resource_matches, owned_inventory_matches, state_matches, created_resource_matches)
from minecraft_private_transfer import TransferError, build_bundle, private_file

DATA_ADDRESS = 'aws_ebs_volume.session_data[0]'
S3_ADDRESSES = SESSION_ADDRESSES | {DATA_ADDRESS}
PHASES = {'begun', 'receipts-verified', 'stopping-host', 'host-stopped', 'applying', 'applied',
          'configured-host', 'provenance-verified', 'initializing-storage', 'storage-ready',
          'runtime-installed', 'restoring', 'restored', 'starting-game', 'removed', 'complete'}


def reconcile(previous, current):
    before, after = resources(previous), resources(current)
    if previous['lineage'] != current['lineage'] or current['serial'] < previous['serial']:
        raise HostError('S3-only state lineage or serial changed unexpectedly')
    if not owned_inventory_matches(before, after, excluded=S3_ADDRESSES):
        raise HostError('protected infrastructure changed during S3-only operation')


def without_data(state):
    result = copy.deepcopy(state)
    result['resources'] = [value for value in result['resources'] if not (value.get('mode') == 'managed'
                           and value.get('type') == 'aws_ebs_volume' and value.get('name') == 'session_data')]
    return result


def validate_s3_plan(plan, *, present, state, deployment, journal):
    inventory = resources(state)
    data_drift = [value for value in plan.get('resource_drift', []) if value.get('address') == DATA_ADDRESS
                  and value.get('change', {}).get('actions') != ['no-op']]
    if data_drift:
        change = data_drift[0].get('change', {})
        if (len(data_drift) != 1 or present or journal.get('removal_started') is not True
                or journal.get('host_stop_requested') is not True or not journal.get('receipts') or not journal.get('receipt_ids')
                or change.get('actions') != ['delete'] or change.get('after') is not None
                or change.get('before') != inventory.get(DATA_ADDRESS)
                or not isinstance(change.get('before'), dict)
                or change['before'].get('id') != deployment['data_volume_id']):
            raise HostError('session data deletion drift lacks exact prior removal and receipt authority')
        del inventory[DATA_ADDRESS]
    reduced = {**plan, 'resource_changes': [value for value in plan.get('resource_changes', []) if value.get('address') != DATA_ADDRESS],
               'resource_drift': [value for value in plan.get('resource_drift', []) if value.get('address') != DATA_ADDRESS]}
    expected_deployment = {**deployment, 'data_volume_id': None if present else deployment['data_volume_id']}
    validate_plan(reduced, present=present, state=without_data(state), deployment=expected_deployment,
                  stopped_host=journal.get('host_stop_requested', False), removal_started=journal.get('removal_started', False),
                  ownership_state=state)
    changes = [value for value in plan.get('resource_changes', []) if value.get('address') == DATA_ADDRESS
               and value.get('change', {}).get('actions') != ['no-op']]
    expected = DATA_ADDRESS not in inventory if present else DATA_ADDRESS in inventory
    if len(changes) != int(expected):
        raise HostError('saved plan lacks exact remaining session data ownership')
    if changes:
        change = changes[0]['change']
        if change.get('actions') != ['create' if present else 'delete']:
            raise HostError('S3-only plan may only create or delete its exact session disk')
        if present:
            after = change.get('after', {})
            if (after.get('encrypted') is not True or after.get('type') != 'gp3'
                    or after.get('availability_zone') != deployment['availability_zone']
                    or after.get('size') != journal['target_storage']['size_gib'] or after.get('snapshot_id') not in (None, '')):
                raise HostError('session data create differs from receipt capacity/encryption/provenance policy')
        elif change.get('before') != inventory[DATA_ADDRESS] or change['before'].get('id') != deployment['data_volume_id']:
            raise HostError('session data delete differs from independently observed source ownership')
        if not present and (not journal.get('receipt_ids') or not journal.get('receipts')):
            raise HostError('data removal has no durable independently verified receipt')


def scan_fresh_marker_versions(store, instances, volume_id, workspace):
    """A fresh disk must not be named by any historical completed marker."""
    from minecraft_backup import MAX_METADATA_BYTES, read_json, validate_metadata
    count = 0
    for instance in sorted(instances):
        prefix = f'instances/{instance}/'
        completed = prefix + 'completed/'
        rows = store.list_versions(prefix)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise HostError('fresh-volume backup marker version inventory is incomplete or excessive')
        seen = set()
        for row in rows:
            try:
                key, version = row['key'], row['version_id']
                if not isinstance(key, str) or not key.startswith(prefix) or not isinstance(version, str) or not version or type(row['delete_marker']) is not bool or (key, version) in seen:
                    raise ValueError
                seen.add((key, version))
                if not key.startswith(completed):
                    continue
                if not re.fullmatch(re.escape(completed) + r'[0-9a-f]{64}\.json', key):
                    raise ValueError
                if row['delete_marker']:
                    continue
                path = Path(workspace) / 'marker-version.json'
                downloaded = store.download_revision(key, path, max_bytes=MAX_METADATA_BYTES, version_id=version)
                if not isinstance(downloaded, dict) or downloaded.get('key') != key or downloaded.get('version_id') != version:
                    raise ValueError
                metadata = validate_metadata(read_json(path), instance, key.rsplit('/', 1)[1][:-5])
                if metadata['source']['volume_id'] == volume_id:
                    raise HostError('target volume was already used by a completed recovery point; first setup forbidden')
                count += 1
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise HostError('fresh-volume marker version evidence is malformed or unavailable') from exc
    return {'completed_marker_versions_checked': count, 'target_volume_unused': True}


class S3Session(OnDemandSession):
    def __init__(self, *args, store_factory=None, receipt_verifier=None, receipt_loader=None, provenance=None, marker_scanner=None, retention=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.store_factory = store_factory or self.make_store
        self.receipt_verifier = receipt_verifier or self.verify_receipts
        self.receipt_loader = receipt_loader or self.load_receipts
        self.marker_scanner = marker_scanner or self.scan_markers
        self.retention = retention or self.reconcile_retention
        if provenance is None:
            from minecraft_provenance import verify_fresh_volume
            provenance = verify_fresh_volume
        self.provenance = provenance

    def make_store(self):
        from minecraft_backup_store import S3Store
        return S3Store(self.deployment['backup_bucket_name'], self.deployment['region'], timeout=3000,
                       environment_provider=self.aws.environment, executable_provider=self.aws.command_path)

    def verify_receipts(self, deployment, drain):
        from minecraft_backup import verify_stop_receipt
        return verify_stop_receipt(deployment, drain, self.directory, self.policy['bootstrap_sha256'],
                                   store=self.store_factory(), protect=True, timeout=3000,
                                   fresh_drain=lambda:self.running_session().remote('host-drain-private'))

    def load_receipts(self, expected_ids):
        from minecraft_backup import load_current_receipt
        from minecraft_retention import verify_current_selection_protection
        if not isinstance(expected_ids, dict) or set(expected_ids) != set(self.deployment['instances']):
            raise HostError('exact recovery selections are required for every configured instance')
        result = {}
        with tempfile.TemporaryDirectory(prefix='.selected-recovery-', dir=self.directory) as temporary:
            for instance in sorted(self.deployment['instances']):
                store = self.store_factory()
                workspace = Path(temporary) / instance
                workspace.mkdir(mode=0o700)
                receipt, selection = load_current_receipt(store, instance, workspace,
                                                           expected_receipt_id=expected_ids[instance])
                if receipt['bootstrap_sha256'] != self.policy['bootstrap_sha256']:
                    raise HostError('selected recovery requires a different pinned bootstrap release')
                protection = verify_current_selection_protection(store, instance, workspace,
                                                                  expected_selection=selection)
                if (not isinstance(protection, dict) or protection.get('instance') != instance
                        or protection.get('receipt_id') != expected_ids[instance]
                        or protection.get('protection') != 'verified'):
                    raise HostError('selected recovery lacks current read-only protection evidence')
                result[instance] = {'receipt_id': receipt['receipt_id'], 'receipt': receipt, 'selection': selection}
        return result

    def recheck_receipts(self, journal):
        current = self.receipt_loader(journal['receipt_ids'])
        if current != journal['receipts']:
            raise HostError('selected recovery changed during the receipt-bound operation')

    def reconcile_retention(self, journal):
        from minecraft_retention import reconcile_retention
        active = {name: {receipt_id} for name, receipt_id in journal['receipt_ids'].items()}
        for name, receipt_id in journal['source_deployment'].get('recovery_receipt_ids', {}).items():
            if name in active:
                active[name].add(receipt_id)
        results = {}
        for name, selected in journal['receipts'].items():
            results[name] = reconcile_retention(self.store_factory(), name, self.directory,
                                                current_selection=selected['selection'], active_receipt_ids=active)
        return results

    def private_bundle(self, journal, *, receipt=None):
        bundle, digest = build_bundle(self.deployment, Path(self.policy['operator_file']), Path(self.policy['secret_file']), receipt=receipt)
        files = json.loads(bundle)['files']
        actual = hashlib.sha256(files['operator.json'].encode() + b'\0' + files['rcon.secret'].encode()).hexdigest()
        if actual != journal['binding']['private_inputs_sha256']:
            raise HostError('private operator inputs changed before S3-only transfer')
        return bundle, digest

    def scan_markers(self):
        with tempfile.TemporaryDirectory(prefix='.fresh-marker-scan-', dir=self.directory) as temporary:
            return scan_fresh_marker_versions(self.store_factory(), self.deployment['instances'],
                                              self.deployment['data_volume_id'], Path(temporary))

    def binding(self):
        binding = super().binding()
        binding.pop('data_volume_id')
        binding.pop('data_filesystem_uuid')
        fixed = {key: value for key, value in self.deployment.items() if key not in ('host_id','session_present','data_volume_id','data_filesystem_uuid','data_volume_size_gib','recovery_receipt_ids')}
        binding['protected_handoff_sha256'] = hashlib.sha256(json.dumps(fixed,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return {**binding, 'lifecycle_mode': 's3-only', 'data_ownership': self.deployment.get('data_ownership')}

    def validate_inventory(self, state, *, allow_partial=False):
        inventory = resources(state)
        if any(key in inventory for key in ('aws_ebs_volume.data', 'aws_ebs_volume.data[0]', 'aws_instance.host', 'aws_volume_attachment.data')):
            raise HostError('S3-only data ownership migration is incomplete; retained resources remain protected')
        present = S3_ADDRESSES <= set(inventory)
        if S3_ADDRESSES.intersection(inventory) and not present and not allow_partial:
            raise HostError('S3-only session inventory is partial; resume its operation')
        if not allow_partial:
            if self.deployment.get('session_present') is not present or bool(self.deployment.get('host_id')) is not present:
                raise HostError('S3-only deployment handoff is stale')
            if present and (inventory['aws_instance.host[0]'].get('id') != self.deployment['host_id']
                            or inventory[DATA_ADDRESS].get('id') != self.deployment['data_volume_id']
                            or inventory['aws_volume_attachment.data[0]'].get('instance_id') != self.deployment['host_id']
                            or inventory['aws_volume_attachment.data[0]'].get('volume_id') != self.deployment['data_volume_id']):
                raise HostError('S3-only host/data/attachment identities differ from current state')
        return present

    def journal(self, action, instance=None):
        try:
            build_bundle(self.deployment, Path(self.policy['operator_file']), Path(self.policy['secret_file']))
        except TransferError as exc:
            raise HostError('reviewed private inputs fail transfer preflight') from exc
        if self.deployment.get('data_ownership') != 'session':
            raise HostError('reviewed session data ownership is required before S3-only lifecycle')
        current = self.tofu.state()
        if self.journal_path.exists():
            previous = json.loads(private_file(self.journal_path))
            try:
                identity = uuid.UUID(previous['operation_id'])
                valid = (previous['schema_version'] == 1 and str(identity) == previous['operation_id']
                         and identity.version == 4 and previous['phase'] in PHASES and previous['action'] in ('start', 'stop'))
            except (KeyError, ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise HostError('S3-only journal identity or schema is invalid; preserve it for repair')
            if previous['phase'] != 'complete':
                if previous['action'] != action or previous['instance'] != instance or previous['binding'] != self.binding():
                    raise HostError('another incomplete or changed S3-only operation requires repair')
                reconcile(previous['initial_state'], current)
                self.verify_recorded_sessions(previous, current)
                self.verify_resume_handoff(previous, current)
                self.resumed_operation = True
                return previous
            save_private(self.directory / (previous['operation_id'] + '.json'), previous)
        self.verify_handoff_inventory()
        self.validate_inventory(current)
        journal = {'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'action': action, 'instance': instance,
                   'phase': 'begun', 'binding': self.binding(), 'initial_state': current,
                   'source_deployment': self.deployment, 'started_at': self.command_started_at}
        save_private(self.journal_path, journal)
        return journal

    def verify_resume_handoff(self, journal, current):
        # A lost observation may leave the original handoff until refresh. Any
        # published successor must match both actual output and recorded targets.
        if self.deployment == journal['source_deployment']:
            return
        if self.tofu.deployment() != self.deployment:
            raise HostError('resumed S3-only handoff differs from authoritative output')
        if self.deployment.get('recovery_receipt_ids') != journal.get('receipt_ids'):
            raise HostError('resumed S3-only handoff changed its recorded recovery selections')
        inventory = resources(current)
        if journal['action'] == 'start':
            target = journal.get('target_storage', {})
            if (self.deployment.get('session_present') is not True
                    or self.deployment.get('host_id') != inventory.get('aws_instance.host[0]', {}).get('id')
                    or self.deployment.get('data_volume_id') != inventory.get(DATA_ADDRESS, {}).get('id')
                    or self.deployment.get('data_filesystem_uuid') != target.get('filesystem_uuid')
                    or self.deployment.get('data_volume_size_gib') != target.get('size_gib')):
                raise HostError('resumed S3-only handoff changed its recorded target storage')
        elif (self.deployment.get('session_present') is not False or S3_ADDRESSES.intersection(inventory)
              or any(self.deployment.get(key) is not None for key in ('host_id', 'data_volume_id', 'data_filesystem_uuid'))):
            raise HostError('resumed S3-only off-state does not match its completed removal')

    def verify_recorded_sessions(self, journal, current):
        inventory = resources(current)
        original = resources(journal['initial_state'])
        for key in S3_ADDRESSES:
            if key in inventory and key in original:
                ignore = HOST_STOP_FIELDS if key == 'aws_instance.host[0]' and journal.get('host_stop_requested') else set()
                if not owned_resource_matches(key, original[key], inventory[key], inventory, ignore=ignore):
                    raise HostError('source session identity or configuration changed during S3-only operation')
        if journal.get('session_inventory') is not None and not owned_inventory_matches(journal['session_inventory'],
                {key: value for key, value in inventory.items() if key in S3_ADDRESSES}, ownership=inventory):
            raise HostError('applied session ownership changed before S3-only completion')
        for value in journal.get('planned_changes', []):
            change, key = value['change'], value['address']
            if change['actions'] == ['create'] and key in inventory and not created_resource_matches(key, change, inventory[key]):
                raise HostError('created session differs from its receipt-bound saved plan')

    def apply_session(self, journal, present):
        current = self.tofu.state()
        reconcile(journal['initial_state'], current)
        self.verify_recorded_sessions(journal, current)
        if self.binding() != journal['binding']:
            raise HostError('S3-only operation inputs changed before planning')
        inventory = resources(current)
        completed = S3_ADDRESSES <= set(inventory) if present else not S3_ADDRESSES.intersection(inventory)
        if journal['phase'] in ('applying', 'applied') and completed:
            if present and not journal.get('successful_apply_window'):
                raise HostError('interrupted create lacks a verified successful apply window; preserve target for explicit repair')
            self.record(journal, 'applied', session_inventory={key: value for key, value in inventory.items() if key in S3_ADDRESSES})
            return
        if journal['phase'] == 'applying' and present:
            raise HostError('partial create lacks proven successful first-use window; preserve resources for explicit repair')
        path = self.directory / (journal['operation_id'] + '.tfplan')
        variables = {'recovery_receipt_ids': journal['receipt_ids']}
        if present:
            variables.update(data_filesystem_uuid=journal['target_storage']['filesystem_uuid'],
                             data_volume_size_gib=journal['target_storage']['size_gib'])
        plan = self.tofu.plan(path, present, variables=variables)
        validate_s3_plan(plan, present=present, state=current, deployment=journal['source_deployment'], journal=journal)
        if not state_matches(self.tofu.state(), current) or self.binding() != journal['binding']:
            raise HostError('S3-only state or inputs changed while planning; apply forbidden')
        digest = digest_file(path)
        started = dt.datetime.now(dt.timezone.utc)
        self.record(journal, 'applying', plan_sha256=digest, planned_state=current,
                    removal_started=not present,
                    planned_changes=[value for value in plan.get('resource_changes', []) if value.get('address') in S3_ADDRESSES])
        if digest_file(path) != digest:
            raise HostError('S3-only saved plan changed before apply')
        # Replaying an operation cannot repair retired tags or changed expiry rules.
        # Every actual create/removal is authorized by freshly verified sole copies.
        self.recheck_receipts(journal)
        if self.binding() != journal['binding'] or digest_file(path) != digest:
            raise HostError('S3-only inputs or saved plan changed during protection verification')
        self.tofu.apply(path)
        completed_at = dt.datetime.now(dt.timezone.utc)
        final = self.tofu.state()
        reconcile(journal['initial_state'], final)
        self.verify_recorded_sessions(journal, final)
        final_inventory = resources(final)
        if (present and not S3_ADDRESSES <= set(final_inventory)) or (not present and S3_ADDRESSES.intersection(final_inventory)):
            raise HostError('S3-only apply lacks exact final resource ownership')
        window = {'started_at_utc': started.replace(microsecond=0).isoformat(), 'completed_at_utc': completed_at.isoformat()}
        self.record(journal, 'applied', successful_apply_window=window if present else journal.get('successful_apply_window'),
                    session_inventory={key: value for key, value in final_inventory.items() if key in S3_ADDRESSES})

    def refresh_deployment(self, journal, *, present):
        candidate = self.tofu.deployment()
        fixed = ('account_id', 'region', 'availability_zone', 'lifecycle_mode', 'data_ownership', 'subnet_id', 'security_group_id',
                 'host_role_arn', 'operator_role_arn', 'host_instance_profile', 'ami_id', 'environment', 'resource_prefix',
                 'backup_bucket_name', 'instances', 'public_hostname', 'cloudflare_zone_id', 'bootstrap_sha256')
        if any(candidate.get(key) != self.deployment.get(key) for key in fixed):
            raise HostError('S3-only handoff changes a protected deployment identity')
        inventory = resources(self.tofu.state())
        if (candidate.get('schema_version') != 2 or candidate.get('session_present') is not present
                or candidate.get('recovery_receipt_ids') != journal['receipt_ids']):
            raise HostError('S3-only handoff lacks exact recovery selections and presence')
        if present:
            target = journal['target_storage']
            if (candidate.get('host_id') != inventory.get('aws_instance.host[0]', {}).get('id')
                    or candidate.get('data_volume_id') != inventory.get(DATA_ADDRESS, {}).get('id')
                    or candidate.get('data_filesystem_uuid') != target['filesystem_uuid']
                    or candidate.get('data_volume_size_gib') != target['size_gib']
                    or inventory.get('aws_volume_attachment.data[0]', {}).get('volume_id') != candidate['data_volume_id']
                    or inventory.get('aws_volume_attachment.data[0]', {}).get('instance_id') != candidate['host_id']):
                raise HostError('new session storage/host handoff differs from its receipt-bound target')
        elif any(candidate.get(key) is not None for key in ('host_id', 'data_volume_id', 'data_filesystem_uuid')) or S3_ADDRESSES.intersection(inventory):
            raise HostError('S3-only off-state still owns session host or data')
        save_private(self.deployment_path, candidate)
        self.deployment = candidate

    def attest_runtime(self, session):
        return super().attest_runtime(session)

    def verify_removed(self, journal):
        source = journal['source_deployment']
        host = resources(journal['initial_state']).get('aws_instance.host[0]')
        identifiers = [source.get('data_volume_id')]
        if host:
            rows = self.aws.run('ec2', 'describe-instances', '--filters', json.dumps([{'Name': 'instance-id', 'Values': [host['id']]}]), timeout=30).get('Reservations')
            if not isinstance(rows, list) or any(not isinstance(value, dict) or not isinstance(value.get('Instances'), list) for value in rows):
                raise HostError('host removal has invalid independent EC2 proof')
            nodes = [value for row in rows for value in row['Instances']]
            if nodes and (len(nodes) != 1 or nodes[0].get('InstanceId') != host['id'] or nodes[0].get('State', {}).get('Name') != 'terminated'):
                raise HostError('S3-only host remains present')
            roots = host.get('root_block_device', [])
            if len(roots) != 1 or not roots[0].get('volume_id'):
                raise HostError('source root disk identity is missing')
            identifiers.append(roots[0]['volume_id'])
        for identifier in identifiers:
            if identifier:
                volumes = self.aws.run('ec2', 'describe-volumes', '--filters', json.dumps([{'Name': 'volume-id', 'Values': [identifier]}]), timeout=30).get('Volumes')
                if volumes != []:
                    raise HostError('S3-only session EBS removal lacks independent proof')

    def stop(self):
        with operation_lock(self.directory):
            journal = self.journal('stop')
            if journal['phase'] == 'begun' and not self.deployment.get('host_id'):
                self.verify_removed(journal)
                receipts = self.receipt_loader(self.deployment['recovery_receipt_ids'])
                self.record(journal, 'removed', receipts=receipts, receipt_ids=self.deployment['recovery_receipt_ids'])
            if journal['phase'] in ('begun', 'receipts-verified', 'stopping-host', 'host-stopped', 'applying') and 'aws_instance.host[0]' in resources(self.tofu.state()):
                session = self.running_session()
                state = self.removal_host_state(journal, session)
                self.state = state
                if state == 'running':
                    session.wait_ssm()
                    self.attest_runtime(session)
                    self.verify_private_inputs(session)
                    drain = session.remote('host-drain-private')
                    self.observed = {'instances': session.instance_statuses(drain)}
                    receipts = self.receipt_verifier(self.deployment, drain)
                    ids = {name: value['receipt_id'] for name, value in receipts.items()}
                    if set(ids) != set(self.deployment['instances']):
                        raise HostError('private receipt inventory differs from configured instances')
                    self.record(journal, 'receipts-verified', receipts=receipts, receipt_ids=ids, drain=drain,
                                source_storage=[{'volume_id': self.deployment['data_volume_id'], 'filesystem_uuid': self.deployment['data_filesystem_uuid']}])
                    retired = self.retention(journal)
                    self.record(journal, 'stopping-host', host_stop_requested=True, retention=retired)
                    session.call('ec2', 'stop-instances', '--instance-ids', self.deployment['host_id'])
                    session.wait_host('stopped', 'stopping', previous='running')
                elif state == 'stopping' and journal.get('host_stop_requested'):
                    session.wait_host('stopped', 'stopping')
                elif state in ('absent', 'terminated') and journal.get('removal_started') is True and journal.get('host_stop_requested') is True and journal.get('receipts'):
                    pass  # exact drift gate and final independent disk proof still apply
                elif state != 'stopped' or not journal.get('host_stop_requested') or not journal.get('receipts'):
                    raise HostError('source lacks verified receipt and graceful stop evidence')
                # Re-read exact selected versions outside the disk on every resumed delete.
                journal['receipts'] = self.receipt_loader(journal['receipt_ids'])
                session.sync_dns()
                self.dns, self.dns_error = session.dns, session.dns_error
                self.record(journal, 'host-stopped')
            if journal['phase'] in ('host-stopped', 'applying', 'applied'):
                journal['receipts'] = self.receipt_loader(journal['receipt_ids'])
                self.apply_session(journal, False)
                self.verify_removed(journal)
                self.refresh_deployment(journal, present=False)
                self.record(journal, 'removed')
            self.state = 'absent'
            session = HostSession(self.deployment, self.aws, self.cloudflare)
            session.state = 'stopped'
            session.sync_dns()
            self.dns, self.dns_error = session.dns, session.dns_error
            self.record(journal, 'complete')
            return {'host': 'absent', 'root_disk': 'absent', 'data_disk': 'absent', 'recovery': 'verified'}

    def prepare_start(self, journal):
        ids = journal['source_deployment'].get('recovery_receipt_ids')
        if not isinstance(ids, dict) or set(ids) != set(self.deployment['instances']):
            raise HostError('S3-only start requires authoritative exact receipt selections')
        receipts = self.receipt_loader(ids)
        if set(receipts) != set(ids) or any(value.get('receipt_id') != ids[name] or value['receipt'].get('bootstrap_sha256') != self.policy['bootstrap_sha256']
                or any(value['receipt']['source'].get(field) != self.deployment[field] for field in ('account_id', 'region', 'backup_bucket_name'))
                for name, value in receipts.items()):
            raise HostError('authoritative recovery ownership or bootstrap release differs from deployment')
        required = sum(value['receipt']['required_capacity_bytes'] for value in receipts.values())
        capacity = max(2, math.ceil(required / (1024 ** 3 * 0.90)))
        if capacity > 128:
            raise HostError('selected recovery exceeds the reviewed maximum session disk capacity')
        sources = [{'volume_id': value['receipt']['source']['volume_id'], 'filesystem_uuid': value['receipt']['source']['filesystem_uuid']} for value in receipts.values()]
        target_uuid = str(uuid.uuid4())
        if any(value['filesystem_uuid'] == target_uuid for value in sources):
            raise HostError('target filesystem identity reuses a recovery source')
        self.record(journal, 'receipts-verified', receipts=receipts, receipt_ids=ids, source_storage=sources,
                    target_storage={'filesystem_uuid': target_uuid, 'size_gib': capacity}, restored_instances=[])

    def start(self, instance):
        with operation_lock(self.directory):
            journal = self.journal('start', instance)
            if journal['phase'] == 'begun' and self.deployment.get('host_id'):
                session = self.running_session()
                session.wait_ssm()
                self.attest_runtime(session)
                self.verify_private_inputs(session)
                self.record(journal, 'starting-game')
            elif journal['phase'] == 'begun':
                self.prepare_start(journal)
            if journal['phase'] in ('receipts-verified', 'applying', 'applied'):
                self.apply_session(journal, True)
                self.refresh_deployment(journal, present=True)
                self.record(journal, 'configured-host')
            session = self.running_session()
            self.state = session.host_state()
            if self.state == 'pending':
                session.wait_host('running', 'pending')
            elif self.state != 'running':
                raise HostError('S3-only target host did not reach running')
            session.wait_ssm()
            if journal['phase'] == 'configured-host':
                proof = self.provenance(self.aws, self.deployment, journal, timeout=1800)
                if (not isinstance(proof, dict) or proof.get('provenance_ready') is not True
                        or proof.get('mandatory_wait_seconds') != 900
                        or proof.get('volume_id') != self.deployment['data_volume_id'] or proof.get('host_id') != self.deployment['host_id']
                        or proof.get('filesystem_uuid') != self.deployment['data_filesystem_uuid']
                        or proof.get('receipt_ids') != journal['receipt_ids']
                        or proof.get('successful_apply_window') != journal['successful_apply_window']):
                    raise HostError('fresh-volume provenance differs from its receipt-bound target')
                marker_evidence = self.marker_scanner()
                if not isinstance(marker_evidence, dict) or marker_evidence.get('target_volume_unused') is not True:
                    raise HostError('fresh-volume marker version scan lacks complete unused-target evidence')
                self.record(journal, 'provenance-verified', provenance=proof, marker_version_scan=marker_evidence)
            if journal['phase'] == 'provenance-verified':
                expiry = dt.datetime.fromisoformat(journal['provenance']['provenance_expires_at_utc'].replace('Z', '+00:00'))
                if dt.datetime.now(dt.timezone.utc) >= expiry:
                    raise HostError('fresh-volume provenance expired before zero-fill; revalidate target for repair')
                self.record(journal, 'initializing-storage')
                volume, filesystem = self.deployment['data_volume_id'], self.deployment['data_filesystem_uuid']
                session.execute_shell(f'/usr/local/sbin/minecraft-storage zero-fill --volume-id {volume} --confirm-volume-id {volume} >/dev/null && '
                                      f'/usr/local/sbin/minecraft-storage initialize --volume-id {volume} --confirm-volume-id {volume} --filesystem-uuid {filesystem} >/dev/null && '
                                      'printf \'{"storage":"initialized"}\\n\'', 'storage-initialize')
                self.record(journal, 'storage-ready')
            elif journal['phase'] == 'initializing-storage':
                # Initialization may have completed after lost observation. A fresh
                # matching mount verification can prove that exact target; no reformat.
                volume, filesystem = self.deployment['data_volume_id'], self.deployment['data_filesystem_uuid']
                session.execute_shell(f'/usr/local/sbin/minecraft-storage mount --volume-id {volume} --filesystem-uuid {filesystem} >/dev/null && '
                                      'printf \'{"storage":"mounted"}\\n\'', 'storage-resume')
                self.record(journal, 'storage-ready')
            if journal['phase'] == 'storage-ready':
                session.execute_shell('bash /var/lib/cloud/instance/scripts/part-001 >/dev/null && printf \'{"bootstrap":"ready"}\\n\'', 'bootstrap-prerequisites')
                digest = self.policy['bootstrap_sha256']
                session.execute_shell(f'/usr/local/sbin/minecraft-bootstrap stage --region {self.deployment["region"]} --bucket {self.deployment["backup_bucket_name"]} --sha256 {digest}', 'bootstrap-release')
                bundle, bundle_digest = self.private_bundle(journal)
                self.transfer(session, bundle, bundle_digest)
                self.verify_private_inputs(session)
                session.execute_shell(f'bash /opt/minecraft-bootstrap/{digest}/scripts/install_runtime.sh >/dev/null && printf \'{{"runtime":"installed"}}\\n\'', 'runtime-install')
                self.attest_runtime(session)
                self.record(journal, 'runtime-installed')
            if journal['phase'] in ('runtime-installed', 'restoring'):
                current = self.receipt_loader(journal['receipt_ids'])
                if any(current[name]['receipt'] != journal['receipts'][name]['receipt'] for name in current):
                    raise HostError('selected recovery changed before restore')
                self.record(journal, 'restoring')
                for name in sorted(journal['receipt_ids']):
                    receipt = journal['receipts'][name]['receipt']
                    bundle, digest = self.private_bundle(journal, receipt=receipt)
                    self.transfer(session, bundle, digest)
                    self.verify_private_inputs(session)
                    command = ('/usr/local/sbin/minecraft-restore --config /etc/minecraft/operator.json '
                               '--receipt /etc/minecraft/recovery-receipt.json --bootstrap-sha256 ' + self.policy['bootstrap_sha256']
                               + ' --timeout 3000 --resume ' + name + ' ' + receipt['recovery_point'])
                    result = session.execute_shell(command, 'receipt-restore')
                    if result.get('status') != 'restored' or result.get('receipt_id') != receipt['receipt_id'] or result.get('application') != 'stopped' or result.get('instance') != name:
                        raise HostError('receipt restore lacks exact complete target evidence')
                    if name not in journal['restored_instances']:
                        journal['restored_instances'].append(name)
                    self.record(journal, 'restoring')
                self.record(journal, 'restored')
            if journal['phase'] == 'restored':
                self.record(journal, 'starting-game')
            if self.binding() != journal['binding']:
                raise HostError('S3-only inputs changed before game start')
            self.verify_private_inputs(session)
            try:
                result = session.start(instance)
            finally:
                self.adopt(session)
            elapsed = self.elapsed_time(journal)
            self.record(journal, 'complete', ready_seconds=elapsed)
            return {**result, 'start_to_ready_seconds': None if elapsed is None else round(elapsed,3), 'startup_target_seconds': None,
                    'startup_target_met': None, 'provenance_wait_seconds': 900, 'restored_from_s3': bool(journal.get('receipts'))}

    def verify_handoff_inventory(self):
        if self.tofu.deployment() != self.deployment:
            raise HostError('complete S3-only handoff differs from authoritative infrastructure output')
        return self.validate_inventory(self.tofu.state())

    def status(self):
        with operation_lock(self.directory):
            if not self.verify_handoff_inventory():
                self.state = 'absent'
                return {'host': 'absent', 'data_disk': 'absent', 'application': 'unavailable', 'recovery': 'unverified'}
            session = self.running_session()
            result = session.status()
            self.adopt(session)
            return result
