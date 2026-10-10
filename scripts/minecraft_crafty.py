#!/usr/bin/env python3
"""Host adapter between the operator lifecycle commands and Crafty Controller.

Installed as /usr/local/sbin/minecraft-instance, so the workstation's minecraft_host.py and the
idle-stop Lambda keep sending the same `start`, `host-drain` and `host-status` requests. Crafty
remains the only process manager: this adapter only asks Crafty's local API to start or stop the
server, then makes a cold archive of the stopped server directory and stores it in S3.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from minecraft_content import _managed_file_bytes  # noqa: E402  (canonical Geyser config bytes)

INSTANCE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CONTAINER_SERVERS = "/crafty/servers/"
ARTIFACT_HOSTS = {"fill-data.papermc.io", "download.geysermc.org", "github.com"}
STATE_DIR = "state"
KEEP_ARCHIVES = 14
STORAGE_FIELDS = ("size_bytes", "available_bytes", "used_percent")


class CraftyError(Exception):
    """A request that must not be reported as success."""


def load_config(path: Path) -> dict:
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CraftyError("cannot read the Crafty operator configuration") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1 or config.get("runtime") != "crafty":
        raise CraftyError("operator configuration is not a schema 1 Crafty configuration")
    url = urllib.parse.urlsplit(config.get("crafty_url", ""))
    # The API is only ever reached on loopback, where Crafty's self-signed certificate is expected.
    if url.scheme != "https" or url.hostname != "127.0.0.1" or url.path not in ("", "/") or url.query:
        raise CraftyError("crafty_url must be the loopback HTTPS address")
    for key in ("token_file", "data_root"):
        if not isinstance(config.get(key), str) or not config[key].startswith("/"):
            raise CraftyError(f"{key} must be an absolute path")
    instances = config.get("instances")
    if (not isinstance(instances, dict) or not instances
            or any(not INSTANCE_ID.fullmatch(name) or not isinstance(value, dict)
                   or not isinstance(value.get("server_name"), str) or not value["server_name"]
                   or type(value.get("java_port")) is not int for name, value in instances.items())):
        raise CraftyError("instances must map instance IDs to a Crafty server name and Java port")
    backup = config.get("backup")
    if (not isinstance(backup, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", str(backup.get("bucket", "")))
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", str(backup.get("region", "")))):
        raise CraftyError("backup requires an S3 bucket and region")
    timeouts = config.get("timeouts", {})
    if not isinstance(timeouts, dict) or any(type(value) is not int or not 10 <= value <= 3600 for value in timeouts.values()):
        raise CraftyError("timeouts must be whole seconds between 10 and 3600")
    return config


class CraftyAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        data = b"" if method == "POST" else None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, method=method, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                body = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise CraftyError(f"Crafty API {method} request failed") from exc
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise CraftyError(f"Crafty API {method} request was refused")
        return body


class Host:
    """Everything that touches the machine, so tests can replace it."""

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def port_open(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                return True
        except OSError:
            return False

    def boot_id(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def storage(self, root: str) -> dict:
        stats = os.statvfs(root)
        size, free, available = stats.f_blocks * stats.f_frsize, stats.f_bfree * stats.f_frsize, stats.f_bavail * stats.f_frsize
        used = size - free
        percent = 0 if used + available == 0 else -(-used * 100 // (used + available))
        return {"size_bytes": size, "available_bytes": available, "used_percent": min(100, percent)}

    def aws(self, region: str, *arguments: str) -> dict:
        command = ["aws", "--region", region, "--output", "json", "--no-cli-pager", *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CraftyError(f"AWS {arguments[1]} could not run") from exc
        if result.returncode:
            raise CraftyError(f"AWS {arguments[1]} failed")
        return json.loads(result.stdout) if result.stdout.strip() else {}


class Adapter:
    def __init__(self, config: dict, api, host=None):
        self.config = config
        self.api = api
        self.host = host or Host()
        self.root = Path(config["data_root"])
        self.timeouts = {"start": 300, "stop": 300, **config.get("timeouts", {})}

    # Crafty lookups -------------------------------------------------------------------------

    def server(self, instance: str) -> dict:
        name = self.config["instances"][instance]["server_name"]
        servers = self.api.request("GET", "/api/v2/servers").get("data")
        matches = [server for server in servers or [] if isinstance(server, dict) and server.get("server_name") == name]
        if len(matches) != 1 or not UUID.fullmatch(str(matches[0].get("server_id", ""))):
            raise CraftyError(f"Crafty must have exactly one server named {name}")
        return matches[0]

    def server_dir(self, server: dict) -> Path:
        path = server.get("path")
        if not isinstance(path, str) or not path.startswith(CONTAINER_SERVERS) or ".." in Path(path).parts:
            raise CraftyError("Crafty reported an unexpected server path")
        return self.root / "crafty" / "servers" / path[len(CONTAINER_SERVERS):]

    def running(self, server: dict) -> bool:
        data = self.api.request("GET", f"/api/v2/servers/{server['server_id']}/stats").get("data")
        if not isinstance(data, dict) or not isinstance(data.get("running"), bool):
            raise CraftyError("Crafty returned invalid server stats")
        return data["running"]

    def action(self, server: dict, action: str) -> None:
        self.api.request("POST", f"/api/v2/servers/{server['server_id']}/action/{action}")

    def import_server(self, instance: str, archive_name: str, max_memory_gb: int) -> dict:
        """Create the Crafty server from an archive in Crafty's import/upload folder (Crafty 4.11 imports only archives)."""
        settings = self.config["instances"][instance]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.zip", archive_name):
            raise CraftyError("archive name must be a plain .zip file name in Crafty's import/upload folder")
        if type(max_memory_gb) is not int or not 1 <= max_memory_gb <= 64:
            raise CraftyError("maximum memory must be a whole number of GB from 1 through 64")
        servers = self.api.request("GET", "/api/v2/servers").get("data")
        if any(isinstance(server, dict) and server.get("server_name") == settings["server_name"] for server in servers or []):
            raise CraftyError(f"Crafty already has a server named {settings['server_name']}")
        created = self.api.request("POST", "/api/v2/servers", {
            "name": settings["server_name"], "roles": [], "monitoring_type": "minecraft_java",
            "minecraft_java_monitoring_data": {"host": "127.0.0.1", "port": settings["java_port"]},
            "create_type": "minecraft_java",
            "minecraft_java_create_data": {"create_type": "import_server", "import_server_create_data": {
                "archive_name": archive_name, "archive_internal_path": "", "jarfile": "paper.jar",
                "mem_min": 1, "mem_max": max_memory_gb, "server_properties_port": settings["java_port"]}},
        }).get("data")
        server_id = created.get("new_server_id") if isinstance(created, dict) else None
        if not UUID.fullmatch(str(server_id)):
            raise CraftyError("Crafty did not report the new server")
        return {"instance": instance, "server_id": server_id}

    # Recorded backup state ------------------------------------------------------------------

    def state_path(self, instance: str) -> Path:
        return self.root / "crafty" / STATE_DIR / f"{instance}.json"

    def backup_state(self, instance: str) -> str:
        try:
            state = json.loads(self.state_path(instance).read_text())
        except FileNotFoundError:
            return "not-required"  # nothing has ever been played on this disk
        except (OSError, ValueError):
            return "failed"
        value = state.get("backup") if isinstance(state, dict) else None
        return value if value in ("pending", "complete", "failed") else "failed"

    def record(self, instance: str, **fields) -> None:
        path = self.state_path(instance)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as output:
            json.dump(fields, output, sort_keys=True)
            temporary = Path(output.name)
        temporary.replace(path)

    # Lifecycle ------------------------------------------------------------------------------

    def wait(self, predicate, seconds: int, failure: str) -> None:
        deadline = self.host.monotonic() + seconds
        while not predicate():
            if self.host.monotonic() >= deadline:
                raise CraftyError(failure)
            self.host.sleep(5)

    def status(self, instance: str) -> dict:
        server = self.server(instance)
        port = self.config["instances"][instance]["java_port"]
        running = self.running(server)
        return {"instance": instance, "application": "running" if running else "stopped",
                "backup": "pending" if running else self.backup_state(instance),
                "ready": running and self.host.port_open(port)}

    def api_ready(self) -> bool:
        try:
            self.api.request("GET", "/api/v2/servers")
            return True
        except CraftyError:
            return False

    def start(self, instance: str) -> dict:
        # Right after an EC2 start, Crafty's container may still be booting.
        self.wait(self.api_ready, self.timeouts["start"], "Crafty's API did not come up; check minecraft-crafty.service")
        server = self.server(instance)
        port = self.config["instances"][instance]["java_port"]
        if not self.running(server):
            # Mark the session first, so a crash before the next drain can never look backed up.
            self.record(instance, backup="pending", boot_id=self.host.boot_id())
            self.action(server, "start_server")
        self.wait(lambda: self.running(server) and self.host.port_open(port), self.timeouts["start"],
                  "the server did not become ready; inspect its Crafty console")
        return {**self.status(instance), "storage": self.host.storage(str(self.root))}

    def stop(self, instance: str) -> dict:
        server = self.server(instance)
        port = self.config["instances"][instance]["java_port"]
        if self.running(server):
            self.action(server, "stop_server")
        self.wait(lambda: not self.running(server) and not self.host.port_open(port), self.timeouts["stop"],
                  "the server did not stop; inspect its Crafty console")
        # Always archive: the server may also have been started and stopped from the Crafty UI,
        # so the only safe evidence that the latest play is stored is a fresh archive.
        try:
            key = self.archive(instance, server)
        except Exception:
            self.record(instance, backup="failed", boot_id=self.host.boot_id())
            raise
        self.record(instance, backup="complete", key=key, boot_id=self.host.boot_id())
        return {"instance": instance, "application": "stopped", "backup": "complete"}

    def archive(self, instance: str, server: dict) -> str:
        """Cold archive of the stopped server directory, stored once and verified in S3."""
        source = self.server_dir(server)
        if not source.is_dir() or source.is_symlink():
            raise CraftyError("the Crafty server directory is missing")
        backup = self.config["backup"]
        stamp = self.host.now().strftime("%Y%m%dT%H%M%SZ")
        prefix = f"instances/{instance}/crafty/"
        key = f"{prefix}{stamp}.tar.gz"
        staging = self.root / "crafty" / "staging"
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=staging, suffix=".tar.gz", delete=False) as output:
            archive = Path(output.name)
        try:
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(source, arcname=instance)
            with archive.open("rb") as stored:
                digest = hashlib.file_digest(stored, "sha256").hexdigest()
            size = archive.stat().st_size
            self.host.aws(backup["region"], "s3api", "put-object", "--bucket", backup["bucket"], "--key", key,
                          "--body", str(archive), "--if-none-match", "*", "--checksum-algorithm", "SHA256",
                          "--metadata", f"sha256={digest}")
            head = self.host.aws(backup["region"], "s3api", "head-object", "--bucket", backup["bucket"], "--key", key)
            if head.get("ContentLength") != size or head.get("Metadata", {}).get("sha256") != digest:
                raise CraftyError("S3 did not confirm the stored archive")
        finally:
            archive.unlink(missing_ok=True)
        self.prune(backup, prefix)
        return key

    def prune(self, backup: dict, prefix: str) -> None:
        listing = self.host.aws(backup["region"], "s3api", "list-objects-v2", "--bucket", backup["bucket"], "--prefix", prefix)
        keys = sorted(item["Key"] for item in listing.get("Contents", []) if re.fullmatch(re.escape(prefix) + r"[0-9]{8}T[0-9]{6}Z\.tar\.gz", item.get("Key", "")))
        for key in keys[:-KEEP_ARCHIVES]:
            self.host.aws(backup["region"], "s3api", "delete-object", "--bucket", backup["bucket"], "--key", key)

    def host_status(self) -> dict:
        return {"draining": False, "instances": {name: self.status(name) for name in self.config["instances"]},
                "storage": self.host.storage(str(self.root))}

    def host_drain(self) -> dict:
        results = {}
        for name in sorted(self.config["instances"]):
            results[name] = self.stop(name)
        return {"draining": True, "instances": results}


