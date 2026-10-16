#!/usr/bin/env python3
"""Host adapter between the operator lifecycle commands and Crafty Controller.

Installed as /usr/local/sbin/minecraft-instance, so the launcher, the idle-stop Lambda and the
workstation's minecraft_host.py keep sending the same `start`, `host-drain`, `host-status` and
`allowlist` requests. Crafty remains the only process manager: this adapter only asks Crafty's local
API to start or stop the server or to run one console command, then makes a cold archive of the
stopped server directory and stores it in S3. Before each start it brings the stopped server to the
pinned content in server.json beside it (docs/adr/0008-the-adapter-applies-the-pinned-server-content.md).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import stat
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
from minecraft_content import _approved_geyser, _managed_file_bytes  # noqa: E402  (approved Geyser configs and their bytes)

# The pinned server content the launcher installs beside the adapter, and the Compose file install_crafty.sh wrote.
MANIFEST = Path(__file__).resolve().parent / "server.json"
COMPOSE = Path("/opt/minecraft-crafty/compose.yaml")
GEYSER_CONFIG = "plugins/Geyser-Spigot/config.yml"
# NetherNet's WebRTC candidates carry the host's public address, which changes with every EC2 start.
ADVERTISE = re.compile(r" -DgeyserAdvertiseAddresses=\S*")
INSTANCE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SHA256 = re.compile(r"[0-9a-f]{64}")
JAR_PATH = re.compile(r"(?:plugins/)?[A-Za-z0-9._-]+\.jar")
JAVA_NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
CONTAINER_SERVERS = "/crafty/servers/"
ARTIFACT_HOSTS = {"fill-data.papermc.io", "download.geysermc.org", "github.com"}
STATE_DIR = "state"
KEEP_ARCHIVES = 14
STORAGE_FIELDS = ("size_bytes", "available_bytes", "used_percent")
# Upper bounds on what the allowlist action reads.
PLAYER_FILE_LIMIT = 4096
ALLOWLIST_LIMIT = 1024 * 1024
LOG_LIMIT = 1024 * 1024
# The game's console answers to `whitelist reload` and `whitelist list`, whole lines as Paper's logs/latest.log
# records them ("[12:00:00] [Server thread/INFO]: Reloaded the whitelist"). Paper puts a logger's name before the
# text only for loggers outside the game's own, which these answers do not come from; one is accepted anyway, but
# only a dotted Java name. Chat ("<Name> text") and /say ("[Name] text") put their sender before the text, and no
# player name holds a dot after a letter, so a player cannot write either line.
LOG_LINE = r"\[[^\]]*\] \[[^\]]*\]: (?:\[(?:[a-z][a-z0-9_]*\.)+[A-Za-z0-9_$]+\] )?"
RELOADED = re.compile(LOG_LINE + r"Reloaded the whitelist")
LISTED = re.compile(LOG_LINE + r"There are (?:no whitelisted players|\d+ whitelisted player\(s\): (.*))")


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

    def request(self, method: str, path: str, body: dict | None = None, *, text: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        data = b"" if method == "POST" else None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif text is not None:
            # Crafty's console endpoint takes the command line itself as the body.
            data = text.encode()
            headers["Content-Type"] = "text/plain; charset=utf-8"
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
        """Whether a Minecraft server answers on the port. Docker's port proxy accepts connections
        for a stopped server and then closes them, so a bare connect proves nothing."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
                connection.sendall(b"\xfe\x01")  # legacy server-list ping; any server replies with a kick packet
                return connection.recv(1) != b""
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

    def upload(self, region: str, bucket: str, key: str, expected_size: int, write) -> None:
        """Stream an object to S3. `aws s3 cp -` uses multipart upload, so archives may exceed 5 GB."""
        command = ["aws", "--region", region, "--no-cli-pager", "s3", "cp", "-", f"s3://{bucket}/{key}",
                   "--only-show-errors", "--expected-size", str(expected_size)]
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise CraftyError("AWS upload could not run") from exc
        try:
            write(process.stdin)
            process.stdin.close()
            code = process.wait(timeout=1800)
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code:
            raise CraftyError("AWS upload failed")

    def download(self, region: str, bucket: str, key: str, read) -> None:
        """Stream an object from S3 into `read`, so a restore needs no second copy on the data disk."""
        command = ["aws", "--region", region, "--no-cli-pager", "s3", "cp", f"s3://{bucket}/{key}", "-", "--only-show-errors"]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise CraftyError("AWS download could not run") from exc
        try:
            read(process.stdout)
            code = process.wait(timeout=1800)
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code:
            raise CraftyError("AWS download failed")

    compose = COMPOSE

    def read_compose(self) -> bytes | None:
        try:
            return self.compose.read_bytes()
        except FileNotFoundError:
            return None

    def write_compose(self, content: bytes) -> None:
        write_durably(self.compose, content, mode=0o644)

    def compose_up(self) -> None:
        """Have Docker bring Crafty's container in line with the Compose file and wait until it is up. Its own
        output goes to standard error, so standard output stays the adapter's JSON answer."""
        command = ["docker", "compose", "-f", str(self.compose), "up", "--detach", "--wait"]
        try:
            result = subprocess.run(command, stdout=sys.stderr, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CraftyError("docker compose could not run") from exc
        if result.returncode:
            raise CraftyError("docker compose did not bring Crafty up")

    def public_ipv4(self) -> str:
        """The host's current public IPv4 from IMDSv2: never through a proxy, and a redirect is refused."""
        endpoint = "http://169.254.169.254/latest"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())
        try:
            request = urllib.request.Request(endpoint + "/api/token", method="PUT",
                                             headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
            with opener.open(request, timeout=5) as reply:
                token = reply.read(4097).decode("ascii")
            if not re.fullmatch(r"[\x21-\x7e]{1,4096}", token):
                raise ValueError("invalid metadata token")
            request = urllib.request.Request(endpoint + "/meta-data/public-ipv4", headers={"X-aws-ec2-metadata-token": token})
            with opener.open(request, timeout=5) as reply:
                address = ipaddress.IPv4Address(reply.read(65).decode("ascii").strip())
            if not address.is_global or address.is_multicast:
                raise ValueError("invalid public IPv4")
            return str(address)
        except (OSError, http.client.HTTPException, UnicodeError, ValueError) as exc:
            raise CraftyError("cannot read the host's public IPv4 for NetherNet; retry once host networking is ready") from exc

    def fetch(self, url: str, sha256: str, target: Path) -> None:
        try:
            download(url, sha256, target)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise CraftyError(f"cannot download {target.name}; retry the start") from exc

    @contextlib.contextmanager
    def lock(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CraftyError("another lifecycle command is running; retry when it finishes") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments, **keywords):
        return None


class _Counted:
    """Write-through sink that measures and hashes the archive as it streams to S3."""

    def __init__(self, sink):
        self.sink, self.size, self.digest = sink, 0, hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.sink.write(data)
        self.size += len(data)
        self.digest.update(data)
        return len(data)


class _CountedReader:
    """Read-through source that hashes an archive as it streams from S3."""

    def __init__(self, source):
        self.source, self.digest = source, hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.digest.update(data)
        return data


def fingerprint(source: Path) -> list:
    """Entry count, total size and newest change of a stopped server folder. Any session Crafty
    runs rewrites logs and world files, and the change time cannot be set back with utime."""
    count, size, newest = 0, 0, 0
    for folder, names, files in os.walk(source):
        for name in (".", *names, *files):
            entry = os.lstat(os.path.join(folder, name))
            count, size = count + 1, size + entry.st_size
            newest = max(newest, entry.st_mtime_ns, entry.st_ctime_ns)
    return [count, size, newest]


class Adapter:
    def __init__(self, config: dict, api, host=None, manifest: dict | None = None):
        self.config = config
        self.api = api
        self.host = host or Host()
        self.root = Path(config["data_root"])
        self.timeouts = {"start": 300, "stop": 300, **config.get("timeouts", {})}
        # The pinned server content a start applies first; without it a start leaves the content as it is.
        self.manifest = manifest

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
        servers = self.root / "crafty" / "servers"
        name = path[len(CONTAINER_SERVERS):] if isinstance(path, str) and path.startswith(CONTAINER_SERVERS) else ""
        # Exactly one plain folder name under the servers folder, never an absolute or nested path.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name.rstrip("/")):
            raise CraftyError("Crafty reported an unexpected server path")
        return servers / name.rstrip("/")

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
            # A rerun after a slow import resumes the wait; a finished server is never imported twice.
            server = self.server(instance)
            if (self.server_dir(server) / "paper.jar").is_file() and not self._importing(server):
                raise CraftyError(f"Crafty already has a server named {settings['server_name']}")
            return self._await_import(instance, server)
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
        return self._await_import(instance, self.server(instance))

    def _importing(self, server: dict) -> bool:
        stats = self.api.request("GET", f"/api/v2/servers/{server['server_id']}/stats").get("data")
        return not isinstance(stats, dict) or bool(stats.get("importing"))

    def _await_import(self, instance: str, server: dict) -> dict:
        # Crafty unpacks the archive in a background thread after the request returns. The archive
        # must stay in place until then, so wait until Crafty clears its importing flag and the jar is there.
        jar = self.server_dir(server) / "paper.jar"
        self.wait(lambda: not self._importing(server) and jar.is_file(), self.timeouts["start"],
                  "Crafty has not finished unpacking the archive; rerun import-server to keep waiting, "
                  "or delete the server in Crafty if its import log shows a failure")
        return {"instance": instance, "server_id": server["server_id"]}

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
        write_durably(self.state_path(instance), fields)

    # Drain barrier: once a drain starts, no session may start again until the host reboots,
    # so a caller that drained can stop EC2 without racing a new start.

    def draining(self) -> bool:
        try:
            state = json.loads((self.root / "crafty" / STATE_DIR / "host-drain.json").read_text())
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True
        return not isinstance(state, dict) or state.get("boot_id") == self.host.boot_id()

    def lifecycle(self):
        return self.host.lock(self.root / "crafty" / STATE_DIR / "lifecycle.lock")

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

    def listed(self, instance: str) -> bool:
        try:
            self.running(self.server(instance))
            return True
        except CraftyError:
            return False

    def start(self, instance: str) -> dict:
        with self.lifecycle():
            if self.draining():
                raise CraftyError("host is draining; finish stopping the host before starting another session")
            return self._start(instance)

    def _start(self, instance: str) -> dict:
        # Right after an EC2 start, Crafty's container may still be booting.
        self.wait(self.api_ready, self.timeouts["start"], "Crafty's API did not come up; check minecraft-crafty.service")
        server = self.server(instance)
        port = self.config["instances"][instance]["java_port"]
        if not self.running(server):
            if self.manifest is not None:
                server = self.apply_content(instance, server)
            # Mark the session first, so a crash before the next drain can never look backed up.
            self.record(instance, backup="pending", boot_id=self.host.boot_id())
            self.action(server, "start_server")
        self.wait(lambda: self.running(server) and self.host.port_open(port), self.timeouts["start"],
                  "the server did not become ready; inspect its Crafty console")
        return {**self.status(instance), "storage": self.host.storage(str(self.root))}

    def stop(self, instance: str) -> dict:
        with self.lifecycle():
            return self._stop(instance)

    def _stop(self, instance: str) -> dict:
        server = self.server(instance)
        port = self.config["instances"][instance]["java_port"]
        if self.running(server):
            self.action(server, "stop_server")
        self.wait(lambda: not self.running(server) and not self.host.port_open(port), self.timeouts["stop"],
                  "the server did not stop; inspect its Crafty console")
        # Always archive: the server may also have been started and stopped from the Crafty UI,
        # so the only safe evidence that the latest play is stored is a fresh archive.
        try:
            source = self.server_dir(server)
            before = fingerprint(source) if source.is_dir() and not source.is_symlink() else None
            # Crafty's own UI can start the server while the archive uploads. That archive may be
            # inconsistent and the session must not be cut off, so the stop fails and is repeated.
            # The check runs before the upload gets its checksum, so it never becomes a recovery point.
            stored = self.archive(instance, server,
                                  settled=lambda: not self.running(server) and fingerprint(source) == before)
        except Exception:
            self.record(instance, backup="failed", boot_id=self.host.boot_id())
            raise
        self.record(instance, backup="complete", **stored, boot_id=self.host.boot_id(), fingerprint=before)
        return {"instance": instance, "application": "stopped", "backup": "complete"}

    def unchanged(self, instance: str) -> bool:
        """Whether the stopped server folder is exactly as it was when its last archive was stored."""
        try:
            state = json.loads(self.state_path(instance).read_text())
            recorded = state.get("fingerprint") if isinstance(state, dict) else None
            return recorded is not None and recorded == fingerprint(self.server_dir(self.server(instance)))
        except (OSError, ValueError, CraftyError):
            return False

    def restore(self, instance: str, key: str) -> dict:
        """Replace the stopped server folder with a verified archive from S3. The folder it
        replaces is kept under crafty/staging until the operator deletes it."""
        with self.lifecycle():
            prefix = f"instances/{instance}/crafty/"
            if not re.fullmatch(re.escape(prefix) + r"[0-9]{8}T[0-9]{6}Z\.tar\.gz", key):
                raise CraftyError("restore needs the key of one of this instance's archives")
            server = self.server(instance)
            if self.running(server):
                raise CraftyError("stop the server before restoring it")
            target, backup = self.server_dir(server), self.config["backup"]
            staging = self.root / "crafty" / "staging"
            staging.mkdir(mode=0o700, parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(dir=staging, prefix="restore-"))
            try:
                expected = self.archive_checksum(backup, key, work / "checksum")
                counted = {}

                def read(source) -> None:
                    counted["source"] = _CountedReader(source)
                    # The archive is this adapter's own, so keep its owners; the tar filter still
                    # refuses absolute paths, links out of the folder and device files.
                    with tarfile.open(fileobj=counted["source"], mode="r|gz") as bundle:
                        bundle.extractall(work / "files", filter="tar", numeric_owner=True)
                    while counted["source"].read(1024 * 1024):
                        pass

                try:
                    self.host.download(backup["region"], backup["bucket"], key, read)
                except (tarfile.TarError, EOFError) as exc:
                    raise CraftyError("the archive could not be unpacked") from exc
                digest = counted["source"].digest.hexdigest()
                restored = work / "files" / instance
                if digest != expected or not restored.is_dir() or restored.is_symlink():
                    raise CraftyError("the archive does not match its recorded SHA-256")
                # Crafty's own UI could have started the server while the archive unpacked.
                if self.running(server):
                    raise CraftyError("the server was started during the restore; stop it and restore again")
                replaced = None
                if target.exists():
                    replaced = staging / f"replaced-{instance}-{self.host.now().strftime('%Y%m%dT%H%M%SZ')}"
                    target.rename(replaced)
                try:
                    restored.rename(target)
                except BaseException:
                    if replaced is not None:
                        replaced.rename(target)  # never leave the server without a folder
                    raise
                self.record(instance, backup="complete", key=key, sha256=digest, boot_id=self.host.boot_id(),
                            fingerprint=fingerprint(target))
            finally:
                shutil.rmtree(work, ignore_errors=True)
        return {"instance": instance, "restored": key, "replaced": str(replaced) if replaced else None}

    def archive_checksum(self, backup: dict, key: str, scratch: Path) -> str:
        """The archive's recorded SHA-256: the `.sha256` object beside it, or the object metadata
        that archives stored before 2026-10-11 carry."""
        try:
            self.host.aws(backup["region"], "s3api", "get-object", "--bucket", backup["bucket"], "--key", f"{key}.sha256", str(scratch))
            value = scratch.read_text().split()[0]
        except (CraftyError, OSError, IndexError):
            head = self.host.aws(backup["region"], "s3api", "head-object", "--bucket", backup["bucket"], "--key", key)
            value = head.get("Metadata", {}).get("sha256", "")
        if not SHA256.fullmatch(value):
            raise CraftyError("the archive has no recorded SHA-256")
        return value

    def archive(self, instance: str, server: dict, settled=None) -> dict:
        """Cold archive of the stopped server directory, streamed to S3 and verified there.

        Streaming needs no second copy of the world on the data disk, and the multipart upload
        has no 5 GB limit. The SHA-256 is stored beside the archive as `<key>.sha256`.
        """
        source = self.server_dir(server)
        if not source.is_dir() or source.is_symlink():
            raise CraftyError("the Crafty server directory is missing")
        backup = self.config["backup"]
        stamp = self.host.now().strftime("%Y%m%dT%H%M%SZ")
        prefix = f"instances/{instance}/crafty/"
        key = f"{prefix}{stamp}.tar.gz"
        existing = self.host.aws(backup["region"], "s3api", "list-objects-v2", "--bucket", backup["bucket"], "--prefix", key)
        if existing.get("Contents"):
            raise CraftyError("an archive with this timestamp already exists; retry the stop")
        expected = sum(entry.stat(follow_symlinks=False).st_size for entry in source.rglob("*"))
        counted = {}

        def write(sink) -> None:
            counted["sink"] = _Counted(sink)
            with tarfile.open(fileobj=counted["sink"], mode="w|gz") as bundle:
                bundle.add(source, arcname=instance)

        self.host.upload(backup["region"], backup["bucket"], key, max(expected, 1), write)
        size, digest = counted["sink"].size, counted["sink"].digest.hexdigest()
        staging = self.root / "crafty" / "staging"
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=staging, suffix=".sha256", delete=False) as output:
            output.write(f"{digest}  {stamp}.tar.gz\n")
            checksum = Path(output.name)
        try:
            head = self.host.aws(backup["region"], "s3api", "head-object", "--bucket", backup["bucket"], "--key", key)
            if head.get("ContentLength") != size:
                raise CraftyError("S3 did not confirm the stored archive")
            if settled is not None and not settled():
                raise CraftyError("the server changed while it was being archived; repeat the stop")
            self.host.aws(backup["region"], "s3api", "put-object", "--bucket", backup["bucket"], "--key", f"{key}.sha256",
                          "--body", str(checksum), "--if-none-match", "*")
        except BaseException:
            # Without its checksum the upload can never be restored; do not leave it to look like a recovery point.
            with contextlib.suppress(CraftyError):
                self.host.aws(backup["region"], "s3api", "delete-object", "--bucket", backup["bucket"], "--key", key)
            raise
        finally:
            checksum.unlink(missing_ok=True)
        try:
            self.prune(backup, prefix)
        except CraftyError as exc:
            # The new archive is stored and verified; old-archive cleanup retries on the next stop.
            print(f"warning: could not prune old archives: {exc}", file=sys.stderr)
        return {"key": key, "sha256": digest}

    def prune(self, backup: dict, prefix: str) -> None:
        listing = self.host.aws(backup["region"], "s3api", "list-objects-v2", "--bucket", backup["bucket"], "--prefix", prefix)
        present = {item.get("Key", "") for item in listing.get("Contents", [])}
        keys = sorted(key for key in present if re.fullmatch(re.escape(prefix) + r"[0-9]{8}T[0-9]{6}Z\.tar\.gz", key))
        # Only an archive with its checksum is a recovery point, so only those count toward the kept
        # number. Anything older than the oldest one kept goes, with or without a checksum.
        verified = [key for key in keys if f"{key}.sha256" in present]
        if len(verified) <= KEEP_ARCHIVES:
            return
        oldest_kept = verified[-KEEP_ARCHIVES]
        for key in (key for key in keys if key < oldest_kept):
            self.host.aws(backup["region"], "s3api", "delete-object", "--bucket", backup["bucket"], "--key", key)
            self.host.aws(backup["region"], "s3api", "delete-object", "--bucket", backup["bucket"], "--key", f"{key}.sha256")

    def host_status(self) -> dict:
        return {"draining": self.draining(), "instances": {name: self.status(name) for name in self.config["instances"]},
                "storage": self.host.storage(str(self.root))}

    def host_drain(self) -> dict:
        with self.lifecycle():
            if self.draining():
                # The operator's stop already drained this boot; EC2's shutdown runs the drain again.
                # Archive only if something has run since, so one session is not stored twice.
                current = {name: self.status(name) for name in sorted(self.config["instances"])}
                # A session run from Crafty's own UI bypasses this adapter's state, so only a folder
                # that is exactly as archived counts. Missing state proves nothing and is archived.
                if all(value["application"] == "stopped" and value["backup"] == "complete" and self.unchanged(name)
                       for name, value in current.items()):
                    return {"draining": True, "instances": {name: {key: value[key] for key in ("instance", "application", "backup")}
                                                            for name, value in current.items()}}
            write_durably(self.root / "crafty" / STATE_DIR / "host-drain.json", {"boot_id": self.host.boot_id()})
            results = {name: self._stop(name) for name in sorted(self.config["instances"])}
        return {"draining": True, "instances": results}

    # Pinned server content (docs/adr/0008-the-adapter-applies-the-pinned-server-content.md) ----

    def apply_content(self, instance: str, server: dict) -> dict:
        """Bring the stopped server to the manifest: its jars and managed files, Crafty's published ports, and the
        launch command's NetherNet address. Answers the server as Crafty reports it afterwards."""
        # A NetherNet start needs the host's address; without it nothing changes and the start fails.
        address = self.host.public_ipv4() if bedrock_transport(self.manifest) == "both" else None
        self.apply_files(instance, server)
        if self.apply_compose():
            # Docker recreated Crafty's container to publish the new ports; its API answers before it lists the server.
            self.wait(lambda: self.listed(instance), self.timeouts["start"], "Crafty did not come back after its ports changed")
            server = self.server(instance)
            if self.running(server):
                raise CraftyError("the server started while Crafty restarted; stop it, then start it again")
        self.apply_launch_command(server, address)
        return server

    def content_path(self, instance: str) -> Path:
        return self.root / "crafty" / STATE_DIR / f"{instance}-content.json"

    def apply_files(self, instance: str, server: dict) -> None:
        """Replace each jar and managed file that differs from its pin, and remove the jars an earlier manifest
        pinned that this one does not. Downloads finish before the server folder changes."""
        artifacts = self.manifest["artifacts"]
        wanted = {artifact["path"] for artifact in artifacts}
        try:
            recorded = json.loads(self.content_path(instance).read_text())
            pinned = recorded["artifacts"]
        except FileNotFoundError:
            recorded, pinned = None, []  # a server staged from the first manifest holds only its own jars
        except (OSError, ValueError, TypeError, KeyError):
            pinned = None
        if not isinstance(pinned, list) or not all(isinstance(path, str) and JAR_PATH.fullmatch(path) for path in pinned):
            raise CraftyError("the record of the server's applied content cannot be read")
        # Every jar a manifest has pinned, so a later one removes it, even from a restored older archive.
        pinned = sorted(wanted | set(pinned))
        with _ServerFolder(self.server_dir(server)) as folder:
            # Paper first, so a later failure never leaves a new Paper for the next start to find unarchived.
            changed = sorted((artifact for artifact in artifacts if folder.digest(artifact["path"]) != artifact["sha256"]),
                             key=lambda artifact: artifact["name"] != "paper")
            managed = [item for item in self.manifest.get("managed_files", []) if folder.digest(item["path"]) != item["sha256"]]
            stale = [path for path in pinned if path not in wanted and folder.exists(path)]
            if changed or managed or stale:
                # A new Paper upgrades the world on its first start, so the last archive must hold the world as it is.
                if any(artifact["name"] == "paper" for artifact in changed) and not self.unchanged(instance):
                    raise CraftyError("a new Paper needs an archive of the world as it is; stop the server once, then start it")
                staging = self.root / "crafty" / "staging"
                staging.mkdir(mode=0o700, parents=True, exist_ok=True)
                work = Path(tempfile.mkdtemp(dir=staging, prefix="content-"))
                try:
                    for artifact in changed:
                        self.host.fetch(artifact["url"], artifact["sha256"], work / f"{artifact['name']}.jar")
                    # Downloads take a while; never change the jars under a server someone started meanwhile.
                    if self.running(server):
                        raise CraftyError("the server started while its content was downloading; stop it, then start it again")
                    # Record every jar before any lands, so a later manifest can remove what this change leaves.
                    write_durably(self.content_path(instance), {"artifacts": pinned})
                    for artifact in changed:
                        with open(work / f"{artifact['name']}.jar", "rb") as staged:
                            folder.write(artifact["path"], iter(lambda: staged.read(1024 * 1024), b""), artifact["sha256"])
                    for item in managed:
                        folder.write(item["path"], [_managed_file_bytes(item)], item["sha256"])
                    for path in stale:
                        folder.remove(path)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
        record = {"artifacts": pinned, "revision": self.manifest.get("revision")}
        if recorded != record:
            write_durably(self.content_path(instance), record)

    def apply_compose(self) -> bool:
        """Write the Compose file the manifest calls for and have Docker apply it, answering whether Docker ran.
        If Docker cannot bring Crafty up with a new file, the previous file goes back."""
        wanted = render_compose(self.manifest, self.root / "crafty")
        digest = hashlib.sha256(wanted).hexdigest()
        record = self.root / "crafty" / STATE_DIR / "compose.json"
        try:
            applied = json.loads(record.read_text()).get("sha256")
        except (OSError, ValueError, AttributeError):
            applied = None
        current = self.host.read_compose()
        # The file alone is no proof: a start cut off before Docker finished, or a rerun installer, leaves it unapplied.
        if current == wanted and applied == digest:
            return False
        if current != wanted:
            self.host.write_compose(wanted)
        try:
            self.host.compose_up()
        except CraftyError:
            if current is None or current == wanted:
                raise CraftyError("docker compose did not bring Crafty up; check minecraft-crafty.service") from None
            self.host.write_compose(current)
            with contextlib.suppress(CraftyError):
                self.host.compose_up()
            raise CraftyError("Crafty did not come up with the new ports; the previous Compose file is back") from None
        write_durably(record, {"sha256": digest})
        return True

    def apply_launch_command(self, server: dict, address: str | None) -> None:
        """Have Crafty launch the server with the host's current NetherNet address, or with none for RakNet alone."""
        path = f"/api/v2/servers/{server['server_id']}"
        current = self.api.request("GET", path).get("data")
        current = current.get("execution_command") if isinstance(current, dict) else None
        if not isinstance(current, str):
            raise CraftyError("Crafty did not report the server's launch command")
        wanted = launch_command(current, address)
        if wanted == current:
            return
        self.api.request("PATCH", path, {"execution_command": wanted})
        stored = self.api.request("GET", path).get("data")
        if not isinstance(stored, dict) or stored.get("execution_command") != wanted:
            raise CraftyError("Crafty did not keep the server's new launch command")

    # Allowlist ------------------------------------------------------------------------------

    def console(self, server: dict, line: str) -> None:
        """Send one command line to the running server's console. Crafty answers whether it was delivered, not
        what the server made of it, so callers confirm the effect in the server's own files."""
        if not line or any(character in line for character in "\r\n\x00"):
            raise CraftyError("a console command must be one line")
        self.api.request("POST", f"/api/v2/servers/{server['server_id']}/stdin", text=line)

    def allowlist(self, instance: str, operation: str, edition: str, player: dict) -> dict:
        """Add or remove one verified player on the running server (docs/operations/allowlist.md)."""
        from minecraft_access import AccessError, change_allowlist  # only this action needs it

        with self.lifecycle():
            if self.draining():
                raise CraftyError("allowlist changes require a running instance outside host drain")
            server = self.server(instance)
            if not (self.running(server) and self.host.port_open(self.config["instances"][instance]["java_port"])):
                raise CraftyError("allowlist changes require a running, ready server")
            console = _Console(self, server)
            try:
                if "uuid" not in player:
                    return self._remove_listed(console, instance, operation, edition, player)
                return change_allowlist(console, instance, operation, edition, player)
            except AccessError as exc:
                raise CraftyError(str(exc)) from None

    def _remove_listed(self, console: "_Console", instance: str, operation: str, edition: str, player: dict) -> dict:
        """Remove the Java entry the allowlist holds under a name Mojang no longer knows, as after a rename.

        Without a current account the server cannot resolve the name for `whitelist remove`, so the entry
        leaves the file directly and the server reloads it."""
        from minecraft_access import read_uuids

        name = player.get("name")
        if (operation != "remove" or edition != "java" or set(player) != {"name"}
                or not isinstance(name, str) or not JAVA_NAME.fullmatch(name)):
            raise CraftyError("only a Java removal may name a player without a UUID")
        result = {"instance": instance, "edition": edition, "operation": operation}
        read_uuids(console, instance)  # the same evidence checks as a change by UUID
        console.log_end()  # the reload is confirmed in the server's log, so a log it cannot read changes nothing
        entries = console.read_allowlist(instance)
        listed = [entry for entry in entries if isinstance(entry.get("name"), str) and entry["name"].lower() == name.lower()]
        if not listed:
            # A removal whose reload never reached the server leaves the entry in its memory only: reload anyway,
            # so a retry finishes it.
            console.reload_without(name)
            return {**result, "allowlist": "not-listed"}
        if len(listed) != 1:
            raise CraftyError("the allowlist holds that name more than once")
        console.write_allowlist([entry for entry in entries if entry is not listed[0]], entries)
        console.reload_without(name)
        # The server writes its whole list on every change, so one made before the reload would bring the entry back.
        if any(isinstance(entry.get("name"), str) and entry["name"].lower() == name.lower()
               for entry in console.read_allowlist(instance)):
            raise CraftyError("the server allowlist changed during the removal; inspect private server state before retrying")
        return {**result, "allowlist": "updated"}


