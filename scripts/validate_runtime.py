#!/usr/bin/env python3
"""Explicit, isolated ARM64 runtime evidence; never run by ordinary CI."""

from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import time

import minecraft_content as content

ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS = ("overworld", "the_nether", "the_end")


def run(command: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def player_fixture() -> bytes:
    """A valid synthetic NBT player record, without a real player's identity."""
    def string(value):
        encoded = value.encode()
        return struct.pack(">H", len(encoded)) + encoded

    return gzip.compress(
        b"\x0a\x00\x00"  # unnamed root compound
        + b"\x03" + string("DataVersion") + struct.pack(">i", 4903)
        + b"\x08" + string("Fixture") + string("issue-5-retained-player-state")
        + b"\x05" + string("Health") + struct.pack(">f", 17.0)
        + b"\x09" + string("Inventory") + b"\x0a\x00\x00\x00\x00"
        + b"\x00", mtime=0,
    )


def bedrock_ping(port: int) -> str:
    magic = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
    packet = b"\x01" + struct.pack(">q", int(time.time() * 1000)) + magic + struct.pack(">q", 5)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(10)
        client.sendto(packet, ("127.0.0.1", port))
        response, _ = client.recvfrom(4096)
    if response[:1] != b"\x1c" or b"MCPE;" not in response:
        raise RuntimeError("Geyser did not answer the Bedrock discovery ping")
    return response[response.index(b"MCPE;"):].decode("utf-8")


def validate(operator_config: Path, output: Path) -> dict:
    if operator_config.stat().st_mode & 0o077:
        raise ValueError("operator config must be private (chmod 600)")
    private = json.loads(operator_config.read_text())
    if private.get("eula_accepted") is not True:
        raise ValueError("explicit EULA acceptance is required in private operator configuration")
    platform = run(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"])
    if platform not in {"linux/aarch64", "linux/arm64"}:
        raise ValueError("runtime validation requires an actual Linux ARM64 Docker engine")
    if output.exists():
        raise ValueError("runtime evidence directory must be new to preserve earlier evidence")
    output.mkdir(parents=True, mode=0o700)
    data = output / "data"
    data.mkdir(mode=0o755)
    manifest = content.load_manifest(ROOT / "runtime/content.json")
    definition = content.load_definition(ROOT / "runtime/survival.json")
    definition["java_port"] = free_port(socket.SOCK_STREAM)
    definition["bedrock_port"] = free_port(socket.SOCK_DGRAM)
    artifacts = content.stage_content(manifest, output / "artifacts")
    compose = content.render_compose(definition, manifest, data_dir=data, artifacts_dir=artifacts,
        rcon_password=secrets.token_urlsafe(32), eula_accepted=True, bind_address="127.0.0.1")
    compose_file = output / "compose.json"
    compose_file.write_text(json.dumps(compose, indent=2, sort_keys=True) + "\n")
    compose_file.chmod(0o600)
    project = "minecraft-verify-" + secrets.token_hex(5)
    base = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    report = {"platform": platform, "manifest_sha256": content.manifest_digest(manifest),
              "image": manifest["image"], "project": project, "status": "incomplete"}
    container = ""

    def rcon(command):
        return run([*base, "exec", "-T", "minecraft", "rcon-cli", command])

    def state():
        return json.loads(run(["docker", "inspect", "--format", "{{json .State}}", container]))

    def start():
        nonlocal container
        run([*base, "up", "-d", "--force-recreate"], timeout=600)
        container = run([*base, "ps", "-q", "minecraft"])
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            current = state()
            if current.get("Health", {}).get("Status") == "healthy":
                if "There are 0 of a max of 6 players online" not in rcon("list"):
                    raise RuntimeError("RCON did not confirm Minecraft readiness")
                return
            if not current["Running"]:
                raise RuntimeError("runtime exited before readiness")
            time.sleep(2)
        raise RuntimeError("Minecraft readiness timed out")

    def stop():
        response = rcon("save-all flush")
        if "Saved the game" not in response:
            raise RuntimeError("Minecraft did not acknowledge its save")
        rcon("stop")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = state()
            if not current["Running"]:
                if current["ExitCode"] != 0 or current["OOMKilled"]:
                    raise RuntimeError("Minecraft shutdown was not clean")
                return
            time.sleep(1)
        raise RuntimeError("Minecraft clean shutdown timed out; retained container for inspection")

    try:
        start()
        first_container = container
        report["server_version"] = rcon("version")
        if "Checking version" in report["server_version"]:
            time.sleep(2)
            report["server_version"] = rcon("version")
        report["plugins"] = rcon("plugins")
        report["geyser_version"] = rcon("geyser version")
        report["bedrock_discovery"] = bedrock_ping(definition["bedrock_port"])
        if not all(name in report["plugins"] for name in ("Geyser", "floodgate")):
            raise RuntimeError("crossplay plugins were not loaded")
        properties = (data / "server.properties").read_text()
        for setting in ("online-mode=true", "white-list=true", "enforce-whitelist=true", "rcon.ip=127.0.0.1"):
            if setting not in properties.splitlines():
                raise RuntimeError("runtime access configuration failed verification")
        inspect = json.loads(run(["docker", "inspect", container]))[0]
        bindings = inspect["HostConfig"]["PortBindings"]
        if "25575/tcp" in bindings or any(binding["HostIp"] != "127.0.0.1" for values in bindings.values() for binding in values):
            raise RuntimeError("runtime smoke ports were not isolated")
        sockets = run([*base, "exec", "-T", "minecraft", "cat", "/proc/net/tcp", "/proc/net/tcp6"])
        rcon_sockets = [line.split()[1] for line in sockets.splitlines() if len(line.split()) > 3 and line.split()[3] == "0A" and line.split()[1].endswith(":63E7")]
        if len(rcon_sockets) != 1 or rcon_sockets[0] not in {"0100007F:63E7", "0000000000000000FFFF00000100007F:63E7"}:
            raise RuntimeError("RCON listener is not exclusively bound to container loopback")
        for dimension in DIMENSIONS:
            rcon(f"execute in minecraft:{dimension} run forceload add 0 0")
            response = rcon(f"execute in minecraft:{dimension} run setblock 0 100 0 minecraft:diamond_block")
            if "Changed the block" not in response:
                raise RuntimeError("world fixture could not be created")
        stop()
        fixtures = {
            "world/players/data/00000000-0000-0000-0000-000000000005.dat": player_fixture(),
            "world/players/stats/00000000-0000-0000-0000-000000000005.json": b'{"stats":{"minecraft:custom":{"minecraft:jump":5}},"DataVersion":4903}\n',
            "world/players/advancements/00000000-0000-0000-0000-000000000005.json": b'{"DataVersion":4903}\n',
            "plugins/floodgate/fixture-state.txt": b"issue-5-floodgate-mutable-state\n",
            "plugins/Geyser-Spigot/fixture-state.txt": b"issue-5-geyser-mutable-state\n",
        }
        for name, value in fixtures.items():
            path = data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        floodgate_key = data / "plugins/floodgate/key.pem"
        if not floodgate_key.is_file():
            raise RuntimeError("Floodgate did not generate its persistent authentication key")
        fixtures["plugins/floodgate/key.pem"] = floodgate_key.read_bytes()
        fixtures["plugins/floodgate/config.yml"] = (data / "plugins/floodgate/config.yml").read_bytes()
        fixtures["plugins/Geyser-Spigot/config.yml"] = (data / "plugins/Geyser-Spigot/config.yml").read_bytes()
        if not (data / "world/level.dat").is_file():
            raise RuntimeError("missing world metadata")
        for dimension in DIMENSIONS:
            if not (data / "world/dimensions/minecraft" / dimension / "region/r.0.0.mca").is_file():
                raise RuntimeError("missing dimension fixture region")
        start()
        if container == first_container:
            raise RuntimeError("container was not recreated")
        for dimension in DIMENSIONS:
            response = rcon(f"execute in minecraft:{dimension} if block 0 100 0 minecraft:diamond_block")
            if "Test passed" not in response:
                raise RuntimeError(f"recognizable {dimension} fixture did not survive recreation: {response}")
        stop()
        for name, value in fixtures.items():
            if (data / name).read_bytes() != value:
                raise RuntimeError("player/plugin/Floodgate fixture changed across recreation")
        report.update(status="passed", clean_starts=2, clean_stops=2,
                      recreated_container=True, dimensions=list(DIMENSIONS),
                      preserved_fixture_files=len(fixtures),
                      limitations=["Synthetic player data; no authenticated player/device join.",
                                   "Local bind storage; EBS guard and backup/AWS orchestration use separate tests.",
                                   "No S3 backup, live recovery, or console compatibility claim."])
        return report
    except RuntimeError as error:
        report["failure"] = str(error)
        raise
    finally:
        if container:
            (output / "container.log").write_text(run(["docker", "logs", container], timeout=30))
            current = state()
            if current["Running"]:
                try:
                    stop()
                except (RuntimeError, subprocess.SubprocessError):
                    report["cleanup"] = "runtime retained after clean-stop failure; manual inspection required"
            if not state()["Running"]:
                run([*base, "down"], timeout=120)
        (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        report = validate(args.operator_config.resolve(), args.output_dir.resolve())
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print(f"runtime validation failed: {detail}; inspect private evidence in {args.output_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
