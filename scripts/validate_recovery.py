#!/usr/bin/env python3
"""Explicit ARM64 Minecraft recovery proof with a local object-store double."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
import time
from unittest import mock

import minecraft_backup as backup
import minecraft_content as content
import minecraft_instance as runtime
import minecraft_restore as recovery
from validate_lifecycle import LocalMachine
from validate_runtime import DIMENSIONS, free_port, player_fixture

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOOK = ["/fixture-only/minecraft-backup", "hook"]


def write_rcon_secret(path, *, exclude=None):
    while True:
        value = secrets.token_urlsafe(32)
        if value != exclude:
            break
    path.write_text(value + "\n")
    path.chmod(0o600)
    return value


def require_rcon_password(path, expected, *, rejected=None):
    properties = path.read_text()
    if f"rcon.password={expected}" not in properties.splitlines():
        raise RuntimeError("effective RCON configuration did not apply the expected private credential")
    if rejected is not None and rejected in properties:
        raise RuntimeError("effective RCON configuration retained the source private credential")


class LocalStore:
    """Private disk objects simulate S3 only; no AWS credentials or calls exist."""

    def __init__(self, root):
        self.root = root
        root.mkdir(mode=0o700)
        self.fail_upload = False

    def download(self, key, target, *, max_bytes):
        source = self.root / key
        if not source.exists():
            return False
        if source.stat().st_size > max_bytes:
            raise backup.BackupError("local object exceeds its permitted size")
        shutil.copyfile(source, target)
        return True

    def upload(self, key, source):
        if self.fail_upload:
            raise backup.BackupError("injected local upload interruption")
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)

    def keys(self, prefix):
        return sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                      if path.is_file() and path.relative_to(self.root).as_posix().startswith(prefix))

    def list_objects(self, prefix):
        return [{"key": key, "last_modified": datetime.fromtimestamp(
                    (self.root / key).stat().st_mtime, timezone.utc).isoformat()}
                for key in self.keys(prefix)]

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)


class RecoveryMachine(LocalMachine):
    def __init__(self, config, root, store, workspace):
        super().__init__(config, root)
        self.store = store
        self.workspace = workspace

    def verify_backup_hook(self, hook):
        if hook != LOCAL_HOOK:
            raise runtime.OperationError("only the local recovery validation hook is permitted")

    def restore_ownership(self, data):
        # Docker's root initialization assigns runtime ownership on local bind data.
        # This is not proof of Linux root-owned restore installation on EBS.
        if not data.is_dir():
            raise runtime.OperationError("restored local data is missing")

    def backup(self, instance, state):
        actual_command = runtime.command

        def invoke(command, **kwargs):
            if command != LOCAL_HOOK:
                return actual_command(command, **kwargs)
            request = json.loads(kwargs["input_text"])
            try:
                with tempfile.TemporaryDirectory(prefix="publish-", dir=self.workspace) as directory:
                    result = backup.publish(request, self.store, Path(directory))
            except backup.BackupError as error:
                raise runtime.OperationError(str(error)) from error
            return json.dumps(result)

        # Exercise the production request and receipt boundary, replacing only
        # hook process dispatch with the real publisher and a local object store.
        with mock.patch.object(runtime, "command", side_effect=invoke):
            return super().backup(instance, state)


def validate(operator_config, output):
    if runtime.private_json(operator_config).get("eula_accepted") is not True:
        raise ValueError("explicit operator EULA acceptance is required")
    platform = runtime.command(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]).strip()
    if platform not in {"linux/arm64", "linux/aarch64"}:
        raise ValueError("recovery validation requires a real Linux ARM64 Docker engine")
    if output.exists():
        raise ValueError("recovery evidence directory must be new")
    output.mkdir(parents=True, mode=0o700)
    instance = "recovery-" + secrets.token_hex(5)
    definition = content.load_definition(ROOT / "runtime/survival.json")
    definition.update(id=instance, java_port=free_port(socket.SOCK_STREAM), bedrock_port=free_port(socket.SOCK_DGRAM))
    manifest = content.load_manifest(ROOT / "runtime/content.json")
    runtime.write_json(output / "definition.json", definition)
    runtime.write_json(output / definition["manifest"], manifest)
    source_secret = write_rcon_secret(output / "source-rcon.secret")
    target_secret = write_rcon_secret(output / "target-rcon.secret", exclude=source_secret)

    def machine(name, number, secret_file):
        directory = output / name
        directory.mkdir(mode=0o700)
        deployment = {
            "data_volume_id": f"vol-{number:017x}",
            "data_filesystem_uuid": f"00000000-0000-4000-8000-{number:012x}",
            "backup_bucket_name": f"fixture-only-{name}",
            "instances": {instance: {"data_path": f"/srv/minecraft/instances/{instance}/data",
                                     "backup_prefix": f"instances/{instance}/"}},
        }
        runtime.write_json(output / f"{name}-deployment.json", deployment)
        config = {"deployment": str(output / f"{name}-deployment.json"),
                  "definitions": {instance: str(output / "definition.json")},
                  "rcon_password_file": str(secret_file), "eula_accepted": True,
                  "backup_hook": LOCAL_HOOK, "readiness_timeout": 600,
                  "shutdown_timeout": 120, "backup_timeout": 900, "warning_seconds": 1}
        return RecoveryMachine(config, directory, LocalStore(output / f"{name}-objects"), output)

    source = machine("source", 6, output / "source-rcon.secret")
    target = machine("target", 7, output / "target-rcon.secret")
    manager = runtime.Manager(source.root, source, [instance])
    restored = runtime.Manager(target.root, target, [instance])
    report = {"status": "incomplete", "platform": platform, "instance": instance,
              "manifest_sha256": content.manifest_digest(manifest),
              "real_boundaries": ["ARM64 Paper, Geyser, Floodgate and ViaVersion startup", "lifecycle locking and journals",
                                  "RCON world checkpoints and confirmed clean stops", "cold archive and file checksums",
                                  "verified publication and restore algorithms", "prepared restored data through normal startup"],
              "simulated_boundaries": ["local filesystem objects instead of S3", "local bind storage instead of EBS",
                                       "Linux root, storage services and volume identity", "hook dispatch and root-owned installation",
                                       "host chown during preparation and restore"],
              "limitations": ["No real S3 recovery, deployed recovery-time claim, EC2 action, or authenticated player/device join.",
                              "Target uses a distinct local directory and simulated volume identity on the same Docker engine."]}

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    render = content.render_compose
    with mock.patch.object(runtime.os, "chown", return_value=None), mock.patch.object(
            content, "render_compose", side_effect=lambda *args, **kwargs: render(*args, **kwargs, bind_address="127.0.0.1")):
        try:
            require(manager.execute("start", instance)["ready"], "source startup did not become ready")
            data = source.root / "instances" / instance / "data"
            require_rcon_password(data / "server.properties", source_secret)
            for dimension in DIMENSIONS:
                source.rcon(instance, f"execute in minecraft:{dimension} run forceload add 0 0")
                response = source.rcon(instance, f"execute in minecraft:{dimension} run setblock 0 100 0 minecraft:diamond_block")
                require("Changed the block" in response, "world fixture could not be created")
            source.store.fail_upload = True
            try:
                manager.execute("stop", instance)
            except runtime.OperationError as error:
                require("injected local upload" in str(error), "stop failed before the planned upload interruption")
            else:
                raise RuntimeError("upload interruption did not fail stop")
            require(not source.inspect(instance)["running"] and manager.state(instance)["backup"] == "failed",
                    "failed upload did not leave a stopped pending session")
            require(not source.store.keys(f"instances/{instance}/completed/"), "failed upload published a recovery point")
            fixtures = {
                "world/players/data/00000000-0000-0000-0000-000000000006.dat": player_fixture(),
                "world/players/stats/00000000-0000-0000-0000-000000000006.json": b'{"stats":{"minecraft:custom":{"minecraft:jump":6}},"DataVersion":4903}\n',
                "world/players/advancements/00000000-0000-0000-0000-000000000006.json": b'{"DataVersion":4903}\n',
                "plugins/floodgate/recovery-checkpoint.txt": b"issue-6-floodgate-checkpoint\n",
                "plugins/Geyser-Spigot/recovery-checkpoint.txt": b"issue-6-geyser-checkpoint\n",
            }
            for name, value in fixtures.items():
                path = data / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            for name in ("plugins/floodgate/key.pem", "plugins/floodgate/config.yml", "plugins/Geyser-Spigot/config.yml"):
                fixtures[name] = (data / name).read_bytes()
            source_before = backup.tree_snapshot(data)
            source.store.fail_upload = False
            require(manager.execute("backup", instance)["backup"] == "complete", "stopped session backup retry failed")
            point = manager.state(instance)["recovery_point"]
            source.compose(instance, "down", timeout=120)
            objects_before = {key: backup.digest_file(source.store.root / key) for key in source.store.keys(f"instances/{instance}/")}
            start = time.monotonic()
            result = recovery.restore(target, source.deployment, instance, point, source.store)
            require(result["status"] == "restored", "restore did not publish a valid isolated target")
            require(restored.execute("start", instance)["ready"], "restored data did not start through the lifecycle controller")
            target_data = target.root / "instances" / instance / "data"
            require_rcon_password(target_data / "server.properties", target_secret, rejected=source_secret)
            for dimension in DIMENSIONS:
                response = target.rcon(instance, f"execute in minecraft:{dimension} if block 0 100 0 minecraft:diamond_block")
                require("Test passed" in response, "restored dimension checkpoint was not recognized")
            require(restored.execute("stop", instance)["backup"] == "complete", "restored instance did not cleanly stop and back up")
            for name, value in fixtures.items():
                require((target_data / name).read_bytes() == value, "restored player/plugin/Floodgate checkpoint changed")
            require(backup.tree_snapshot(data) == source_before, "restore altered the source instance data")
            require(all(backup.digest_file(source.store.root / key) == value for key, value in objects_before.items()),
                    "restore altered the selected backup objects")
            report.update(status="passed", recovery_point=point, dimensions=list(DIMENSIONS),
                          preserved_checkpoint_files=len(fixtures), healthy_starts=2, clean_stops=2,
                          local_restore_and_start_seconds=round(time.monotonic() - start, 3),
                          interrupted_upload_retry=True, rcon_secret_rotated=True,
                          source_data_unchanged=True, selected_backup_unchanged=True)
            return report
        finally:
            for name, local in (("target", target), ("source", source)):
                state = local.inspect(instance)
                if state["exists"]:
                    if state["running"]:
                        try:
                            local.save(instance)
                            local.shutdown(instance)
                        except runtime.OperationError:
                            report["cleanup"] = "retained running validation container after clean-stop failure"
                    state = local.inspect(instance)
                    (output / f"{name}-container.log").write_text(runtime.command(["docker", "logs", state["container_id"]]))
                    if not state["running"]:
                        local.compose(instance, "down", timeout=120)
            runtime.write_json(output / "report.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(validate(args.operator_config.resolve(), args.output_dir.resolve()), indent=2, sort_keys=True))
        return 0
    except (backup.BackupError, runtime.OperationError, RuntimeError, ValueError, OSError) as error:
        detail = str(error) if isinstance(error, (backup.BackupError, runtime.OperationError, RuntimeError, ValueError)) else type(error).__name__
        print(f"recovery validation failed: {detail}; inspect private evidence in {args.output_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
