#!/usr/bin/env python3
"""Read-only fresh-disk provenance; success never initializes or mounts a disk."""
from __future__ import annotations

import datetime as dt
import json
import re

from minecraft_host import HostError, InvocationNotVisible

WAIT_SECONDS = 900
MAX_EVENT_PAGES = 100


def read(aws, deadline, service, operation, *arguments):
    remaining = deadline - aws.monotonic()
    if remaining <= 0:
        raise HostError('fresh-volume provenance timed out; no initialization is authorized')
    return aws.run(service, operation, *arguments, timeout=min(30, remaining))


def moment(value):
    if not isinstance(value, str):
        raise HostError('fresh-volume evidence has an invalid timestamp')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise HostError('fresh-volume evidence has an invalid timestamp') from exc


def response_moment(value):
    if type(value) in (int, float):
        try:
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            raise HostError('attachment response timestamp is invalid') from exc
    return moment(value)


def inputs(deployment, journal):
    try:
        for field, pattern in (('account_id', r'[0-9]{12}'), ('host_id', r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})'),
                               ('data_volume_id', r'vol-(?:[0-9a-f]{8}|[0-9a-f]{17})'),
                               ('data_filesystem_uuid', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')):
            if not isinstance(deployment[field], str) or not re.fullmatch(pattern, deployment[field]):
                raise ValueError
        if (not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', deployment['region'])
                or not re.fullmatch(re.escape(deployment['region']) + r'[a-z]', deployment['availability_zone'])
                or not re.fullmatch('arn:aws:iam::' + deployment['account_id'] + r':role/.+', deployment['operator_role_arn'])
                or type(deployment['data_volume_size_gib']) is not int or not 2 <= deployment['data_volume_size_gib'] <= 16384):
            raise ValueError
        window = journal['successful_apply_window']
        if set(window) != {'started_at_utc', 'completed_at_utc'}:
            raise ValueError
        start, end = moment(window['started_at_utc']), moment(window['completed_at_utc'])
        if start >= end:
            raise ValueError
        sources, receipts = journal['source_storage'], journal['receipt_ids']
        if not isinstance(sources, list) or not sources or not isinstance(receipts, dict) or not receipts:
            raise ValueError
        for source in sources:
            if (set(source) != {'volume_id', 'filesystem_uuid'}
                    or not isinstance(source['volume_id'], str) or not re.fullmatch(r'vol-(?:[0-9a-f]{8}|[0-9a-f]{17})', source['volume_id'])
                    or not isinstance(source['filesystem_uuid'], str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', source['filesystem_uuid'])
                    or source['volume_id'] == deployment['data_volume_id']
                    or source['filesystem_uuid'] == deployment['data_filesystem_uuid']):
                raise ValueError
        if any(not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name)
               or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for name, value in receipts.items()):
            raise ValueError
        return start, end
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HostError('fresh-volume provenance lacks exact successful-apply and recovery identities') from exc


def inventory(aws, deployment, start, end, deadline):
    volumes = read(aws, deadline, 'ec2', 'describe-volumes', '--volume-ids', deployment['data_volume_id']).get('Volumes')
    hosts = read(aws, deadline, 'ec2', 'describe-instances', '--instance-ids', deployment['host_id']).get('Reservations')
    try:
        instances = [host for reservation in hosts for host in reservation['Instances']]
        if not isinstance(volumes, list) or len(volumes) != 1 or len(instances) != 1:
            raise ValueError
        volume, host = volumes[0], instances[0]
        attachments = volume['Attachments']
        if (volume['VolumeId'] != deployment['data_volume_id'] or volume['Encrypted'] is not True
                or type(volume['Size']) is not int or volume['Size'] != deployment['data_volume_size_gib'] or volume['VolumeType'] != 'gp3'
                or volume.get('SnapshotId') or volume.get('SourceVolumeId') or volume.get('MultiAttachEnabled') is True
                or volume['AvailabilityZone'] != deployment['availability_zone'] or volume['State'] != 'in-use'
                or not isinstance(attachments, list) or len(attachments) != 1
                or attachments[0]['InstanceId'] != deployment['host_id'] or attachments[0]['VolumeId'] != deployment['data_volume_id']
                or attachments[0]['State'] != 'attached' or host['InstanceId'] != deployment['host_id']
                or host['Placement']['AvailabilityZone'] != deployment['availability_zone']
                or host['State']['Name'] != 'running'
                or not start <= moment(volume['CreateTime']) <= end
                or not start <= moment(attachments[0]['AttachTime']) <= end):
            raise ValueError
        return volume, host
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HostError('current fresh-volume or host identity differs from its successful apply') from exc


def events(aws, identifier, deadline):
    result, tokens, token = {}, set(), None
    for _ in range(MAX_EVENT_PAGES):
        args = ['--lookup-attributes', 'AttributeKey=ResourceName,AttributeValue=' + identifier,
                '--max-results', '50', '--no-paginate']
        if token:
            args += ['--next-token', token]
        page = read(aws, deadline, 'cloudtrail', 'lookup-events', *args)
        rows = page.get('Events')
        if not isinstance(rows, list):
            raise HostError('CloudTrail returned incomplete fresh-volume evidence')
        for event in rows:
            try:
                identity = event['EventId']
                if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f-]{36}', identity) or identity in result:
                    raise ValueError
                raw = json.loads(event['CloudTrailEvent'])
                if (raw['eventID'] != identity or raw['eventName'] != event['EventName']
                        or moment(raw['eventTime']) != moment(event['EventTime'])
                        or event['ReadOnly'] not in ('true', 'false') or not isinstance(raw, dict)):
                    raise ValueError
                result[identity] = (event, raw)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise HostError('CloudTrail returned ambiguous fresh-volume evidence') from exc
        token = page.get('NextToken')
        if token is None:
            return result
        if not isinstance(token, str) or not token or token in tokens:
            raise HostError('CloudTrail pagination lacks complete fresh-volume evidence')
        tokens.add(token)
    raise HostError('fresh-volume event inventory exceeds its bounded scan')


