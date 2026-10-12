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

FAKE_TOFU = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TOFU_LOG"
for argument in "$@"; do
  case "$argument" in
  init) exit 0 ;;
  plan) cat "$FAKE_PLAN_OUTPUT"; exit 0 ;;
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

    def run_script(self, mode):
        summary = self.root / "summary.md"
        env = dict(os.environ, DEPLOY_DIR=str(self.deploy_dir), GITHUB_STEP_SUMMARY=str(summary),
                   FAKE_TOFU_LOG=str(self.root / "tofu.log"), FAKE_PLAN_OUTPUT=str(self.root / "plan.txt"),
                   FAKE_APPLY_OUTPUT=str(self.root / "apply.txt"))
        completed = subprocess.run(["bash", str(self.root / "scripts" / "deploy_tofu.sh"), mode],
                                   capture_output=True, text=True, env=env, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout, summary.read_text(), (self.root / "tofu.log").read_text()

    def test_plan_summary_keeps_headers_and_totals_without_ids_or_values(self):
        stdout, summary, calls = self.run_script("plan")
        for shown in ["## OpenTofu plan", "  # aws_iam_role.launcher will be updated in-place",
                      "aws_s3_bucket.backups: Refreshing state...", "data.aws_caller_identity.current: Read complete after 0s",
                      "Plan: 0 to add, 1 to change, 0 to destroy."]:
            self.assertIn(shown, summary)
            self.assertIn(shown, stdout)
        for hidden in ["[id=", "private-bucket-name", "111122223333", "assume_role_policy", "private\": \"input"]:
            self.assertNotIn(hidden, summary)
            self.assertNotIn(hidden, stdout)
        self.assertNotIn("apply", calls.replace("-var-file", ""))

    def test_apply_summary_strips_ids_from_progress_lines(self):
        stdout, summary, calls = self.run_script("apply")
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