class _Console:
    """The machine minecraft_access.change_allowlist drives: the server's console through Crafty, and the
    whitelist.json in the server folder as the evidence of each change."""

    def __init__(self, adapter: Adapter, server: dict):
        self.adapter = adapter
        self.server = server
        self.monotonic = adapter.host.monotonic
        self.sleep = adapter.host.sleep
        self.path = adapter.server_dir(server) / "whitelist.json"
        self.log = adapter.server_dir(server) / "logs" / "latest.log"

    def rcon(self, instance: str, text: str) -> None:
        self.adapter.console(self.server, text)

    def reload_without(self, name: str) -> None:
        """Have the server load whitelist.json again, and confirm from its console log that it did and that the
        list it now holds lacks name. Crafty's answer says only that the lines reached the server, and the file
        cannot show the reload, because the adapter wrote it."""
        lost = "the server log could not be read to confirm the allowlist reload; inspect private server state before retrying"
        try:
            mark = self.log_end()
        except CraftyError:
            raise CraftyError(lost) from None  # the file may already have changed
        self.rcon("", "whitelist reload")
        self.rcon("", "whitelist list")
        deadline = self.monotonic() + 60
        while True:
            try:
                names = self._listed_after(mark)
            except CraftyError:
                raise CraftyError(lost) from None
            if names is not None:
                if name.lower() in names:
                    raise CraftyError("the server still holds the entry after reloading its allowlist; inspect private server state before retrying")
                return
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise CraftyError("the server did not confirm the allowlist reload; inspect private server state before retrying")
            self.sleep(min(1, remaining))

    def _open_log(self):
        """The server's console log, opened for reading with its metadata, or None while it does not exist."""
        try:
            descriptor = os.open(self.log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise CraftyError("cannot read the server log") from None
        stream = os.fdopen(descriptor, "rb")
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            stream.close()
            raise CraftyError("the server log is not a regular file")
        return stream, metadata

    def log_end(self) -> tuple:
        """Where the server's console log ends now: its device, inode and size."""
        opened = self._open_log()
        if opened is None:
            raise CraftyError("cannot read the server log")
        stream, metadata = opened
        stream.close()
        return metadata.st_dev, metadata.st_ino, metadata.st_size

    def _listed_after(self, mark: tuple) -> set | None:
        """The names, lowercased, of the first list the log shows after the first reload logged since mark, or None
        until both are there. A log that rolled over since mark is read from its start."""
        opened = self._open_log()
        if opened is None:
            return None
        stream, metadata = opened
        with stream:
            same = (metadata.st_dev, metadata.st_ino) == mark[:2] and metadata.st_size >= mark[2]
            stream.seek(mark[2] if same else 0)
            data = stream.read(LOG_LIMIT + 1)
        if len(data) > LOG_LIMIT:
            raise CraftyError("the server log grew too fast to confirm the allowlist reload")
        reloaded = False
        for line in data.decode("utf-8", "replace").split("\n")[:-1]:  # whole lines only
            line = line.rstrip("\r")
            if not reloaded:
                reloaded = RELOADED.fullmatch(line) is not None
            elif (listed := LISTED.fullmatch(line)) is not None:
                return set() if listed.group(1) is None else {listed_name.lower() for listed_name in listed.group(1).split(", ")}
        return None

    def read_allowlist(self, instance: str) -> list:
        # The server rewrites the file in place, so a read can catch it half written: read once more.
        for attempt in range(2):
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                data = stream.read(ALLOWLIST_LIMIT + 1)
            if len(data) > ALLOWLIST_LIMIT:
                raise CraftyError("the server allowlist exceeded its size limit")
            try:
                return json.loads(data)
            except ValueError:
                if attempt:
                    raise
                self.sleep(1)
        raise AssertionError("unreachable")

    def write_allowlist(self, entries: list, expected: list) -> None:
        """Replace whitelist.json, read as expected, atomically and durably, keeping the owner and mode the
        server's own account needs."""
        current = os.lstat(self.path)
        if not stat.S_ISREG(current.st_mode):
            raise CraftyError("the server allowlist is not a regular file")
        descriptor, partial = tempfile.mkstemp(prefix=".whitelist.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                # Through the descriptor, never the name: the server's account can swap the name for a link.
                os.fchown(handle.fileno(), current.st_uid, current.st_gid)
                os.fchmod(handle.fileno(), stat.S_IMODE(current.st_mode) & 0o777)
                json.dump(entries, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # The server or the Crafty console may have changed the list since it was read; never write over that.
            if self.read_allowlist("") != expected:
                raise CraftyError("the server allowlist changed during the removal; retry it")
            os.replace(partial, self.path)
        except BaseException:
            Path(partial).unlink(missing_ok=True)
            raise
        folder = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


class _ServerFolder:
    """The stopped server's folder, reached only through descriptors that never follow a link. Crafty's own account
    can write there, so a name it swapped for a link must never send root's reads or writes elsewhere."""

    def __init__(self, path: Path):
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise CraftyError("the Crafty server directory is missing") from exc

    def __enter__(self):
        return self

    def __exit__(self, *details):
        os.close(self.fd)

    def _parent(self, path: str, create: bool = False) -> tuple[int, str]:
        """A descriptor of the folder holding path, which the caller closes, and path's last name. A missing folder
        raises FileNotFoundError, or with create is made as the folder above it is owned."""
        *folders, name = path.split("/")
        current = os.dup(self.fd)
        try:
            for folder in folders:
                try:
                    below = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(folder, 0o755, dir_fd=current)
                    below = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    above = os.fstat(current)
                    os.fchown(below, above.st_uid, above.st_gid)
                os.close(current)
                current = below
        except FileNotFoundError:
            os.close(current)
            raise
        except OSError as exc:
            os.close(current)
            raise CraftyError(f"{path} in the server folder is not reached through plain folders") from exc
        return current, name

    def digest(self, path: str) -> str | None:
        """The SHA-256 of the regular file at path, or None for anything else."""
        try:
            folder, name = self._parent(path)
        except FileNotFoundError:
            return None
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        except OSError:
            return None  # missing, or a link: never the pinned file, and replacing it never follows it
        finally:
            os.close(folder)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def exists(self, path: str) -> bool:
        try:
            folder, name = self._parent(path)
        except FileNotFoundError:
            return False
        try:
            os.stat(name, dir_fd=folder, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(folder)

    def write(self, path: str, chunks, sha256: str) -> None:
        """Replace path with chunks' bytes, which must hash to sha256, owned as the file it replaces or else as its
        folder, so the server's own account can still read and replace it."""
        folder, name = self._parent(path, create=True)
        try:
            try:
                current = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and stat.S_ISREG(current.st_mode):
                owner, mode = (current.st_uid, current.st_gid), stat.S_IMODE(current.st_mode) & 0o775
            else:
                above = os.fstat(folder)
                owner, mode = (above.st_uid, above.st_gid), 0o644
            partial = f".{name}.{secrets.token_hex(8)}"
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    os.fchown(output.fileno(), *owner)
                    os.fchmod(output.fileno(), mode)
                    digest = hashlib.sha256()
                    for chunk in chunks:
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    written = os.fstat(output.fileno())
                if digest.hexdigest() != sha256:
                    raise CraftyError(f"{path} does not match its pinned SHA-256")
                os.replace(partial, name, src_dir_fd=folder, dst_dir_fd=folder)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(partial, dir_fd=folder)
                raise
            landed = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if (landed.st_dev, landed.st_ino) != (written.st_dev, written.st_ino):
                raise CraftyError(f"{path} changed while it was replaced; start the server again")
            os.fsync(folder)
        except OSError as exc:
            raise CraftyError(f"{path} could not be written in the server folder") from exc
        finally:
            os.close(folder)

    def remove(self, path: str) -> None:
        try:
            folder, name = self._parent(path)
        except FileNotFoundError:
            return
        try:
            os.unlink(name, dir_fd=folder)  # removes a link itself, never what it points at
            os.fsync(folder)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CraftyError(f"{path} could not be removed from the server folder") from exc
        finally:
            os.close(folder)


def read_player_file(path: Path) -> dict:
    """Read the private identity file the launcher writes for one allowlist change: a regular file owned by
    this user with mode 0600, never a link. Its contents are never echoed."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CraftyError("cannot open the private player file") from exc
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise CraftyError("the player file must be a private regular file (mode 0600) owned by this user")
        data = stream.read(PLAYER_FILE_LIMIT + 1)
    if len(data) > PLAYER_FILE_LIMIT:
        raise CraftyError("the player file exceeded its size limit")
    try:
        value = json.loads(data)
    except ValueError:
        raise CraftyError("the player file is not JSON") from None
    if not isinstance(value, dict):
        raise CraftyError("the player file must hold a JSON object")
    return value


def write_durably(path: Path, value: dict | bytes, *, mode: int | None = None) -> None:
    """Replace a small JSON state file, or a file holding value's bytes, so that, once this returns, a power loss
    cannot undo it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as output:
        output.write(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode())
        if mode is not None:
            os.fchmod(output.fileno(), mode)
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    temporary.replace(path)
    folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


# First setup: stage the reviewed server files for Crafty's "import from folder" ------------

def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CraftyError("cannot read the pinned server content") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not re.fullmatch(
            r"registry\.gitlab\.com/crafty-controller/crafty-4@sha256:[0-9a-f]{64}", manifest.get("crafty", {}).get("image", "")):
        raise CraftyError("manifest must pin the Crafty image by digest")
    names, paths = set(), set()
    for artifact in manifest.get("artifacts", []):
        url = urllib.parse.urlsplit(artifact.get("url", ""))
        path = artifact.get("path", "")
        if (artifact.get("name") in names or path in paths or not SHA256.fullmatch(artifact.get("sha256", ""))
                or url.scheme != "https" or url.netloc not in ARTIFACT_HOSTS or url.query or "latest" in url.path.lower()
                or not JAR_PATH.fullmatch(path)):
            raise CraftyError("every artifact needs a unique jar path, an exact HTTPS URL and a SHA-256")
        names.add(artifact["name"])
        paths.add(path)
    if not {"paper", "geyser", "floodgate", "multiverse-core", "multiverse-inventories"} <= names:
        raise CraftyError("manifest must include Paper, Geyser, Floodgate and Multiverse")
    for managed in manifest.get("managed_files", []):
        # Written under sudo, so only plain relative paths inside the server folder are accepted.
        if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*", str(managed.get("path", ""))):
            raise CraftyError("managed file paths must be plain relative paths")
        if hashlib.sha256(_managed_file_bytes(managed)).hexdigest() != managed.get("sha256"):
            raise CraftyError("managed configuration checksum mismatch")
    if not isinstance(manifest.get("server_properties"), dict) or type(manifest["server_properties"].get("server-port")) is not int:
        raise CraftyError("manifest must include server properties with the Java port")
    bedrock_transport(manifest)
    return manifest


def bedrock_transport(manifest: dict) -> str:
    """The Bedrock transport the manifest's Geyser configuration selects, which must be one Craig approved (#30):
    RakNet alone, or both, which adds NetherNet's built-in signaling on TCP 19132 and WebRTC on UDP 19133."""
    configs = [item.get("content") for item in manifest.get("managed_files", []) if item.get("path") == GEYSER_CONFIG]
    if configs == [_approved_geyser(8)]:
        return "raknet"
    if configs == [_approved_geyser(8, dual_transport=True)]:
        return "both"
    raise CraftyError("the Geyser configuration must be one of the approved Bedrock transports")


def render_compose(manifest: dict, crafty_root: Path) -> bytes:
    """Crafty's Compose file: the pinned image, the game ports the transport needs, the web UI on loopback only (reached
    through SSM), and Crafty's folders on the retained disk."""
    java = manifest["server_properties"]["server-port"]
    ports = [f"{java}:{java}/tcp", "19132:19132/udp"]
    if bedrock_transport(manifest) == "both":
        ports += ["19132:19132/tcp", "19133:19133/udp"]
    lines = ["services:", "  crafty:", f"    image: {manifest['crafty']['image']}", "    platform: linux/arm64",
             "    restart: unless-stopped", "    stop_grace_period: 180s", "    environment:", "      TZ: Etc/UTC", "    ports:"]
    lines += [f'      - "{port}"' for port in [*ports, "127.0.0.1:8443:8443/tcp"]]
    lines.append("    volumes:")
    lines += [f"      - {crafty_root}/{folder}:/crafty/{inside}" for folder, inside in
              (("backups", "backups"), ("logs", "logs"), ("servers", "servers"), ("config", "app/config"), ("import", "import"))]
    lines += ["    logging:", "      driver: json-file", "      options:", "        max-size: 10m", '        max-file: "3"']
    return ("\n".join(lines) + "\n").encode()


def launch_command(current: str, address: str | None) -> str:
    """Crafty's launch command with the NetherNet address flag for address before -jar, or with no flag for None."""
    command = ADVERTISE.sub("", current)
    if address is None:
        return command
    try:
        public = ipaddress.IPv4Address(address).is_global
    except ValueError:
        public = False
    if command.count(" -jar ") != 1 or not public:
        raise CraftyError("the server's launch command cannot carry the NetherNet address")
    return command.replace(" -jar ", f" -DgeyserAdvertiseAddresses={address} -jar ", 1)


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
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the destination and rename on success, so a failed download leaves nothing to clean up.
    partial = Path(tempfile.mkdtemp(prefix=f".{destination.name}.partial-", dir=destination.parent))
    created = None
    try:
        (partial / "plugins").mkdir()
        for artifact in manifest["artifacts"]:
            fetch(artifact["url"], artifact["sha256"], partial / artifact["path"])
        for managed in manifest.get("managed_files", []):
            target = partial / managed["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_managed_file_bytes(managed))
        (partial / "server.properties").write_bytes(render_properties(manifest["server_properties"]))
        # Craig accepted the Minecraft EULA for this family server on issue #5. No trailing newline:
        # Crafty 4.11 compares the first line exactly and silently refuses to start otherwise.
        (partial / "eula.txt").write_text("eula=true")
        if archive is not None:
            archive = Path(archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            # The jars are already compressed, so store them; server files sit at the archive root.
            # Crafty's own account can write to this folder, so never follow or reuse a planted link.
            try:
                folder = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    descriptor = os.open(archive.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=folder)
                finally:
                    os.close(folder)
            except OSError as exc:
                raise CraftyError("the import archive could not be created as a new regular file in a real folder") from exc
            created = archive
            with os.fdopen(descriptor, "wb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as bundle:
                os.fchmod(output.fileno(), 0o660)
                for path in sorted(partial.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(partial).as_posix())
        partial.chmod(0o755)
        partial.rename(destination)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        if created is not None:
            created.unlink(missing_ok=True)
        raise
    return destination


def main(argv=None, *, api=None, host=None, manifest=MANIFEST) -> int:
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
    compose = commands.add_parser("render-compose", help="print the Compose file the pinned server content calls for")
    compose.add_argument("--manifest", type=Path, required=True)
    compose.add_argument("--data-root", type=Path, required=True)
    create = commands.add_parser("import-server", help="create the Crafty server from a staged archive in import/upload")
    create.add_argument("instance")
    create.add_argument("--archive-name", required=True)
    create.add_argument("--max-memory-gb", type=int, default=5)
    restore = commands.add_parser("restore", help="replace the stopped server folder with a verified archive from S3")
    restore.add_argument("instance")
    restore.add_argument("--key", required=True)
    allow = commands.add_parser("allowlist", help="add or remove one verified player on the running server")
    allow.add_argument("instance")
    allow.add_argument("operation", choices=("add", "remove"))
    allow.add_argument("--edition", choices=("java", "bedrock"), required=True)
    allow.add_argument("--player-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "stage-import":
            path = stage_import(load_manifest(args.manifest), args.destination, archive=args.archive)
            print(json.dumps({"staged": str(path), **({"archive": str(args.archive)} if args.archive else {})}))
            return 0
        if args.action == "render-compose":
            sys.stdout.buffer.write(render_compose(load_manifest(args.manifest), args.data_root / "crafty"))
            return 0
        config = load_config(args.config)
        if getattr(args, "instance", None) is not None and args.instance not in config["instances"]:
            raise CraftyError("instance is not configured")
        if api is None:
            token = Path(config["token_file"]).read_text().strip()
            api = CraftyAPI(config["crafty_url"], token)
        # A start applies the pinned content the launcher installed beside the adapter, and fails without it.
        adapter = Adapter(config, api, host, load_manifest(manifest) if args.action == "start" else None)
        handlers = {"start": adapter.start, "stop": adapter.stop, "status": adapter.status}
        if args.action in handlers:
            output = handlers[args.action](args.instance)
        elif args.action == "restore":
            output = adapter.restore(args.instance, args.key)
        elif args.action == "allowlist":
            output = adapter.allowlist(args.instance, args.operation, args.edition, read_player_file(args.player_file))
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