def verify_history(deployment, volume, history, start, end):
    """Return false only for delayed expected events; contradictory history refuses."""
    launches, creates, attaches = [], [], []
    try:
        for event, raw in history.values():
            request = raw.get('requestParameters') or {}
            response = raw.get('responseElements') or {}
            name = event['EventName']
            if event['ReadOnly'] == 'true':
                continue
            if name in ('CreateReplaceRootVolumeTask', 'ModifyInstanceAttribute'):
                raise ValueError
            if request.get('volumeId') == deployment['data_volume_id'] and name not in ('CreateVolume', 'AttachVolume'):
                raise ValueError
            if name == 'CreateVolume' and response.get('volumeId') == deployment['data_volume_id']:
                creates.append((event, raw))
                requested_size = request.get('size')
                if isinstance(requested_size, str) and re.fullmatch(r'[1-9][0-9]*', requested_size):
                    requested_size = int(requested_size)
                response_size = response.get('size')
                if isinstance(response_size, str) and re.fullmatch(r'[1-9][0-9]*', response_size):
                    response_size = int(response_size)
                if (request.get('snapshotId') or request.get('sourceVolumeId') or request.get('encrypted') is not True
                        or type(requested_size) is not int or requested_size != deployment['data_volume_size_gib']
                        or request.get('zone') != deployment['availability_zone'] or request.get('volumeType') != 'gp3'
                        or type(response_size) is not int or response_size != deployment['data_volume_size_gib'] or response.get('encrypted') is not True
                        or response.get('zone') != deployment['availability_zone'] or response.get('volumeType') != 'gp3'
                        or response_moment(response.get('createTime')).replace(microsecond=0)
                        != moment(volume['CreateTime']).replace(microsecond=0)):
                    raise ValueError
            elif name == 'RunInstances':
                launched = response['instancesSet']['items']
                if not isinstance(launched, list) or len(launched) != 1 or launched[0]['instanceId'] != deployment['host_id']:
                    raise ValueError
                launches.append((event, raw))
            elif name == 'AttachVolume' and request.get('volumeId') == deployment['data_volume_id']:
                if (request.get('instanceId') != deployment['host_id']
                        or response.get('instanceId') != deployment['host_id'] or response.get('volumeId') != deployment['data_volume_id']
                        or response_moment(response.get('attachTime')).replace(microsecond=0)
                        != moment(volume['Attachments'][0]['AttachTime']).replace(microsecond=0)
                        or not start <= response_moment(response.get('attachTime')) <= end):
                    raise ValueError
                attaches.append((event, raw))
        if len(launches) > 1 or len(creates) > 1 or len(attaches) > 1:
            raise ValueError
        if not launches or not creates or not attaches:
            return False
        creator = launches[0][0]['Username']
        for event, raw in launches + creates + attaches:
            identity = raw['userIdentity']
            if (event['Username'] != creator or not start <= moment(event['EventTime']) <= end
                    or raw.get('errorCode') or raw['awsRegion'] != deployment['region']
                    or raw['eventSource'] != 'ec2.amazonaws.com' or raw['recipientAccountId'] != deployment['account_id']
                    or identity['type'] != 'AssumedRole' or identity['accountId'] != deployment['account_id']
                    or identity['sessionContext']['sessionIssuer']['arn'] != deployment['operator_role_arn']):
                raise ValueError
        return True
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HostError('fresh-volume creation or attachment history is incompatible with first setup') from exc


