import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "docs" / "operations" / "health-role"
SUB = "repo:CraigDevJohnson@42454849/minecraft@1359070973:ref:refs/heads/main"


class HealthRoleTests(unittest.TestCase):
    def test_trust_rejects_other_contexts_and_identities(self):
        policy = json.loads((POLICIES / "trust.json").read_text())
        self.assertEqual(policy["Version"], "2012-10-17")
        self.assertEqual(len(policy["Statement"]), 1)
        statement = policy["Statement"][0]
        self.assertEqual(statement["Effect"], "Allow")
        self.assertEqual(statement["Action"], "sts:AssumeRoleWithWebIdentity")
        self.assertEqual(statement["Principal"], {"Federated": "arn:aws:iam::793680745829:oidc-provider/token.actions.githubusercontent.com"})
        self.assertEqual(statement["Condition"], {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": SUB,
        }})
        subjects = [
            SUB.replace("refs/heads/main", "refs/heads/feature"),
            SUB.replace(":ref:refs/heads/main", ":pull_request"),
            SUB.replace(":ref:refs/heads/main", ":environment:production"),
            SUB.replace("@1359070973", "@1359070974"),
            SUB.replace("@42454849", "@42454850"),
            "repo:CraigDevJohnson/minecraft:ref:refs/heads/main",
        ]
        exact = statement["Condition"]["StringEquals"]
        for subject in subjects:
            with self.subTest(subject=subject):
                self.assertNotEqual(subject, exact["token.actions.githubusercontent.com:sub"])
        self.assertNotIn("*", json.dumps(statement))
        self.assertNotIn("?", json.dumps(statement))

    def test_permission_inventory_is_only_two_region_scoped_ec2_reads(self):
        policy = json.loads((POLICIES / "permissions.json").read_text())
        self.assertEqual(policy["Version"], "2012-10-17")
        self.assertEqual(len(policy["Statement"]), 1)
        statement = policy["Statement"][0]
        self.assertEqual(statement["Effect"], "Allow")
        self.assertEqual(set(statement["Action"]), {"ec2:DescribeInstances", "ec2:DescribeInstanceStatus"})
        self.assertEqual(statement["Resource"], "*")
        self.assertEqual(statement["Condition"], {"StringEquals": {"aws:RequestedRegion": "us-west-2"}})

    def test_workflow_has_no_untrusted_checkout_or_pr_entry_point(self):
        text = (ROOT / ".github/workflows/minecraft-health.yml").read_text()
        for denied in ["schedule:", "cron:", "push:", "pull_request:", "pull_request_target:", "workflow_call:", "inputs:", "aws-access-key-id:", "aws-secret-access-key:"]:
            self.assertNotIn(denied, text)
        for required in ["workflow_dispatch:", "github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'", "github.repository_id == '1359070973'", "github.repository_owner_id == '42454849'", "persist-credentials: false", "role-duration-seconds: 900", "allowed-account-ids: '793680745829'", "timeout-minutes: 5"]:
            self.assertIn(required, text)
        self.assertEqual(text.count("id-token: write"), 1)
        self.assertEqual(text.count("secrets."), 1)
        self.assertIn("secrets.MINECRAFT_HEALTH_ROLE_ID", text)
        self.assertIn('^AROA[A-Z0-9]{17}$', text)
        self.assertIn("::add-mask::%s", text)
        self.assertLess(text.index("Mask AWS role metadata"), text.index("Obtain temporary read-only AWS credentials"))


if __name__ == "__main__":
    unittest.main()
