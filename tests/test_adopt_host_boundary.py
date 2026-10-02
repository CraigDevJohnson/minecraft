from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import adopt_host_boundary as adopt  # noqa: E402

ACCOUNT = "111122223333"
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/minecraft-host-boundary"
OLD_POLICY = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ec2:*", "Resource": "*"}]}


class FakeAWS:
    """Records calls; tests never contact AWS."""

    def __init__(self, *, account=ACCOUNT, boundary=None, attached=(), inline=("permissions", "state-backend"), roles=None):
        self.account = account
        self.boundary = boundary
        self.attached = list(attached)
        self.inline = list(inline)
        self.roles = roles if roles is not None else {"family-host": {}, "minecraft-operator": {}}
        self.policies = {"permissions": OLD_POLICY}
        self.calls = []

    def run(self, service, operation, *arguments):
        self.calls.append((service, operation, *arguments))
        arg = dict(zip(arguments[::2], arguments[1::2]))
        if (service, operation) == ("sts", "get-caller-identity"):
            return {"Account": self.account}
        if operation == "list-roles":
            return {"Roles": [{"RoleName": name} for name in self.roles]}
        if operation == "get-role":
            role = {"RoleName": arg["--role-name"]}
            boundary = self.roles.get(arg["--role-name"], {}).get("boundary")
            if boundary:
                role["PermissionsBoundary"] = {"PermissionsBoundaryArn": boundary}
            return {"Role": role}
        if operation == "list-attached-role-policies":
            return {"AttachedPolicies": [{"PolicyArn": arn} for arn in self.attached]}
        if operation == "list-role-policies":
            return {"PolicyNames": self.inline}
        if operation == "get-role-policy":
            return {"PolicyDocument": self.policies[arg["--policy-name"]]}
        if operation == "get-policy":
            if self.boundary is None:
                raise adopt.NoSuchEntity("get-policy")
            return {"Policy": {"DefaultVersionId": "v1"}}
        if operation == "get-policy-version":
            return {"PolicyVersion": {"Document": self.boundary}}
        if operation == "create-policy":
            self.boundary = json.loads(arg["--policy-document"])
            return {}
        if operation == "put-role-policy":
            self.policies[arg["--policy-name"]] = json.loads(arg["--policy-document"])
            return {}
        if operation == "validate-policy":
            return {"findings": []}
        raise AssertionError(f"unexpected call {service} {operation}")

    def writes(self):
        return [call[1] for call in self.calls if call[1] in ("create-policy", "put-role-policy")]


class AdoptHostBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "boundary"

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *argv, aws=None):
        base = ["--account-id", ACCOUNT, "--region", "us-west-2", "--iam-prefix", "family-", "--out-dir", str(self.out)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = adopt.main([*base, *argv], aws=aws or FakeAWS())
        return code, output.getvalue()

    def apply(self, aws):
        return self.run_tool("apply", "--policy-name", "permissions", "--confirm-operator-role", "minecraft-operator", aws=aws)

    def test_render_substitutes_every_placeholder(self):
        code, _ = self.run_tool("render")
        self.assertEqual(code, 0)
        for name in (adopt.BOUNDARY_FILE, adopt.OPERATOR_FILE):
            text = (self.out / name).read_text()
            self.assertNotIn("example", text.lower())
            self.assertNotIn("123456789012", text)
            self.assertEqual((self.out / name).stat().st_mode & 0o777, 0o600)
        permissions = (self.out / adopt.OPERATOR_FILE).read_text()
        self.assertIn(BOUNDARY_ARN, permissions)
        self.assertIn(f"arn:aws:iam::{ACCOUNT}:role/family-*", permissions)

    def test_names_inside_the_prefix_are_refused(self):
        code, output = self.run_tool("--boundary-name", "family-boundary", "render")
        self.assertEqual(code, 1)
        self.assertIn("outside the IAM prefix", output)
        self.assertFalse(self.out.exists())

    def test_wrong_account_credentials_stop_before_any_write(self):
        self.run_tool("render")
        aws = FakeAWS(account="999999999999")
        code, output = self.apply(aws)
        self.assertEqual(code, 1)
        self.assertIn("not 111122223333", output)
        self.assertEqual(aws.writes(), [])

    def test_apply_creates_boundary_saves_rollback_then_replaces_only_the_named_policy(self):
        self.run_tool("render")
        aws = FakeAWS()
        code, output = self.apply(aws)
        self.assertEqual(code, 0, output)
        self.assertEqual(aws.writes(), ["create-policy", "put-role-policy"])
        saved = json.loads((self.out / adopt.ROLLBACK_FILE).read_text())
        self.assertEqual(saved, {"role": "minecraft-operator", "policy_name": "permissions", "document": OLD_POLICY})
        self.assertEqual(aws.policies["permissions"], json.loads((self.out / adopt.OPERATOR_FILE).read_text()))
        self.assertIn("'state-backend'", output)

        code, output = self.run_tool("rollback", aws=aws)
        self.assertEqual(code, 0, output)
        self.assertEqual(aws.policies["permissions"], OLD_POLICY)

    def test_apply_refuses_a_second_run_over_an_existing_rollback(self):
        self.run_tool("render")
        aws = FakeAWS()
        self.apply(aws)
        code, output = self.apply(aws)
        self.assertEqual(code, 1)
        self.assertIn("already exists", output)

    def test_apply_stops_on_a_differing_existing_boundary(self):
        self.run_tool("render")
        aws = FakeAWS(boundary={"Version": "2012-10-17", "Statement": []})
        code, output = self.apply(aws)
        self.assertEqual(code, 1)
        self.assertIn("different document", output)
        self.assertEqual(aws.writes(), [])
        self.assertFalse((self.out / adopt.ROLLBACK_FILE).exists())

    def test_apply_stops_on_attached_policies_or_a_missing_inline_policy(self):
        self.run_tool("render")
        for aws, message in ((FakeAWS(attached=["arn:aws:iam::aws:policy/AdministratorAccess"]), "attached managed"),
                             (FakeAWS(inline=["other"]), "not found")):
            with self.subTest(message=message):
                code, output = self.apply(aws)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                self.assertEqual(aws.writes(), [])

    def test_confirmation_must_repeat_the_operator_role(self):
        self.run_tool("render")
        aws = FakeAWS()
        code, _ = self.run_tool("apply", "--policy-name", "permissions", "--confirm-operator-role", "other", aws=aws)
        self.assertEqual(code, 1)
        self.assertEqual(aws.calls, [])

    def test_inventory_reports_unbounded_roles_and_step_six_fails_on_them(self):
        aws = FakeAWS(roles={"family-host": {}, "family-recovery": {"boundary": BOUNDARY_ARN}, "minecraft-operator": {}})
        code, output = self.run_tool("inventory", aws=aws)
        self.assertEqual(code, 0, output)
        self.assertIn("without the host boundary: family-host", output)
        self.assertNotIn("minecraft-operator:", output)
        code, output = self.run_tool("inventory", "--expect-all-bounded", aws=aws)
        self.assertEqual(code, 1)
        self.assertIn("still lack", output)

    def test_inventory_stops_on_a_foreign_boundary(self):
        aws = FakeAWS(roles={"family-host": {"boundary": f"arn:aws:iam::{ACCOUNT}:policy/other"}})
        code, output = self.run_tool("inventory", aws=aws)
        self.assertEqual(code, 1)
        self.assertIn("stop and review", output)


if __name__ == "__main__":
    unittest.main()
