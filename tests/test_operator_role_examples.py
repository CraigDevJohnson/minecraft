"""Guard the reviewed operator-role and host-boundary examples against privilege regressions."""

from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs/operations/operator-role"
ACCOUNT = "123456789012"
REGION = "us-west-2"
ROLE_PREFIX = "example-prefix-"
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/example-host-boundary"
BOUNDARY_SENSITIVE_ACTIONS = [
    "iam:CreateRole",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PutRolePermissionsBoundary",
]
BOUNDARY_POLICY_CHANGES = [
    "iam:CreatePolicyVersion",
    "iam:DeletePolicy",
    "iam:DeletePolicyVersion",
    "iam:SetDefaultPolicyVersion",
]
EC2_SECURITY_SETTINGS = [
    "ec2:DisableEbsEncryptionByDefault",
    "ec2:ModifyEbsDefaultKmsKeyId",
    "ec2:ResetEbsDefaultKmsKeyId",
    "ec2:DisableSnapshotBlockPublicAccess",
    "ec2:DisableImageBlockPublicAccess",
    "ec2:EnableSerialConsoleAccess",
    "ec2:ModifySnapshotAttribute",
    "ec2:ModifyImageAttribute",
    "ec2:ModifyInstanceMetadataDefaults",
    "ec2:ModifyVpcBlockPublicAccessOptions",
    "ec2:DisableAllowedImagesSettings",
    "ec2:DeleteFlowLogs",
    "ec2:CreateNetworkInterfacePermission",
    "ec2:EnableAllowedImagesSettings",
    "ec2:ReplaceImageCriteriaInAllowedImagesSettings",
    "ec2:CreateVpcBlockPublicAccessExclusion",
    "ec2:ModifyVpcBlockPublicAccessExclusion",
    "ec2:DeleteVpcBlockPublicAccessExclusion",
]
HOST_POLICY_SOURCES = [ROOT / "infra/iam.tf", ROOT / "infra/recovery-rehearsal/iam.tf"]


