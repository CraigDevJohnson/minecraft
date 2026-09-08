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
            (fixture / ".tools/bin").mkdir(parents=True)
            (fixture / ".tools/bin/tofu").symlink_to(ROOT / ".tools/bin/tofu")
            (fixture / "infra").mkdir()
            (fixture / "infra/main.tf").write_text("terraform {}\n")
            inherited_config = fixture / "inherited.tfrc"
            inherited_config.write_text("provider_installation {\n")
            temporary_data = fixture / "temporary-data"
            temporary_data.mkdir()
            environment = os.environ.copy()
            environment.update(
                TF_CLI_CONFIG_FILE=str(inherited_config),
                TERRAFORM_CONFIG=str(inherited_config),
                TF_LOG="DEBUG",
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
            self.assertIn("Success! The configuration is valid.", output)
            self.assertEqual(
                output.count(
                    "Not reading CLI config directory because config location "
                    "is overridden by environment variable"
                ),
                2,
                output,
            )
            self.assertEqual(list(temporary_data.iterdir()), [])
            self.assertEqual(
                inherited_config.read_text(), "provider_installation {\n"
            )


if __name__ == "__main__":
    unittest.main()
