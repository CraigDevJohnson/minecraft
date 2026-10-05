#!/usr/bin/env python3
"""Operator-owned, resumable session lifecycle with actual saved-plan verification."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from minecraft_host import AWSCLI, HostError, HostSession, source_principal, verified_executable
from minecraft_private_transfer import TransferError, build_bundle, private_file, send_bundle

HOST_STOP_FIELDS = frozenset(('instance_state', 'public_ip', 'public_dns', 'associate_public_ip_address'))
SESSION_ADDRESSES = frozenset(('aws_instance.host[0]', 'aws_volume_attachment.data[0]'))
SHA256 = re.compile('[0-9a-f]{64}')
POLICY_FIELDS = frozenset(('schema_version', 'workstation', 'aws_profile', 'aws_source_identity', 'aws_cli_path', 'aws_cli_sha256', 'aws_cli_file_identity',
                          'session_plugin_path', 'session_plugin_sha256', 'session_plugin_file_identity', 'infra_root', 'variables_file', 'variables_sha256',
                          'source_sha256', 'backend', 'backend_sha256', 'tofu_sha256', 'tofu_file_identity', 'private_inputs_sha256', 'bootstrap_sha256', 'operator_file', 'secret_file', 'journal_dir'))


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workstation_identity() -> str:
    """Hash the trusted platform identity without exposing its raw hardware ID."""
    try:
        if sys.platform == 'darwin':
            result = subprocess.run(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                                    capture_output=True, text=True, check=True, timeout=5,
                                    env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
            matches = re.findall(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
            if len(matches) != 1 or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', matches[0]):
                raise ValueError()
            identifier = uuid.UUID(matches[0]).hex
        elif sys.platform.startswith('linux'):
            descriptor = os.open('/etc/machine-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'r', encoding='ascii') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError()
                identifier = stream.read(34)
            if not re.fullmatch('[0-9a-f]{32}\n?', identifier):
                raise ValueError()
            identifier = identifier.removesuffix('\n')
        else:
            raise ValueError()
        if identifier == '0' * 32:
            raise ValueError()
        digest = hashlib.sha256(('minecraft-workstation-v1\0' + sys.platform + '\0' + identifier).encode('ascii')).hexdigest()
        return 'device-sha256:' + digest
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        raise HostError('cannot establish stable operator workstation identity') from None


def verify_workstation(expected):
    if not isinstance(expected, str) or not expected:
        raise HostError('lifecycle workstation identity is invalid')
    if expected.startswith('device-sha256:'):
        if not SHA256.fullmatch(expected.removeprefix('device-sha256:')):
            raise HostError('lifecycle workstation device digest is invalid')
        actual = workstation_identity()
    else:
        actual = socket.gethostname()
    if expected != actual:
        raise HostError('lifecycle policy is assigned to another operator workstation')


def canonical_infra_root(path: Path) -> Path:
    """Bind the executed root to the exact repository directory that is hashed."""
    try:
        if not path.is_absolute() or path.name != 'infra' or any(part.is_symlink() for part in [path, *path.parents]):
            raise HostError('lifecycle must execute the canonical reviewed infra directory without symlinks')
        resolved = path.resolve(strict=True)
        if resolved != path or not resolved.is_dir():
            raise HostError('lifecycle infrastructure directory is not canonical')
        return resolved
    except OSError as exc:
        raise HostError('cannot establish canonical lifecycle infrastructure ownership') from exc


def source_digest(root: Path) -> str:
    root = canonical_infra_root(root / 'infra').parent
    paths = sorted(path for pattern in ('*.tf', '*.tf.json', '*.tofu', '*.tofu.json') for path in (root / 'infra').glob(pattern)) + [root / 'infra/.terraform.lock.hcl']
    paths += [root / 'scripts' / name for name in ('host-bootstrap.sh.tftpl', 'on-demand-bootstrap.sh.tftpl', 'install_aws_cli.sh', 'install_runtime.sh')]
    paths += sorted((root / 'scripts').glob('*.py'))
    digest = hashlib.sha256()
    for path in paths:
        if path.is_symlink() or path.name in ('override.tf', 'override.tf.json', 'override.tofu', 'override.tofu.json') or path.name.endswith(('_override.tf', '_override.tf.json', '_override.tofu', '_override.tofu.json')):
            raise HostError('unreviewed infrastructure override or symlink blocks lifecycle')
        digest.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def private_directory(path: Path) -> None:
    try:
        if not path.exists():
            path.mkdir(mode=0o700, parents=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise HostError('lifecycle journal requires a trusted owner-only directory')
    except OSError as exc:
        raise HostError('cannot access lifecycle journal directory') from exc


def save_private(path: Path, value: dict) -> None:
    private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix='.journal-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def operation_lock(directory: Path):
    private_directory(directory)
    try:
        descriptor = os.open(directory / 'operation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise HostError('lifecycle operation lock is untrusted')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HostError('another lifecycle operation owns the workstation lock') from exc
        yield
    except OSError as exc:
        raise HostError('cannot acquire lifecycle operation lock') from exc
    finally:
        if 'descriptor' in locals():
            os.close(descriptor)


def nonempty(value):
    if isinstance(value, dict):
        return any(nonempty(item) for item in value.values())
    if isinstance(value, list):
        return any(nonempty(item) for item in value)
    return value not in (None, '', False)


def backend_digest(root, policy):
    try:
        path = root / '.terraform/terraform.tfstate'
        if path.is_symlink() or (root / '.terraform').is_symlink():
            raise HostError('initialized backend metadata must not be symlinked')
        metadata = json.loads(path.read_text())['backend']
        actual = metadata['config']
        forbidden = ('endpoints', 'endpoint', 's3_endpoint', 'sts_endpoint', 'iam_endpoint', 'dynamodb_endpoint',
                     'access_key', 'secret_key', 'token', 'shared_credentials_file', 'shared_credentials_files', 'shared_config_files',
                     'custom_ca_bundle', 'insecure', 'skip_credentials_validation', 'skip_region_validation',
                     'skip_requesting_account_id', 'skip_s3_checksum', 'force_path_style', 'use_path_style')
        if any(nonempty(actual.get(name)) for name in forbidden) or nonempty(actual.get('profile')):
            raise HostError('initialized backend uses unreviewed endpoints, credentials or insecure settings')
        role = actual.get('assume_role_with_web_identity')
        if nonempty(role):
            raise HostError('initialized backend has an unreviewed alternate credential path')
        workspace = root / '.terraform/environment'
        if workspace.is_symlink() or workspace.exists() and workspace.read_text().strip() != 'default':
            raise HostError('initialized backend workspace differs from reviewed default')
        return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HostError('cannot establish reviewed backend configuration identity') from exc


def tool_identity(root, policy):
    path = root.parent / '.tools/bin/tofu'
    try:
        if any(value.is_symlink() for value in [path, *path.parents]):
            raise HostError('reviewed OpenTofu executable must have no symlinked path component')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or stat.S_IMODE(info.st_mode) & 0o022 or not stat.S_IMODE(info.st_mode) & 0o111):
                raise HostError('reviewed OpenTofu executable is not trusted and executable')
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        expected = policy['tofu_file_identity']
        if (set(expected) != {'device', 'inode'} or any(type(expected[name]) is not int for name in expected)
                or expected != {'device': info.st_dev, 'inode': info.st_ino} or digest != policy['tofu_sha256']):
            raise HostError('OpenTofu executable identity or digest differs from reviewed policy')
        return info.st_dev, info.st_ino
    except (OSError, KeyError, TypeError) as exc:
        raise HostError('cannot validate reviewed OpenTofu executable') from exc


def private_inputs_digest(policy):
    return hashlib.sha256(private_file(Path(policy['operator_file'])) + b'\0' + private_file(Path(policy['secret_file']))).hexdigest()


def execution_tools(policy):
    aws = verified_executable(policy['aws_cli_path'], policy['aws_cli_sha256'], policy['aws_cli_file_identity'])
    plugin = verified_executable(policy['session_plugin_path'], policy['session_plugin_sha256'], policy['session_plugin_file_identity'])
    if Path(plugin).name != 'session-manager-plugin':
        raise HostError('reviewed Session Manager plugin has an invalid executable name')
    return aws, plugin


def load_policy(path: Path, expected_sha256: str, deployment: dict) -> dict:
    try:
        data = private_file(path)
        if not SHA256.fullmatch(expected_sha256 or '') or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise HostError('lifecycle policy does not match its reviewed SHA-256')
        policy = json.loads(data)
        if set(policy) != POLICY_FIELDS or policy['schema_version'] != 4:
            raise HostError('lifecycle policy schema is invalid')
        if not isinstance(policy['aws_profile'], str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', policy['aws_profile']):
            raise HostError('lifecycle AWS profile must be explicit and valid')
        identity=policy['aws_source_identity']
        if not isinstance(identity,dict) or set(identity)!={'account_id','principal_arn','principal_id'}:
            raise HostError('reviewed source principal schema is invalid')
        assumed=isinstance(identity['principal_arn'],str) and ':assumed-role/' in identity['principal_arn']
        caller={'Account':identity['account_id'],'Arn':identity['principal_arn'] + ('/reviewed-session' if assumed else ''),
                'UserId':identity['principal_id'] + (':reviewed-session' if assumed else '')}
        if source_principal(caller)!=identity:
            raise HostError('reviewed source principal representation is invalid')
        verify_workstation(policy['workstation'])
        for name in ('infra_root', 'variables_file', 'operator_file', 'secret_file', 'journal_dir'):
            if not isinstance(policy[name], str) or not Path(policy[name]).is_absolute():
                raise HostError('lifecycle paths must be explicit absolute paths')
        root = canonical_infra_root(Path(policy['infra_root']))
        if (root / '.terraform').is_symlink() or not root.is_dir():
            raise HostError('lifecycle infrastructure root is invalid')
        if set(policy['backend']) != {'bucket', 'key', 'region'} or any(not isinstance(value, str) or not value for value in policy['backend'].values()):
            raise HostError('lifecycle backend identity is invalid')
        automatic = list(root.glob('*.auto.tfvars')) + list(root.glob('*.auto.tfvars.json')) + [root / 'terraform.tfvars', root / 'terraform.tfvars.json']
        if any(path.exists() and path.resolve() != Path(policy['variables_file']).resolve() for path in automatic):
            raise HostError('unreviewed automatic Terraform variables block lifecycle')
        workspace = root / '.terraform/environment'
        if workspace.exists() and workspace.read_text().strip() != 'default':
            raise HostError('lifecycle requires the reviewed default backend workspace')
        metadata = json.loads((root / '.terraform/terraform.tfstate').read_text())['backend']
        actual = metadata['config']
        role = actual.get('assume_role')
        if isinstance(role, list) and len(role) == 1:
            role = role[0]
        if (not isinstance(role, dict) or role.get('role_arn') != deployment.get('operator_role_arn')
                or actual.get('allowed_account_ids') != [deployment['account_id']]):
            raise HostError('initialized backend account or operator role differs from deployment')
        if metadata['type'] != 's3' or any(actual.get(name) != value for name, value in policy['backend'].items()) or actual.get('encrypt') is not True or actual.get('use_lockfile') is not True:
            raise HostError('initialized backend differs from the reviewed lifecycle policy')
        if backend_digest(root, policy) != policy['backend_sha256']:
            raise HostError('initialized backend security configuration differs from reviewed policy')
        tool_identity(root, policy)
        execution_tools(policy)
        if private_inputs_digest(policy) != policy['private_inputs_sha256']:
            raise HostError('private operator inputs differ from reviewed policy')
        build_bundle(deployment, Path(policy['operator_file']), Path(policy['secret_file']))
        if digest_file(Path(policy['variables_file'])) != policy['variables_sha256'] or source_digest(root.parent) != policy['source_sha256']:
            raise HostError('lifecycle infrastructure source or private variables changed')
        if not SHA256.fullmatch(policy['bootstrap_sha256']) or deployment.get('bootstrap_sha256') != policy['bootstrap_sha256']:
            raise HostError('deployment bootstrap release differs from the lifecycle policy')
        private_file(Path(policy['variables_file']))
        private_file(Path(policy['operator_file']))
        private_file(Path(policy['secret_file']))
        return policy
    except (TransferError, OSError, ValueError, KeyError, TypeError) as exc:
        raise HostError('cannot validate trusted lifecycle policy and backend') from exc


def resources(state: dict) -> dict:
    """Exact resource state, including protected ownership, for interruption reconciliation."""
    if not isinstance(state, dict) or not isinstance(state.get('lineage'), str) or not state['lineage'] or type(state.get('serial')) is not int:
        raise HostError('infrastructure state lacks lineage and serial')
    result = {}
    for resource in state.get('resources', []):
        if resource.get('mode') == 'data':
            continue
        if resource.get('mode') != 'managed' or resource.get('module'):
            raise HostError('unexpected infrastructure state resource scope')
        for instance in resource.get('instances', []):
            address = resource['type'] + '.' + resource['name']
            if 'index_key' in instance:
                key = instance['index_key']
                address += '[' + (str(key) if type(key) is int else json.dumps(key)) + ']'
            if address in result or instance.get('deposed') or not isinstance(instance.get('attributes'), dict):
                raise HostError('infrastructure state contains ambiguous resource ownership')
            result[address] = instance['attributes']
    return result


def state_matches(before: dict, after: dict) -> bool:
    """Compare full state, canonicalizing only unordered check-result identities."""
    def ordered(items, identity):
        if not isinstance(items, list):
            raise ValueError()
        names = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get(identity), str) or not item[identity]:
                raise ValueError()
            names.append(item[identity])
        if len(set(names)) != len(names):
            raise ValueError()
        return sorted(items, key=lambda item: item[identity])

    def canonical(state):
        if not isinstance(state, dict):
            raise ValueError()
        if 'check_results' not in state or state['check_results'] is None:
            return state
        checks = []
        for item in ordered(state['check_results'], 'config_addr'):
            value = dict(item)
            if 'objects' in value and value['objects'] is not None:
                value['objects'] = ordered(value['objects'], 'object_addr')
            checks.append(value)
        return {**state, 'check_results': checks}

    try:
        return json.dumps(canonical(before), sort_keys=True, separators=(',', ':'), allow_nan=False) == json.dumps(
            canonical(after), sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise HostError('infrastructure state has malformed or duplicate check-result identities') from None


def owned_resource_matches(address, before, after, inventory, *, ignore=(), require_refresh=False) -> bool:
    """Accept only refreshed computed metadata proven by separately owned resources."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    old = {key: value for key, value in before.items() if key not in ignore}
    new = {key: value for key, value in after.items() if key not in ignore}
    if old == new and not require_refresh:
        return True
    try:
        if address == 'aws_iam_role.host':
            if {key: value for key, value in old.items() if key != 'inline_policy'} != {key: value for key, value in new.items() if key != 'inline_policy'}:
                return False
            known = {'aws_iam_role_policy.ssm': 'session-manager', 'aws_iam_role_policy.backups': 'instance-backups',
                     'aws_iam_role_policy.bootstrap[0]': 'read-pinned-bootstrap'}
            if not isinstance(after.get('name'), str) or after.get('id') != after['name']:
                return False
            managed = {}
            for key, value in inventory.items():
                if key.startswith('aws_iam_role_policy.') and value.get('role') == after['name']:
                    if key not in known or value.get('name') != known[key]:
                        return False
                    document = json.loads(value['policy'])
                    if not isinstance(document, dict):
                        return False
                    managed[value['name']] = document
            if not {'session-manager', 'instance-backups'} <= set(managed):
                return False
            def inline(value):
                result = {}
                if not isinstance(value, list):
                    raise ValueError()
                for item in value:
                    if set(item) != {'name', 'policy'} or item['name'] in result or item['name'] not in managed:
                        raise ValueError()
                    result[item['name']] = json.loads(item['policy'])
                    if not isinstance(result[item['name']], dict):
                        raise ValueError()
                return result
            # BEFORE is pinned cached provider state, possibly predating an owned
            # policy update. Only the fully refreshed AFTER may establish its body.
            inline(before.get('inline_policy', []))
            return inline(after.get('inline_policy', [])) == managed
        if address == 'aws_instance.host[0]':
            if {key: value for key, value in old.items() if key != 'ebs_block_device'} != {key: value for key, value in new.items() if key != 'ebs_block_device'}:
                return False
            attachment = inventory.get('aws_volume_attachment.data[0]', {})
            disks = [inventory[key] for key in ('aws_ebs_volume.data', 'aws_ebs_volume.data[0]', 'aws_ebs_volume.session_data[0]') if key in inventory]
            if (len(disks) != 1 or attachment.get('instance_id') != after.get('id')
                    or attachment.get('volume_id') != disks[0].get('id') or attachment.get('device_name') != '/dev/sdf'
                    or attachment.get('force_detach') is not False or attachment.get('skip_destroy') is not False):
                return False
            disk = disks[0]
            if (disk.get('encrypted') is not True or disk.get('type') != 'gp3'
                    or type(disk.get('size')) is not int or disk['size'] < 1
                    or disk.get('availability_zone') != after.get('availability_zone')):
                return False
            block = {'delete_on_termination': False, 'device_name': attachment['device_name'],
                     'volume_id': disk['id'], 'encrypted': disk['encrypted'], 'volume_type': disk['type'], 'volume_size': disk['size'],
                     # Both computed instance maps report the data volume's
                     # effective tags, including provider default tags.
                     'tags': disk['tags_all'],
                     **{key: disk[key] for key in ('iops', 'kms_key_id', 'snapshot_id', 'tags_all', 'throughput')}}
            if not isinstance(block['tags'], dict):
                return False
            def valid_block(value):
                if (not isinstance(value, dict) or value.get('delete_on_termination') is not False
                        or value.get('encrypted') is not True or type(value.get('volume_size')) is not int):
                    return False
                return value == block
            old_blocks, new_blocks = before.get('ebs_block_device', []), after.get('ebs_block_device')
            return (old_blocks == [] or isinstance(old_blocks, list) and len(old_blocks) == 1 and valid_block(old_blocks[0])) and isinstance(new_blocks, list) and len(new_blocks) == 1 and valid_block(new_blocks[0])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return False


