#!/usr/bin/env python3
"""Operator-side bounded EC2/SSM lifecycle for configured Minecraft instances and the host's DNS record."""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


INSTANCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
HOSTNAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
CLOUDFLARE_ID = re.compile(r"[0-9a-f]{32}")
REMOTE_COMMAND = "/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json"
HOST_TIMEOUT = 300
REMOTE_TIMEOUT = 3600
# Fixed in code: a deployment file must never choose where the token is sent.
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_TOKEN = "CLOUDFLARE_API_TOKEN"
CLOUDFLARE_RESPONSE_LIMIT = 1 << 20
# RFC 5737 documentation address, never routed: a stopped host's released address may
# be assigned to another AWS customer, so the hostname must not keep pointing at it.
PARKED_ADDRESS = "192.0.2.1"
DNS_TTL = 60
# Growing the data disk takes minutes; warn while that is still comfortably ahead of a full disk.
STORAGE_WARNING_PERCENT = 80
STORAGE_FIELDS = ("size_bytes", "available_bytes", "used_percent")


class HostError(Exception):
    """A controlled diagnostic safe to include in operator status output."""


class InvocationNotVisible(HostError):
    """Run Command is eventually consistent immediately after submission."""


class AWSCLI:
    def __init__(self, region: str, profile: str | None = None, role_arn: str | None = None):
        self.region = region
        self.profile = profile
        self.role_arn = role_arn
        self.credentials = None
        self.credentials_deadline = 0

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def environment(self) -> dict:
        environment = {key: value for key, value in os.environ.items() if key != CLOUDFLARE_TOKEN and not key.startswith("AWS_ENDPOINT_URL")}
        environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
        if self.profile is not None:
            for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
                environment.pop(name, None)
        if self.role_arn is not None:
            if self.credentials is None or self.monotonic() >= self.credentials_deadline:
                response = AWSCLI(self.region, self.profile).run("sts", "assume-role", "--role-arn", self.role_arn,
                         "--role-session-name", "minecraft-host", "--duration-seconds", "3600", timeout=30)
                credentials = response.get("Credentials")
                role_name = self.role_arn.rsplit("/", 1)[-1]
                account = self.role_arn.split(":")[4]
                expected = f"arn:aws:sts::{account}:assumed-role/{role_name}/minecraft-host"
                if (not isinstance(credentials, dict) or response.get("AssumedRoleUser", {}).get("Arn") != expected
                        or any(not isinstance(credentials.get(name), str) or not credentials[name] for name in ("AccessKeyId", "SecretAccessKey", "SessionToken"))):
                    raise HostError("STS did not confirm the reviewed temporary operator role")
                self.credentials = {"AWS_ACCESS_KEY_ID": credentials["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
                                    "AWS_SESSION_TOKEN": credentials["SessionToken"]}
                self.credentials_deadline = self.monotonic() + 3000
            for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SECURITY_TOKEN"):
                environment.pop(name, None)
            environment.update(self.credentials)
        return environment

    def run(self, service: str, operation: str, *arguments: str, timeout: float) -> dict:
        command = ["aws", "--region", self.region, "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt", service, operation, *arguments]
        if self.profile is not None and self.role_arn is None:
            command[1:1] = ["--profile", self.profile]
        environment = self.environment()
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=environment)
        except FileNotFoundError as exc:
            raise HostError("AWS CLI is required on the operator computer") from exc
        except OSError as exc:
            raise HostError("cannot execute AWS CLI on the operator computer") from exc
        except subprocess.TimeoutExpired as exc:
            raise HostError(f"AWS {operation} timed out; outcome may be incomplete") from exc
        if result.returncode:
            if operation == "get-command-invocation" and "(InvocationDoesNotExist)" in result.stderr:
                raise InvocationNotVisible("SSM invocation is not visible yet")
            raise HostError(f"AWS {operation} failed; inspect private AWS diagnostics")
        try:
            response = json.loads(result.stdout)
        except ValueError as exc:
            raise HostError(f"AWS {operation} returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise HostError(f"AWS {operation} returned an invalid response")
        return response


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Following a redirect would send the token to a host this code did not name."""

    def redirect_request(self, *arguments, **keywords):
        return None


class CloudflareAPI:
    """Cloudflare v4 API boundary; the token is sent only in the Authorization header."""

    def request(self, method: str, path: str, *, query=None, body=None, timeout: float) -> dict:
        token = os.environ.get(CLOUDFLARE_TOKEN, "")
        if not re.fullmatch(r"[\x21-\x7e]+", token):
            raise HostError(f"{CLOUDFLARE_TOKEN} is not set to an API token for this command")
        request = urllib.request.Request(
            CLOUDFLARE_API + path + ("?" + urllib.parse.urlencode(query) if query else ""), method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "minecraft-host-dns"})
        request.add_unredirected_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.build_opener(RefuseRedirects).open(request, timeout=timeout) as reply:
                content = reply.read(CLOUDFLARE_RESPONSE_LIMIT)  # a longer reply is cut short and fails to parse
        except urllib.error.HTTPError as exc:
            exc.close()
            raise HostError(f"Cloudflare API returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise HostError("cannot verify the Cloudflare API certificate; this Python needs a CA bundle") from exc
            raise HostError("cannot reach the Cloudflare API") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise HostError("Cloudflare API request failed or timed out; outcome may be incomplete") from exc
        try:
            response = json.loads(content)
        except ValueError as exc:
            raise HostError("Cloudflare API returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("success") is not True:
            raise HostError("Cloudflare API returned a failure or an invalid response")
        return response


def global_ipv4(value) -> str | None:
    """A public address safe to publish; anything else counts as not reported yet."""
    try:
        address = ipaddress.IPv4Address(value if isinstance(value, str) else "")
    except ValueError:
        return None
    return str(address) if address.is_global and not address.is_multicast else None


def load_deployment(path: Path) -> dict:
    try:
        deployment = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HostError("cannot read deployment JSON") from exc
    if not isinstance(deployment, dict):
        raise HostError("deployment must be a JSON object")
    account_id, host_id, region, instances = (deployment.get(key) for key in ("account_id", "host_id", "region", "instances"))
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id):
        raise HostError("deployment account_id must be a 12-digit AWS account ID")
    lifecycle = deployment.get("lifecycle_mode", "retained-host")
    if lifecycle not in ("retained-host", "on-demand-host"):
        raise HostError("deployment lifecycle mode is invalid")
    absent = lifecycle == "on-demand-host" and deployment.get("schema_version") == 2 and deployment.get("session_present") is False and host_id is None
    if not absent and (not isinstance(host_id, str) or not re.fullmatch(r"i-(?:[a-f0-9]{8}|[a-f0-9]{17})", host_id)):
        raise HostError("deployment host_id is invalid")
    if lifecycle == "on-demand-host":
        if deployment.get("schema_version") != 2 or type(deployment.get("session_present")) is not bool or bool(host_id) != deployment["session_present"]:
            raise HostError("deployment has an invalid on-demand session handoff")
        for key, expression in (("data_volume_id", r"vol-[0-9a-f]{17}"), ("data_filesystem_uuid", r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"),
                                ("bootstrap_sha256", r"[0-9a-f]{64}"), ("backup_bucket_name", r"[a-z][a-z0-9-]{1,61}[a-z0-9]")):
            if not isinstance(deployment.get(key), str) or not re.fullmatch(expression, deployment[key]):
                raise HostError("deployment on-demand identity is invalid")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
        raise HostError("deployment region is invalid")
    if not isinstance(instances, dict) or not instances or any(not INSTANCE_ID.fullmatch(key) or not isinstance(value, dict) for key, value in instances.items()):
        raise HostError("deployment instances must contain valid configured instance IDs")
    hostname, zone_id = deployment.get("public_hostname"), deployment.get("cloudflare_zone_id")
    if hostname is not None or zone_id is not None:
        if not isinstance(hostname, str) or len(hostname) > 253 or not HOSTNAME.fullmatch(hostname):
            raise HostError("deployment public_hostname must be a lowercase DNS hostname")
        if not isinstance(zone_id, str) or not CLOUDFLARE_ID.fullmatch(zone_id):
            raise HostError("deployment cloudflare_zone_id must be a 32-character lowercase hexadecimal ID")
    return deployment


def public_instance(value: dict, instance: str) -> dict:
    if not isinstance(value, dict) or value.get("instance") != instance:
        raise HostError("remote response does not match the requested instance")
    if value.get("application") not in ("running", "stopped", "unknown") or value.get("backup") not in ("pending", "complete", "not-required", "failed"):
        raise HostError("remote response has invalid application or backup status")
    result = {key: value[key] for key in ("instance", "application", "backup")}
    if isinstance(value.get("ready"), bool):
        result["ready"] = value["ready"]
    return result


def storage_report(response: dict) -> dict:
    """Validated data-disk usage from the host, with a warning once growing the disk is
    due. A host runtime that predates the report sends none; that is not an error."""
    if "storage" not in response:
        return {}
    storage = response["storage"]
    if (not isinstance(storage, dict) or any(type(storage.get(field)) is not int for field in STORAGE_FIELDS)
            or not 0 <= storage["available_bytes"] <= storage["size_bytes"] or not 0 <= storage["used_percent"] <= 100):
        raise HostError("remote response has invalid storage usage")
    report = {"storage": {field: storage[field] for field in STORAGE_FIELDS}}
    if storage["used_percent"] >= STORAGE_WARNING_PERCENT:
        report["storage_warning"] = f"data disk {storage['used_percent']}% used; grow it (docs/operations/data-disk-capacity.md)"
    return report


class HostSession:
    def __init__(self, deployment: dict, aws, cloudflare=None):
        self.deployment = deployment
        self.aws = aws
        self.cloudflare = cloudflare
        self.state = "unknown"
        self.address = None
        self.observed = {"application": "unknown", "backup": "unverified"}
        self.dns = {}
        self.dns_error = None

    def call(self, service: str, operation: str, *arguments: str, deadline=None) -> dict:
        timeout = 30 if deadline is None else min(30, deadline - self.aws.monotonic())
        if timeout <= 0:
            raise HostError(f"AWS {operation} wait timed out")
        return self.aws.run(service, operation, *arguments, timeout=timeout)

    def verify_account(self) -> None:
        identity = self.call("sts", "get-caller-identity")
        if identity.get("Account") != self.deployment["account_id"]:
            raise HostError("active AWS account does not match the reviewed deployment account")

    def host_state(self, deadline=None) -> str:
        response = self.call("ec2", "describe-instances", "--instance-ids", self.deployment["host_id"], deadline=deadline)
        try:
            instances = [instance for reservation in response["Reservations"] for instance in reservation["Instances"]]
            if len(instances) != 1 or instances[0]["InstanceId"] != self.deployment["host_id"]:
                raise ValueError
            state = instances[0]["State"]["Name"]
            if state not in ("pending", "running", "stopping", "stopped", "shutting-down", "terminated"):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise HostError("AWS returned an unknown host state") from exc
        self.state = state
        self.address = global_ipv4(instances[0].get("PublicIpAddress"))
        return state

    def public_address(self) -> str:
        """EC2 can report running before it publishes the address; poll only then."""
        deadline = self.aws.monotonic() + HOST_TIMEOUT
        while self.address is None:
            self.aws.sleep(min(3, max(0, deadline - self.aws.monotonic())))
            if self.aws.monotonic() >= deadline:
                raise HostError("the running host reported no public IPv4 address")
            if self.host_state(deadline) != "running":
                raise HostError("the host left running before reporting a public IPv4 address")
        return self.address

    def set_record(self, content: str) -> bool:
        """Point the one existing A record at content; never create or delete a record."""
        zone_id, hostname = self.deployment["cloudflare_zone_id"], self.deployment["public_hostname"]
        records = self.cloudflare.request("GET", f"/zones/{zone_id}/dns_records", query={"name.exact": hostname}, timeout=30).get("result")
        # Any second record, such as a stray AAAA or CNAME, would send some clients elsewhere.
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise HostError("the hostname must have exactly one DNS record")
        record = records[0]
        if record.get("type") != "A" or record.get("name") != hostname or not isinstance(record.get("id"), str) or not CLOUDFLARE_ID.fullmatch(record["id"]):
            raise HostError("the hostname's only DNS record must be an A record")

        def matches(value) -> bool:
            return isinstance(value, dict) and value.get("content") == content and value.get("ttl") == DNS_TTL and value.get("proxied") is False

        if matches(record):
            return False
        # DNS only (never proxied: the proxy carries only HTTP) and a TTL that follows a restart.
        body = {"type": "A", "name": hostname, "content": content, "ttl": DNS_TTL, "proxied": False}
        updated = self.cloudflare.request("PATCH", f"/zones/{zone_id}/dns_records/{record['id']}", body=body, timeout=30)
        result = updated.get("result")
        if not matches(result) or (result.get("id"), result.get("name"), result.get("type")) != (record["id"], hostname, "A"):
            raise HostError("Cloudflare did not confirm the DNS record update")
        return True

    def sync_dns(self) -> None:
        """Follow a running host or park a stopped one. A failure is recorded, never
        raised: DNS must not block or undo the game start, the drain, or the EC2 stop."""
        if self.deployment.get("public_hostname") is None:
            return
        running = self.state == "running"
        try:
            changed = self.set_record(self.public_address() if running else PARKED_ADDRESS)
            self.dns = {"dns": "current" if not changed else "updated" if running else "parked"}
        except Exception as exc:  # anything unexpected is a DNS failure too, with no detail printed
            reason = exc if isinstance(exc, HostError) else "unexpected Cloudflare client error"
            # The address is the one exception to printing no deployment identifiers:
            # the operator can hand it to players until the record is repaired.
            self.dns = {"dns": "failed", **({"public_ip": self.address} if self.state == "running" and self.address else {})}
            self.dns_error = f"DNS record not updated: {reason}; fix the cause and rerun the dns command"

    def wait_host(self, expected: str, transitional: str, *, previous=None) -> None:
        deadline = self.aws.monotonic() + HOST_TIMEOUT
        while True:
            state = self.host_state(deadline)
            if state == expected:
                return
            if state not in (transitional, previous):
                raise HostError(f"host cannot reach {expected} from {state}")
            self.aws.sleep(min(3, max(0, deadline - self.aws.monotonic())))

    def wait_ssm(self) -> None:
        deadline = self.aws.monotonic() + HOST_TIMEOUT
        while True:
            response = self.call("ssm", "describe-instance-information", "--filters", json.dumps([{"Key": "InstanceIds", "Values": [self.deployment["host_id"]]}]), deadline=deadline)
            nodes = response.get("InstanceInformationList", [])
            if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
                raise HostError("AWS returned invalid SSM node information")
            if any(node.get("InstanceId") == self.deployment["host_id"] and node.get("PingStatus") == "Online" for node in nodes):
                return
            self.aws.sleep(min(3, max(0, deadline - self.aws.monotonic())))

    def remote(self, action: str, instance=None) -> dict:
        command = REMOTE_COMMAND + " " + action + (" " + instance if instance else "")
        return self.execute_shell(command, action)

    def execute_shell(self, command: str, action: str) -> dict:
        response = self.call("ssm", "send-command", "--instance-ids", self.deployment["host_id"], "--document-name", "AWS-RunShellScript", "--timeout-seconds", "60", "--parameters", json.dumps({"commands": [command], "executionTimeout": [str(REMOTE_TIMEOUT)]}))
        metadata = response.get("Command")
        command_id = metadata.get("CommandId") if isinstance(metadata, dict) else None
        if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", command_id):
            raise HostError("SSM did not return a command identity")
        deadline = self.aws.monotonic() + REMOTE_TIMEOUT + 60
        while True:
            try:
                invocation = self.call("ssm", "get-command-invocation", "--command-id", command_id, "--instance-id", self.deployment["host_id"], deadline=deadline)
            except InvocationNotVisible:
                self.aws.sleep(min(3, max(0, deadline - self.aws.monotonic())))
                continue
            status = invocation.get("Status")
            if status == "Success":
                if invocation.get("ResponseCode") != 0:
                    raise HostError(f"remote {action} returned failure")
                try:
                    result = json.loads(invocation["StandardOutputContent"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise HostError(f"remote {action} returned invalid completion evidence") from exc
                if not isinstance(result, dict):
                    raise HostError(f"remote {action} returned invalid completion evidence")
                return result
            if status not in ("Pending", "InProgress", "Delayed"):
                raise HostError(f"remote {action} failed; inspect private SSM command output")
            self.aws.sleep(min(3, max(0, deadline - self.aws.monotonic())))

    def start(self, instance: str) -> dict:
        state = self.host_state()
        if state == "stopped":
            self.call("ec2", "start-instances", "--instance-ids", self.deployment["host_id"])
            self.wait_host("running", "pending", previous="stopped")
        elif state == "pending":
            self.wait_host("running", "pending")
        elif state != "running":
            raise HostError(f"host start cannot proceed from {state}; retry after the current transition")
        self.sync_dns()  # before the game boots, so the record propagates meanwhile
        self.wait_ssm()
        response = self.remote("start", instance)
        result = public_instance(response, instance)
        self.observed = result
        if result["application"] != "running" or result.get("ready") is not True:
            raise HostError("instance did not report ready after start")
        return {"host": self.state, **result, **storage_report(response)}

    def instance_statuses(self, response: dict) -> dict:
        instances = response.get("instances")
        if not isinstance(instances, dict) or set(instances) != set(self.deployment["instances"]):
            raise HostError("remote instance inventory does not match the deployment")
        return {instance: public_instance(value, instance) for instance, value in instances.items()}

    def stop(self) -> dict:
        state = self.host_state()
        if state == "stopped":
            self.sync_dns()
            return {"host": state, "application": "unavailable", "backup": "unverified"}
        if state != "running":
            raise HostError(f"host stop cannot proceed from {state}; retry after the current transition")
        self.wait_ssm()
        response = self.remote("host-drain")
        instances = self.instance_statuses(response)
        self.observed = {"instances": instances}
        if response.get("draining") is not True or any(value["application"] != "stopped" or value["backup"] not in ("complete", "not-required") for value in instances.values()):
            raise HostError("host drain lacks complete shutdown and backup evidence; EC2 stop forbidden")
        self.call("ec2", "stop-instances", "--instance-ids", self.deployment["host_id"])
        self.wait_host("stopped", "stopping", previous="running")
        self.sync_dns()  # only now has EC2 released the address
        return {"host": self.state, "draining": True, "instances": instances}

    def repair_dns(self) -> dict:
        """Catch the record up after a start or stop that this command did not perform."""
        self.observed = {}
        if self.host_state() not in ("running", "stopped"):
            raise HostError(f"dns cannot proceed from {self.state}; retry after the current transition")
        self.sync_dns()
        return {"host": self.state}

    def status(self) -> dict:
        if self.host_state() != "running":
            return {"host": self.state, "application": "unavailable", "backup": "unverified"}
        self.wait_ssm()
        response = self.remote("host-status")
        instances = self.instance_statuses(response)
        self.observed = {"instances": instances}
        if not isinstance(response.get("draining"), bool):
            raise HostError("remote host status lacks drain state")
        return {"host": self.state, "draining": response["draining"], "instances": instances, **storage_report(response)}


def main(argv=None, *, aws=None, cloudflare=None) -> int:
    command_started_at = time.time()
    command_started_monotonic = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--lifecycle", type=Path, help="private reviewed on-demand controller policy")
    parser.add_argument("--policy-sha256", help="SHA-256 of the approved recurring lifecycle policy")
    subparsers = parser.add_subparsers(dest="action", required=True)
    start = subparsers.add_parser("start", help="start host if necessary and a named instance")
    start.add_argument("--instance", required=True)
    subparsers.add_parser("stop", help="drain every instance and stop EC2 after required backups")
    subparsers.add_parser("status", help="inspect host, application and backup states")
    subparsers.add_parser("dns", help="point the public hostname at a running host, or park it for a stopped one")
    args = parser.parse_args(argv)
    session = None
    try:
        deployment = load_deployment(args.deployment)
        if args.action == "start" and (args.instance not in deployment["instances"] or not INSTANCE_ID.fullmatch(args.instance)):
            raise HostError("instance is invalid or not configured in the deployment")
        if args.action == "dns" and deployment.get("public_hostname") is None:
            raise HostError("deployment has no public_hostname; it has no DNS record to manage")
        if deployment.get("lifecycle_mode") == "on-demand-host":
            if args.lifecycle is None:
                raise HostError("on-demand mode requires a reviewed lifecycle policy")
            from minecraft_on_demand import OnDemandSession, load_policy
            policy = load_policy(args.lifecycle, args.policy_sha256, deployment)
            session = OnDemandSession(deployment, aws or AWSCLI(deployment["region"], policy["aws_profile"], deployment["operator_role_arn"]), cloudflare or CloudflareAPI(), policy, args.deployment, started_at=command_started_at, started_monotonic=command_started_monotonic)
        else:
            session = HostSession(deployment, aws or AWSCLI(deployment["region"]), cloudflare or CloudflareAPI())
        session.verify_account()
        if args.action == "start":
            output = session.start(args.instance)
        elif args.action == "stop":
            output = session.stop()
        elif args.action == "dns":
            output = session.repair_dns()
        else:
            output = session.status()
        output.update(session.dns)
        if session.dns_error:
            output["error"] = session.dns_error
        print(json.dumps(output, sort_keys=True))
        return 1 if session.dns_error else 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, HostError) else "lifecycle command failed; preserve its private journal and inspect the failed phase"
        observed = session.observed if session else {"application": "unknown", "backup": "unverified"}
        dns = session.dns if session else {}
        print(json.dumps({"host": session.state if session else "unknown", **observed, **dns, "error": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
