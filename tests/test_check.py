from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InfrastructureValidationTests(unittest.TestCase):
    def test_validation_ignores_inherited_cli_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts/check.sh", fixture / "scripts/check.sh")
            shutil.copy2(ROOT / "scripts/validate_infra.py", fixture / "scripts/validate_infra.py")
            (fixture / ".tools/bin").mkdir(parents=True)
            (fixture / ".tools/bin/tofu").symlink_to(ROOT / ".tools/bin/tofu")
            (fixture / "infra").mkdir()
            (fixture / "infra/main.tf").write_text("terraform {}\n")
            state_root = fixture / "infra/state-bootstrap"
            state_root.mkdir()
            (state_root / "main.tf").write_text("terraform {}\n")
            private_files = {
                fixture / "infra/private.auto.tfvars": "invalid private input {",
                fixture / "infra/override.tf": "invalid private override {",
                fixture / "infra/terraform.tfstate": "private state sentinel",
                fixture / "infra/.terraform/terraform.tfstate": "private backend sentinel",
                state_root / "private.auto.tfvars": "invalid private state input {",
            }
            for path, content in private_files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            inherited_config = fixture / "inherited.tfrc"
            inherited_config.write_text("provider_installation {\n")
            temporary_data = fixture / "temporary-data"
            temporary_data.mkdir()
            environment = os.environ.copy()
            environment.update(
                TF_CLI_CONFIG_FILE=str(inherited_config),
                TERRAFORM_CONFIG=str(inherited_config),
                TF_LOG="DEBUG",
                TF_CLI_ARGS_init="-not-a-valid-option",
                TMPDIR=str(temporary_data),
            )
            environment.pop("TF_LOG_PATH", None)

            result = subprocess.run(
                ["bash", str(fixture / "scripts/check.sh"), "infra:validate"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn(str(inherited_config), output)
            self.assertNotIn("problems with the CLI configuration", output)
            self.assertEqual(output.count("Success! The configuration is valid."), 2, output)
            for path, content in private_files.items():
                self.assertEqual(path.read_text(), content)
                self.assertNotIn(content, output)
            self.assertNotIn("TF_CLI_ARGS", output)
            self.assertNotIn("DEBUG", output)
            self.assertEqual(list(temporary_data.iterdir()), [])
            self.assertEqual(
                inherited_config.read_text(), "provider_installation {\n"
            )


if __name__ == "__main__":
    unittest.main()