def owned_inventory_matches(before, after, *, excluded=(), ownership=None) -> bool:
    old = {key: value for key, value in before.items() if key not in excluded}
    new = {key: value for key, value in after.items() if key not in excluded}
    return set(old) == set(new) and all(owned_resource_matches(key, value, new[key], after if ownership is None else ownership) for key, value in old.items())


def reconcile_state(previous: dict, current: dict) -> None:
    before, after = resources(previous), resources(current)
    if previous['lineage'] != current['lineage'] or current['serial'] < previous['serial']:
        raise HostError('infrastructure state lineage or serial changed unexpectedly')
    if not owned_inventory_matches(before, after, excluded=SESSION_ADDRESSES):
        raise HostError('protected infrastructure changed during this lifecycle operation')


def matches_known(expected, actual, unknown=None) -> bool:
    """Compare planned configuration, excluding only provider-declared unknown fields."""
    if unknown is True:
        return True
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(matches_known(value, actual.get(name), unknown.get(name) if isinstance(unknown, dict) else None)
                                                for name, value in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
                and all(matches_known(value, actual[index], unknown[index] if isinstance(unknown, list) and index < len(unknown) else None)
                        for index, value in enumerate(expected)))
    return expected == actual


def created_resource_matches(address, change, actual) -> bool:
    """Resolve only the omitted EC2 default-disabled hibernation value."""
    expected, unknown = change.get('after'), change.get('after_unknown')
    if address == 'aws_instance.host[0]' and isinstance(expected, dict) and isinstance(actual, dict):
        if any(value is not None and value is not False for value in (expected.get('hibernation'), actual.get('hibernation'))):
            return False
        if 'hibernation' in expected and expected['hibernation'] is None and actual.get('hibernation') is False:
            expected = {**expected, 'hibernation': False}
    return matches_known(expected, actual, unknown)


