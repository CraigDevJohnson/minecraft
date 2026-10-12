import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "docs" / "operations" / "server-control-role"
SUB = "repo:CraigDevJohnson@42454849/minecraft@1359070973:environment:server"


class ServerControlRoleTests(unittest.TestCase):
    def test_trust_accepts_only_the_server_environment_of_this_repository(self):
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
        for subject in [
            SUB.replace(":environment:server", ":environment:deploy"),
            SUB.replace(":environment:server", ":ref:refs/heads/main"),
            SUB.replace(":environment:server", ":pull_request"),
            SUB.replace("@1359070973", "@1359070974"),
            SUB.replace("@42454849", "@42454850"),
            "repo:CraigDevJohnson/minecraft:environment:server",
        ]:
            with self.subTest(subject=subject):
                self.assertNotEqual(subject, statement["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"])
        self.assertNotIn("*", json.dumps(statement))
        self.assertNotIn("?", json.dumps(statement))

    def test_permissions_only_call_the_launcher_functions(self):
        policy = json.loads((POLICIES / "permissions.json").read_text())
        self.assertEqual(policy["Version"], "2012-10-17")
        self.assertEqual(len(policy["Statement"]), 1)
        statement = policy["Statement"][0]
        self.assertEqual(statement["Effect"], "Allow")
        self.assertEqual(set(statement["Action"]), {"lambda:InvokeFunctionUrl", "lambda:InvokeFunction"})
        self.assertEqual(statement["Resource"], "arn:aws:lambda:us-west-2:793680745829:function:minecraft-family-*-launcher")
        self.assertNotIn("Condition", statement)

    def test_operator_trust_example_admits_only_the_deploy_environment_from_github(self):
        policy = json.loads((ROOT / "docs/operations/operator-role/trust.json.example").read_text())
        github = [statement for statement in policy["Statement"] if "Federated" in statement.get("Principal", {})]
        self.assertEqual(len(github), 1)
        statement = github[0]
        self.assertEqual(statement["Action"], "sts:AssumeRoleWithWebIdentity")
        self.assertEqual(statement["Condition"], {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:CraigDevJohnson@42454849/minecraft@1359070973:environment:deploy",
        }})
        self.assertNotIn("*", json.dumps(statement))

    def test_workflows_run_only_from_main_of_this_repository_with_pinned_identities(self):
        for name, role_variable in [("server-control", "AWS_SERVER_CONTROL_ROLE_ARN"), ("deploy", "AWS_OPERATOR_ROLE_ARN")]:
            with self.subTest(workflow=name):
                text = (ROOT / f".github/workflows/{name}.yml").read_text()
                for denied in ["pull_request:", "pull_request_target:", "workflow_call:", "schedule:", "aws-access-key-id:", "aws-secret-access-key:"]:
                    self.assertNotIn(denied, text)
                for required in ["workflow_dispatch:", "github.ref == 'refs/heads/main'", "github.repository_id == '1359070973'", "github.repository_owner_id == '42454849'", "github.actor_id == '42454849'", "github.triggering_actor == 'CraigDevJohnson'", "persist-credentials: false", "allowed-account-ids: '793680745829'", "audience: sts.amazonaws.com", f"vars.{role_variable}", "::add-mask::"]:
                    self.assertIn(required, text)
                self.assertEqual(text.count("id-token: write"), 1)
        server = (ROOT / ".github/workflows/server-control.yml").read_text()
        self.assertIn("environment: server", server)
        self.assertNotIn("secrets.", server)
        # The log shows a step's environment; the player's name is masked from the event file before then.
        mask = server.index('.get("player")')
        self.assertLess(server.index("GITHUB_EVENT_PATH"), server.index("PLAYER: ${{ inputs.player }}"))
        self.assertLess(mask, server.index("PLAYER: ${{ inputs.player }}"))
        self.assertEqual(server.count("${{ inputs.player }}"), 1)
        deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("environment: deploy", deploy)
        self.assertEqual(sorted(re.findall(r"secrets\.([A-Z_]+)", deploy)), ["BACKEND_CONFIG", "DEPLOYMENT_TFVARS"])
        self.assertIn("secrets.DEPLOYMENT_TFVARS", deploy)
        self.assertIn("secrets.BACKEND_CONFIG", deploy)
        # Every Deploy run holds the operator's write credentials, so only a run Craig starts may have them:
        # no push or other event starts it, and a run he starts plans unless he picks apply (ADR 0006).
        self.assertRegex(deploy, r"\non:\n  workflow_dispatch:\n")
        self.assertNotRegex(deploy, r"(?m)^  (push|pull_request|pull_request_target|schedule|workflow_run|repository_dispatch|release|issue_comment):")
        self.assertIn("MODE: ${{ inputs.mode || 'plan' }}", deploy)
        self.assertNotIn("|| 'apply'", deploy)
        # An apply run applies only the plan whose digest Craig gives it, read from the plan run's summary; the
        # digest reaches the script through the environment, never the script's text.
        self.assertIn("APPROVED_PLAN: ${{ inputs.plan }}", deploy)
        self.assertEqual(deploy.count("inputs.plan"), 1)
        # OpenTofu's errors reach the log unfiltered, so the private inputs' values are masked before it runs.
        self.assertLess(deploy.index("scripts/mask_private_inputs.py"), deploy.index("bash scripts/deploy_tofu.sh"))

    def test_no_other_workflow_reaches_the_deploy_or_server_environment(self):
        # The roles trust a job by its environment, and the Craig-only checks live in these two workflows, so a
        # job anywhere else that named either environment would get a role without those checks. No other
        # workflow names an environment at all; giving one an environment is a deliberate change to this test.
        workflows = sorted([*(ROOT / ".github/workflows").glob("*.yml"), *(ROOT / ".github/workflows").glob("*.yaml")])
        self.assertIn(ROOT / ".github/workflows/deploy.yml", workflows)
        for workflow in workflows:
            with self.subTest(workflow=workflow.name):
                environments = re.findall(r"(?m)^\s*environment\s*:.*$", workflow.read_text())
                expected = {"deploy.yml": ["    environment: deploy"], "server-control.yml": ["    environment: server"]}
                self.assertEqual(environments, expected.get(workflow.name, []))


if __name__ == "__main__":
    unittest.main()