def statements(name):
    return json.loads((EXAMPLES / f"{name}.json.example").read_text())["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def matches(patterns, value):
    return any(fnmatchcase(value.lower(), pattern.lower()) for pattern in as_list(patterns))


def covers(statement, action, resource="*"):
    return matches(statement["Action"], action) and (resource == "*" or matches(statement["Resource"], resource))


def unconditionally_denied(policy, action, resource):
    return any(s["Effect"] == "Deny" and "Condition" not in s and covers(s, action, resource) for s in policy)


def host_policy_actions(path):
    """Read Action lists from formatter-normalized aws_iam_role_policy blocks, not arbitrary HCL."""
    actions = set()
    blocks = re.findall(r'^resource "aws_iam_role_policy" "[^"]+" \{\n(.*?)^\}', path.read_text(), re.M | re.S)
    for body in blocks:
        if "NotAction" in body:
            raise AssertionError(f"{path.name} uses NotAction, which this guard cannot bound")
        for value in re.findall(r'\bAction\s*=\s*(\[[^\]]*\]|"[^"]*")', body):
            actions.update(re.findall(r'"([^"]+)"', value))
    return blocks, actions


class OperatorRoleExampleTests(unittest.TestCase):
    def setUp(self):
        self.permissions = statements("permissions")
        self.boundary = statements("host-boundary")

    def test_examples_are_explicit_allow_or_deny_documents(self):
        for name, policy in [("permissions", self.permissions), ("host-boundary", self.boundary)]:
            for statement in policy:
                with self.subTest(policy=name, sid=statement.get("Sid")):
                    self.assertIn(statement["Effect"], ("Allow", "Deny"))
                    self.assertIn("Action", statement)
                    self.assertIn("Resource", statement)
                    self.assertNotIn("NotAction", statement)
                    self.assertNotIn("NotResource", statement)

    def test_s3_only_retention_supplement_is_named_instance_scoped_and_never_version_deletes(self):
        policy = statements("s3-only-retention")
        bucket = "arn:aws:s3:::example-prefix-backups"
        for statement in policy:
            for resource in as_list(statement["Resource"]):
                self.assertTrue(resource == bucket or resource.startswith(bucket + "/instances/survival/"))
            for action in as_list(statement["Action"]):
                self.assertTrue(action.startswith("s3:"))
                self.assertNotIn("*", action)
        self.assertTrue(unconditionally_denied(policy, "s3:DeleteObjectVersion", bucket + "/instances/survival/objects/point/data.tar.gz"))
        self.assertTrue(unconditionally_denied(policy, "s3:DeleteObject", bucket + "/instances/survival/receipts/current.json"))
        listing = next(statement for statement in policy if statement["Sid"] == "ListNamedRecoveryVersions")
        self.assertEqual(set(listing["Action"]), {"s3:ListBucket", "s3:ListBucketVersions"})
        self.assertEqual(listing["Condition"], {"StringLike": {"s3:prefix": ["instances/survival/*"]}})
        locating = next(statement for statement in policy if statement["Sid"] == "LocateTheRecoveryBucket")
        self.assertEqual(locating["Action"], "s3:GetBucketLocation")
        self.assertNotIn("Condition", locating)
        lifecycle = next(statement for statement in policy if statement["Sid"] == "ReadTheRecoveryBucketLifecycle")
        self.assertEqual(lifecycle["Effect"], "Allow")
        self.assertEqual(lifecycle["Action"], "s3:GetLifecycleConfiguration")
        self.assertEqual(lifecycle["Resource"], bucket)
        self.assertNotIn("Condition", lifecycle)

    def test_s3_only_tag_and_receipt_publication_require_their_actual_conditions(self):
        policy = statements("s3-only-retention")
        tagging = next(statement for statement in policy if statement["Sid"] == "SetOnlyTheRequiredRetentionTag")
        self.assertEqual(tagging["Condition"], {
            "StringEquals": {"s3:RequestObjectTag/minecraft-retention": ["keep", "retired"]},
            "ForAllValues:StringEquals": {"s3:RequestObjectTagKeys": ["minecraft-retention"]},
            "Null": {"s3:RequestObjectTagKeys": "false", "s3:RequestObjectTag/minecraft-retention": "false"},
        })
        immutable = next(statement for statement in policy if statement["Sid"] == "PublishImmutableReceipt")
        prefix = "arn:aws:s3:::example-prefix-backups/instances/survival/receipts/"
        self.assertTrue(matches(immutable["Resource"], prefix + "a" * 64 + ".json"))
        self.assertFalse(matches(immutable["Resource"], prefix + "current.json"))
        self.assertEqual(immutable["Condition"], {"StringEquals": {"s3:if-none-match": "*"}})
        advance = next(statement for statement in policy if statement["Sid"] == "AdvanceTheObservedSelectionConditionally")
        self.assertEqual(advance["Resource"], prefix + "current.json")
        self.assertEqual(advance["Condition"], {"Null": {"s3:if-match": "false"}})

    def test_role_permission_changes_require_the_host_boundary(self):
        granted = set()
        for statement in self.permissions:
            if statement["Effect"] != "Allow":
                continue
            for action in BOUNDARY_SENSITIVE_ACTIONS:
                if matches(statement["Action"], action):
                    granted.add(action)
                    with self.subTest(sid=statement.get("Sid"), action=action):
                        condition = statement.get("Condition", {}).get("StringEquals", {})
                        self.assertEqual(condition.get("iam:PermissionsBoundary"), BOUNDARY_ARN)
        self.assertEqual(granted, set(BOUNDARY_SENSITIVE_ACTIONS))

    def test_boundaries_can_never_be_removed_or_rewritten(self):
        for role in [f"arn:aws:iam::{ACCOUNT}:role/{ROLE_PREFIX}host", f"arn:aws:iam::{ACCOUNT}:role/unrelated"]:
            self.assertTrue(unconditionally_denied(self.permissions, "iam:DeleteRolePermissionsBoundary", role))
        for action in BOUNDARY_POLICY_CHANGES:
            with self.subTest(action=action):
                self.assertTrue(unconditionally_denied(self.permissions, action, BOUNDARY_ARN))

    def test_boundary_policy_is_named_outside_the_role_prefix(self):
        name = BOUNDARY_ARN.rsplit("/", 1)[1]
        self.assertFalse(name.lower().startswith(ROLE_PREFIX))
        role_scopes = {
            resource
            for statement in self.permissions
            if statement["Effect"] == "Allow" and matches(statement["Action"], "iam:CreateRole")
            for resource in as_list(statement["Resource"])
        }
        self.assertEqual(role_scopes, {f"arn:aws:iam::{ACCOUNT}:role/{ROLE_PREFIX}*"})
        for statement in self.permissions:
            if statement["Effect"] == "Allow" and any(action.lower().startswith("iam:") for action in as_list(statement["Action"])):
                with self.subTest(sid=statement.get("Sid")):
                    self.assertFalse(matches(statement["Resource"], BOUNDARY_ARN))

    def test_boundary_allows_every_action_the_host_and_recovery_policies_grant(self):
        for path in HOST_POLICY_SOURCES:
            blocks, actions = host_policy_actions(path)
            self.assertGreaterEqual(len(blocks), 2, path)
            self.assertTrue(
                {"ssm:UpdateInstanceInformation", "ssmmessages:OpenDataChannel", "s3:GetObject", "s3:ListBucket"} <= actions,
                path,
            )
            for action in sorted(actions):
                with self.subTest(source=path.name, action=action):
                    self.assertRegex(action, r"^(ssm|ssmmessages|s3):[A-Za-z]+$")
                    self.assertTrue(any(s["Effect"] == "Allow" and covers(s, action) for s in self.boundary))
                    self.assertFalse(any(s["Effect"] == "Deny" and covers(s, action) for s in self.boundary))

    def test_boundary_bucket_access_stays_in_the_deployment_account(self):
        bucket_statements = [s for s in self.boundary if any(a.startswith("s3:") for a in as_list(s["Action"]))]
        self.assertGreaterEqual(len(bucket_statements), 2)
        for statement in bucket_statements:
            with self.subTest(sid=statement.get("Sid")):
                self.assertEqual(statement.get("Condition"), {"StringEquals": {"s3:ResourceAccount": ACCOUNT}})

    def test_ec2_account_security_settings_and_sharing_are_denied(self):
        for action in EC2_SECURITY_SETTINGS:
            with self.subTest(action=action):
                self.assertTrue(
                    any(
                        s["Effect"] == "Deny" and "Condition" not in s and s["Resource"] == "*" and matches(s["Action"], action)
                        for s in self.permissions
                    )
                )

    def test_wildcard_resource_grants_are_limited_to_the_deployment_region(self):
        wildcard = [s for s in self.permissions if s["Effect"] == "Allow" and "*" in as_list(s["Resource"])]
        self.assertTrue(any(covers(s, "cloudtrail:LookupEvents") for s in wildcard))
        for statement in wildcard:
            with self.subTest(sid=statement.get("Sid")):
                self.assertEqual(statement.get("Condition", {}).get("StringEquals", {}).get("aws:RequestedRegion"), REGION)

    def test_sessions_can_be_resumed_or_ended_only_by_their_starting_role_session(self):
        for action in ["ssm:TerminateSession", "ssm:ResumeSession"]:
            grants = [s for s in self.permissions if s["Effect"] == "Allow" and matches(s["Action"], action)]
            self.assertTrue(grants, action)
            for statement in grants:
                with self.subTest(action=action, sid=statement.get("Sid")):
                    self.assertEqual(
                        statement.get("Condition"),
                        {"StringLike": {"ssm:resourceTag/aws:ssmmessages:session-id": ["${aws:userid}*"]}},
                    )


if __name__ == "__main__":
    unittest.main()