def verify_resumed_sessions(journal: dict, current: dict) -> None:
    inventory = resources(current)
    initial = resources(journal['initial_state'])
    for address in SESSION_ADDRESSES:
        ignore = HOST_STOP_FIELDS if address == 'aws_instance.host[0]' and journal.get('host_stop_requested') else ()
        if address in initial and address in inventory and not owned_resource_matches(address, initial[address], inventory[address], inventory, ignore=ignore):
            raise HostError('original session resource identity or configuration changed during the operation')
    observed = journal.get('session_inventory')
    if observed is not None and not owned_inventory_matches(observed, {name: value for name, value in inventory.items() if name in SESSION_ADDRESSES}, ownership=inventory):
        raise HostError('applied session inventory changed before operation completion')
    for change in journal.get('planned_changes', []):
        address = change['address']
        if change['change']['actions'] == ['create'] and address in inventory:
            if not created_resource_matches(address, change['change'], inventory[address]):
                raise HostError('partially created session does not match its pinned saved plan')


def validate_plan(plan: dict, *, present: bool, state: dict, deployment: dict, stopped_host=False,
                  removal_started=False, ownership_state=None) -> None:
    if not isinstance(plan, dict) or plan.get('errored') or plan.get('applyable') is False:
        raise HostError('session plan is invalid or cannot be applied')
    inventory = resources(state)
    ownership = resources(state if ownership_state is None else ownership_state)
    drift_seen = set()
    for drift in plan.get('resource_drift', []):
        change = drift.get('change', {})
        if change.get('actions') == ['no-op']:
            continue
        address = drift.get('address')
        before, after = change.get('before'), change.get('after')
        if address in drift_seen:
            raise HostError('session plan repeats resource drift')
        drift_seen.add(address)
        if (address in ('aws_iam_role.host', 'aws_instance.host[0]') and change.get('actions') == ['update'] and before == inventory.get(address) and isinstance(after, dict)
                and owned_resource_matches(address, before, after, ownership, require_refresh=address == 'aws_iam_role.host')):
            inventory[address] = after
            continue
        if (not present and stopped_host is True and removal_started is True
                and address in SESSION_ADDRESSES and change.get('actions') == ['delete']
                and isinstance(before, dict) and before == inventory.get(address) and after is None):
            if (address == 'aws_instance.host[0]' and before.get('id') != deployment['host_id']
                    or address == 'aws_volume_attachment.data[0]' and
                    (before.get('volume_id') != deployment['data_volume_id']
                     or before.get('instance_id') != deployment['host_id'])):
                raise HostError('deleted session drift differs from the recorded source identity')
            del inventory[address]
            continue
        if (present or not stopped_host or address != 'aws_instance.host[0]' or change.get('actions') != ['update']
                or before != inventory.get(address) or not isinstance(after, dict)
                or after.get('instance_state') != 'stopped'
                or after.get('public_ip') not in ('', None) or after.get('associate_public_ip_address') is not False
                or not owned_resource_matches(address, before, after, ownership, ignore=HOST_STOP_FIELDS)):
            raise HostError('session plan contains unreviewed resource drift')
        inventory[address] = after
    seen = set()
    for resource in plan.get('resource_changes', []):
        actions = resource.get('change', {}).get('actions')
        if actions == ['no-op'] or resource.get('mode') == 'data' and actions == ['read']:
            continue
        if (not present and resource.get('mode') == 'data' and resource.get('address') in ('data.aws_ami.host', 'data.aws_ami.host[0]')
                and actions == ['delete'] and resource.get('change', {}).get('before', {}).get('id') == deployment['ami_id']):
            # Removing a launch-only lookup from state deletes no AWS image.
            continue
        address = resource.get('address')
        if address not in SESSION_ADDRESSES or address in seen or actions != (['create'] if present else ['delete']):
            raise HostError('saved plan includes changes outside the approved session policy')
        seen.add(address)
        change = resource['change']
        if present:
            after = change.get('after')
            if not isinstance(after, dict):
                raise HostError('session create lacks configuration evidence')
            if address == 'aws_instance.host[0]':
                roots = after.get('root_block_device', [])
                metadata = after.get('metadata_options', [])
                if (after.get('ami') != deployment['ami_id'] or after.get('instance_type') != 'm7g.large'
                        or after.get('hibernation') is not None and after.get('hibernation') is not False
                        or after.get('iam_instance_profile') != deployment['host_instance_profile']
                        or after.get('availability_zone') != deployment['availability_zone']
                        or after.get('subnet_id') != deployment['subnet_id'] or after.get('vpc_security_group_ids') != [deployment['security_group_id']]
                        or after.get('associate_public_ip_address') is not True or len(roots) != 1
                        or roots[0].get('encrypted') is not True or roots[0].get('delete_on_termination') is not True
                        or roots[0].get('volume_size') != 20 or roots[0].get('volume_type') != 'gp3'
                        or len(metadata) != 1 or metadata[0].get('http_tokens') != 'required' or metadata[0].get('http_put_response_hop_limit') != 1):
                    raise HostError('session host create differs from protected network/root/credential policy')
            elif after.get('volume_id') != deployment['data_volume_id'] or after.get('force_detach') is not False or after.get('skip_destroy') is not False:
                raise HostError('session attachment create differs from retained data policy')
        else:
            before = change.get('before')
            if address not in inventory or before != inventory[address]:
                raise HostError('session delete does not match the independently observed state inventory')
            expected = deployment['host_id'] if address == 'aws_instance.host[0]' else deployment['data_volume_id']
            field = 'id' if address == 'aws_instance.host[0]' else 'volume_id'
            if before.get(field) != expected:
                raise HostError('session delete identity differs from the deployment')
    expected = {address for address in SESSION_ADDRESSES if (address not in inventory if present else address in inventory)}
    if seen != expected:
        raise HostError('saved plan does not cover the expected remaining session resources')
    if plan.get('deferred_changes'):
        raise HostError('session plan has deferred changes without ownership proof')


