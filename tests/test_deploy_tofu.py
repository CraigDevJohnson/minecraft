import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PLAN_OUTPUT = """data.aws_caller_identity.current: Reading...
data.aws_caller_identity.current: Read complete after 0s [id=111122223333]
aws_s3_bucket.backups: Refreshing state... [id=private-bucket-name]
aws_iam_role.launcher: Refreshing state... [id=minecraft-family-launcher]

OpenTofu will perform the following actions:

  # aws_iam_role.launcher will be updated in-place
  ~ resource "aws_iam_role" "launcher" {
        name                  = "minecraft-family-launcher"
      ~ assume_role_policy    = jsonencode({"private": "input"})
    }

Plan: 0 to add, 1 to change, 0 to destroy.
"""

APPLY_OUTPUT = """aws_iam_role.launcher: Modifying... [id=minecraft-family-launcher]
aws_iam_role.launcher: Still modifying... [id=minecraft-family-launcher, 10s elapsed]
aws_iam_role.launcher: Modifications complete after 12s [id=minecraft-family-launcher]

Apply complete! Resources: 0 added, 1 changed, 0 destroyed.
"""

# What `tofu show -json` answers for the saved plan: private values that must never reach the log.
SHOW_OUTPUT = {"resource_changes": [
    {"address": "aws_iam_role.launcher", "change": {"actions": ["update"], "before": {"name": "minecraft-family-launcher"},
                                                    "after": {"name": "minecraft-family-launcher", "assume_role_policy": "private-policy"}}},
    {"address": "aws_s3_bucket.backups", "change": {"actions": ["no-op"], "before": {"bucket": "private-bucket-name"},
                                                    "after": {"bucket": "private-bucket-name"}}},
]}

FAKE_TOFU = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TOFU_LOG"
for argument in "$@"; do
  case "$argument" in
  init) exit 0 ;;
  plan) cat "$FAKE_PLAN_OUTPUT"; exit 0 ;;
  show) cat "$FAKE_SHOW_OUTPUT"; exit 0 ;;
  apply) cat "$FAKE_APPLY_OUTPUT"; exit 0 ;;
  esac
done
exit 3
"""


class DeployTofuSummaryTests(unittest.TestCase):
    """The deploy script's summary carries resource addresses and totals, never ids or attribute values."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="deploy-tofu-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "deploy_tofu.sh", self.root / "scripts" / "deploy_tofu.sh")
        shutil.copy(ROOT / "scripts" / "plan_digest.py", self.root / "scripts" / "plan_digest.py")
        (self.root / "infra").mkdir()
        tofu = self.root / ".tools" / "bin" / "tofu"
        tofu.parent.mkdir(parents=True)
        tofu.write_text(FAKE_TOFU)
        tofu.chmod(tofu.stat().st_mode | stat.S_IXUSR)
        self.deploy_dir = self.root / "deploy"
        (self.deploy_dir / "packages").mkdir(parents=True)
        for name in ["deployment.tfvars", "backend.hcl", "packages/launcher.zip", "packages/idle-stop.zip"]:
            (self.deploy_dir / name).write_text("private\n")
        (self.root / "plan.txt").write_text(PLAN_OUTPUT)
        (self.root / "apply.txt").write_text(APPLY_OUTPUT)
        (self.root / "show.json").write_text(json.dumps(SHOW_OUTPUT))
        self.digest = subprocess.run(["python3", str(ROOT / "scripts" / "plan_digest.py")], input=json.dumps(SHOW_OUTPUT),
                                     capture_output=True, text=True, check=True).stdout.strip()

    def run_script(self, mode, approved="", code=0):
        summary = self.root / "summary.md"
        summary.touch()
        (self.root / "tofu.log").touch()
        env = dict(os.environ, DEPLOY_DIR=str(self.deploy_dir), GITHUB_STEP_SUMMARY=str(summary),
                   FAKE_TOFU_LOG=str(self.root / "tofu.log"), FAKE_PLAN_OUTPUT=str(self.root / "plan.txt"),
                   FAKE_APPLY_OUTPUT=str(self.root / "apply.txt"), FAKE_SHOW_OUTPUT=str(self.root / "show.json"),
                   APPROVED_PLAN=approved)
        completed = subprocess.run(["bash", str(self.root / "scripts" / "deploy_tofu.sh"), mode],
                                   capture_output=True, text=True, env=env, check=False)
        self.assertEqual(completed.returncode, code, completed.stderr)
        return completed.stdout, summary.read_text(), (self.root / "tofu.log").read_text()

    def test_plan_summary_keeps_headers_and_totals_without_ids_or_values(self):
        stdout, summary, calls = self.run_script("plan")
        for shown in ["## OpenTofu plan", "  # aws_iam_role.launcher will be updated in-place",
                      "aws_s3_bucket.backups: Refreshing state...", "data.aws_caller_identity.current: Read complete after 0s",
                      "Plan: 0 to add, 1 to change, 0 to destroy."]:
            self.assertIn(shown, summary)
            self.assertIn(shown, stdout)
        for hidden in ["[id=", "private-bucket-name", "111122223333", "assume_role_policy", "private\": \"input", "private-policy"]:
            self.assertNotIn(hidden, summary)
            self.assertNotIn(hidden, stdout)
        self.assertNotIn("apply", calls.replace("-var-file", ""))
        # The plan's identity, which an apply run must be given.
        self.assertRegex(self.digest, r"^[0-9a-f]{16}$")
        self.assertIn(f"Plan digest: `{self.digest}`", summary)
        self.assertIn(f"mode `apply` and plan `{self.digest}`", summary)

    def test_apply_needs_the_digest_of_the_plan_craig_read(self):
        stdout, summary, calls = self.run_script("apply", code=1)
        self.assertEqual(calls, "", "nothing may run without an approved plan")
        stdout, summary, calls = self.run_script("apply", approved="0123456789abcdef", code=1)
        self.assertIn(" plan ", calls)
        self.assertNotIn(" apply ", calls)
        self.assertIn("Nothing was applied", summary)
        self.assertIn(f"mode `apply` and plan `{self.digest}`", summary)
        self.assertNotIn("## OpenTofu apply", summary)

    def test_apply_summary_strips_ids_from_progress_lines(self):
        stdout, summary, calls = self.run_script("apply", approved=self.digest)
        for shown in ["## OpenTofu plan", "## OpenTofu apply", "aws_iam_role.launcher: Modifying...",
                      "aws_iam_role.launcher: Still modifying...", "aws_iam_role.launcher: Modifications complete after 12s",
                      "Apply complete! Resources: 0 added, 1 changed, 0 destroyed."]:
            self.assertIn(shown, summary)
        for hidden in ["[id=", "minecraft-family-launcher", "10s elapsed", "private-bucket-name"]:
            self.assertNotIn(hidden, summary)
            self.assertNotIn(hidden, stdout)
        self.assertIn(" apply ", calls)


if __name__ == "__main__":
    unittest.main()
