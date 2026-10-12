import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

TFVARS = '''account_id         = "111122223333"
environment        = "family"
region             = "us-west-2"
backup_bucket_name = "private-backup-bucket"
instance_ids       = ["survival"]
monthly_budget_usd = 25
budget_notification_email = "operator@example.invalid"
launcher = {
  sites = {
    dev = "arn:aws:iam::111122223333:role/site-dev-server"
  }
}
note = "a \\"quoted\\" value"
'''

BACKEND = '''bucket       = "private-state-bucket"
key          = "minecraft/infra.tfstate"
region       = "us-west-2"
encrypt      = true
use_lockfile = true
'''


class MaskPrivateInputsTests(unittest.TestCase):
    """Every quoted value of the private inputs is masked before OpenTofu can quote it in an error."""

    def test_masks_each_quoted_value_once(self):
        with tempfile.TemporaryDirectory() as directory:
            tfvars, backend = Path(directory) / "deployment.tfvars", Path(directory) / "backend.hcl"
            tfvars.write_text(TFVARS)
            backend.write_text(BACKEND)
            completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "mask_private_inputs.py"), str(tfvars), str(backend)],
                                       capture_output=True, text=True, check=True)
        masked = completed.stdout.splitlines()
        for value in ["111122223333", "family", "us-west-2", "private-backup-bucket", "survival", "operator@example.invalid",
                      "arn:aws:iam::111122223333:role/site-dev-server", "private-state-bucket", "minecraft/infra.tfstate",
                      'a "quoted" value']:
            self.assertIn(f"::add-mask::{value}", masked)
        self.assertTrue(all(line.startswith("::add-mask::") for line in masked))
        self.assertEqual(len(masked), len(set(masked)))
        # Short words would blank out the whole log; numbers and booleans are not quoted.
        for unmasked in ["dev", "25", "true"]:
            self.assertNotIn(f"::add-mask::{unmasked}", masked)

    def test_needs_a_file(self):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "mask_private_inputs.py")], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 2)


if __name__ == "__main__":
    unittest.main()
