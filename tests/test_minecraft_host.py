from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_host  # noqa: E402

HOST_ID = "i-0123456789abcdef0"
ACCOUNT_ID = "123456789012"


class FakeAWS:
    """AWS CLI boundary: tests never contact AWS or run a real host command."""

    def __init__(self):
        self.state = "stopped"
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
            return {"Reservations": [{"Instances": [{"InstanceId": HOST_ID, "State": {"Name": state}}]}]}
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


class HostCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.deployment = Path(self.temp.name) / "deployment.json"
        self.deployment.write_text(json.dumps({"account_id": ACCOUNT_ID, "host_id": HOST_ID, "region": "us-west-2", "instances": {"survival": {}}}))

    def invoke(self, aws, action, *extra):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = minecraft_host.main(["--deployment", str(self.deployment), action, *extra], aws=aws)
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


if __name__ == "__main__":
    unittest.main()