# First setup: stage the reviewed server files for Crafty's "import from folder" ------------

def load_manifest(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema_version") != 1 or not re.fullmatch(
            r"registry\.gitlab\.com/crafty-controller/crafty-4@sha256:[0-9a-f]{64}", manifest.get("crafty", {}).get("image", "")):
        raise CraftyError("manifest must pin the Crafty image by digest")
    names, paths = set(), set()
    for artifact in manifest.get("artifacts", []):
        url = urllib.parse.urlsplit(artifact.get("url", ""))
        path = artifact.get("path", "")
        if (artifact.get("name") in names or path in paths or not SHA256.fullmatch(artifact.get("sha256", ""))
                or url.scheme != "https" or url.netloc not in ARTIFACT_HOSTS or url.query or "latest" in url.path.lower()
                or not re.fullmatch(r"(?:plugins/)?[A-Za-z0-9._-]+\.jar", path)):
            raise CraftyError("every artifact needs a unique jar path, an exact HTTPS URL and a SHA-256")
        names.add(artifact["name"])
        paths.add(path)
    if not {"paper", "geyser", "floodgate", "multiverse-core", "multiverse-inventories"} <= names:
        raise CraftyError("manifest must include Paper, Geyser, Floodgate and Multiverse")
    for managed in manifest.get("managed_files", []):
        if hashlib.sha256(_managed_file_bytes(managed)).hexdigest() != managed.get("sha256"):
            raise CraftyError("managed configuration checksum mismatch")
    if not isinstance(manifest.get("server_properties"), dict):
        raise CraftyError("manifest must include server properties")
    return manifest