def first_setup_command(deployment, launched_at):
    expected = {'volume_id': deployment['data_volume_id'], 'filesystem_uuid': deployment['data_filesystem_uuid'],
                'launch_epoch': launched_at.timestamp()}
    return "bash -se <<'MINECRAFT_PROVENANCE'\npython3 - <<'VERIFY'\n" + r'''import pathlib,subprocess,json,re
expected = ''' + json.dumps(expected) + r'''
def run(args):
    return subprocess.run(args,capture_output=True,text=True,check=True).stdout
config = pathlib.Path('/etc/minecraft/storage.conf').read_text()
fields = dict(line.split('=',1) for line in config.splitlines() if '=' in line)
assert fields['DATA_VOLUME_ID'] == expected['volume_id']
assert fields['DATA_FILESYSTEM_UUID'] == expected['filesystem_uuid']
boots = run(['journalctl','--list-boots','--no-pager','--utc'])
rows = [line for line in boots.splitlines() if re.match(r'\s*-?\d+\s+[0-9a-f]{32}\s',line)]
assert len(rows) == 1
first = run(['journalctl','-b','0','--output=short-unix','--no-pager'])
assert abs(float(re.match(r'^([0-9.]+)',first).group(1))-expected['launch_epoch']) < 300
cloud = pathlib.Path('/var/log/cloud-init-output.log').read_text()
logs = run(['journalctl','-o','cat','--no-pager','-u','minecraft-storage.service','-u','minecraft-storage-*','-u','docker.service','-u','cloud-final.service'])
assert not re.search(r'storage (mount|verify) succeeded|filesystem initialized',cloud+'\n'+logs)
assert all(not pathlib.Path(p).exists() for p in ['/opt/minecraft','/etc/minecraft/deployment.json','/etc/minecraft/operator.json'])
mount = subprocess.run(['findmnt','--mountpoint','/srv/minecraft'],capture_output=True,text=True)
assert mount.returncode == 1
root = pathlib.Path('/srv/minecraft')
assert not root.exists() or not any(root.iterdir())
print(json.dumps({'storage_config_matches':True,'first_boot_only':True,'first_boot_matches_launch':True,
                  'prior_storage_use_absent':True,'runtime_absent':True,'mount_absent':True}))
VERIFY
MINECRAFT_PROVENANCE'''


