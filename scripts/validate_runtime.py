#!/usr/bin/env python3
"""Explicit, isolated ARM64 runtime evidence; never run by ordinary CI."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import io
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
# Expectations for the pinned content in runtime/content.json. Paper 26.2 speaks Java protocol
# 776; ViaVersion 5.12.0 lets Java 26.3 (protocol 777) clients join. Geyser 2.11.3 advertises its
# newest Bedrock codec (protocol 2193, labelled 26.51) in discovery.
CROSSPLAY_PLUGINS = ("Geyser-Spigot", "floodgate", "ViaVersion")
JAVA_PROTOCOLS = {"26.2": 776, "26.3": 777}
BEDROCK_DISCOVERY = {"protocol": 2193, "version": "26.51"}
MAX_STATUS_BYTES = 256 * 1024
# Official eclipse-temurin:25-jdk-noble Linux ARM64 manifest, resolved 2026-10-03.
# Only this disposable compiler uses a JDK; the server stays on its existing pinned JRE.
NATIVE_PROBE_COMPILER = "eclipse-temurin@sha256:12f85590d237a882284d98f76886e6395a50764e91038d13a8a87b4f03b8b64b"


def run(command: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def free_signaling_port(webrtc_port: int = 19133, java_port: int | None = None) -> int:
    """One loopback host port carries both RakNet UDP and HTTP signaling TCP."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            if port in (webrtc_port, java_port):
                continue
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("cannot allocate one free TCP/UDP signaling port")


def require_local_webrtc_port(port: int = 19133) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        raise RuntimeError(f"local UDP {port} is occupied; preserve the other runtime and retry validation later") from error


def check_nethernet_metadata(value: dict) -> dict:
    expected = {"protocol": 2193, "version": "26.51", "maxPlayers": 6,
                "transportLayer": 2, "connection": 4, "onlineAuth": True, "selfSignedAuth": False}
    if not isinstance(value, dict) or any(type(value.get(key)) is not type(item) or value[key] != item
                                         for key, item in expected.items()):
        raise RuntimeError("NetherNet signaling metadata differs from the pinned authenticated transport")
    if type(value.get("players")) is not int or not 0 <= value["players"] <= value["maxPlayers"]:
        raise RuntimeError("NetherNet signaling player count is invalid")
    return {**expected, "players": value["players"]}


def nethernet_signaling(port: int) -> dict:
    """Discovery and admission denial only; an unauthenticated request creates no ICE peer."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/v1/join")
        reply = connection.getresponse()
        body = reply.read(65537)
        if (reply.status != 200 or reply.getheader("Content-Type", "").split(";")[0] != "application/json"
                or len(body) > 65536):
            raise RuntimeError("NetherNet signaling did not return bounded discovery JSON")
        try:
            metadata = check_nethernet_metadata(json.loads(body))
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("NetherNet signaling returned malformed JSON") from error
        connection.request("POST", "/v1/join/native-probe", body=b"v=0\r\n",
                           headers={"Content-Type": "application/sdp"})
        reply = connection.getresponse()
        if reply.status != 401 or reply.read(65537):
            raise RuntimeError("NetherNet signaling did not reject unauthenticated admission")
        return {"discovery": metadata, "unauthenticated_admission": "denied",
                "authenticated_geyser_peer": "unverified", "real_bedrock_join": "unverified"}
    except (OSError, http.client.HTTPException) as error:
        raise RuntimeError("NetherNet signaling request failed") from error
    finally:
        connection.close()


def native_transport_probe(manifest: dict, artifacts: Path, output: Path) -> dict:
    """Separate native peer pair; no production auth override or Minecraft assertion."""
    classes = output / "native-probe-classes"
    classes.mkdir(mode=0o700)
    jar = artifacts / "plugins/Geyser-Spigot.jar"
    mounts = ["--mount", f"type=bind,source={jar},target=/probe/geyser.jar,readonly",
              "--mount", f"type=bind,source={classes},target=/probe/classes"]
    isolated = ["docker", "run", "--rm", "--platform", "linux/arm64", "--network", "none",
                "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=64m"]
    run([*isolated, *mounts, "--mount", f"type=bind,source={ROOT / 'scripts/NetherNetNativeProbe.java'},target=/probe/NetherNetNativeProbe.java,readonly",
         "--entrypoint", "javac", NATIVE_PROBE_COMPILER, "--release", "17", "-cp", "/probe/geyser.jar",
         "-d", "/probe/classes", "/probe/NetherNetNativeProbe.java"], timeout=300)
    port = content.webrtc_port(manifest)
    result = json.loads(run([*isolated, *mounts, "--entrypoint", "java", manifest["image"],
        "--enable-native-access=ALL-UNNAMED", "-cp", "/probe/classes:/probe/geyser.jar",
        "NetherNetNativeProbe", str(port)], timeout=45))
    expected = {"status": "passed", "scope": "isolated_bundled_native_transport", "udp_port": port,
                "reliable_echo": True, "unreliable_echo": True,
                "geyser_admission_verified": False, "bedrock_join_verified": False}
    if result != expected or any(type(result.get(key)) is not type(value) for key, value in expected.items()):
        raise RuntimeError("bundled native UDP/data-channel probe failed or returned invalid evidence")
    return result


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


def check_bedrock_discovery(advertisement: str) -> dict:
    """Require the pinned Geyser's Bedrock protocol/version in its discovery answer."""
    fields = advertisement.split(";")
    try:
        if fields[0] != "MCPE":
            raise ValueError("not a Bedrock edition advertisement")
        advertised = {"protocol": int(fields[2]), "version": fields[3],
                      "online": int(fields[4]), "max_players": int(fields[5])}
    except (IndexError, ValueError) as error:
        raise RuntimeError("Bedrock discovery response is malformed") from error
    if {key: advertised[key] for key in BEDROCK_DISCOVERY} != BEDROCK_DISCOVERY:
        raise RuntimeError(f"Bedrock discovery advertised protocol {advertised['protocol']} version "
                           f"{advertised['version']}; expected {BEDROCK_DISCOVERY}")
    return advertised


