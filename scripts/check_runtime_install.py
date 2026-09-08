#!/usr/bin/env python3
"""Read-only installation preflight; the caller holds the runtime operation lock."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from minecraft_instance import OperationError, command, private_json, verify_retained_paths

ROOT = Path("/srv/minecraft")
INSTANCE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")


def container_instances() -> set[str]:
    identifiers = command(["docker", "ps", "--all", "--quiet", "--filter",
                           "label=com.docker.compose.service=minecraft"]).split()
    if not identifiers:
        return set()
    containers = json.loads(command(["docker", "inspect", *identifiers]))
    if not isinstance(containers, list) or len(containers) != len(identifiers):
        raise OperationError("cannot establish the complete runtime container inventory")
    instances = set()
    for container in containers:
        project = container["Config"]["Labels"].get("com.docker.compose.project", "")
        instance = project.removeprefix("minecraft-")
        state = container["State"]
        if (not project.startswith("minecraft-") or not INSTANCE_ID.fullmatch(instance)
                or instance in instances or state.get("Running") is not False or state.get("ExitCode") != 0):
            raise OperationError("runtime inventory is active, ambiguous, or not cleanly stopped")
        instances.add(instance)
    return instances


def check_installation(deployment_path: Path) -> None:
    deployment = private_json(deployment_path)
    configured = deployment["instances"]
    if not isinstance(configured, dict) or not configured or any(not INSTANCE_ID.fullmatch(name) for name in configured):
        raise OperationError("deployment must identify the configured instances")
    for instance, definition in configured.items():
        if (definition.get("data_path") != f"/srv/minecraft/instances/{instance}/data"
                or definition.get("backup_prefix") != f"instances/{instance}/"):
            raise OperationError("deployment instance paths do not match the retained storage contract")
    command(["/usr/local/sbin/minecraft-storage", "verify", "--volume-id", deployment["data_volume_id"],
             "--filesystem-uuid", deployment["data_filesystem_uuid"]])
    verify_retained_paths(ROOT)
    containers = container_instances()
    directory = ROOT / "instances"
    existing = set()
    if directory.exists():
        for instance in directory.iterdir():
            if not instance.is_dir() or not INSTANCE_ID.fullmatch(instance.name):
                raise OperationError("retained instance inventory contains an unknown entry")
            existing.add(instance.name)
    for instance in sorted(set(configured) | existing | containers):
        instance_path = directory / instance
        journal = instance_path / "operation.json"
        if not journal.exists():
            if instance in containers or (instance_path.exists() and any(instance_path.iterdir())):
                raise OperationError("untracked retained runtime data prevents installation; investigate with the prior release")
            continue
        state = private_json(journal)
        if (state.get("phase") != "stopped" or state.get("backup") != "complete"
                or not isinstance(state.get("recovery_point"), str) or not state["recovery_point"].strip()):
            raise OperationError("retained sessions require a clean stop and completed backup before installation; retry with the prior release")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, default=Path("/etc/minecraft/deployment.json"))
    args = parser.parse_args(argv)
    try:
        check_installation(args.deployment)
    except (OperationError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, OperationError) else "invalid private deployment or retained runtime state"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
    print("Retained runtime state permits installation; no state was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
