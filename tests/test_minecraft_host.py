from __future__ import annotations

import contextlib
import http.client
import http.server
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_host  # noqa: E402

HOST_ID = "i-0123456789abcdef0"
ACCOUNT_ID = "123456789012"
HOSTNAME = "mc.example.com"
ZONE_ID = "0123456789abcdef0123456789abcdef"
RECORD_ID = "fedcba9876543210fedcba9876543210"
# RFC 7723/8155 anycast addresses: special-purpose, yet global to `ipaddress`, so these
# fixtures name no real host. The RFC 5737 documentation ranges are not global.
HOST_IP = "192.0.0.9"
NEXT_HOST_IP = "192.0.0.10"
PARKED_IP = "192.0.2.1"
TOKEN = "placeholder-cloudflare-token"
CLOUDFLARE_API = minecraft_host.CLOUDFLARE_API
DRAINED = {"draining": True, "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "complete"}}}
STORAGE = {"size_bytes": 5_196_382_208, "available_bytes": 4_100_000_000, "used_percent": 16}
STORAGE_DOC = "docs/operations/data-disk-capacity.md"


class FakeAWS:
    """AWS CLI boundary: tests never contact AWS or run a real host command."""

    def __init__(self):
        self.state = "stopped"
        self.reported = None
        self.address = HOST_IP
        self.address_lag = 0
        self.calls = []
        self.now = 0.0
        self.online = True
        self.fail_operation = None
        self.invocation_status = "Success"
        self.account_id = ACCOUNT_ID
        self.remote = {"instance": "survival", "application": "running", "backup": "pending", "ready": True}

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def run(self, service, operation, *arguments, timeout):
        if not 0 < timeout <= 30:
            raise AssertionError(f"unbounded AWS command timeout: {timeout}")
        self.calls.append((service, operation, arguments))
        if operation == self.fail_operation:
            raise minecraft_host.HostError(f"AWS {operation} failed")
        if operation == "get-caller-identity":
            return {"Account": self.account_id, "Arn": "private-operator-identity", "UserId": "private-user-id"}
        if operation == "describe-instances":
            state = self.state
            if self.state == "pending":
                self.state = "running"
            if self.state == "stopping":
                self.state = "stopped"
            self.reported = state
            instance = {"InstanceId": HOST_ID, "State": {"Name": state}}
            # EC2 publishes an auto-assigned address only for a running host, sometimes late.
            if state == "running" and self.address_lag:
                self.address_lag -= 1
            elif state == "running" and self.address is not None:
                instance["PublicIpAddress"] = self.address
            return {"Reservations": [{"Instances": [instance]}]}
        if operation == "start-instances":
            self.state = "pending"
            return {}
        if operation == "stop-instances":
            self.state = "stopping"
            return {}
        if operation == "describe-instance-information":
            return {"InstanceInformationList": [{"InstanceId": HOST_ID, "PingStatus": "Online" if self.online else "ConnectionLost"}]}
        if operation == "send-command":
            return {"Command": {"CommandId": "12345678-1234-1234-1234-123456789abc"}}
        if operation == "get-command-invocation":
            return {"Status": self.invocation_status, "ResponseCode": 0, "StandardOutputContent": json.dumps(self.remote)}
        raise AssertionError(f"unexpected AWS operation: {operation}")


class ScriptedStatesAWS(FakeAWS):
    def __init__(self, states):
        super().__init__()
        self.states = list(states)

    def run(self, service, operation, *arguments, timeout):
        response = super().run(service, operation, *arguments, timeout=timeout)
        if operation == "describe-instances":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            response["Reservations"][0]["Instances"][0]["State"]["Name"] = state
            self.reported = state
        return response


class FakeCloudflare:
    """Cloudflare API boundary: tests never contact Cloudflare."""

    def __init__(self, content=PARKED_IP, *, aws=None, **fields):
        self.records = [{"id": RECORD_ID, "type": "A", "name": HOSTNAME, "content": content, "ttl": 60, "proxied": False, **fields}]
        self.aws = aws
        self.calls = []
        self.context = []
        self.fail_method = None
        self.fail_with = None
        self.confirm = True
        self.confirm_as = {}

    def request(self, method, path, *, query=None, body=None, timeout):
        if not 0 < timeout <= 30:
            raise AssertionError(f"unbounded Cloudflare request timeout: {timeout}")
        self.calls.append((method, path, query, body))
        if self.aws:
            self.context.append((self.aws.reported, [call[1] for call in self.aws.calls]))
        if method == self.fail_method:
            raise self.fail_with or minecraft_host.HostError(f"Cloudflare {method} failed")
        if (method, path, query, body) == ("GET", f"/zones/{ZONE_ID}/dns_records", {"name.exact": HOSTNAME}, None):
            return {"success": True, "result": json.loads(json.dumps(self.records))}
        if method == "PATCH" and query is None:
            for record in self.records:
                if path == f"/zones/{ZONE_ID}/dns_records/{record['id']}":
                    if self.confirm:
                        record.update(body)
                    return {"success": True, "result": {**record, **self.confirm_as}}
        raise AssertionError(f"unexpected Cloudflare request: {method} {path}")

    def patches(self):
        return [body for method, _, _, body in self.calls if method == "PATCH"]


class HostCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.deployment = Path(self.temp.name) / "deployment.json"
        self.deployment.write_text(json.dumps({"account_id": ACCOUNT_ID, "host_id": HOST_ID, "region": "us-west-2", "instances": {"survival": {}}}))

    def invoke(self, aws, action, *extra, cloudflare=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = minecraft_host.main(["--deployment", str(self.deployment), action, *extra], aws=aws, cloudflare=cloudflare)
        return result, json.loads(stdout.getvalue())

    def test_start_waits_for_host_and_ssm_and_duplicate_start_is_safe(self):
        aws = FakeAWS()
        result, output = self.invoke(aws, "start", "--instance", "survival")
        self.assertEqual(result, 0, output)
        self.assertEqual(aws.calls[0], ("sts", "get-caller-identity", ()))
        self.assertEqual(aws.calls[1][1], "describe-instances")
        self.assertEqual(output, {"host": "running", "instance": "survival", "application": "running", "backup": "pending", "ready": True})
        self.assertEqual(self.invoke(aws, "start", "--instance", "survival")[0], 0)
        self.assertEqual(sum(call[1] == "get-caller-identity" for call in aws.calls), 2)
        self.assertEqual(sum(call[1] == "start-instances" for call in aws.calls), 1)
        sends = [call for call in aws.calls if call[1] == "send-command"]
        self.assertEqual(len(sends), 2)
        parameters = json.loads(sends[0][2][sends[0][2].index("--parameters") + 1])
        self.assertEqual(parameters["commands"], ["/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json start survival"])
        self.assertGreater(int(parameters["executionTimeout"][0]), 0)

    def test_wrong_aws_account_blocks_start_stop_and_status_before_ec2_or_ssm(self):
        for action in ("start", "stop", "status"):
            with self.subTest(action=action):
                aws = FakeAWS()
                aws.account_id = "987654321098"
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 1, output)
                self.assertEqual(aws.calls, [("sts", "get-caller-identity", ())])
                self.assertIn("account", output["error"])
                for private_value in (ACCOUNT_ID, aws.account_id, "private-operator-identity", "private-user-id"):
                    self.assertNotIn(private_value, json.dumps(output))

    def test_acknowledged_ec2_mutations_retry_the_previous_state_before_transition(self):
        for action, states, expected in (
            ("start", ["stopped", "stopped", "pending", "running"], "running"),
            ("stop", ["running", "running", "stopping", "stopped"], "stopped"),
        ):
            with self.subTest(action=action):
                aws = ScriptedStatesAWS(states)
                if action == "stop":
                    aws.remote = {"draining": True, "instances": {"survival": {
                        "instance": "survival", "application": "stopped", "backup": "complete"}}}
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 0, output)
                self.assertEqual(output["host"], expected)
                self.assertEqual(sum(call[1] == action + "-instances" for call in aws.calls), 1)
                self.assertEqual(sum(call[1] == "describe-instances" for call in aws.calls), 4)
                self.assertEqual(aws.calls[0], ("sts", "get-caller-identity", ()))
                self.assertGreater(aws.now, 0)

    def test_persistent_previous_state_times_out_without_reissuing_mutation(self):
        for action, previous in (("start", "stopped"), ("stop", "running")):
            with self.subTest(action=action):
                aws = ScriptedStatesAWS([previous])
                if action == "stop":
                    aws.remote = {"draining": True, "instances": {"survival": {
                        "instance": "survival", "application": "stopped", "backup": "complete"}}}
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 1, output)
                self.assertIn("timed out", output["error"])
                self.assertEqual(output["host"], previous)
                self.assertEqual(aws.now, 300)
                self.assertEqual(sum(call[1] == action + "-instances" for call in aws.calls), 1)
                self.assertEqual(sum(call[1] == "send-command" for call in aws.calls), 1 if action == "stop" else 0)
                if action == "stop":
                    self.assertEqual(output["instances"]["survival"]["backup"], "complete")

    def test_unsupported_state_after_mutation_still_fails_immediately(self):
        for action, previous, unsupported in (
            ("start", "stopped", "stopping"), ("start", "stopped", "terminated"),
            ("start", "stopped", "shutting-down"), ("stop", "running", "pending"),
            ("stop", "running", "terminated"), ("stop", "running", "shutting-down"),
        ):
            with self.subTest(action=action, unsupported=unsupported):
                aws = ScriptedStatesAWS([previous, unsupported])
                if action == "stop":
                    aws.remote = {"draining": True, "instances": {"survival": {
                        "instance": "survival", "application": "stopped", "backup": "complete"}}}
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 1, output)
                self.assertIn(unsupported, output["error"])
                self.assertEqual(aws.now, 0)
                self.assertEqual(sum(call[1] == action + "-instances" for call in aws.calls), 1)

    def test_previous_state_retry_requires_an_acknowledged_mutation(self):
        aws = ScriptedStatesAWS(["pending", "stopped"])
        result, output = self.invoke(aws, "start", "--instance", "survival")
        self.assertEqual(result, 1, output)
        self.assertIn("cannot reach running from stopped", output["error"])
        self.assertEqual(aws.now, 0)
        self.assertFalse(any(call[1] in ("start-instances", "send-command") for call in aws.calls))

    def test_missing_or_malformed_deployment_account_blocks_every_aws_call(self):
        for account_id in (None, "", "12345678901", "1234567890123", 123456789012, "12345678901x", "123456789012\n", "１２３４５６７８９０１２"):
            with self.subTest(account_id=account_id):
                deployment = {"host_id": HOST_ID, "region": "us-west-2", "instances": {"survival": {}}}
                if account_id is not None:
                    deployment["account_id"] = account_id
                self.deployment.write_text(json.dumps(deployment))
                aws = FakeAWS()
                result, output = self.invoke(aws, "start", "--instance", "survival")
                self.assertEqual(result, 1, output)
                self.assertIn("account_id", output["error"])
                self.assertEqual(aws.calls, [])

    def test_unverified_caller_identity_blocks_ec2_and_ssm(self):
        for identity in ({}, {"Account": None}, {"Account": 123456789012}, {"Account": ACCOUNT_ID + "\n"}):
            for action in ("start", "stop", "status"):
                with self.subTest(identity=identity, action=action):
                    class InvalidIdentityAWS(FakeAWS):
                        def run(self, service, operation, *arguments, timeout):
                            response = super().run(service, operation, *arguments, timeout=timeout)
                            return identity if operation == "get-caller-identity" else response
                    aws = InvalidIdentityAWS()
                    result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                    self.assertEqual(result, 1, output)
                    self.assertEqual(aws.calls, [("sts", "get-caller-identity", ())])

    def test_sts_failure_blocks_all_lifecycle_commands(self):
        for action in ("start", "stop", "status"):
            with self.subTest(action=action):
                aws = FakeAWS()
                aws.fail_operation = "get-caller-identity"
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 1, output)
                self.assertEqual(aws.calls, [("sts", "get-caller-identity", ())])
                self.assertIn("get-caller-identity", output["error"])

    def test_stop_drains_every_instance_before_ec2_stop_and_repeat_is_safe(self):
        aws = FakeAWS()
        aws.state = "running"
        aws.remote = {"draining": True, "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "complete"}}}
        result, output = self.invoke(aws, "stop")
        self.assertEqual(result, 0, output)
        self.assertEqual(output["host"], "stopped")
        self.assertEqual(output["instances"]["survival"]["backup"], "complete")
        operations = [call[1] for call in aws.calls]
        self.assertLess(operations.index("get-command-invocation"), operations.index("stop-instances"))
        result, output = self.invoke(aws, "stop")
        self.assertEqual(result, 0, output)
        self.assertEqual(output, {"host": "stopped", "application": "unavailable", "backup": "unverified"})
        self.assertEqual(sum(call[1] == "stop-instances" for call in aws.calls), 1)
        self.assertEqual(sum(call[1] == "send-command" for call in aws.calls), 1)

    def test_status_separates_host_application_and_backup_and_redacts_details(self):
        aws = FakeAWS()
        aws.state = "running"
        aws.remote = {"draining": False, "secret": "rcon-secret", "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "failed", "players": ["private-player"]}}}
        result, output = self.invoke(aws, "status")
        self.assertEqual(result, 0, output)
        self.assertEqual(output, {"host": "running", "draining": False, "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "failed"}}})
        self.assertNotIn("private-player", json.dumps(output))
        self.assertNotIn("rcon-secret", json.dumps(output))
        aws.state = "stopped"
        result, output = self.invoke(aws, "status")
        self.assertEqual(result, 0, output)
        self.assertEqual(output["backup"], "unverified")

    def test_ec2_stop_failure_retains_verified_application_and_backup_outcomes(self):
        aws = FakeAWS()
        aws.state = "running"
        aws.fail_operation = "stop-instances"
        aws.remote = {"draining": True, "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "complete"}}}
        result, output = self.invoke(aws, "stop")
        self.assertEqual(result, 1, output)
        self.assertEqual(output["host"], "running")
        self.assertEqual(output["instances"]["survival"]["application"], "stopped")
        self.assertEqual(output["instances"]["survival"]["backup"], "complete")

    def test_new_ssm_invocation_is_retried_until_aws_exposes_it(self):
        class EventuallyConsistentAWS(FakeAWS):
            def run(self, service, operation, *arguments, timeout):
                if operation == "get-command-invocation" and not getattr(self, "queried", False):
                    self.queried = True
                    with mock.patch.object(minecraft_host.subprocess, "run", return_value=subprocess.CompletedProcess([], 254, "", "An error occurred (InvocationDoesNotExist) when calling the GetCommandInvocation operation")):
                        return minecraft_host.AWSCLI("us-west-2").run(service, operation, *arguments, timeout=timeout)
                return super().run(service, operation, *arguments, timeout=timeout)
        aws = EventuallyConsistentAWS()
        result, output = self.invoke(aws, "start", "--instance", "survival")
        self.assertEqual(result, 0, output)
        self.assertTrue(output["ready"])

    def test_failed_or_incomplete_drain_never_stops_ec2_and_retry_resolves_pending_backup(self):
        for failure in ("pending", "failed", "running", "missing-instance", "missing-barrier", "remote-failure"):
            with self.subTest(failure=failure):
                aws = FakeAWS()
                aws.state = "running"
                status = {"instance": "survival", "application": "stopped", "backup": "complete"}
                aws.remote = {"draining": True, "instances": {"survival": status}}
                if failure in ("pending", "failed"):
                    status["backup"] = failure
                elif failure == "running":
                    status["application"] = "running"
                elif failure == "missing-instance":
                    aws.remote["instances"] = {}
                elif failure == "missing-barrier":
                    aws.remote["draining"] = False
                else:
                    aws.invocation_status = "Failed"
                result, output = self.invoke(aws, "stop")
                self.assertEqual(result, 1, output)
                self.assertFalse(any(call[1] == "stop-instances" for call in aws.calls))
                self.assertEqual(aws.state, "running")
                aws.invocation_status = "Success"
                aws.remote = {"draining": True, "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "complete"}}}
                self.assertEqual(self.invoke(aws, "stop")[0], 0)
                self.assertEqual(sum(call[1] == "send-command" for call in aws.calls), 2)

    def test_actual_lifecycle_backup_failures_block_ec2_stop_on_stopped_session_retries(self):
        import minecraft_instance
        from test_minecraft_instance import FakeMachine

        retained = Path(self.temp.name) / "retained"
        machine = FakeMachine(retained)
        manager = minecraft_instance.Manager(retained, machine, ["survival"])
        manager.execute("start", "survival")
        session = manager.state("survival")["session"]

        class LifecycleAWS(FakeAWS):
            def run(self, service, operation, *arguments, timeout):
                response = super().run(service, operation, *arguments, timeout=timeout)
                if operation == "send-command":
                    parameters = json.loads(arguments[arguments.index("--parameters") + 1])
                    if parameters["commands"] != ["/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json host-drain"]:
                        raise AssertionError("host stop did not request guarded lifecycle drain")
                    try:
                        self.remote = manager.execute("host-drain")
                        self.invocation_status = "Success"
                    except minecraft_instance.OperationError:
                        self.invocation_status = "Failed"
                return response

        aws = LifecycleAWS()
        aws.state = "running"
        for _ in range(2):
            result, output = self.invoke(aws, "stop")
            self.assertEqual(result, 1, output)
            self.assertFalse(machine.running)
            self.assertEqual(manager.state("survival")["backup"], "failed")
            self.assertEqual(manager.state("survival")["session"], session)
            self.assertFalse(any(call[1] == "stop-instances" for call in aws.calls))
        machine.backup_works = True
        result, output = self.invoke(aws, "stop")
        self.assertEqual(result, 0, output)
        self.assertEqual(output["host"], "stopped")
        self.assertEqual(machine.events.count("backup"), 3)
        self.assertEqual(machine.events.count("save"), 1)
        self.assertEqual(machine.events.count("shutdown"), 1)
        self.assertEqual(sum(call[1] == "stop-instances" for call in aws.calls), 1)

    def test_drain_accounts_for_all_configured_instances_including_never_started(self):
        deployment = json.loads(self.deployment.read_text())
        deployment["instances"]["creative"] = {}
        self.deployment.write_text(json.dumps(deployment))
        aws = FakeAWS()
        aws.state = "running"
        aws.remote = {"draining": True, "instances": {
            "survival": {"instance": "survival", "application": "stopped", "backup": "complete"},
            "creative": {"instance": "creative", "application": "stopped", "backup": "not-required"},
        }}
        result, output = self.invoke(aws, "stop")
        self.assertEqual(result, 0, output)
        self.assertEqual(set(output["instances"]), {"survival", "creative"})

    def test_aws_failures_preserve_errors_and_prevent_later_actions(self):
        for operation in ("describe-instances", "start-instances", "describe-instance-information", "send-command", "get-command-invocation"):
            with self.subTest(operation=operation):
                aws = FakeAWS()
                aws.fail_operation = operation
                result, output = self.invoke(aws, "start", "--instance", "survival")
                self.assertEqual(result, 1, output)
                self.assertIn(operation, output["error"])
                self.assertEqual(aws.calls[-1][1], operation)

    def test_readiness_and_ssm_execution_failures_never_claim_session_started(self):
        for failure in ("not-ready", "TimedOut", "Cancelled", "Failed"):
            with self.subTest(failure=failure):
                aws = FakeAWS()
                if failure == "not-ready":
                    aws.remote["ready"] = False
                else:
                    aws.invocation_status = failure
                self.assertEqual(self.invoke(aws, "start", "--instance", "survival")[0], 1)

    def test_unavailable_ssm_and_hung_remote_command_have_bounded_waits(self):
        for stage in ("ssm-offline", "remote-pending"):
            with self.subTest(stage=stage):
                aws = FakeAWS()
                aws.state = "running"
                aws.online = stage != "ssm-offline"
                if stage == "remote-pending":
                    aws.invocation_status = "InProgress"
                result, output = self.invoke(aws, "stop")
                self.assertEqual(result, 1, output)
                self.assertIn("timed out", output["error"])
                self.assertLessEqual(aws.now, 3660)
                self.assertFalse(any(call[1] == "stop-instances" for call in aws.calls))

    def test_unexpected_host_states_fail_without_mutating_aws(self):
        for state in ("terminated", "shutting-down", "stopping", "invented"):
            for action in ("start", "stop"):
                with self.subTest(state=state, action=action):
                    aws = FakeAWS()
                    aws.state = state
                    result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                    self.assertEqual(result, 1, output)
                    self.assertEqual([call[1] for call in aws.calls], ["get-caller-identity", "describe-instances"])

    def test_invalid_or_unconfigured_instance_is_rejected_before_aws(self):
        for instance in ("../survival", "survival;echo secret", "creative", "Survival"):
            aws = FakeAWS()
            result, output = self.invoke(aws, "start", "--instance", instance)
            self.assertEqual(result, 1, output)
            self.assertEqual(aws.calls, [])

    def test_aws_process_failure_diagnostics_are_bounded_and_private(self):
        for failure in (subprocess.TimeoutExpired("aws", 30), FileNotFoundError(), subprocess.CompletedProcess([], 254, "private-player", "credential-secret")):
            with self.subTest(failure=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(minecraft_host.subprocess, "run", **kwargs) as command:
                    result, output = self.invoke(minecraft_host.AWSCLI("us-west-2"), "status")
                self.assertEqual(result, 1, output)
                self.assertEqual(command.call_args.kwargs["timeout"], 30)
                self.assertEqual(command.call_args.args[0][-2:], ["sts", "get-caller-identity"])
                self.assertNotIn("private-player", json.dumps(output))
                self.assertNotIn("credential-secret", json.dumps(output))

    def test_malformed_aws_responses_fail_closed_with_a_structured_error(self):
        for operation, response in (("describe-instances", {"Reservations": None}), ("describe-instance-information", {"InstanceInformationList": [None]}), ("send-command", {"Command": None}), ("get-command-invocation", {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": "{}"})):
            with self.subTest(operation=operation):
                class MalformedAWS(FakeAWS):
                    def run(self, service, actual, *arguments, timeout):
                        if actual == operation:
                            self.calls.append((service, actual, arguments))
                            return response
                        return super().run(service, actual, *arguments, timeout=timeout)
                aws = MalformedAWS()
                aws.state = "running"
                result, output = self.invoke(aws, "stop")
                self.assertEqual(result, 1, output)
                self.assertIn("error", output)
                self.assertFalse(any(call[1] == "stop-instances" for call in aws.calls))

    def test_ec2_transition_timeout_does_not_claim_the_target_state(self):
        for action, state in (("start", "pending"), ("stop", "stopping")):
            with self.subTest(action=action):
                class StalledAWS(FakeAWS):
                    def run(self, service, operation, *arguments, timeout):
                        response = super().run(service, operation, *arguments, timeout=timeout)
                        if operation == "describe-instances" and response["Reservations"][0]["Instances"][0]["State"]["Name"] == state:
                            self.state = state
                        return response
                aws = StalledAWS()
                if action == "stop":
                    aws.state = "running"
                    aws.remote = {"draining": True, "instances": {"survival": {"instance": "survival", "application": "stopped", "backup": "complete"}}}
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 1, output)
                self.assertEqual(output["host"], state)
                self.assertEqual(aws.now, 300)
                self.assertIn("timed out", output["error"])

    def test_private_deployment_contract_is_validated_before_aws(self):
        for change in ({"host_id": "i-wrong"}, {"region": "--profile secret"}, {"instances": {}}, {"instances": {"../survival": {}}}):
            with self.subTest(change=change):
                self.deployment.write_text(json.dumps({"account_id": ACCOUNT_ID, "host_id": HOST_ID, "region": "us-west-2", "instances": {"survival": {}}, **change}))
                aws = FakeAWS()
                result, output = self.invoke(aws, "status")
                self.assertEqual(result, 1, output)
                self.assertEqual(aws.calls, [])

    def remote_for(self, action, storage):
        """A host response to start or host-status, optionally carrying data-disk usage."""
        status = {"instance": "survival", "application": "running", "backup": "pending"}
        response = {**status, "ready": True} if action == "start" else {"draining": False, "instances": {"survival": status}}
        return response if storage is None else {**response, "storage": storage}

    def test_start_and_status_report_validated_data_disk_usage(self):
        for action in ("start", "status"):
            with self.subTest(action=action):
                aws = FakeAWS()
                aws.state = "running"
                aws.remote = self.remote_for(action, {**STORAGE, "device": "private-device-name"})
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 0, output)
                self.assertEqual(output, {"host": "running", **self.remote_for(action, STORAGE)})
                self.assertNotIn("private-device-name", json.dumps(output))

    def test_storage_warning_starts_at_80_percent_and_never_fails_the_command(self):
        for action in ("start", "status"):
            for percent, warned in ((0, False), (79, False), (80, True), (81, True), (100, True)):
                with self.subTest(action=action, percent=percent):
                    aws = FakeAWS()
                    aws.state = "running"
                    aws.remote = self.remote_for(action, {**STORAGE, "used_percent": percent})
                    result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                    self.assertEqual(result, 0, output)
                    self.assertEqual(output["storage"]["used_percent"], percent)
                    self.assertNotIn("error", output)
                    if warned:
                        self.assertEqual(output["storage_warning"], f"data disk {percent}% used; grow it ({STORAGE_DOC})")
                    else:
                        self.assertNotIn("storage_warning", output)
        self.assertTrue((ROOT / STORAGE_DOC).is_file(), "the warning names a document that does not exist")

    def test_host_runtime_that_reports_no_storage_keeps_the_previous_output(self):
        for action in ("start", "status"):
            with self.subTest(action=action):
                aws = FakeAWS()
                aws.state = "running"
                aws.remote = self.remote_for(action, None)
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual((result, output), (0, {"host": "running", **aws.remote}))

    def test_malformed_storage_usage_is_rejected_and_keeps_the_verified_states(self):
        malformed = (
            None, [], "16%", 16, {}, {"size_bytes": 5, "available_bytes": 4},
            {**STORAGE, "used_percent": 16.0}, {**STORAGE, "used_percent": "16"}, {**STORAGE, "used_percent": True},
            {**STORAGE, "used_percent": 101}, {**STORAGE, "used_percent": -1},
            {**STORAGE, "size_bytes": 4_099_999_999}, {**STORAGE, "available_bytes": -1},
            {**STORAGE, "size_bytes": None}, {**STORAGE, "available_bytes": "4100000000"},
        )
        for action in ("start", "status"):
            for storage in malformed:
                with self.subTest(action=action, storage=storage):
                    aws = FakeAWS()
                    aws.state = "running"
                    aws.remote = {**self.remote_for(action, None), "storage": storage}
                    result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                    self.assertEqual(result, 1, output)
                    self.assertIn("storage", output["error"])
                    self.assertNotIn("storage", output)
                    self.assertNotIn("storage_warning", output)
                    # The game was started and verified; only the usage report is refused.
                    observed = output if action == "start" else output["instances"]["survival"]
                    self.assertEqual((observed["application"], observed["backup"]), ("running", "pending"))
                    self.assertEqual(output.get("ready"), True if action == "start" else None)

    def test_stop_ignores_storage_and_requires_exactly_the_previous_drain_evidence(self):
        for storage in (STORAGE, {**STORAGE, "used_percent": 95}, "malformed"):
            with self.subTest(storage=storage):
                aws = FakeAWS()
                aws.state = "running"
                aws.remote = {**DRAINED, "storage": storage}
                self.assertEqual(self.invoke(aws, "stop"), (0, {"host": "stopped", **DRAINED}))
                self.assertEqual(sum(call[1] == "stop-instances" for call in aws.calls), 1)

    def test_actual_lifecycle_start_and_status_usage_reaches_the_operator(self):
        import minecraft_instance
        from test_minecraft_instance import FakeMachine

        retained = Path(self.temp.name) / "retained"
        retained.mkdir()
        machine = FakeMachine(retained)
        # The real statvfs report of the fixture root, exactly as the host serializes it.
        machine.storage_usage = lambda: minecraft_instance.LinuxMachine.storage_usage(machine)
        manager = minecraft_instance.Manager(retained, machine, ["survival"])

        class LifecycleAWS(FakeAWS):
            def run(self, service, operation, *arguments, timeout):
                response = super().run(service, operation, *arguments, timeout=timeout)
                if operation == "send-command":
                    parameters = json.loads(arguments[arguments.index("--parameters") + 1])
                    action = parameters["commands"][0].removeprefix(minecraft_host.REMOTE_COMMAND + " ").split()
                    self.remote = json.loads(json.dumps(manager.execute(*action)))
                return response

        aws = LifecycleAWS()
        expected = minecraft_instance.LinuxMachine.storage_usage(machine)
        for action in ("start", "status"):
            with self.subTest(action=action):
                result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []))
                self.assertEqual(result, 0, output)
                self.assertEqual(set(output["storage"]), {"size_bytes", "available_bytes", "used_percent"})
                self.assertEqual(output["storage"]["size_bytes"], expected["size_bytes"])
                self.assertEqual("storage_warning" in output, output["storage"]["used_percent"] >= 80)
        self.assertTrue(machine.running)

    def test_deployment_without_public_hostname_makes_no_dns_call_and_prints_no_dns_field(self):
        aws, cloudflare = FakeAWS(), FakeCloudflare()
        result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
        self.assertEqual((result, output), (0, {"host": "running", "instance": "survival", "application": "running", "backup": "pending", "ready": True}))
        aws.remote = {"draining": False, "instances": {"survival": {"instance": "survival", "application": "running", "backup": "pending"}}}
        self.assertEqual(self.invoke(aws, "status", cloudflare=cloudflare), (0, {"host": "running", **aws.remote}))
        aws.remote = DRAINED
        for expected in ({"host": "stopped", **DRAINED}, {"host": "stopped", "application": "unavailable", "backup": "unverified"}):
            self.assertEqual(self.invoke(aws, "stop", cloudflare=cloudflare), (0, expected))
        self.assertEqual(cloudflare.calls, [])
        self.assertEqual(sum(call[1] == "describe-instances" for call in aws.calls), 8)
        aws = FakeAWS()
        result, output = self.invoke(aws, "dns", cloudflare=cloudflare)
        self.assertEqual(result, 1, output)
        self.assertIn("public_hostname", output["error"])
        self.assertEqual((aws.calls, cloudflare.calls), ([], []))

    def test_cloudflare_token_is_withheld_from_the_aws_process(self):
        directory = Path(self.temp.name) / "bin"
        directory.mkdir()
        capture = Path(self.temp.name) / "aws-environment"
        executable = directory / "aws"
        executable.write_text(f"#!/bin/sh\nenv >'{capture}'\nprintf '%s' '{{\"Account\": \"{ACCOUNT_ID}\"}}'\n")
        executable.chmod(0o700)
        environment = {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}", "CLOUDFLARE_API_TOKEN": TOKEN, "AWS_PROFILE": "operator"}
        with mock.patch.dict(os.environ, environment):
            response = minecraft_host.AWSCLI("us-west-2").run("sts", "get-caller-identity", timeout=30)
        self.assertEqual(response, {"Account": ACCOUNT_ID})
        received = capture.read_text()
        self.assertIn("AWS_PROFILE=operator", received)
        self.assertNotIn("CLOUDFLARE", received)
        self.assertNotIn(TOKEN, received)


class HostDNSTests(unittest.TestCase):
    """Only a deployment that names a public hostname has a DNS record to follow the host."""

    invoke = HostCommandTests.invoke
    update = {"type": "A", "name": HOSTNAME, "content": HOST_IP, "ttl": 60, "proxied": False}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.deployment = Path(self.temp.name) / "deployment.json"
        self.write_deployment()
        # No test may reach the real API or read a real token, even by mistake.
        for patch in (mock.patch.object(minecraft_host, "CLOUDFLARE_API", "https://cloudflare.invalid/client/v4"), mock.patch.dict(os.environ, {"no_proxy": "*"})):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("CLOUDFLARE_API_TOKEN", None)

    def write_deployment(self, **changes):
        self.deployment.write_text(json.dumps({"account_id": ACCOUNT_ID, "host_id": HOST_ID, "region": "us-west-2", "instances": {"survival": {}}, "public_hostname": HOSTNAME, "cloudflare_zone_id": ZONE_ID, **changes}))

    @contextlib.contextmanager
    def cloudflare_server(self, respond):
        """Loopback stand-in for the API: records exactly what the real adapter sends."""
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append((self.command, self.path, dict(self.headers), body))
                status, headers, payload = respond(self.command, self.path, body)
                self.send_response(status)
                for name, value in {**headers, "Content-Length": str(len(payload))}.items():
                    self.send_header(name, value)
                self.end_headers()
                with contextlib.suppress(OSError):  # the adapter stops reading an oversized reply
                    self.wfile.write(payload)

            do_GET = do_PATCH = answer

            def log_message(self, *arguments):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with mock.patch.object(minecraft_host, "CLOUDFLARE_API", f"http://127.0.0.1:{server.server_port}/client/v4"), mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": TOKEN}):
                yield requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_start_points_the_hostname_at_the_host_before_starting_the_game_and_follows_a_new_address(self):
        aws = FakeAWS()
        cloudflare = FakeCloudflare(aws=aws)
        result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
        self.assertEqual(result, 0, output)
        self.assertEqual(output, {"host": "running", "instance": "survival", "application": "running", "backup": "pending", "ready": True, "dns": "updated"})
        self.assertEqual([method for method, *_ in cloudflare.calls], ["GET", "PATCH"])
        self.assertEqual(cloudflare.patches(), [self.update])
        for reported, operations in cloudflare.context:
            self.assertEqual(reported, "running")
            self.assertNotIn("send-command", operations)
        # The address comes from the describe-instances responses that start already needs.
        self.assertEqual([call[1] for call in aws.calls], ["get-caller-identity", "describe-instances", "start-instances", "describe-instances", "describe-instances", "describe-instance-information", "send-command", "get-command-invocation"])
        result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
        self.assertEqual((result, output["dns"]), (0, "current"))
        self.assertEqual(len(cloudflare.patches()), 1)
        aws.state, aws.address = "stopped", NEXT_HOST_IP
        result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
        self.assertEqual((result, output["dns"]), (0, "updated"))
        self.assertEqual(cloudflare.patches()[1:], [{**self.update, "content": NEXT_HOST_IP}])
        self.assertNotIn("public_ip", output)

    def test_storage_usage_and_its_warning_are_reported_beside_any_dns_outcome(self):
        full = {**STORAGE, "used_percent": 85}
        warning = f"data disk 85% used; grow it ({STORAGE_DOC})"
        started = {"host": "running", "instance": "survival", "application": "running", "backup": "pending", "ready": True, "storage": full, "storage_warning": warning}
        for dns_works in (True, False):
            with self.subTest(dns_works=dns_works):
                aws = FakeAWS()
                aws.remote = {**aws.remote, "storage": full}
                cloudflare = FakeCloudflare()
                cloudflare.fail_method = None if dns_works else "GET"
                result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
                if dns_works:
                    self.assertEqual((result, output), (0, {**started, "dns": "updated"}))
                else:
                    self.assertEqual((result, output), (1, {**started, "dns": "failed", "public_ip": HOST_IP, "error": output["error"]}))
                    self.assertIn("DNS record not updated", output["error"])

    def test_a_drifted_record_is_forced_back_to_dns_only_and_ttl_60(self):
        for drift in ({"proxied": True}, {"ttl": 1}, {"ttl": 300}, {"proxied": True, "ttl": 1}):
            with self.subTest(drift=drift):
                aws = FakeAWS()
                aws.state = "running"
                cloudflare = FakeCloudflare(HOST_IP, **drift)
                self.assertEqual(self.invoke(aws, "dns", cloudflare=cloudflare), (0, {"host": "running", "dns": "updated"}))
                self.assertEqual(cloudflare.patches(), [self.update])

    def test_start_polls_a_bounded_time_for_a_global_address(self):
        aws = FakeAWS()
        aws.address_lag = 2
        result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=FakeCloudflare())
        self.assertEqual((result, output["dns"]), (0, "updated"))
        self.assertEqual(sum(call[1] == "describe-instances" for call in aws.calls), 5)
        for address in (None, "10.0.0.5", "100.64.0.5", "224.0.0.1", PARKED_IP, "not-an-address", 3221225481, [HOST_IP]):
            with self.subTest(address=address):
                aws = FakeAWS()
                aws.address = address
                cloudflare = FakeCloudflare()
                result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
                self.assertEqual(result, 1, output)
                self.assertEqual((output["dns"], output["application"], output["ready"]), ("failed", "running", True))
                self.assertIn("no public IPv4 address", output["error"])
                self.assertIn("rerun", output["error"])
                self.assertNotIn("public_ip", output)
                self.assertEqual(cloudflare.calls, [])
                self.assertEqual(aws.now, 303)
                self.assertEqual(sum(call[1] == "send-command" for call in aws.calls), 1)

    def test_dns_failures_never_block_the_game_and_report_the_address(self):
        other = {"id": "0" * 32, "type": "AAAA", "name": HOSTNAME, "content": "2001:db8::1", "ttl": 60, "proxied": False}
        failures = {
            "lookup failed": lambda api: setattr(api, "fail_method", "GET"),
            "update failed": lambda api: setattr(api, "fail_method", "PATCH"),
            "update unconfirmed": lambda api: setattr(api, "confirm", False),
            "update confirms another record": lambda api: setattr(api, "confirm_as", {"id": "2" * 32}),
            "update confirms another name": lambda api: setattr(api, "confirm_as", {"name": "www.example.com"}),
            "update confirms another type": lambda api: setattr(api, "confirm_as", {"type": "AAAA"}),
            "lookup failed unexpectedly": lambda api: (setattr(api, "fail_method", "GET"), setattr(api, "fail_with", RecursionError("deep reply"))),
            "update failed unexpectedly": lambda api: (setattr(api, "fail_method", "PATCH"), setattr(api, "fail_with", ValueError("bad proxy"))),
            "no record": lambda api: api.records.clear(),
            "no record list": lambda api: setattr(api, "records", None),
            "not a record": lambda api: api.records.__setitem__(0, "A"),
            "stray AAAA": lambda api: api.records.append(other),
            "duplicate A": lambda api: api.records.append({**api.records[0], "id": "1" * 32}),
            "CNAME": lambda api: api.records[0].update(type="CNAME", content="other.example.com"),
            "another name": lambda api: api.records[0].update(name="www.example.com"),
            "unsafe record id": lambda api: api.records[0].update(id="../../zones"),
        }
        for name, fail in failures.items():
            with self.subTest(failure=name):
                aws = FakeAWS()
                cloudflare = FakeCloudflare()
                fail(cloudflare)
                result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
                self.assertEqual(result, 1, output)
                self.assertEqual(output, {"host": "running", "instance": "survival", "application": "running", "backup": "pending", "ready": True, "dns": "failed", "public_ip": HOST_IP, "error": output["error"]})
                self.assertIn("rerun", output["error"])
                self.assertEqual(sum(call[1] == "send-command" for call in aws.calls), 1)
                self.assertEqual(len(cloudflare.patches()), name.startswith("update"))

    def test_a_failed_game_start_reports_the_game_error_and_keeps_the_dns_outcome(self):
        for dns_works in (True, False):
            with self.subTest(dns_works=dns_works):
                aws = FakeAWS()
                aws.invocation_status = "Failed"
                cloudflare = FakeCloudflare()
                cloudflare.fail_method = None if dns_works else "GET"
                result, output = self.invoke(aws, "start", "--instance", "survival", cloudflare=cloudflare)
                self.assertEqual(result, 1, output)
                self.assertIn("remote start failed", output["error"])
                self.assertEqual((output["dns"], output.get("public_ip")), ("updated", None) if dns_works else ("failed", HOST_IP))

    def test_stop_parks_the_hostname_only_after_ec2_reports_stopped_and_a_repeat_stop_parks_again(self):
        aws = FakeAWS()
        aws.state = "running"
        aws.remote = DRAINED
        cloudflare = FakeCloudflare(HOST_IP, aws=aws)
        self.assertEqual(self.invoke(aws, "stop", cloudflare=cloudflare), (0, {"host": "stopped", **DRAINED, "dns": "parked"}))
        self.assertEqual(cloudflare.patches(), [{**self.update, "content": PARKED_IP}])
        self.assertEqual([reported for reported, _ in cloudflare.context], ["stopped", "stopped"])
        self.assertTrue(all("stop-instances" in operations for _, operations in cloudflare.context))
        stopped = {"host": "stopped", "application": "unavailable", "backup": "unverified"}
        self.assertEqual(self.invoke(aws, "stop", cloudflare=cloudflare), (0, {**stopped, "dns": "current"}))
        # A stop outside this command leaves the record on the released address.
        cloudflare.records[0]["content"] = HOST_IP
        self.assertEqual(self.invoke(aws, "stop", cloudflare=cloudflare), (0, {**stopped, "dns": "parked"}))
        self.assertEqual(len(cloudflare.patches()), 2)
        self.assertEqual(sum(call[1] == "stop-instances" for call in aws.calls), 1)

    def test_a_stop_that_does_not_reach_stopped_makes_no_dns_call(self):
        for failure in ("backup-failed", "ec2-stop-failed", "ec2-stalled"):
            with self.subTest(failure=failure):
                aws = ScriptedStatesAWS(["running", "stopping"]) if failure == "ec2-stalled" else FakeAWS()
                aws.state = "running"
                aws.remote = json.loads(json.dumps(DRAINED))
                if failure == "backup-failed":
                    aws.remote["instances"]["survival"]["backup"] = "failed"
                elif failure == "ec2-stop-failed":
                    aws.fail_operation = "stop-instances"
                cloudflare = FakeCloudflare(HOST_IP)
                result, output = self.invoke(aws, "stop", cloudflare=cloudflare)
                self.assertEqual(result, 1, output)
                self.assertNotIn("dns", output)
                self.assertEqual(cloudflare.calls, [])

    def test_a_failed_park_keeps_the_drain_evidence_and_the_ec2_stop(self):
        aws = FakeAWS()
        aws.state = "running"
        aws.remote = DRAINED
        cloudflare = FakeCloudflare(HOST_IP)
        cloudflare.fail_method = "PATCH"
        result, output = self.invoke(aws, "stop", cloudflare=cloudflare)
        self.assertEqual(result, 1, output)
        self.assertEqual(output, {"host": "stopped", **DRAINED, "dns": "failed", "error": output["error"]})
        self.assertIn("rerun", output["error"])
        self.assertEqual(aws.state, "stopped")
        cloudflare.fail_method = None
        self.assertEqual(self.invoke(aws, "dns", cloudflare=cloudflare), (0, {"host": "stopped", "dns": "parked"}))

    def test_dns_syncs_a_running_host_parks_a_stopped_one_and_refuses_transitions(self):
        aws = FakeAWS()
        cloudflare = FakeCloudflare(NEXT_HOST_IP)
        for state, outcome in (("running", "updated"), ("running", "current"), ("stopped", "parked"), ("stopped", "current")):
            aws.state = state
            self.assertEqual(self.invoke(aws, "dns", cloudflare=cloudflare), (0, {"host": state, "dns": outcome}))
        self.assertEqual([body["content"] for body in cloudflare.patches()], [HOST_IP, PARKED_IP])
        self.assertEqual({call[1] for call in aws.calls}, {"get-caller-identity", "describe-instances"})
        for state in ("pending", "stopping", "shutting-down", "terminated"):
            with self.subTest(state=state):
                aws = FakeAWS()
                aws.state = state
                cloudflare = FakeCloudflare()
                result, output = self.invoke(aws, "dns", cloudflare=cloudflare)
                self.assertEqual(result, 1, output)
                self.assertNotIn("dns", output)
                self.assertIn("retry", output["error"])
                self.assertEqual(cloudflare.calls, [])
                self.assertEqual([call[1] for call in aws.calls], ["get-caller-identity", "describe-instances"])

    def test_a_failed_dns_command_reports_the_running_address(self):
        aws = FakeAWS()
        aws.state = "running"
        cloudflare = FakeCloudflare()
        cloudflare.fail_method = "GET"
        result, output = self.invoke(aws, "dns", cloudflare=cloudflare)
        self.assertEqual((result, output), (1, {"host": "running", "dns": "failed", "public_ip": HOST_IP, "error": output["error"]}))

    def test_status_and_a_wrong_account_make_no_cloudflare_call(self):
        cloudflare = FakeCloudflare()
        for state in ("running", "stopped"):
            aws = FakeAWS()
            aws.state = state
            aws.remote = {"draining": False, "instances": {"survival": {"instance": "survival", "application": "running", "backup": "pending"}}}
            result, output = self.invoke(aws, "status", cloudflare=cloudflare)
            self.assertEqual(result, 0, output)
            self.assertNotIn("dns", output)
        for action in ("start", "stop", "dns"):
            aws = FakeAWS()
            aws.account_id = "987654321098"
            result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []), cloudflare=cloudflare)
            self.assertEqual(result, 1, output)
            self.assertEqual(aws.calls, [("sts", "get-caller-identity", ())])
        self.assertEqual(cloudflare.calls, [])

    def test_hostname_and_zone_are_validated_before_aws_or_cloudflare(self):
        hostnames = ("MC.example.com", "mc", "https://mc.example.com", "mc.example.com.", "mc..example.com", "-mc.example.com", "mc.example.com:25565", "mc.example.com/", PARKED_IP, "mc.example.com\n", ".".join(["a" * 63] * 4), "a" * 64 + ".example.com", 5, None)
        zones = (ZONE_ID.upper(), ZONE_ID + "0", ZONE_ID[:-1], "../" + ZONE_ID[3:], ZONE_ID + "\n", 5, None)
        for change in [{"public_hostname": value} for value in hostnames] + [{"cloudflare_zone_id": value} for value in zones]:
            for action in ("start", "stop", "status", "dns"):
                with self.subTest(change=change, action=action):
                    self.write_deployment(**change)
                    aws, cloudflare = FakeAWS(), FakeCloudflare()
                    result, output = self.invoke(aws, action, *(["--instance", "survival"] if action == "start" else []), cloudflare=cloudflare)
                    self.assertEqual(result, 1, output)
                    self.assertIn(next(iter(change)), output["error"])
                    self.assertEqual((aws.calls, cloudflare.calls), ([], []))

    def test_real_adapter_sends_the_token_only_in_the_authorization_header(self):
        record = {"id": RECORD_ID, "type": "A", "name": HOSTNAME, "content": PARKED_IP, "ttl": 60, "proxied": False}

        def respond(method, path, body):
            if method == "PATCH":
                record.update(json.loads(body))
            return 200, {"Content-Type": "application/json"}, json.dumps({"success": True, "result": record if method == "PATCH" else [record]}).encode()

        aws = FakeAWS()
        aws.state = "running"
        with self.cloudflare_server(respond) as requests:
            result, output = self.invoke(aws, "dns")
        self.assertEqual((result, output), (0, {"host": "running", "dns": "updated"}))
        self.assertEqual(CLOUDFLARE_API, "https://api.cloudflare.com/client/v4")
        self.assertEqual([(method, path) for method, path, _, _ in requests], [
            ("GET", f"/client/v4/zones/{ZONE_ID}/dns_records?name.exact={HOSTNAME}"),
            ("PATCH", f"/client/v4/zones/{ZONE_ID}/dns_records/{RECORD_ID}")])
        self.assertEqual(json.loads(requests[1][3]), self.update)
        for _, path, headers, body in requests:
            self.assertEqual([name for name, value in headers.items() if TOKEN in value], ["Authorization"])
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            self.assertNotIn(TOKEN, path + body.decode())

    def test_real_adapter_never_follows_a_redirect(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                aws = FakeAWS()
                aws.state = "running"
                with self.cloudflare_server(lambda *_: (status, {"Location": "/token-collector"}, b"")) as requests:
                    result, output = self.invoke(aws, "dns")
                self.assertEqual((result, output["dns"]), (1, "failed"))
                self.assertEqual([path for _, path, _, _ in requests], [f"/client/v4/zones/{ZONE_ID}/dns_records?name.exact={HOSTNAME}"])

    def test_real_adapter_failures_are_bounded_and_private(self):
        current = [{"id": RECORD_ID, "type": "A", "name": HOSTNAME, "content": HOST_IP, "ttl": 60, "proxied": False}]
        private = json.dumps({"success": False, "errors": [{"code": 10000, "message": "private-detail " + TOKEN}], "result": current}).encode()
        responses = {
            "rejected": (403, private),
            "api failure": (200, private),
            "not json": (200, b"private-detail"),
            "not an object": (200, b'["private-detail"]'),
            "oversized": (200, json.dumps({"success": True, "result": current, "padding": "x" * (2 << 20)}).encode()),
        }
        transports = {
            "timeout": TimeoutError("private-detail"),
            "unreachable": urllib.error.URLError("private-detail"),
            "disconnected": http.client.RemoteDisconnected("private-detail"),
            "untrusted certificate": urllib.error.URLError(ssl.SSLCertVerificationError("private-detail")),
        }
        for name in [*responses, *transports]:
            with self.subTest(failure=name):
                aws = FakeAWS()
                aws.state = "running"
                if name in responses:
                    with self.cloudflare_server(lambda *_: (responses[name][0], {}, responses[name][1])):
                        result, output = self.invoke(aws, "dns")
                else:
                    with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": TOKEN}), mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=transports[name]) as opened:
                        result, output = self.invoke(aws, "dns")
                    self.assertEqual(opened.call_args.kwargs["timeout"], 30)
                self.assertEqual((result, output), (1, {"host": "running", "dns": "failed", "public_ip": HOST_IP, "error": output["error"]}))
                self.assertNotIn(TOKEN, json.dumps(output))
                self.assertNotIn("private-detail", json.dumps(output))
                if name == "untrusted certificate":
                    self.assertIn("certificate", output["error"])

    def test_a_missing_or_malformed_token_fails_dns_without_a_request(self):
        for token in (None, "", "two words", "line\nbreak", "tab\ttoken", "café-token"):
            with self.subTest(token=token):
                aws = FakeAWS()
                with mock.patch.dict(os.environ, {} if token is None else {"CLOUDFLARE_API_TOKEN": token}), mock.patch.object(urllib.request.OpenerDirector, "open") as opened:
                    result, output = self.invoke(aws, "start", "--instance", "survival")
                self.assertEqual(result, 1, output)
                self.assertEqual((output["dns"], output["public_ip"], output["ready"]), ("failed", HOST_IP, True))
                self.assertIn("CLOUDFLARE_API_TOKEN", output["error"])
                opened.assert_not_called()
                if token:
                    self.assertNotIn(token, json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()

class TemporaryOperatorCredentialsTests(unittest.TestCase):
    def test_available_profile_assumes_reviewed_role_in_memory_and_never_forwards_token(self):
        role = 'arn:aws:iam::123456789012:role/minecraft-operator'
        payload = {'Credentials': {'AccessKeyId': 'PRIVATE_ACCESS', 'SecretAccessKey': 'PRIVATE_SECRET', 'SessionToken': 'PRIVATE_SESSION'},
                   'AssumedRoleUser': {'Arn': 'arn:aws:sts::123456789012:assumed-role/minecraft-operator/minecraft-host'}}
        replies = [subprocess.CompletedProcess([], 0, json.dumps(payload), ''), subprocess.CompletedProcess([], 0, '{}', ''),
                   subprocess.CompletedProcess([], 0, '{}', '')]
        client = minecraft_host.AWSCLI('us-west-2', 'workloads-admin', role)
        with mock.patch.dict(os.environ, {minecraft_host.CLOUDFLARE_TOKEN: 'PRIVATE_CLOUDFLARE',
                                        'AWS_ACCESS_KEY_ID': 'AMBIENT_ACCESS', 'AWS_SECRET_ACCESS_KEY': 'AMBIENT_SECRET',
                                        'AWS_SESSION_TOKEN': 'AMBIENT_SESSION', 'AWS_SECURITY_TOKEN': 'AMBIENT_LEGACY',
                                        'AWS_CONFIG_FILE': '/unreviewed/config', 'AWS_SHARED_CREDENTIALS_FILE': '/unreviewed/credentials'}), \
                mock.patch.object(minecraft_host.subprocess, 'run', side_effect=replies) as run:
            client.run('ec2', 'describe-instances', timeout=30)
            client.run('ssm', 'describe-instance-information', timeout=30)
        self.assertIn('--profile', run.call_args_list[0].args[0])
        self.assertIn('assume-role', run.call_args_list[0].args[0])
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN', 'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE'):
            self.assertNotIn(name, run.call_args_list[0].kwargs['env'])
        for call in run.call_args_list[1:]:
            self.assertNotIn('--profile', call.args[0])
            self.assertEqual(call.kwargs['env']['AWS_SECRET_ACCESS_KEY'], 'PRIVATE_SECRET')
            self.assertNotIn(minecraft_host.CLOUDFLARE_TOKEN, call.kwargs['env'])
            self.assertNotIn('PRIVATE_SECRET', json.dumps(call.args))
        self.assertEqual(sum('assume-role' in call.args[0] for call in run.call_args_list), 1)

    def test_wrong_assumed_role_blocks_operational_call(self):
        response = {'Credentials': {'AccessKeyId': 'PRIVATE_ACCESS', 'SecretAccessKey': 'PRIVATE_SECRET', 'SessionToken': 'PRIVATE_SESSION'},
                    'AssumedRoleUser': {'Arn': 'arn:aws:sts::123456789012:assumed-role/wrong-role/minecraft-host'}}
        client = minecraft_host.AWSCLI('us-west-2', 'workloads-admin', 'arn:aws:iam::123456789012:role/minecraft-operator')
        with mock.patch.object(minecraft_host.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(response), '')) as run:
            with self.assertRaises(minecraft_host.HostError): client.run('ec2', 'stop-instances', timeout=30)
        self.assertEqual(run.call_count, 1)
