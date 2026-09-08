#!/usr/bin/env python3
"""Explicit real-Docker controller exercise with simulated local host boundaries."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import sys
from unittest import mock

import minecraft_content as content
import minecraft_instance as runtime
from validate_runtime import free_port
import socket

ROOT = Path(__file__).resolve().parents[1]

HOOK = '''import json
from pathlib import Path
import sys

request = json.load(sys.stdin)
directory = Path(sys.argv[1])
directory.mkdir(exist_ok=True)
assert request["schema_version"] == 1
assert Path(request["data_path"], "world/level.dat").is_file()
assert Path(request["manifest_path"]).is_file()
assert json.loads(Path(request["definition_path"]).read_text())["id"] == request["instance"]
assert request["destination"]["prefix"] == "instances/" + request["instance"] + "/"
(directory / (request["session"] + ".json")).write_text(json.dumps(request, sort_keys=True))
print(json.dumps({"schema_version": 1, "status": "complete", "instance": request["instance"],
                  "session": request["session"], "manifest_sha256": request["manifest_sha256"],
                  "recovery_point": "fixture-only/" + request["session"]}))
'''


class LocalMachine(runtime.LinuxMachine):
    """Only Linux host prerequisites are simulated; controller and subprocesses are real."""

    def __init__(self, config, root):
        super().__init__(config)
        self.root = root
        self.simulated_boot = "fixture-first-boot"
        self.storage_checks = 0

    @contextlib.contextmanager
    def lock(self):
        # Exercise the real flock implementation on a local file without requiring root.
        with runtime.operation_lock(self.root / "runtime.lock"):
            yield

    def verify_storage(self):
        # This is deliberately not evidence of EBS identity or Linux service readiness.
        if not self.root.is_dir() or self.root.is_symlink():
            raise runtime.OperationError("local fixture root is unavailable")
        self.storage_checks += 1

    def boot_id(self):
        return self.simulated_boot

    def verify_inventory(self):
        # Limit inventory to the configured test project, leaving other local apps alone.
        for instance in self.definitions:
            self.inspect(instance)

    def verify_backup_hook(self, hook):
        # Local macOS paths cannot establish Linux root-owned executable ancestry.
        expected = [str(Path(sys.executable).resolve()), str(self.root.parent / "fixture-backup.py"),
                    str(self.root.parent / "fixture-backup-requests")]
        if hook != expected or runtime.private_text(hook[1]) != HOOK:
            raise runtime.OperationError("only the exact local fixture backup hook is permitted")


def validate(operator_config: Path, output: Path) -> dict:
    private = runtime.private_json(operator_config)
    if private.get("eula_accepted") is not True:
        raise ValueError("explicit operator EULA acceptance is required")
    platform = runtime.command(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]).strip()
    if platform not in {"linux/arm64", "linux/aarch64"}:
        raise ValueError("lifecycle validation requires a real Linux ARM64 Docker engine")
    if output.exists():
        raise ValueError("lifecycle evidence directory must be new")
    output.mkdir(parents=True, mode=0o700)
    local_root = output / "retained-fixture"
    local_root.mkdir()
    instance = "lifecycle-" + secrets.token_hex(5)
    definition = content.load_definition(ROOT / "runtime/survival.json")
    definition.update(id=instance, java_port=free_port(socket.SOCK_STREAM), bedrock_port=free_port(socket.SOCK_DGRAM))
    definition_path = output / "definition.json"
    runtime.write_json(definition_path, definition)
    manifest = content.load_manifest(ROOT / "runtime/content.json")
    runtime.write_json(output / definition["manifest"], manifest)
    deployment_path = output / "deployment.json"
    runtime.write_json(deployment_path, {
        "data_volume_id": "vol-00000000000000000",
        "data_filesystem_uuid": "00000000-0000-4000-8000-000000000005",
        "backup_bucket_name": "fixture-only-no-s3-calls",
        "instances": {instance: {"data_path": f"/srv/minecraft/instances/{instance}/data",
                                 "backup_prefix": f"instances/{instance}/"}},
    })
    secret = output / "rcon-password.txt"
    secret.write_text(secrets.token_urlsafe(32) + "\n")
    config = {"deployment": str(deployment_path), "definitions": {instance: str(definition_path)},
              "rcon_password_file": str(secret), "eula_accepted": True,
              "readiness_timeout": 600, "shutdown_timeout": 120, "backup_timeout": 60, "warning_seconds": 1}
    machine = LocalMachine(config, local_root)
    manager = runtime.Manager(local_root, machine, [instance])
    render = content.render_compose
    report = {"status": "incomplete", "platform": platform, "instance": instance,
              "manifest_sha256": content.manifest_digest(manifest),
              "real_boundaries": ["Manager state transitions and persisted operation records", "LinuxMachine prepare, Docker Compose, readiness and RCON", "backup subprocess request and receipt validation", "bounded file flock"],
              "simulated_boundaries": ["Linux root privilege and retained EBS/service checks", "host chown (Docker still assigns container data ownership)", "boot identity", "inventory limited to the configured test project", "root-owned backup executable ancestry (only the exact local fixture hook is permitted)", "fixture backup receipt, without S3 or a recovery copy"],
              "network_override": "The real renderer receives bind_address=127.0.0.1 for local game ports."}

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    def expect_error(action, expected):
        try:
            manager.execute(action, instance)
        except runtime.OperationError as error:
            require(expected in str(error), "operation failed for an unexpected reason")
        else:
            raise RuntimeError("required lifecycle rejection did not happen")

    # Keep these test-only substitutions out of the production CLI and driver.
    with mock.patch.object(runtime.os, "chown", return_value=None), mock.patch.object(
            content, "render_compose", side_effect=lambda *args, **kwargs: render(*args, **kwargs, bind_address="127.0.0.1")):
        try:
            prepare = machine.prepare
            def interrupted_preparation(configured):
                prepare(configured)
                raise runtime.OperationError("fixture interruption after preparation")
            with mock.patch.object(machine, "prepare", side_effect=interrupted_preparation):
                expect_error("start", "fixture interruption after preparation")
            require((local_root / "instances" / instance / "data").is_dir(), "preparation fixture did not create real data")
            require(manager.state(instance)["phase"] == "starting" and not machine.inspect(instance)["exists"],
                    "preparation interruption lost intent or executed a container")
            manager = runtime.Manager(local_root, machine, [instance])
            require(manager.execute("status", instance)["backup"] == "not-required", "unexecuted preparation could not reconcile")
            require(manager.execute("start", instance)["ready"], "initial start did not become ready")
            first_state = manager.state(instance)
            first_container = machine.inspect(instance)["container_id"]
            require(first_state["phase"] == "running" and first_state["backup"] == "pending", "running session was not persisted")
            machine.rcon(instance, "execute in minecraft:overworld run forceload add 0 0")
            response = machine.rcon(instance, "execute in minecraft:overworld run setblock 0 100 0 minecraft:diamond_block")
            require("Changed the block" in response, "fixture block was not created")
            key_path = local_root / "instances" / instance / "data/plugins/floodgate/key.pem"
            key = key_path.read_bytes()
            manager.execute("start", instance)
            require(manager.state(instance)["session"] == first_state["session"] and machine.inspect(instance)["container_id"] == first_container, "duplicate start replaced an active session")
            expect_error("stop", "required backup hook is absent")
            failed = manager.state(instance)
            stopped = machine.inspect(instance)
            require(failed["phase"] == "stopped" and failed["backup"] == "failed", "missing-hook failure was not persisted")
            require(not stopped["running"] and stopped["exit_code"] == 0, "missing hook did not follow a real clean stop")
            expect_error("start", "previous session needs a successful backup")
            hook_path = output / "fixture-backup.py"
            hook_path.write_text(HOOK)
            receipts = output / "fixture-backup-requests"
            machine.config["backup_hook"] = [str(Path(sys.executable).resolve()), str(hook_path), str(receipts)]
            require(manager.execute("stop", instance)["backup"] == "complete", "backup retry did not complete")
            require(manager.state(instance)["session"] == first_state["session"], "backup retry changed session identity")
            manager.execute("stop", instance)
            require(len(list(receipts.glob("*.json"))) == 1, "duplicate stop reran a completed backup")
            require(manager.execute("host-drain")["draining"], "host drain barrier was not persisted")
            expect_error("start", "host is draining")
            machine.simulated_boot = "fixture-second-boot"
            prior_execution = machine.inspect(instance)
            # A real Compose CLI rejection proves the old stopped container never ran.
            with mock.patch.object(machine, "launch", side_effect=lambda configured: machine.compose(
                    configured, "up", "--fixture-invalid-before-launch")):
                expect_error("start", "command failed")
            require(manager.execute("status", instance)["backup"] == "complete", "prelaunch failure lost the completed backup")
            require(manager.state(instance)["session"] == first_state["session"], "prelaunch failure replaced the completed session")
            unchanged = machine.inspect(instance)
            require(not unchanged["running"] and unchanged["container_id"] == prior_execution["container_id"]
                    and unchanged["started_at"] == prior_execution["started_at"], "prelaunch failure unexpectedly started a container")
            require(manager.execute("start", instance)["ready"], "new boot did not permit the next session")
            require(manager.state(instance)["session"] != first_state["session"], "new session reused the earlier identity")
            response = machine.rcon(instance, "execute in minecraft:overworld if block 0 100 0 minecraft:diamond_block")
            require("Test passed" in response and key_path.read_bytes() == key, "world or Floodgate state changed after the controller restart")
            drained = manager.execute("host-drain")
            require(drained["draining"] and drained["instances"][instance] == {"instance": instance, "application": "stopped", "backup": "complete"}, "running host drain did not stop and back up its instance")
            require(len(list(receipts.glob("*.json"))) == 2, "both distinct sessions did not receive completion evidence")
            report.update(status="passed", healthy_starts=2, clean_stops=2, backup_receipts=2,
                          interrupted_first_preparation_retry_succeeded=True,
                          duplicate_start_preserved_session=True, missing_hook_persisted_failure=True,
                          backup_retry_preserved_session=True, duplicate_stop_reused_receipt=True,
                          same_boot_start_blocked=True, next_boot_start_allowed=True,
                          real_prelaunch_failure_preserved_backup=True, prelaunch_failure_retry_succeeded=True,
                          running_host_drain_completed=True, world_and_floodgate_key_preserved=True,
                          simulated_storage_checks=machine.storage_checks)
            return report
        except (runtime.OperationError, RuntimeError) as error:
            report["failure"] = str(error)
            raise
        finally:
            state = machine.inspect(instance)
            if state["exists"]:
                if state["running"]:
                    try:
                        machine.save(instance)
                        machine.shutdown(instance)
                    except runtime.OperationError:
                        report["cleanup"] = "retained running container after clean-stop failure"
                state = machine.inspect(instance)
                log = runtime.command(["docker", "logs", state["container_id"]])
                (output / "container.log").write_text(log)
                if not state["running"]:
                    machine.compose(instance, "down", timeout=120)
            runtime.write_json(output / "report.json", report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(validate(args.operator_config.resolve(), args.output_dir.resolve()), indent=2, sort_keys=True))
        return 0
    except (runtime.OperationError, RuntimeError, ValueError, OSError) as error:
        detail = str(error) if isinstance(error, (runtime.OperationError, RuntimeError, ValueError)) else type(error).__name__
        print(f"lifecycle validation failed: {detail}; inspect private evidence in {args.output_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