class TofuCLI:
    def __init__(self, policy: dict, *, source_environment_provider=None):
        self.policy = policy
        source=AWSCLI(policy['backend']['region'],policy['aws_profile'],executable=policy['aws_cli_path'],
                      executable_sha256=policy['aws_cli_sha256'],executable_identity=policy['aws_cli_file_identity'],
                      source_identity=policy['aws_source_identity'])
        self.source_environment_provider=source_environment_provider or source.source_environment
        self.root = canonical_infra_root(Path(policy['infra_root']))
        metadata = self.root.stat()
        self.root_identity = (metadata.st_dev, metadata.st_ino)
        self.binary = self.root.parent / '.tools/bin/tofu'
        tool_identity(self.root, self.policy)

    def run(self, *arguments: str, timeout=600) -> dict | None:
        root = canonical_infra_root(self.root)
        metadata = root.stat()
        if (metadata.st_dev, metadata.st_ino) != self.root_identity or str(root) != self.policy['infra_root']:
            raise HostError('lifecycle infrastructure directory identity changed before execution')
        tool_identity(root, self.policy)
        execution_tools(self.policy)
        if backend_digest(root, self.policy) != self.policy['backend_sha256']:
            raise HostError('backend security configuration changed before infrastructure execution')
        if private_inputs_digest(self.policy) != self.policy['private_inputs_sha256']:
            raise HostError('private inputs changed before infrastructure execution')
        if digest_file(Path(self.policy['variables_file'])) != self.policy['variables_sha256'] or source_digest(self.root.parent) != self.policy['source_sha256']:
            raise HostError('reviewed lifecycle source or variables changed before infrastructure execution')
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('TF_', 'TOFU_', 'AWS_ENDPOINT_URL')) and key not in ('CLOUDFLARE_API_TOKEN', 'AWS_DEBUG', 'AWS_CA_BUNDLE', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')}
        environment.update(TF_IN_AUTOMATION='true', TF_LOG='OFF', AWS_IGNORE_CONFIGURED_ENDPOINT_URLS='true')
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN', 'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE',
                     'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_SESSION_NAME'):
            environment.pop(name, None)
        environment.pop('AWS_PROFILE',None)
        environment.pop('AWS_DEFAULT_PROFILE',None)
        admitted=self.source_environment_provider()
        if any(not isinstance(admitted.get(name),str) or not admitted[name] for name in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY')):
            raise HostError('admitted source credentials are unavailable')
        environment.update({name:admitted[name] for name in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN') if name in admitted})
        execution_tools(self.policy)
        try:
            with tempfile.TemporaryDirectory(prefix='minecraft-trusted-tofu-') as temporary:
                directory = Path(temporary)
                configuration = directory / 'trusted.tfrc'
                configuration.write_bytes(b'')
                configuration.chmod(0o600)
                for name in ('config', 'data'):
                    (directory / name).mkdir(mode=0o700)
                aws_config=directory/'aws-config'
                aws_credentials=directory/'aws-credentials'
                for path in (aws_config,aws_credentials):
                    path.write_bytes(b''); path.chmod(0o600)
                environment.update(TF_CLI_CONFIG_FILE=str(configuration), TERRAFORM_CONFIG=str(configuration),
                                   XDG_CONFIG_HOME=str(directory / 'config'), XDG_CONFIG_DIRS=str(directory / 'config'),
                                   XDG_DATA_HOME=str(directory / 'data'), XDG_DATA_DIRS=str(directory / 'data'))
                environment.update(AWS_CONFIG_FILE=str(aws_config),AWS_SHARED_CREDENTIALS_FILE=str(aws_credentials))
                result = subprocess.run([str(self.binary), f'-chdir={self.root}', *arguments], capture_output=True,
                                        text=True, timeout=timeout, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError('infrastructure command interrupted; resume the same lifecycle operation') from exc
        if result.returncode:
            raise HostError('infrastructure command failed; preserve resources and resume after private diagnosis')
        if arguments[0] in ('state', 'show', 'output'):
            try:
                return json.loads(result.stdout)
            except ValueError as exc:
                raise HostError('infrastructure command returned invalid evidence') from exc
        return None

    def state(self) -> dict:
        state = self.run('state', 'pull')
        resources(state)
        return state

    def plan(self, destination: Path, present: bool, *, variables=None) -> dict:
        arguments = []
        for name, value in (variables or {}).items():
            if name not in ("data_filesystem_uuid", "data_volume_size_gib", "recovery_receipt_ids"):
                raise HostError("unapproved lifecycle variable override")
            encoded = value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))
            arguments.append(f"-var={name}={encoded}")
        self.run('plan', '-input=false', '-lock-timeout=30s', '-no-color',
                 f'-var-file={self.policy["variables_file"]}', f'-var=session_present={str(present).lower()}', *arguments, f'-out={destination}')
        destination.chmod(0o600)
        return self.run('show', '-json', str(destination))

    def apply(self, path: Path) -> None:
        self.run('apply', '-input=false', '-lock-timeout=30s', '-no-color', str(path), timeout=1200)

    def deployment(self) -> dict:
        return self.run('output', '-json', 'deployment')


class OnDemandSession:
    def __init__(self, deployment: dict, aws, cloudflare, policy: dict, deployment_path: Path, *, tofu=None, transfer=None, started_at=None, started_monotonic=None):
        self.command_started_monotonic = time.monotonic() if started_monotonic is None else started_monotonic
        self.resumed_operation = False
        self.command_started_at = time.time() if started_at is None else started_at
        self.deployment = deployment
        self.aws = aws
        self.cloudflare = cloudflare
        self.policy = policy
        self.deployment_path = deployment_path
        self.tofu = tofu or TofuCLI(policy,source_environment_provider=self.aws.source_environment)
        self.transfer = transfer or self.transfer_private
        self.directory = Path(policy['journal_dir'])
        self.journal_path = self.directory / 'operation.json'
        self.state = 'unknown'
        self.observed = {'application': 'unknown', 'backup': 'unverified'}
        self.dns = {}
        self.dns_error = None

    def running_session(self) -> HostSession:
        if not self.deployment.get('host_id') or self.deployment.get('session_present') is not True:
            raise HostError('session host is absent')
        return HostSession(self.deployment, self.aws, self.cloudflare)

    def verify_account(self):
        response = self.aws.run('sts', 'get-caller-identity', timeout=30)
        if response.get('Account') != self.deployment['account_id']:
            raise HostError('active AWS account does not match the reviewed deployment account')

    def binding(self):
        execution_tools(self.policy)
        variables = hashlib.sha256(private_file(Path(self.policy['variables_file']))).hexdigest()
        source = source_digest(Path(self.policy['infra_root']).parent)
        if variables != self.policy['variables_sha256'] or source != self.policy['source_sha256']:
            raise HostError('reviewed lifecycle source or variables changed during the operation')
        private_digest = private_inputs_digest(self.policy)
        if private_digest != self.policy['private_inputs_sha256']:
            raise HostError('private operator inputs differ from the reviewed policy')
        protected_handoff = {key: value for key, value in self.deployment.items() if key not in ('host_id', 'session_present')}
        return {'policy_sha256': hashlib.sha256(json.dumps(self.policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'protected_handoff_sha256': hashlib.sha256(json.dumps(protected_handoff, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'account_id': self.deployment['account_id'], 'aws_profile': self.policy['aws_profile'], 'backend': self.policy['backend'],
                'variables_sha256': variables, 'source_sha256': source,
                'bootstrap_sha256': self.policy['bootstrap_sha256'],
                'private_inputs_sha256': private_digest,
                'data_volume_id': self.deployment['data_volume_id'], 'data_filesystem_uuid': self.deployment['data_filesystem_uuid']}

    def journal(self, action: str, instance=None):
        # Deterministic local format errors must precede journal or resource creation.
        try:
            build_bundle(self.deployment, Path(self.policy['operator_file']), Path(self.policy['secret_file']))
        except TransferError as exc:
            raise HostError('reviewed private inputs fail transfer preflight') from exc
        state = self.tofu.state()
        if self.journal_path.exists():
            previous = json.loads(private_file(self.journal_path))
            phases = {'begun', 'configured-host', 'runtime-installed', 'drained', 'stopping-host', 'host-stopped', 'applying', 'applied', 'removed', 'complete'}
            try:
                operation_id = uuid.UUID(previous['operation_id'])
                valid = (previous.get('schema_version') == 1 and str(operation_id) == previous['operation_id'] and operation_id.version == 4
                         and previous.get('action') in ('start', 'stop') and previous.get('phase') in phases)
            except (KeyError, TypeError, ValueError, AttributeError):
                valid = False
            if not valid:
                raise HostError('private lifecycle journal identity or schema is invalid; preserve it for repair')
            if previous.get('phase') != 'complete':
                if previous.get('action') != action or previous.get('instance') != instance or previous.get('binding') != self.binding():
                    raise HostError('another incomplete or changed lifecycle operation requires repair')
                reconcile_state(previous['initial_state'], state)
                verify_resumed_sessions(previous, state)
                self.resumed_operation = True
                return previous
        if self.journal_path.exists():
            save_private(self.directory / (previous['operation_id'] + '.json'), previous)
        self.verify_handoff_inventory()
        inventory = resources(state)
        if self.deployment.get('host_id'):
            if (inventory.get('aws_instance.host[0]', {}).get('id') != self.deployment['host_id']
                    or inventory.get('aws_volume_attachment.data[0]', {}).get('instance_id') != self.deployment['host_id']
                    or inventory.get('aws_volume_attachment.data[0]', {}).get('volume_id') != self.deployment['data_volume_id']):
                raise HostError('deployment session identity differs from observed state')
        elif SESSION_ADDRESSES.intersection(inventory):
            raise HostError('off-state deployment has unexpected or partial session resources')
        journal = {'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'action': action, 'instance': instance,
                   'binding': self.binding(), 'initial_state': state, 'phase': 'begun', 'source_deployment': self.deployment,
                   'started_at': self.command_started_at}
        save_private(self.journal_path, journal)
        return journal

    def record(self, journal: dict, phase: str, **values):
        observed_at = time.time()
        prior = journal.get('last_observed_at', journal['started_at'])
        if not math.isfinite(observed_at) or observed_at < prior or observed_at < journal['started_at']:
            journal['timing_unverified'] = True
        journal.update(phase=phase, last_observed_at=observed_at, **values)
        save_private(self.journal_path, journal)

    def elapsed_time(self, journal):
        now = time.time()
        started = journal.get('started_at')
        if (self.resumed_operation or journal.get('timing_unverified') or type(started) not in (int, float)
                or not math.isfinite(started) or not math.isfinite(now) or started <= 0 or now < started
                or now < journal.get('last_observed_at', started)):
            return None
        monotonic_elapsed = time.monotonic() - self.command_started_monotonic
        if monotonic_elapsed < 0:
            return None
        return max(now - started, monotonic_elapsed)

    def apply_session(self, journal: dict, present: bool):
        current = self.tofu.state()
        if self.binding() != journal['binding']:
            raise HostError('private lifecycle inputs changed during the operation')
        reconcile_state(journal['initial_state'], current)
        inventory = resources(current)
        expected_present = SESSION_ADDRESSES <= set(inventory)
        expected_absent = not SESSION_ADDRESSES.intersection(inventory)
        if present and journal['phase'] == 'applying':
            self.verify_no_unrecorded_creation(current)
        if (present and expected_present or not present and expected_absent) and journal['phase'] in ('applying', 'applied'):
            verify_resumed_sessions(journal, current)
            self.record(journal, 'applied', session_inventory={name: value for name, value in inventory.items() if name in SESSION_ADDRESSES})
            return
        path = self.directory / (journal['operation_id'] + '.tfplan')
        plan = self.tofu.plan(path, present)
        validate_plan(plan, present=present, state=current, deployment=journal['source_deployment'],
                      stopped_host=journal.get('host_stop_requested', False),
                      removal_started=journal.get('removal_started', False))
        # The workstation lock does not serialize another Terraform client; compare state
        # again and let the saved-plan/backend lock reject a race after this check.
        if not state_matches(self.tofu.state(), current):
            raise HostError('infrastructure state changed after the plan; preserve the operation for review')
        if self.binding() != journal['binding']:
            raise HostError('reviewed lifecycle inputs changed while planning; apply forbidden')
        plan_digest = digest_file(path)
        self.record(journal, 'applying', planned_state=current, plan_sha256=plan_digest,
                    removal_started=not present,
                    planned_changes=[value for value in plan.get('resource_changes', []) if value.get('address') in SESSION_ADDRESSES])
        if digest_file(path) != plan_digest:
            raise HostError('saved infrastructure plan changed before apply')
        self.tofu.apply(path)
        final_state = self.tofu.state()
        reconcile_state(journal['initial_state'], final_state)
        final_inventory = resources(final_state)
        if present and not SESSION_ADDRESSES <= set(final_inventory) or not present and SESSION_ADDRESSES.intersection(final_inventory):
            raise HostError('session apply did not produce the expected resource inventory')
        verify_resumed_sessions(journal, final_state)
        self.record(journal, 'applied', session_inventory={name: value for name, value in final_inventory.items() if name in SESSION_ADDRESSES})

    def verify_no_unrecorded_creation(self, current: dict):
        """Never create again based solely on unchanged state after an uncertain apply."""
        inventory = resources(current)
        recorded = inventory.get('aws_instance.host[0]', {}).get('id')
        if not recorded:
            # EC2 list APIs are eventually consistent. Empty tag-filter results cannot
            # prove RunInstances never completed before a lost state write.
            raise HostError('interrupted create has no recorded host identity; preserve journal and cloud resources for explicit repair')
        prefix = self.deployment.get('resource_prefix')
        environment = self.deployment.get('environment')
        if not isinstance(prefix, str) or not re.fullmatch('[a-z][a-z0-9-]{2,39}', prefix) or not isinstance(environment, str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', environment):
            raise HostError('uncertain create lacks stable cloud ownership tags; preserve journal for repair')
        filters = [{'Name': 'tag:Project', 'Values': ['minecraft']}, {'Name': 'tag:Environment', 'Values': [environment]},
                   {'Name': 'tag:Name', 'Values': [prefix + '-host']}, {'Name': 'tag:Purpose', 'Values': ['replaceable-host']},
                   {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped', 'shutting-down']}]
        response = self.aws.run('ec2', 'describe-instances', '--filters', json.dumps(filters), timeout=30)
        reservations = response.get('Reservations')
        if not isinstance(reservations, list) or any(not isinstance(value, dict) or not isinstance(value.get('Instances'), list) for value in reservations):
            raise HostError('cannot establish independent cloud ownership after interrupted create')
        hosts = [value for reservation in reservations for value in reservation['Instances']]
        recorded = inventory.get('aws_instance.host[0]', {}).get('id')
        if any(not isinstance(value, dict) or not isinstance(value.get('InstanceId'), str) for value in hosts) or {value['InstanceId'] for value in hosts} != ({recorded} if recorded else set()):
            raise HostError('unrecorded or missing session host after interrupted create; preserve resources for explicit repair')
        roots = inventory.get('aws_instance.host[0]', {}).get('root_block_device', [])
        recorded_roots = {value['volume_id'] for value in roots if isinstance(value.get('volume_id'), str)}
        filters = [{'Name': 'tag:Project', 'Values': ['minecraft']}, {'Name': 'tag:Environment', 'Values': [environment]},
                   {'Name': 'tag:Name', 'Values': [prefix + '-root']}, {'Name': 'tag:Purpose', 'Values': ['replaceable-root']}]
        volumes = self.aws.run('ec2', 'describe-volumes', '--filters', json.dumps(filters), timeout=30).get('Volumes')
        if not isinstance(volumes, list) or any(not isinstance(value, dict) or not isinstance(value.get('VolumeId'), str) for value in volumes) or {value['VolumeId'] for value in volumes} != recorded_roots:
            raise HostError('unrecorded or missing root disk after interrupted create; preserve resources for explicit repair')
        # The attachment call may have succeeded while its state write was lost. Do
        # not issue another AttachVolume without independently proving it is free.
        if 'aws_volume_attachment.data[0]' not in inventory:
            data = self.aws.run('ec2', 'describe-volumes', '--volume-ids', self.deployment['data_volume_id'], timeout=30).get('Volumes')
            if (not isinstance(data, list) or len(data) != 1 or data[0].get('VolumeId') != self.deployment['data_volume_id']
                    or data[0].get('State') != 'available' or data[0].get('Attachments') != [] or data[0].get('Encrypted') is not True):
                raise HostError('retained disk attachment outcome is uncertain; preserve resources for explicit repair')


    def refresh_deployment(self, *, present: bool):
        candidate = self.tofu.deployment()
        for name in ('account_id', 'region', 'lifecycle_mode', 'data_volume_id', 'data_filesystem_uuid', 'subnet_id',
                     'security_group_id', 'host_role_arn', 'operator_role_arn', 'host_instance_profile', 'ami_id', 'availability_zone', 'environment', 'resource_prefix', 'backup_bucket_name', 'instances', 'public_hostname', 'cloudflare_zone_id', 'bootstrap_sha256'):
            if candidate.get(name) != self.deployment.get(name):
                raise HostError('new deployment handoff changed a protected identity')
        if candidate.get('schema_version') != 2 or candidate.get('session_present') is not present or bool(candidate.get('host_id')) is not present:
            raise HostError('new deployment handoff has invalid session presence')
        inventory = resources(self.tofu.state())
        if present:
            if (inventory.get('aws_instance.host[0]', {}).get('id') != candidate['host_id']
                    or inventory.get('aws_volume_attachment.data[0]', {}).get('instance_id') != candidate['host_id']
                    or inventory.get('aws_volume_attachment.data[0]', {}).get('volume_id') != candidate['data_volume_id']):
                raise HostError('new deployment host/attachment identity differs from observed state')
        elif SESSION_ADDRESSES.intersection(inventory):
            raise HostError('off-state deployment still owns session resources')
        save_private(self.deployment_path, candidate)
        self.deployment = candidate

    def transfer_private(self, session: HostSession, bundle: bytes, digest: str):
        session.execute_shell('systemd-run --unit=minecraft-private-transfer --collect --property=RuntimeMaxSec=150 '
                              f'/usr/bin/python3 -B /opt/minecraft-bootstrap/{self.policy["bootstrap_sha256"]}/scripts/minecraft_private_transfer.py '
                              f'receive --sha256 {digest} --port 19140 --timeout 120 >/dev/null && '
                              'printf \'{"private_receiver":"started"}\\n\'', 'private-receiver')
        with socket.socket() as local:
            local.bind(('127.0.0.1', 0))
            port = local.getsockname()[1]
        environment = {key: value for key, value in self.aws.environment().items() if not key.startswith('TF_')}
        _, plugin = execution_tools(self.policy)
        environment['PATH'] = str(Path(plugin).parent) + os.pathsep + str(Path(self.policy['infra_root']).parent / '.tools/bin') + os.pathsep + environment.get('PATH', os.defpath)
        aws_path = self.aws.command_path()
        try:
            process = subprocess.Popen([aws_path, '--region', self.deployment['region'], '--no-cli-pager', '--no-cli-auto-prompt',
                                        'ssm', 'start-session', '--target', self.deployment['host_id'],
                                        '--document-name', 'AWS-StartPortForwardingSession', '--parameters',
                                        json.dumps({'portNumber': ['19140'], 'localPortNumber': [str(port)]})],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
        except OSError as exc:
            raise HostError('AWS CLI and Session Manager plugin are required for private transfer') from exc
        try:
            deadline = time.monotonic() + 90
            # A released reservation is not tunnel identity: another local process can
            # bind it. Send nothing until the trusted CLI/plugin confirms its own bind.
            opened = False
            waiting = False
            buffer = b''
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not (opened and waiting):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise HostError('private SSM tunnel did not confirm authenticated local-port readiness')
                    for key, _ in selector.select(timeout=0.2):
                        chunk = os.read(key.fileobj.fileno(), 1024)
                        if not chunk:
                            raise HostError('private SSM tunnel closed before local-port readiness')
                        buffer += chunk
                        if len(buffer) > 8192:
                            raise HostError('private SSM tunnel readiness output exceeded its limit')
                        lines = buffer.splitlines()
                        opened = opened or any(re.fullmatch(rb'Port ' + str(port).encode() + rb' opened for sessionId [A-Za-z0-9_.@-]+\s*', line) for line in lines)
                        waiting = waiting or any(line.strip() == b'Waiting for connections...' for line in lines)
            while True:
                if process.poll() is not None:
                    raise HostError('private SSM tunnel ended before file publication')
                try:
                    send_bundle(port, bundle)
                    break
                except TransferError:
                    if time.monotonic() >= deadline:
                        raise HostError('private SSM transfer timed out; host remains stopped')
                    time.sleep(1)
        finally:
            process.terminate()
            if process.stdout is not None:
                process.stdout.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        # Installed file content is never placed in an SSM command or response.
        session.execute_shell('test -f /etc/minecraft/deployment.json && test -f /etc/minecraft/operator.json && '
                              'test -f /etc/minecraft/rcon.secret && printf \'{"private_files":"present"}\\n\'', 'private-file-check')

    def adopt(self, session):
        self.state, self.observed, self.dns, self.dns_error = session.state, session.observed, session.dns, session.dns_error

    def verify_private_inputs(self, session):
        digest = self.policy['bootstrap_sha256']
        session.execute_shell(f'/usr/local/sbin/minecraft-bootstrap stage --region {self.deployment["region"]} --bucket {self.deployment["backup_bucket_name"]} --sha256 {digest}', 'private-verifier-release')
        observed = session.execute_shell(f'/usr/bin/python3 -B /opt/minecraft-bootstrap/{digest}/scripts/minecraft_private_transfer.py '
                                         f'verify-inputs --sha256 {self.policy["private_inputs_sha256"]}', 'private-input-verification')
        if observed != {'private_inputs': 'verified'}:
            raise HostError('installed private inputs lack reviewed verification; explicit reconfiguration required')

    def attest_runtime(self, session):
        digest = self.policy['bootstrap_sha256']
        loader_digest = digest_file(Path(self.policy['infra_root']).parent / 'scripts/minecraft_bootstrap_release.py')
        # The verifier itself must be trusted before it inspects installed launchers.
        session.execute_shell(f'test "$(sha256sum /usr/local/sbin/minecraft-bootstrap | cut -d\' \' -f1)" = {loader_digest} && '
                              f'/usr/bin/python3 -B /usr/local/sbin/minecraft-bootstrap stage --region {self.deployment["region"]} '
                              f'--bucket {self.deployment["backup_bucket_name"]} --sha256 {digest}', 'runtime-verifier-release')
        observed = session.execute_shell(f'/usr/bin/python3 -B /usr/local/sbin/minecraft-bootstrap verify --sha256 {digest}', 'runtime-attestation')
        if observed != {'runtime': 'installed', 'bootstrap_sha256': digest}:
            raise HostError('installed runtime differs from the pinned public release')

    def start(self, instance: str):
        with operation_lock(self.directory):
            journal = self.journal('start', instance)
            if journal['phase'] == 'begun' and self.deployment.get('host_id'):
                inventory = resources(journal['initial_state'])
                if inventory.get('aws_instance.host[0]', {}).get('id') != self.deployment['host_id'] or not SESSION_ADDRESSES <= set(inventory):
                    raise HostError('existing host handoff differs from observed session inventory')
                existing = self.running_session()
                if existing.host_state() != 'running':
                    raise HostError('existing on-demand host is not running; repair its prior transition')
                existing.wait_ssm()
                probe = existing.execute_shell('if test -x /usr/local/sbin/minecraft-instance; then '
                                               'printf \'{"runtime":"installed"}\\n\'; else '
                                               'printf \'{"runtime":"missing"}\\n\'; fi', 'runtime-probe')
                if probe.get('runtime') not in ('installed', 'missing'):
                    raise HostError('existing host runtime installation probe is invalid')
                if probe['runtime'] == 'installed':
                    self.attest_runtime(existing)
                    self.verify_private_inputs(existing)
                self.record(journal, 'runtime-installed' if probe['runtime'] == 'installed' else 'configured-host')
            if journal['phase'] in ('begun', 'applying', 'applied'):
                self.apply_session(journal, True)
                self.refresh_deployment(present=True)
                self.record(journal, 'configured-host')
            session = self.running_session()
            self.state = session.host_state()
            if self.state != 'running':
                if self.state == 'pending':
                    session.wait_host('running', 'pending')
                else:
                    raise HostError('on-demand session host must be running after provisioning')
            session.wait_ssm()
            if journal['phase'] == 'configured-host':
                # Repeat is safe only for the same journaled release/input bytes, before
                # normal start. Cloud-init may have met attachment eventual consistency.
                session.execute_shell('(cloud-init status --wait >/dev/null 2>&1 || bash /var/lib/cloud/instance/scripts/part-001 >/dev/null) && '
                                      'test -x /usr/local/sbin/minecraft-bootstrap && printf \'{"bootstrap":"ready"}\\n\'', 'bootstrap-prerequisites')
                digest = self.policy['bootstrap_sha256']
                session.execute_shell(f'/usr/local/sbin/minecraft-bootstrap stage --region {self.deployment["region"]} --bucket {self.deployment["backup_bucket_name"]} --sha256 {digest}', 'bootstrap-release')
                bundle, bundle_digest = build_bundle(self.deployment, Path(self.policy['operator_file']), Path(self.policy['secret_file']))
                files = json.loads(bundle)['files']
                transferred_inputs = hashlib.sha256(files['operator.json'].encode() + b'\0' + files['rcon.secret'].encode()).hexdigest()
                if transferred_inputs != journal['binding']['private_inputs_sha256']:
                    raise HostError('private operator inputs changed before secure transfer')
                self.transfer(session, bundle, bundle_digest)
                session.execute_shell(f'bash /opt/minecraft-bootstrap/{digest}/scripts/install_runtime.sh >/dev/null && printf \'{{"runtime":"installed"}}\\n\'', 'runtime-install')
                self.record(journal, 'runtime-installed')
            if self.binding() != journal['binding']:
                raise HostError('private lifecycle inputs changed before game start')
            self.verify_private_inputs(session)
            self.attest_runtime(session)
            # Preserve a separate phase before the game command: an interrupted start
            # resumes the normal idempotent instance command without reinstalling files.
            try:
                result = session.start(instance)
            finally:
                self.adopt(session)
            elapsed = self.elapsed_time(journal)
            self.record(journal, 'complete', ready_seconds=elapsed)
            return {**result, 'start_to_ready_seconds': None if elapsed is None else round(elapsed, 3), 'startup_target_seconds': 180,
                    'controller_ready_within_target': None if elapsed is None else elapsed <= 180 and session.dns_error is None,
                    'startup_target_met': None if elapsed is None or elapsed <= 180 and session.dns_error is None else False, 'host_recreated': journal['source_deployment'].get('host_id') is None}

    def removal_host_state(self, journal, session):
        if journal.get('removal_started') is not True or journal.get('host_stop_requested') is not True:
            return session.host_state()
        host_id = journal['source_deployment']['host_id']
        response = self.aws.run('ec2', 'describe-instances', '--filters',
                                json.dumps([{'Name': 'instance-id', 'Values': [host_id]}]), timeout=30)
        reservations = response.get('Reservations')
        if not isinstance(reservations, list) or any(not isinstance(row, dict) or not isinstance(row.get('Instances'), list) for row in reservations):
            raise HostError('interrupted removal has invalid independent host evidence')
        nodes = [node for row in reservations for node in row['Instances']]
        if nodes and (len(nodes) != 1 or not isinstance(nodes[0], dict)
                      or nodes[0].get('InstanceId') != host_id):
            raise HostError('interrupted removal has mismatched independent host evidence')
        if not nodes or nodes[0].get('State', {}).get('Name') == 'terminated':
            session.state, session.address = 'stopped', None
            return 'absent' if not nodes else 'terminated'
        return session.host_state()

    def verify_removal(self, journal):
        source = journal['source_deployment']
        host = resources(journal['initial_state']).get('aws_instance.host[0]')
        if host:
            response = self.aws.run('ec2', 'describe-instances', '--filters', json.dumps([{'Name': 'instance-id', 'Values': [host['id']]}]), timeout=30)
            reservations = response.get('Reservations')
            if not isinstance(reservations, list) or any(not isinstance(value, dict) or not isinstance(value.get('Instances'), list) for value in reservations):
                raise HostError('session host removal returned invalid independent EC2 evidence')
            observed = [value for reservation in reservations for value in reservation['Instances']]
            if any(not isinstance(value, dict) for value in observed):
                raise HostError('session host removal returned invalid independent EC2 evidence')
            if observed and (len(observed) != 1 or observed[0].get('InstanceId') != host['id'] or observed[0].get('State', {}).get('Name') != 'terminated'):
                raise HostError('session host termination lacks independent EC2 proof')
            roots = host.get('root_block_device', [])
            if len(roots) != 1 or not roots[0].get('volume_id'):
                raise HostError('source root volume identity is missing')
            # An exact ID filter returns no match once the root volume is gone, without
            # treating a generic API failure as deletion proof.
            volumes = self.aws.run('ec2', 'describe-volumes', '--filters', json.dumps([{'Name': 'volume-id', 'Values': [roots[0]['volume_id']]}]), timeout=30)
            if volumes.get('Volumes') != []:
                raise HostError('session root disk removal lacks independent EC2 proof')
        volumes = self.aws.run('ec2', 'describe-volumes', '--volume-ids', source['data_volume_id'], timeout=30).get('Volumes')
        if not isinstance(volumes, list) or len(volumes) != 1 or volumes[0].get('VolumeId') != source['data_volume_id'] or volumes[0].get('Encrypted') is not True or volumes[0].get('Attachments') != [] or volumes[0].get('State') != 'available':
            raise HostError('retained data disk ownership or detach proof is incomplete')

    def stop(self):
        with operation_lock(self.directory):
            journal = self.journal('stop')
            if journal['phase'] == 'begun' and not self.deployment.get('host_id'):
                if SESSION_ADDRESSES.intersection(resources(journal['initial_state'])):
                    raise HostError('off-state session inventory is partial; repair its prior operation')
                self.verify_removal(journal)
                self.record(journal, 'removed', backup='unverified')
            if journal['phase'] == 'begun' and self.deployment.get('host_id'):
                session = self.running_session()
                if session.host_state() != 'running':
                    raise HostError('on-demand stop requires a running host for verified drain')
                session.wait_ssm()
                self.attest_runtime(session)
                self.verify_private_inputs(session)
                response = session.remote('host-drain')
                instances = session.instance_statuses(response)
                self.observed = {'instances': instances}
                if response.get('draining') is not True or any(value['application'] != 'stopped' or value['backup'] not in ('complete', 'not-required') for value in instances.values()):
                    raise HostError('host drain lacks complete shutdown and backup evidence; removal forbidden')
                self.record(journal, 'drained', drain=response, backup='complete' if any(value['backup'] == 'complete' for value in instances.values()) else 'not-required')
            if journal['phase'] in ('drained', 'stopping-host', 'host-stopped', 'applying') and 'aws_instance.host[0]' in resources(self.tofu.state()):
                session = self.running_session()
                state = self.removal_host_state(journal, session)
                if state == 'running':
                    # Historical output alone does not authorize deletion. Refresh the
                    # drain before requesting the idempotent graceful EC2 stop.
                    session.wait_ssm()
                    self.attest_runtime(session)
                    self.verify_private_inputs(session)
                    fresh = session.remote('host-drain')
                    instances = session.instance_statuses(fresh)
                    self.observed = {'instances': instances}
                    if fresh.get('draining') is not True or any(value['application'] != 'stopped' or value['backup'] not in ('complete', 'not-required') for value in instances.values()):
                        raise HostError('resumed host drain is incomplete; removal forbidden')
                    self.record(journal, 'stopping-host', drain=fresh, host_stop_requested=True,
                                backup='complete' if any(value['backup'] == 'complete' for value in instances.values()) else 'not-required')
                    session.call('ec2', 'stop-instances', '--instance-ids', self.deployment['host_id'])
                    session.wait_host('stopped', 'stopping', previous='running')
                elif state == 'stopping' and journal.get('host_stop_requested'):
                    session.wait_host('stopped', 'stopping')
                elif state in ('absent', 'terminated') and journal.get('removal_started') is True and journal.get('host_stop_requested') is True:
                    pass
                elif state != 'stopped' or not journal.get('host_stop_requested'):
                    raise HostError('source host lacks independently verified graceful stop evidence')
                session.sync_dns()  # EC2 has already released this address; park it promptly.
                self.dns, self.dns_error = session.dns, session.dns_error
                self.record(journal, 'host-stopped')
            if journal['phase'] in ('begun', 'host-stopped', 'applying', 'applied'):
                self.apply_session(journal, False)
                self.verify_removal(journal)
                self.refresh_deployment(present=False)
                self.record(journal, 'removed')
            self.state = 'absent'
            session = HostSession(self.deployment, self.aws, self.cloudflare)
            session.state = 'stopped'
            session.sync_dns()
            self.dns, self.dns_error = session.dns, session.dns_error
            self.record(journal, 'complete')
            return {'host': 'absent', 'root_disk': 'absent', 'data_disk': 'retained', 'backup': journal.get('backup', 'unverified')}

    def verify_handoff_inventory(self) -> bool:
        if self.tofu.deployment() != self.deployment:
            raise HostError('complete deployment handoff differs from authoritative infrastructure output')
        inventory = resources(self.tofu.state())
        if {'aws_instance.host', 'aws_volume_attachment.data'}.intersection(inventory):
            raise HostError('session ownership migration is incomplete; DNS and status require the current handoff')
        actual_present = SESSION_ADDRESSES <= set(inventory)
        if SESSION_ADDRESSES.intersection(inventory) and not actual_present:
            raise HostError('session infrastructure is partial; resume its operation')
        if bool(self.deployment.get('host_id')) != actual_present or self.deployment.get('session_present') != actual_present:
            raise HostError('deployment host identity is stale; resume the saved lifecycle operation')
        if actual_present:
            host, attachment = (inventory[name] for name in ('aws_instance.host[0]', 'aws_volume_attachment.data[0]'))
            if (host.get('id') != self.deployment['host_id'] or attachment.get('instance_id') != self.deployment['host_id']
                    or attachment.get('volume_id') != self.deployment['data_volume_id']):
                raise HostError('deployment host or attachment identity differs from infrastructure state')
        return actual_present

    def status(self):
        with operation_lock(self.directory):
            if not self.verify_handoff_inventory():
                self.state = 'absent'
                return {'host': 'absent', 'application': 'unavailable', 'backup': 'unverified', 'data_disk': 'retained'}
            session = self.running_session()
            result = session.status()
            self.adopt(session)
            return result

    def repair_dns(self):
        with operation_lock(self.directory):
            if self.journal_path.exists():
                try:
                    journal = json.loads(private_file(self.journal_path))
                except (TransferError, ValueError) as exc:
                    raise HostError('cannot validate the private lifecycle journal before DNS repair') from exc
                if not isinstance(journal, dict) or journal.get('schema_version') != 1 or journal.get('phase') != 'complete':
                    raise HostError('DNS repair requires resuming the incomplete lifecycle operation first')
            present = self.verify_handoff_inventory()
            session = HostSession(self.deployment, self.aws, self.cloudflare)
            if present:
                result = session.repair_dns()
            else:
                session.state = 'stopped'
                session.sync_dns()
                result = {'host': 'absent'}
            self.adopt(session)
            return result
