#!/usr/bin/env python3
"""Pinned content and deterministic Compose generation; no implicit EULA acceptance."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request


class ContentError(ValueError):
    """A runtime definition, artifact, or private input is not safe to use."""


# Fixed identity of each pinned server/plugin JAR: name -> (staged path, official download host).
ARTIFACTS = {
    "paper": ("paper.jar", "fill-data.papermc.io"),
    "geyser": ("plugins/Geyser-Spigot.jar", "download.geysermc.org"),
    "floodgate": ("plugins/floodgate-spigot.jar", "download.geysermc.org"),
    "viaversion": ("plugins/ViaVersion.jar", "github.com"),
}
# GitHub release assets have no CI build number: the exact release tag (the version) names
# the asset, so the URL must be that tag's official asset and the artifact carries no build.
RELEASE_ASSETS = {"viaversion": "/ViaVersion/ViaVersion/releases/download/{version}/ViaVersion-{version}.jar"}


def _approved_geyser(config_version: int, *, dual_transport: bool = False) -> dict:
    configuration = {
        "config-version": config_version,
        "bedrock": {"address": "0.0.0.0", "port": 19132, "clone-remote-port": False},
        "java": {"auth-type": "floodgate"},
        "log-player-ip-addresses": False,
        "saved-user-logins": [],
    }
    if dual_transport:
        configuration["bedrock"].update({"transport": "both", "webrtc-port": 19133,
                                         "signaling": {"mode": "builtin"}})
    return configuration


# This release's content policy: the exact artifact set and its approved Geyser configuration.
CONTENT_POLICY = (frozenset(ARTIFACTS), _approved_geyser(8, dual_transport=True))
# Earlier reviewed policies that completed recovery points may still record. They are accepted
# only when validating recorded recovery metadata, so catalog, status and retention keep working
# after a content upgrade; this release never stages or launches them.
RECORDED_CONTENT_POLICIES = (
    CONTENT_POLICY,
    (frozenset(ARTIFACTS), _approved_geyser(8)),  # survival-2026-10-02-bedrock-2650-viaversion
    (frozenset({"paper", "geyser", "floodgate"}), _approved_geyser(7)),  # survival-2026-09-08-managed-config
)
# An earlier policy admits only the exact manifests that were reviewed under it.
EARLIER_REVIEWED_MANIFESTS = frozenset({
    "ebe2fd621e9328dd708aa77918dbd96f8039670cddc3364c4f45e582b931c29e",
    "72f0640583a5fec446ccd299cbbf35c5aff6815a358eb251cece7975bd148c82",
})


def load_definition(path: Path) -> dict:
    definition = json.loads(Path(path).read_text())
    _validate_definition(definition)
    return definition


def _validate_definition(definition: dict) -> None:
    if definition.get("schema_version") != 1 or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", definition.get("id", "")):
        raise ContentError("invalid instance definition or ID")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+\.json", definition.get("manifest", "")):
        raise ContentError("manifest must name a local JSON file")
    for field, maximum in (("java_port", 65535), ("bedrock_port", 65535), ("max_players", 100), ("view_distance", 32), ("simulation_distance", 32)):
        value = definition.get(field)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ContentError(f"invalid instance {field}")
    if not re.fullmatch(r"[1-9][0-9]*[MG]", definition.get("memory", "")):
        raise ContentError("memory must be an explicit positive number of M or G")


def load_manifest(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: dict, *, recorded: bool = False) -> None:
    """Check this release's content policy; ``recorded`` also admits an earlier reviewed policy."""
    if manifest.get("schema_version") != 1 or manifest.get("platform") != "linux/arm64":
        raise ContentError("manifest must use schema 1 and linux/arm64")
    if not re.fullmatch(r"itzg/minecraft-server@sha256:[a-f0-9]{64}", manifest.get("image", "")):
        raise ContentError("runtime image must be pinned by digest")
    for field in ("revision", "java_version", "minecraft_version"):
        if not isinstance(manifest.get(field), str) or not re.fullmatch(r"[a-zA-Z0-9.+_-]+", manifest[field]) or "latest" in manifest[field].lower():
            raise ContentError(f"manifest requires an exact {field}")
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ContentError("manifest must contain exactly Paper, Geyser, Floodgate and ViaVersion")
    found = set()
    for artifact in artifacts:
        name = artifact.get("name") if isinstance(artifact, dict) else None
        if name not in ARTIFACTS or name in found or artifact.get("path") != ARTIFACTS[name][0]:
            raise ContentError("unexpected or duplicate runtime artifact")
        found.add(name)
        if not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", "")):
            raise ContentError("artifact requires a SHA-256 checksum")
        version = artifact.get("version", "")
        release = RELEASE_ASSETS.get(name)
        exact_build = ("build" not in artifact if release
                       else type(artifact.get("build")) is int and artifact["build"] >= 1)
        if not exact_build or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version):
            raise ContentError("artifact requires an exact version and build")
        url = urllib.parse.urlsplit(artifact.get("url", ""))
        if (url.scheme != "https" or url.netloc != ARTIFACTS[name][1] or url.query or url.fragment
                or "latest" in url.path.lower() or (release and url.path != release.format(version=version))):
            raise ContentError("artifact requires an exact official HTTPS download URL")
    policies = RECORDED_CONTENT_POLICIES if recorded else (CONTENT_POLICY,)
    approved = [geyser for names, geyser in policies if names == found]
    if not approved:
        raise ContentError("manifest must contain exactly Paper, Geyser, Floodgate and ViaVersion")
    if artifacts[0]["name"] != "paper" or artifacts[0]["version"] != manifest["minecraft_version"]:
        raise ContentError("Paper and Minecraft versions must match")
    managed_files = manifest.get("managed_files")
    if not isinstance(managed_files, list) or len(managed_files) != 1:
        raise ContentError("manifest must include the managed Geyser configuration")
    managed = managed_files[0]
    if (not isinstance(managed, dict) or set(managed) != {"path", "format", "content", "sha256"}
            or managed["path"] != "plugins/Geyser-Spigot/config.yml" or managed["format"] != "canonical-json-v1"):
        raise ContentError("unexpected managed configuration file or encoding")
    if (not isinstance(managed["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", managed["sha256"])
            or hashlib.sha256(_managed_file_bytes(managed)).hexdigest() != managed["sha256"]):
        raise ContentError("managed configuration checksum mismatch")
    configuration = managed["content"]
    # Policy validation only: staged bytes always come from the manifest payload.
    # JSON equality keeps scalar types distinct (8.0/8 and false/0 differ).
    if (not isinstance(configuration, dict)
            or not any(_managed_file_bytes(managed) == _managed_file_bytes({"content": schema}) for schema in approved)):
        raise ContentError("managed configuration schema must preserve the approved Geyser "
                           "authentication, network and privacy settings")
    if ((found, configuration) != CONTENT_POLICY
            and manifest_digest(manifest) not in EARLIER_REVIEWED_MANIFESTS):
        raise ContentError("recorded manifest is not an exact earlier reviewed release")


def _managed_file_bytes(managed: dict) -> bytes:
    """canonical-json-v1: sorted keys, compact UTF-8 JSON, and one final LF."""
    return (json.dumps(managed["content"], sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stage_content(manifest: dict, cache_dir: Path) -> Path:
    """Download once, check every use, and never trust a partial or corrupt cache."""
    _validate_manifest(manifest)
    destination = Path(cache_dir).resolve() / manifest_digest(manifest)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o755)
    for artifact in manifest["artifacts"]:
        target = destination / artifact["path"]
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != destination.parent):
            raise ContentError("artifact cache cannot contain symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(0o755)
        if target.exists():
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ContentError("cached artifact checksum mismatch; remove the corrupt cache before retrying")
            continue
        request = urllib.request.Request(artifact["url"], headers={"User-Agent": "CraigMinecraft/1.0 (https://github.com/CraigDevJohnson/minecraft)"})
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                size = 0
                deadline = time.monotonic() + 300
                with urllib.request.urlopen(request, timeout=30) as response:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 256 * 1024 * 1024 or time.monotonic() > deadline:
                            raise ContentError("artifact download exceeded its size or time limit")
                        output.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != artifact["sha256"]:
                    raise ContentError("downloaded artifact checksum mismatch")
            temporary.chmod(0o644)
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    for managed in manifest["managed_files"]:
        target = destination / managed["path"]
        if target.is_symlink() or target.parent.is_symlink():
            raise ContentError("managed configuration cannot contain symlinks")
        target.parent.mkdir(exist_ok=True)
        target.parent.chmod(0o755)
        if target.exists():
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != managed["sha256"]:
                raise ContentError("cached managed configuration checksum mismatch; remove the corrupt cache before retrying")
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(_managed_file_bytes(managed))
            temporary.chmod(0o644)
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return destination


def render_compose(definition: dict, manifest: dict, *, data_dir: Path, artifacts_dir: Path,
                   rcon_password: str, eula_accepted: bool, bind_address: str = "0.0.0.0",
                   nethernet_advertise_address: str | None = None) -> dict:
    """Return Compose JSON; callers keep the generated file private (mode 0600)."""
    _validate_definition(definition)
    _validate_manifest(manifest)
    if eula_accepted is not True:
        raise ContentError("explicit private operator EULA acceptance is required")
    if not isinstance(rcon_password, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", rcon_password):
        raise ContentError("RCON password requires 16-128 letters, digits, underscores or hyphens")
    try:
        ipaddress.IPv4Address(bind_address)
    except ipaddress.AddressValueError as error:
        raise ContentError("game bind address must be an IPv4 address") from error
    if definition["java_port"] == definition["bedrock_port"] or definition["bedrock_port"] == 19133:
        raise ContentError("Java/signaling TCP and RakNet/WebRTC UDP ports must be distinct")
    if nethernet_advertise_address is not None:
        if not isinstance(nethernet_advertise_address, str):
            raise ContentError("NetherNet advertisement must be one IPv4 address")
        try:
            address = ipaddress.IPv4Address(nethernet_advertise_address)
        except ipaddress.AddressValueError as error:
            raise ContentError("NetherNet advertisement must be one IPv4 address") from error
        if address.is_multicast or (not address.is_global and not (
                bind_address == "127.0.0.1" and address == ipaddress.IPv4Address("127.0.0.1"))):
            raise ContentError("NetherNet advertisement must be a global IPv4 address outside local validation")
    data_dir, artifacts_dir = Path(data_dir), Path(artifacts_dir)
    if not data_dir.is_absolute() or not artifacts_dir.is_absolute():
        raise ContentError("runtime bind mounts require absolute paths")

    def bind(source: Path, target: str, readonly: bool) -> dict:
        return {"type": "bind", "source": str(source), "target": target,
                "read_only": readonly, "bind": {"create_host_path": False}}

    return {"services": {"minecraft": {
        "image": manifest["image"],
        "platform": "linux/arm64",
        "restart": "no",
        "stop_grace_period": "120s",
        "cpus": "2",
        "environment": {
            "EULA": "TRUE",
            "TYPE": "CUSTOM",
            "FAMILY": "SPIGOT",
            "VERSION": manifest["minecraft_version"],
            "CUSTOM_SERVER": "/runtime/paper.jar",
            "MEMORY": definition["memory"],
            "UID": "1000", "GID": "1000",
            "MODE": "survival",
            "DIFFICULTY": "normal",
            "MAX_PLAYERS": str(definition["max_players"]),
            "VIEW_DISTANCE": str(definition["view_distance"]),
            "SIMULATION_DISTANCE": str(definition["simulation_distance"]),
            "LEVEL": "world",
            "ONLINE_MODE": "TRUE",
            "ENABLE_WHITELIST": "TRUE",
            "ENFORCE_WHITELIST": "TRUE",
            "ENFORCE_SECURE_PROFILE": "FALSE",
            "ENABLE_RCON": "TRUE",
            "RCON_PASSWORD": rcon_password,
            "RCON_PORT": "25575",
            "CUSTOM_SERVER_PROPERTIES": "rcon.ip=127.0.0.1",
            "BROADCAST_RCON_TO_OPS": "FALSE",
            "ENABLE_QUERY": "FALSE",
            "ENABLE_AUTOPAUSE": "FALSE",
            "ENABLE_AUTOSTOP": "FALSE",
            "SYNC_SKIP_NEWER_IN_DESTINATION": "FALSE",
            "REPLACE_ENV_DURING_SYNC": "FALSE",
            "LOG_TIMESTAMP": "TRUE",
            **({"JVM_OPTS": f"-DgeyserAdvertiseAddresses={nethernet_advertise_address}"}
               if nethernet_advertise_address is not None else {}),
        },
        "ports": [f"{bind_address}:{definition['java_port']}:25565/tcp",
                  f"{bind_address}:{definition['bedrock_port']}:19132/udp",
                  f"{bind_address}:{definition['bedrock_port']}:19132/tcp",
                  f"{bind_address}:19133:19133/udp"],
        "volumes": [bind(data_dir, "/data", False), bind(artifacts_dir / "paper.jar", "/runtime/paper.jar", True), bind(artifacts_dir / "plugins", "/plugins", True)],
        "healthcheck": {"test": ["CMD", "mc-health"], "interval": "10s", "timeout": "5s", "start_period": "180s", "retries": 6},
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }}}