def check_plugins(listing: str) -> None:
    if not all(name in listing for name in CROSSPLAY_PLUGINS):
        raise RuntimeError("crossplay plugins were not loaded")


def varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def handshake(protocol: int, port: int) -> bytes:
    """A Java Edition handshake from a loopback client asking for status (next state 1)."""
    host = b"127.0.0.1"
    body = varint(0) + varint(protocol) + varint(len(host)) + host + struct.pack(">H", port) + varint(1)
    return varint(len(body)) + body


def read_varint(stream) -> int:
    value = 0
    for shift in range(0, 35, 7):
        byte = stream.read(1)
        if not byte:
            raise RuntimeError("Java status response ended early")
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value
    raise RuntimeError("Java status response has an invalid length")


def java_status(port: int, protocol: int) -> dict:
    """Ask the loopback Java listener for its status as a client speaking ``protocol``."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client, client.makefile("rb") as stream:
            client.sendall(handshake(protocol, port) + varint(1) + varint(0))
            length = read_varint(stream)
            body = stream.read(length) if 0 < length <= MAX_STATUS_BYTES else b""
    except OSError as error:
        raise RuntimeError(f"Java status ping with protocol {protocol} failed") from error
    if not body or len(body) != length:
        raise RuntimeError("Java status response ended early or exceeded its size limit")
    packet = io.BytesIO(body)
    if read_varint(packet) != 0:
        raise RuntimeError("Java listener did not answer with a status response")
    text = packet.read(read_varint(packet))
    try:
        version = json.loads(text.decode("utf-8"))["version"]
        status = {"name": version["name"], "protocol": version["protocol"]}
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Java status response is malformed") from error
    if not isinstance(status["name"], str) or type(status["protocol"]) is not int:
        raise RuntimeError("Java status response is malformed")
    return status


def java_statuses(port: int) -> dict:
    """Paper must answer 26.2 natively and ViaVersion must accept 26.3 clients."""
    statuses = {}
    for release, protocol in JAVA_PROTOCOLS.items():
        statuses[release] = java_status(port, protocol)
        if statuses[release]["protocol"] != protocol:
            raise RuntimeError(f"Java {release} status handshake (protocol {protocol}) was answered "
                               f"with protocol {statuses[release]['protocol']}")
    return statuses


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
    native_port = content.webrtc_port(manifest)
    definition["java_port"] = free_port(socket.SOCK_STREAM)
    definition["bedrock_port"] = free_signaling_port(native_port, definition["java_port"])
    # The pinned native API advertises same-port NAT only.
    require_local_webrtc_port(native_port)
    artifacts = content.stage_content(manifest, output / "artifacts")
    compose = content.render_compose(definition, manifest, data_dir=data, artifacts_dir=artifacts,
        rcon_password=secrets.token_urlsafe(32), eula_accepted=True, bind_address="127.0.0.1",
        nethernet_advertise_address="127.0.0.1")
    compose_file = output / "compose.json"
    compose_file.write_text(json.dumps(compose, indent=2, sort_keys=True) + "\n")
    compose_file.chmod(0o600)
    project = "minecraft-verify-" + secrets.token_hex(5)
    base = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    report = {"platform": platform, "manifest_sha256": content.manifest_digest(manifest),
              "image": manifest["image"], "project": project, "webrtc_port": native_port, "status": "incomplete"}
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
        report["nethernet_native_transport"] = native_transport_probe(manifest, artifacts, output)
        start()
        first_container = container
        report["server_version"] = rcon("version")
        if "Checking version" in report["server_version"]:
            time.sleep(2)
            report["server_version"] = rcon("version")
        report["plugins"] = rcon("plugins")
        report["geyser_version"] = rcon("geyser version")
        report["viaversion_version"] = rcon("version ViaVersion")
        report["bedrock_discovery"] = bedrock_ping(definition["bedrock_port"])
        check_plugins(report["plugins"])
        report["bedrock_advertised"] = check_bedrock_discovery(report["bedrock_discovery"])
        report["java_status"] = java_statuses(definition["java_port"])
        report["nethernet_signaling"] = nethernet_signaling(definition["bedrock_port"])
        report["nethernet_acceptance"] = "unverified"
        properties = (data / "server.properties").read_text()
        for setting in ("online-mode=true", "white-list=true", "enforce-whitelist=true", "rcon.ip=127.0.0.1"):
            if setting not in properties.splitlines():
                raise RuntimeError("runtime access configuration failed verification")
        inspect = json.loads(run(["docker", "inspect", container]))[0]
        if "JVM_OPTS=-DgeyserAdvertiseAddresses=127.0.0.1" not in inspect["Config"]["Env"]:
            raise RuntimeError("the local NetherNet NAT advertisement override was not applied")
        bindings = inspect["HostConfig"]["PortBindings"]
        expected_bindings = {
            "25565/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(definition["java_port"])}],
            "19132/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(definition["bedrock_port"])}],
            "19132/udp": [{"HostIp": "127.0.0.1", "HostPort": str(definition["bedrock_port"])}],
            f"{native_port}/udp": [{"HostIp": "127.0.0.1", "HostPort": str(native_port)}],
        }
        if bindings != expected_bindings:
            raise RuntimeError("runtime smoke ports did not preserve the isolated dual-transport mapping")
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
        identity = data / "plugins/Geyser-Spigot/nethernet/identity.pem"
        if not identity.is_file():
            raise RuntimeError("NetherNet did not retain its first-use trust identity")
        fixtures["plugins/Geyser-Spigot/nethernet/identity.pem"] = identity.read_bytes()
        # Geyser rewrites config.yml only when its config-version is not the pinned build's.
        report["geyser_config_sha256"] = hashlib.sha256(fixtures["plugins/Geyser-Spigot/config.yml"]).hexdigest()
        if report["geyser_config_sha256"] != manifest["managed_files"][0]["sha256"]:
            raise RuntimeError("Geyser rewrote the managed configuration; check its config-version")
        if not (data / "world/level.dat").is_file():
            raise RuntimeError("missing world metadata")
        for dimension in DIMENSIONS:
            if not (data / "world/dimensions/minecraft" / dimension / "region/r.0.0.mca").is_file():
                raise RuntimeError("missing dimension fixture region")
        start()
        report["nethernet_signaling_after_recreation"] = nethernet_signaling(definition["bedrock_port"])
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
                                   "Signaling discovery/denial and separate bundled native transport smoke do not prove authenticated Geyser WebRTC admission or external UDP reachability.",
                                   "Java 26.3 support is a ViaVersion status handshake, not a 26.3 client join.",
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
