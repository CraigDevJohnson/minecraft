#!/usr/bin/env python3
"""Operator-side bounded EC2/SSM lifecycle for configured Minecraft instances."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


INSTANCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
REMOTE_COMMAND = "/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json"
HOST_TIMEOUT = 300
REMOTE_TIMEOUT = 3600


class HostError(Exception):
    """A controlled diagnostic safe to include in operator status output."""


class InvocationNotVisible(HostError):
    """Run Command is eventually consistent immediately after submission."""


class AWSCLI:
    def __init__(self, region: str):
        self.region = region

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def run(self, service: str, operation: str, *arguments: str, timeout: float) -> dict:
        command = ["aws", "--region", self.region, "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt", service, operation, *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
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


def load_deployment(path: Path) -> dict:
    try:
        deployment = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HostError("cannot read deployment JSON") from exc
    if not isinstance(deployment, dict):
        raise HostError("deployment must be a JSON object")
    host_id, region, instances = (deployment.get(key) for key in ("host_id", "region", "instances"))
    if not isinstance(host_id, str) or not re.fullmatch(r"i-(?:[a-f0-9]{8}|[a-f0-9]{17})", host_id):
        raise HostError("deployment host_id is invalid")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
        raise HostError("deployment region is invalid")
    if not isinstance(instances, dict) or not instances or any(not INSTANCE_ID.fullmatch(key) or not isinstance(value, dict) for key, value in instances.items()):
        raise HostError("deployment instances must contain valid configured instance IDs")
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


class HostSession:
    def __init__(self, deployment: dict, aws):
        self.deployment = deployment
        self.aws = aws
        self.state = "unknown"
        self.observed = {"application": "unknown", "backup": "unverified"}

    def call(self, service: str, operation: str, *arguments: str, deadline=None) -> dict:
        timeout = 30 if deadline is None else min(30, deadline - self.aws.monotonic())
        if timeout <= 0:
            raise HostError(f"AWS {operation} wait timed out")
        return self.aws.run(service, operation, *arguments, timeout=timeout)

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
        return state

    def wait_host(self, expected: str, transitional: str) -> None:
        deadline = self.aws.monotonic() + HOST_TIMEOUT
        while True:
            state = self.host_state(deadline)
            if state == expected:
                return
            if state != transitional:
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
            self.wait_host("running", "pending")
        elif state == "pending":
            self.wait_host("running", "pending")
        elif state != "running":
            raise HostError(f"host start cannot proceed from {state}; retry after the current transition")
        self.wait_ssm()
        result = public_instance(self.remote("start", instance), instance)
        self.observed = result
        if result["application"] != "running" or result.get("ready") is not True:
            raise HostError("instance did not report ready after start")
        return {"host": self.state, **result}

    def instance_statuses(self, response: dict) -> dict:
        instances = response.get("instances")
        if not isinstance(instances, dict) or set(instances) != set(self.deployment["instances"]):
            raise HostError("remote instance inventory does not match the deployment")
        return {instance: public_instance(value, instance) for instance, value in instances.items()}

    def stop(self) -> dict:
        state = self.host_state()
        if state == "stopped":
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
        self.wait_host("stopped", "stopping")
        return {"host": self.state, "draining": True, "instances": instances}

    def status(self) -> dict:
        if self.host_state() != "running":
            return {"host": self.state, "application": "unavailable", "backup": "unverified"}
        self.wait_ssm()
        response = self.remote("host-status")
        instances = self.instance_statuses(response)
        self.observed = {"instances": instances}
        if not isinstance(response.get("draining"), bool):
            raise HostError("remote host status lacks drain state")
        return {"host": self.state, "draining": response["draining"], "instances": instances}


def main(argv=None, *, aws=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    start = subparsers.add_parser("start", help="start host if necessary and a named instance")
    start.add_argument("--instance", required=True)
    subparsers.add_parser("stop", help="drain every instance and stop EC2 after required backups")
    subparsers.add_parser("status", help="inspect host, application and backup states")
    args = parser.parse_args(argv)
    session = None
    try:
        deployment = load_deployment(args.deployment)
        if args.action == "start" and (args.instance not in deployment["instances"] or not INSTANCE_ID.fullmatch(args.instance)):
            raise HostError("instance is invalid or not configured in the deployment")
        session = HostSession(deployment, aws or AWSCLI(deployment["region"]))
        if args.action == "start":
            output = session.start(args.instance)
        elif args.action == "stop":
            output = session.stop()
        else:
            output = session.status()
        print(json.dumps(output, sort_keys=True))
        return 0
    except HostError as exc:
        observed = session.observed if session else {"application": "unknown", "backup": "unverified"}
        print(json.dumps({"host": session.state if session else "unknown", **observed, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