def host_check(aws, deployment, command, deadline):
    sent = read(aws, deadline, 'ssm', 'send-command', '--instance-ids', deployment['host_id'], '--document-name',
                   'AWS-RunShellScript', '--timeout-seconds', '60', '--parameters',
                   json.dumps({'commands': [command], 'executionTimeout': ['120']}))
    identity = sent.get('Command', {}).get('CommandId')
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f-]{36}', identity):
        raise HostError('first-setup inspection lacks a command identity')
    while aws.monotonic() < deadline:
        try:
            result = read(aws, deadline, 'ssm', 'get-command-invocation', '--command-id', identity,
                          '--instance-id', deployment['host_id'])
        except InvocationNotVisible:
            aws.sleep(2)
            continue
        if result.get('Status') == 'Success' and result.get('ResponseCode') == 0:
            try:
                if result.get('CommandId') != identity or result.get('InstanceId') != deployment['host_id']:
                    raise ValueError
                evidence = json.loads(result['StandardOutputContent'])
                fields = {'storage_config_matches', 'first_boot_only', 'first_boot_matches_launch',
                          'prior_storage_use_absent', 'runtime_absent', 'mount_absent'}
                if set(evidence) != fields or any(value is not True for value in evidence.values()):
                    raise ValueError
                return evidence
            except (KeyError, TypeError, ValueError) as exc:
                raise HostError('host first-setup inspection returned incompatible evidence') from exc
        if result.get('Status') not in ('Pending', 'InProgress', 'Delayed'):
            raise HostError('host first-setup inspection failed; preserve the target disk')
        aws.sleep(2)
    raise HostError('first-setup inspection timed out; preserve the target disk')


def verify_fresh_volume(aws, deployment, journal, *, timeout=1800):
    """Prove first setup at the operator boundary; return only private evidence."""
    start, end = inputs(deployment, journal)
    if type(timeout) is not int or not 120 <= timeout <= 3600:
        raise HostError('fresh-volume provenance timeout is invalid')
    deadline = aws.monotonic() + timeout
    caller = read(aws, deadline, 'sts', 'get-caller-identity')
    if caller.get('Account') != deployment['account_id']:
        raise HostError('fresh-volume inspection is using another AWS account')
    wallclock = getattr(aws, 'utcnow', lambda: dt.datetime.now(dt.timezone.utc))
    original = None
    while aws.monotonic() < deadline:
        volume, _ = inventory(aws, deployment, start, end, deadline)
        attachment = volume['Attachments'][0]
        binding = (volume['VolumeId'], volume['CreateTime'], attachment['InstanceId'], attachment['AttachTime'])
        if original is not None and binding != original:
            raise HostError('fresh-volume attachment changed during its provenance wait')
        original = binding
        remaining = WAIT_SECONDS - (wallclock()-moment(attachment['AttachTime'])).total_seconds()
        if remaining > 0:
            aws.sleep(min(30, remaining, max(0, deadline-aws.monotonic())))
            continue
        history = events(aws, deployment['data_volume_id'], deadline)
        if any(event['ReadOnly'] == 'false' and event['EventName'] not in ('CreateVolume', 'AttachVolume')
               for event, _ in history.values()):
            raise HostError('fresh data volume has another write in its history')
        for identity, event in events(aws, deployment['host_id'], deadline).items():
            if identity in history and history[identity] != event:
                raise HostError('CloudTrail indexes disagree about fresh-volume provenance')
            history[identity] = event
        if not verify_history(deployment, volume, history, start, end):
            aws.sleep(min(15, max(0, deadline-aws.monotonic())))
            continue
        launches = [raw for event, raw in history.values() if event['EventName'] == 'RunInstances']
        checked = host_check(aws, deployment, first_setup_command(deployment, moment(launches[0]['eventTime'])), deadline)
        final, _ = inventory(aws, deployment, start, end, deadline)
        final_binding = (final['VolumeId'], final['CreateTime'], final['Attachments'][0]['InstanceId'], final['Attachments'][0]['AttachTime'])
        if final_binding != original:
            raise HostError('fresh-volume attachment changed before first-setup certification')
        return {'provenance_ready': True, 'confirmed_at_utc': wallclock().isoformat(),
                'successful_apply_window': journal['successful_apply_window'], 'receipt_ids': journal['receipt_ids'],
                'volume_id': deployment['data_volume_id'], 'host_id': deployment['host_id'],
                'filesystem_uuid': deployment['data_filesystem_uuid'], 'attach_time': attachment['AttachTime'],
                'provenance_expires_at_utc': (wallclock() + dt.timedelta(minutes=10)).isoformat(),
                'mandatory_wait_seconds': WAIT_SECONDS, 'host_first_setup': checked}
    raise HostError('fresh-volume provenance remains incomplete; no initialization is authorized')