def download(url: str, sha256: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "CraigMinecraft/1.0 (https://github.com/CraigDevJohnson/minecraft)"})
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    if digest.hexdigest() != sha256:
        temporary.unlink(missing_ok=True)
        raise CraftyError(f"downloaded {target.name} does not match its pinned SHA-256")
    temporary.chmod(0o644)
    temporary.replace(target)


def render_properties(values: dict) -> bytes:
    lines = []
    for key in sorted(values):
        value = values[key]
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if not re.fullmatch(r"[a-z0-9.-]+", key) or "\n" in text:
            raise CraftyError("server properties must be simple key/value pairs")
        lines.append(f"{key}={text}\n")
    return "".join(lines).encode()


def stage_import(manifest: dict, destination: Path, *, fetch=download, archive: Path | None = None) -> Path:
    """Write the server folder, and optionally the archive Crafty imports. Refuses to touch existing ones."""
    destination = Path(destination)
    if destination.exists() or (archive is not None and os.path.lexists(archive)):
        raise CraftyError("the import folder or archive already exists; remove it after a completed import")
    destination.mkdir(parents=True)
    (destination / "plugins").mkdir()
    for artifact in manifest["artifacts"]:
        fetch(artifact["url"], artifact["sha256"], destination / artifact["path"])
    for managed in manifest.get("managed_files", []):
        target = destination / managed["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_managed_file_bytes(managed))
    (destination / "server.properties").write_bytes(render_properties(manifest["server_properties"]))
    # Craig accepted the Minecraft EULA for this family server on issue #5. No trailing newline:
    # Crafty 4.11 compares the first line exactly and silently refuses to start otherwise.
    (destination / "eula.txt").write_text("eula=true")
    if archive is not None:
        archive = Path(archive)
        archive.parent.mkdir(parents=True, exist_ok=True)
        # The jars are already compressed, so store them; server files sit at the archive root.
        # Crafty's own account can write to this folder, so never follow or reuse a planted link.
        try:
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660)
        except OSError as exc:
            raise CraftyError("the import archive could not be created as a new regular file") from exc
        with os.fdopen(descriptor, "wb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as bundle:
            os.fchmod(output.fileno(), 0o660)
            for path in sorted(destination.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(destination).as_posix())
    return destination


def main(argv=None, *, api=None, host=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/minecraft/operator.json"))
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "stop", "status"):
        commands.add_parser(name).add_argument("instance")
    commands.add_parser("host-drain")
    commands.add_parser("host-status")
    stage = commands.add_parser("stage-import", help="download and verify the pinned server files for a first Crafty import")
    stage.add_argument("--manifest", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    stage.add_argument("--archive", type=Path, help="also write the .zip that Crafty 4.11 imports")
    create = commands.add_parser("import-server", help="create the Crafty server from a staged archive in import/upload")
    create.add_argument("instance")
    create.add_argument("--archive-name", required=True)
    create.add_argument("--max-memory-gb", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        if args.action == "stage-import":
            path = stage_import(load_manifest(args.manifest), args.destination, archive=args.archive)
            print(json.dumps({"staged": str(path), **({"archive": str(args.archive)} if args.archive else {})}))
            return 0
        config = load_config(args.config)
        if getattr(args, "instance", None) is not None and args.instance not in config["instances"]:
            raise CraftyError("instance is not configured")
        if api is None:
            token = Path(config["token_file"]).read_text().strip()
            api = CraftyAPI(config["crafty_url"], token)
        adapter = Adapter(config, api, host)
        handlers = {"start": adapter.start, "stop": adapter.stop, "status": adapter.status}
        if args.action in handlers:
            output = handlers[args.action](args.instance)
        elif args.action == "import-server":
            output = adapter.import_server(args.instance, args.archive_name, args.max_memory_gb)
        elif args.action == "host-drain":
            output = adapter.host_drain()
        else:
            output = adapter.host_status()
        print(json.dumps(output, sort_keys=True))
        return 0
    except (CraftyError, OSError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, CraftyError) else "host file access failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
