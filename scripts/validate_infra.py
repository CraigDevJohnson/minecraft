"""Validate checked source in disposable directories, with no operator environment."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="minecraft-tofu-") as directory:
        workspace = Path(directory)
        for folder, patterns in {
            "infra": ("*.tf", "*.tftest.hcl", ".terraform.lock.hcl", "*.placeholder", "tests/fixtures/packages/*.zip"),
            "scripts": ("*.py", "*.sh", "*.tftpl"),
        }.items():
            for pattern in patterns:
                for source in (ROOT / folder).rglob(pattern):
                    relative = source.relative_to(ROOT)
                    if any(part.startswith(".") for part in relative.parts[:-1]):
                        continue
                    if source.name == "override.tf" or source.name.endswith("_override.tf"):
                        continue
                    destination = workspace / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
        (workspace / "validation.tfrc").touch()
        (workspace / "home").mkdir()
        (workspace / "provider-cache").mkdir()
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(workspace / "home"),
            "TF_CLI_CONFIG_FILE": str(workspace / "validation.tfrc"),
            "TF_PLUGIN_CACHE_DIR": str(workspace / "provider-cache"),
            "AWS_CONFIG_FILE": str(workspace / "no-aws-config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(workspace / "no-aws-credentials"),
            "AWS_EC2_METADATA_DISABLED": "true",
            "TF_IN_AUTOMATION": "true",
        }
        roots = [
            workspace / "infra",
            workspace / "infra/state-bootstrap",
            workspace / "infra/recovery-rehearsal",
        ]
        for module in roots:
            if not module.is_dir():
                continue
            for arguments in (
                ["init", "-backend=false", "-input=false", "-lockfile=readonly", "-no-color"],
                ["validate", "-no-color"],
                ["test", "-no-color"],
            ):
                subprocess.run(
                    [str(ROOT / ".tools/bin/tofu"), f"-chdir={module}", *arguments],
                    env=environment,
                    check=True,
                    timeout=300,
                )


if __name__ == "__main__":
    main()
