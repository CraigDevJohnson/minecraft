#!/usr/bin/env python3
"""Operate one configured Minecraft instance on verified retained storage."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import ipaddress
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
from pathlib import Path


class OperationError(Exception):
    pass


class RefuseMetadataRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments, **keywords):
        return None


def ec2_public_ipv4() -> str:
    """Current same-port NAT advertisement; fixed IMDSv2 endpoint, never through a proxy."""
    endpoint = "http://169.254.169.254/latest"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RefuseMetadataRedirects())
    try:
        request = urllib.request.Request(endpoint + "/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with opener.open(request, timeout=5) as reply:
            token = reply.read(4097).decode("ascii")
        if not re.fullmatch(r"[\x21-\x7e]{1,4096}", token):
            raise ValueError("invalid metadata token")
        request = urllib.request.Request(endpoint + "/meta-data/public-ipv4",
            headers={"X-aws-ec2-metadata-token": token})
        with opener.open(request, timeout=5) as reply:
            address = ipaddress.IPv4Address(reply.read(65).decode("ascii").strip())
        if not address.is_global or address.is_multicast:
            raise ValueError("invalid public IPv4")
        return str(address)
    except (OSError, http.client.HTTPException, UnicodeError, ValueError) as error:
        raise OperationError("cannot verify the current EC2 public IPv4 for NetherNet; retry after host networking is ready") from error


def ensure_runtime_lock_directory(parent: Path = Path("/run")) -> Path:
    """Protect the shared lock name from replacement by non-operator accounts."""
    metadata = os.lstat(parent)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022):
        raise OperationError("runtime lock parent must be an operator-owned directory without other-account write access")
    directory = parent / "minecraft"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = os.lstat(directory)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise OperationError("runtime lock directory must be operator-owned mode 0700; inspect and repair it explicitly")
    return directory


@contextlib.contextmanager
def operation_lock(path, *, timeout=10):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error:
        raise OperationError("cannot open trusted runtime lock") from error
    with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise OperationError("runtime lock must be an operator-owned regular file without other-account write access")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OperationError("another runtime operation holds the host lock; retry later")
                time.sleep(min(0.1, timeout))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Manager:
    def __init__(self, root: Path, machine, instances: list[str]):
        self.root = root
        self.machine = machine
        self.instances = instances

    def state_path(self, instance):
        return self.root / "instances" / instance / "operation.json"

    def state(self, instance):
        path = self.state_path(instance)
        return json.loads(path.read_text()) if path.exists() else None

    @staticmethod
    def execution(container):
        if not container["exists"]:
            return None
        return {"container_id": container["container_id"], "started_at": container["started_at"]}

    def reconcile_session(self, instance, state, container):
        if container["running"] and (not state or state["phase"] not in ("starting", "running", "stopping")
                                     or state["backup"] != "pending"):
            raise OperationError("untracked container execution; preserve the prior session and investigate before operating")
        if state and state["phase"] == "starting" and not container["running"]:
            unchanged = self.execution(container) == state.get("previous_execution")
            never_started = container["exists"] and container.get("started_at", "").startswith("0001-01-01")
            if "previous_execution" in state and (unchanged or never_started):
                state = state.get("previous_state") or {
                    "phase": "prepared", "backup": "not-required", "revision": state["revision"], "session": state["session"]}
                write_json(self.state_path(instance), state)
        return state

    def status(self, instance):
        state = self.state(instance)
        container = self.machine.inspect(instance)
        state = self.reconcile_session(instance, state, container)
        return {
            "instance": instance,
            "application": "running" if container["running"] else "stopped",
            "backup": state["backup"] if state else ("pending" if container["exists"] or (
                self.root / "instances" / instance / "data").exists() else "not-required"),
        }

    def start(self, instance, *, updating=False):
        if self.draining():
            raise OperationError("host is draining; retry host stop before starting another session")
        state = self.state(instance)
        revision = self.machine.desired_revision(instance)
        container = self.machine.inspect(instance)
        state = self.reconcile_session(instance, state, container)
        if not state and (container["exists"] or (self.root / "instances" / instance / "data").exists()):
            raise OperationError("untracked instance data; investigate or restore before operating")
        if state and state["revision"] != revision and not updating:
            raise OperationError("content changed; use update after a successful backup")
        if state and state["phase"] == "prepared" and state.get("restored_from"):
            if container["exists"]:
                raise OperationError("restored data requires an isolated target without an existing container")
            self.machine.verify_restored(instance, state)
        if state and state["phase"] in ("stopping", "stopped") and state["backup"] != "complete":
            raise OperationError("previous session needs a successful backup; retry stop")
        if not container["running"]:
            if state and (state["phase"] == "running" or (state["phase"] == "starting" and container["exists"])):
                raise OperationError("instance stopped unexpectedly; clean shutdown is not proven")
            state = {"phase": "starting", "backup": "pending", "revision": revision, "session": str(uuid.uuid4()),
                     "previous_execution": self.execution(container), "previous_state": state}
            write_json(self.state_path(instance), state)
            self.machine.prepare(instance)
            self.machine.verify_storage()
            self.machine.launch(instance)
        self.machine.wait_ready(instance)
        state["phase"] = "running"
        state.pop("previous_execution", None)
        state.pop("previous_state", None)
        write_json(self.state_path(instance), state)
        return {**self.status(instance), "ready": True}

    def stop(self, instance):
        state = self.state(instance)
        container = self.machine.inspect(instance)
        state = self.reconcile_session(instance, state, container)
        if not state:
            if container["exists"] or (self.root / "instances" / instance / "data").exists():
                raise OperationError("untracked instance data; investigate or restore before operating")
            return self.status(instance)
        if state["phase"] == "prepared" and not container["running"]:
            return self.status(instance)
        if container["running"]:
            self.machine.save(instance)
            state.update(phase="stopping", backup="pending")
            write_json(self.state_path(instance), state)
            self.machine.shutdown(instance)
            container = self.machine.inspect(instance)
        if (container["running"] or state["phase"] not in ("stopping", "stopped")
                or (state["phase"] == "stopping" and not container["exists"])):
            raise OperationError("clean shutdown is not proven; investigate before backing up")
        if container["exists"] and container["exit_code"] != 0:
            raise OperationError("container did not exit cleanly")
        state["phase"] = "stopped"
        state.pop("previous_execution", None)
        state.pop("previous_state", None)
        write_json(self.state_path(instance), state)
        if state["backup"] != "complete":
            try:
                result = self.machine.backup(instance, state)
                expected = {"schema_version": 1, "status": "complete", "instance": instance,
                            "session": state["session"], "manifest_sha256": state["revision"]}
                if (not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items())
                        or not isinstance(result.get("recovery_point"), str) or not result["recovery_point"].strip()):
                    raise OperationError("backup hook returned incomplete or mismatched completion evidence")
            except (OperationError, ValueError, OSError):
                state["backup"] = "failed"
                write_json(self.state_path(instance), state)
                raise
            state.update(backup="complete", recovery_point=result["recovery_point"])
            write_json(self.state_path(instance), state)
        return self.status(instance)

    def draining(self):
        path = self.root / "host-drain.json"
        return path.exists() and json.loads(path.read_text())["boot_id"] == self.machine.boot_id()

    def execute(self, action, instance=None, *, operation=None, edition=None, player=None):
        if action not in ("host-drain", "host-status") and (
                not instance or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", instance) or instance not in self.instances):
            raise OperationError("an explicit configured instance ID is required")
        with self.machine.lock():
            self.machine.verify_storage()
            if action in ("host-drain", "host-status"):
                if action == "host-drain":
                    write_json(self.root / "host-drain.json", {"boot_id": self.machine.boot_id()})
                    self.machine.verify_inventory()
                    for configured in self.instances:
                        self.stop(configured)
                result = {"draining": self.draining(), "instances": {
                    configured: self.status(configured) for configured in self.instances}}
                # The drain response is completion evidence with a fixed shape; only status reports usage.
                return {**result, "storage": self.machine.storage_usage()} if action == "host-status" else result
            if action == "start":
                return {**self.start(instance), "storage": self.machine.storage_usage()}
            if action == "status":
                return self.status(instance)
            if action in ("stop", "backup"):
                return self.stop(instance)
            if action == "update":
                if self.draining():
                    raise OperationError("host is draining; updates are blocked")
                self.stop(instance)
                return self.start(instance, updating=True)
            if action == "allowlist":
                from minecraft_access import AccessError, change_allowlist
                if self.draining() or not self.machine.inspect(instance)["running"]:
                    raise OperationError("allowlist changes require a running instance outside host drain")
                state = self.state(instance)
                if not state or state["phase"] != "running" or state["backup"] != "pending":
                    raise OperationError("allowlist changes require an active tracked session")
                try:
                    return change_allowlist(self.machine, instance, operation, edition, player)
                except AccessError as error:
                    raise OperationError(str(error)) from error
            raise OperationError("unknown instance operation")


def command(argv, *, timeout=30, input_text=None):
    """Only the external process boundary; subprocess diagnostics can contain secrets."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            pending = memoryview((input_text or "").encode("utf-8"))
            output = bytearray()
            received = 0
            with selectors.DefaultSelector() as streams:
                for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(pipe.fileno(), False)
                    streams.register(pipe, selectors.EVENT_READ, name)
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    streams.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
                while streams.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv[0], timeout)
                    for key, _ in streams.select(remaining):
                        pipe = key.fileobj
                        if key.data == "stdin":
                            try:
                                sent = os.write(pipe.fileno(), pending[:65536])
                                pending = pending[sent:]
                            except BrokenPipeError:
                                pending = memoryview(b"")
                            except BlockingIOError:
                                continue
                            if not pending:
                                streams.unregister(pipe)
                                pipe.close()
                            continue
                        try:
                            chunk = os.read(pipe.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            streams.unregister(pipe)
                            pipe.close()
                            continue
                        received += len(chunk)
                        if received > 1024 * 1024:
                            raise OperationError("command output exceeded its limit")
                        if key.data == "stdout":
                            output.extend(chunk)
            # EOF on both pipes does not prove that the process has exited.
            process.wait(timeout=max(0, deadline - time.monotonic()))
            if process.returncode:
                raise OperationError(f"{Path(argv[0]).name} command failed (exit {process.returncode})")
            try:
                return output.decode("utf-8")
            except UnicodeDecodeError:
                raise OperationError("command returned invalid UTF-8 output") from None
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            raise
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
    except subprocess.TimeoutExpired:
        raise OperationError(f"{Path(argv[0]).name} command timed out") from None
    except OSError as error:
        raise OperationError(f"cannot execute {Path(argv[0]).name}") from error


def private_text(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise OperationError("cannot open private operator file") from error
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or
                stat.S_IMODE(metadata.st_mode) != 0o600):
            raise OperationError("operator file must be owned by the operator and a private regular file (mode 0600)")
        value = stream.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise OperationError("operator file exceeded its size limit")
        return value


def private_json(path):
    value = json.loads(private_text(path))
    if not isinstance(value, dict):
        raise OperationError("operator configuration must be a JSON object")
    return value


def verify_retained_paths(root: Path) -> None:
    """Reject submounts, including bind mounts with the retained disk's device ID."""
    mounts = json.loads(command([
        "findmnt", "--kernel", "--json", "--submounts", "--mountpoint", str(root),
        "--output", "TARGET,MAJ:MIN,FSROOT,OPTIONS",
    ])).get("filesystems", [])
    if len(mounts) != 1 or mounts[0].get("target") != str(root):
        raise OperationError("retained filesystem mount cannot be established")
    mount = mounts[0]
    if mount.get("children"):
        raise OperationError("nested mounts under retained storage are forbidden; remove the maintenance mount first")
    device = root.stat().st_dev
    if (mount.get("maj:min") != f"{os.major(device)}:{os.minor(device)}" or
            mount.get("fsroot") != "/" or "rw" not in mount.get("options", "").split(",")):
        raise OperationError("retained mount changed or is not a writable whole filesystem")
    instances = root / "instances"
    paths = [root, instances, root / "host-drain.json"]
    if instances.is_dir() and not instances.is_symlink():
        for directory in instances.iterdir():
            paths.extend((directory, directory / "data", directory / "runtime", directory / "operation.json"))
    for path in paths:
        if path.is_symlink():
            raise OperationError("instance control/data paths must not be symlinks")
        if path.exists() and path.stat().st_dev != device:
            raise OperationError("instance control/data path is outside the retained filesystem")


def verify_trusted_executable(executable: str) -> None:
    path = Path(executable)
    if not path.is_absolute() or ".." in path.parts:
        raise OperationError("backup executable requires an absolute path without parent traversal")
    for component in (*reversed(path.parents), path):
        try:
            metadata = os.lstat(component)
        except OSError as error:
            raise OperationError("cannot inspect backup executable path") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise OperationError("backup executable path must not contain symlinks")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise OperationError("backup executable and ancestors must be operator-owned without other-account write access")
        if component == path:
            if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & stat.S_IXUSR:
                raise OperationError("backup executable must be a regular file executable by its owner")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise OperationError("backup executable ancestors must be directories")


class LinuxMachine:
    """The real Linux/storage, Docker, RCON, and backup-hook boundary."""

    root = Path("/srv/minecraft")
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def __init__(self, config):
        from minecraft_content import load_definition, load_manifest
        self.config = config
        self.deployment = private_json(config["deployment"])
        self.definitions = {}
        self.manifests = {}
        for instance, filename in config["definitions"].items():
            definition = load_definition(Path(filename))
            expected = f"/srv/minecraft/instances/{instance}/data"
            deployed = self.deployment["instances"].get(instance, {})
            if definition["id"] != instance or deployed.get("data_path") != expected or deployed.get("backup_prefix") != f"instances/{instance}/":
                raise OperationError("instance definition differs from the deployment contract")
            self.definitions[instance] = definition
            self.manifests[instance] = load_manifest(Path(filename).parent / definition["manifest"])
        if not self.definitions or set(self.definitions) != set(self.deployment["instances"]):
            raise OperationError("every deployed instance must have a configured definition")
        for field, default, maximum in (("readiness_timeout", 300, 900), ("shutdown_timeout", 120, 300),
                                        ("backup_timeout", 900, 3000), ("warning_seconds", 10, 60)):
            value = config.get(field, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise OperationError(f"invalid bounded {field}")
            setattr(self, field, value)

    @contextlib.contextmanager
    def lock(self):
        if os.geteuid() != 0:
            raise OperationError("instance operations require root on the Linux host")
        with operation_lock(ensure_runtime_lock_directory() / "runtime.lock"):
            yield

    def verify_storage(self):
        command(["systemctl", "is-active", "--quiet", "minecraft-storage.service"])
        command(["systemctl", "is-active", "--quiet", "docker.service"])
        command(["/usr/local/sbin/minecraft-storage", "verify", "--volume-id", self.deployment["data_volume_id"],
                 "--filesystem-uuid", self.deployment["data_filesystem_uuid"]])
        verify_retained_paths(self.root)

    def storage_usage(self):
        """Retained-disk capacity as an unprivileged writer sees it; used_percent is df's Use%."""
        usage = os.statvfs(self.root)
        used = usage.f_blocks - usage.f_bfree
        writable = used + usage.f_bavail  # excludes the blocks ext4 reserves for root
        return {"size_bytes": usage.f_frsize * usage.f_blocks,
                "available_bytes": usage.f_frsize * usage.f_bavail,
                "used_percent": -(-100 * used // writable) if writable else 100}

    @staticmethod
    def boot_id():
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def runtime(self, instance):
        return self.root / "instances" / instance / "runtime"

    def desired_revision(self, instance):
        from minecraft_content import manifest_digest
        return manifest_digest(self.manifests[instance])

    def verify_inventory(self):
        ids = command(["docker", "ps", "-q"]).split()
        if not ids:
            return
        for container in json.loads(command(["docker", "inspect", *ids])):
            labels = container["Config"].get("Labels") or {}
            if (labels.get("com.docker.compose.service") != "minecraft" or
                    labels.get("com.docker.compose.project") not in {f"minecraft-{item}" for item in self.definitions}):
                raise OperationError("host has unconfigured running containers; refusing host drain")

    def inspect(self, instance):
        ids = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project=minecraft-{instance}",
                       "--filter", "label=com.docker.compose.service=minecraft"]).split()
        if not ids:
            return {"exists": False, "running": False, "exit_code": 0}
        if len(ids) != 1:
            raise OperationError("multiple containers found for one instance; refusing concurrent writers")
        state = json.loads(command(["docker", "inspect", "--format", "{{json .State}}", ids[0]]))
        return {"exists": True, "running": state["Running"], "exit_code": state["ExitCode"],
                "healthy": state.get("Health", {}).get("Status") == "healthy", "container_id": ids[0],
                "started_at": state["StartedAt"]}

    def prepare(self, instance):
        from minecraft_content import render_compose, stage_content
        if self.config.get("eula_accepted") is not True:
            raise OperationError("explicit private operator EULA acceptance is required")
        password = private_text(self.config["rcon_password_file"]).strip()
        public_address = self.nethernet_advertise_address()
        manifest = self.manifests[instance]
        runtime = self.runtime(instance)
        artifacts = stage_content(manifest, runtime / "content")
        command(["docker", "pull", "--platform", "linux/arm64", manifest["image"]], timeout=600)
        self.verify_storage()
        data = self.root / "instances" / instance / "data"
        compose = render_compose(self.definitions[instance], manifest, data_dir=data, artifacts_dir=artifacts,
                                 rcon_password=password, eula_accepted=True,
                                 nethernet_advertise_address=public_address)
        data.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(data, 1000, 1000)
        active = runtime / "manifest.json"
        if active.exists():
            from minecraft_content import manifest_digest
            previous = json.loads(active.read_text())
            write_json(runtime / "history" / f"{manifest_digest(previous)}.json", previous)
        write_json(active, manifest)
        write_json(runtime / "definition.json", self.definitions[instance])
        write_json(runtime / "compose.json", compose)

    def nethernet_advertise_address(self):
        return ec2_public_ipv4()

    def verify_restored(self, instance, state):
        from minecraft_content import load_definition, load_manifest, manifest_digest
        runtime = self.runtime(instance)
        manifest = load_manifest(runtime / "manifest.json")
        definition = load_definition(runtime / "definition.json")
        if (manifest_digest(manifest) != state["revision"] or manifest != self.manifests[instance]
                or definition != self.definitions[instance]):
            raise OperationError("restored data requires its exact recorded content manifest and instance definition")

    def compose(self, instance, *arguments, timeout=30):
        return command(["docker", "compose", "--project-name", f"minecraft-{instance}",
                        "--file", str(self.runtime(instance) / "compose.json"), *arguments], timeout=timeout)

    def launch(self, instance):
        self.compose(instance, "up", "-d", "--no-build", "--pull", "never", timeout=60)

    def wait_ready(self, instance):
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            container = self.inspect(instance)
            if not container["running"]:
                raise OperationError("Minecraft exited before readiness")
            if container.get("healthy"):
                output = self.compose(instance, "exec", "-T", "minecraft", "rcon-cli", "list")
                if "players online" in output:
                    return
            time.sleep(2)
        raise OperationError("Minecraft readiness timed out; inspect private container logs")

    def save(self, instance):
        self.compose(instance, "exec", "-T", "minecraft", "rcon-cli", f"say Server stopping in {self.warning_seconds} seconds.")
        time.sleep(self.warning_seconds)
        output = self.compose(instance, "exec", "-T", "minecraft", "rcon-cli", "save-all flush", timeout=60)
        if "Saved the game" not in output:
            raise OperationError("Minecraft did not confirm saving the world")

    def rcon(self, instance, text):
        return self.compose(instance, "exec", "-T", "minecraft", "rcon-cli", text)

    def read_allowlist(self, instance):
        return json.loads((self.root / "instances" / instance / "data" / "whitelist.json").read_text())

    def shutdown(self, instance):
        self.compose(instance, "exec", "-T", "minecraft", "rcon-cli", "stop")
        deadline = time.monotonic() + self.shutdown_timeout
        while time.monotonic() < deadline:
            state = self.inspect(instance)
            if state["exists"] and not state["running"]:
                if state["exit_code"] != 0:
                    raise OperationError("Minecraft did not exit cleanly")
                return
            time.sleep(1)
        raise OperationError("Minecraft shutdown timed out; no force kill or backup was attempted")

    def verify_backup_hook(self, hook):
        verify_trusted_executable(hook[0])

    def backup(self, instance, state):
        hook = self.config.get("backup_hook")
        if (not isinstance(hook, list) or not hook or not all(isinstance(arg, str) and arg for arg in hook)
                or not Path(hook[0]).is_absolute()):
            raise OperationError("required backup hook is absent or invalid")
        from minecraft_content import manifest_digest
        manifest_path = self.runtime(instance) / "manifest.json"
        if manifest_digest(json.loads(manifest_path.read_text())) != state["revision"]:
            raise OperationError("active content manifest differs from the stopped session")
        request = {"schema_version": 1, "instance": instance, "session": state["session"],
                   "data_path": str(self.root / "instances" / instance / "data"),
                   "manifest_path": str(manifest_path), "manifest_sha256": state["revision"],
                   "definition_path": str(self.runtime(instance) / "definition.json"),
                   "source": {"volume_id": self.deployment["data_volume_id"],
                              "filesystem_uuid": self.deployment["data_filesystem_uuid"]},
                   "destination": {"bucket": self.deployment["backup_bucket_name"], "prefix": f"instances/{instance}/"}}
        self.verify_backup_hook(hook)
        return json.loads(command(hook, timeout=self.backup_timeout, input_text=json.dumps(request) + "\n"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=("start", "stop", "backup", "status", "update", "host-status", "host-drain", "allowlist"))
    parser.add_argument("instance", nargs="?")
    parser.add_argument("operation", nargs="?", choices=("add", "remove"))
    parser.add_argument("--edition", choices=("java", "bedrock"))
    parser.add_argument("--player-file")
    args = parser.parse_args(argv)
    try:
        machine = LinuxMachine(private_json(args.config))
        player = private_json(args.player_file) if args.player_file else None
        result = Manager(machine.root, machine, list(machine.definitions)).execute(
            args.action, args.instance, operation=args.operation, edition=args.edition, player=player)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OperationError, OSError, ValueError, KeyError, TypeError) as error:
        # Avoid echoing malformed private values or subprocess diagnostics.
        message = str(error) if isinstance(error, OperationError) else "invalid runtime configuration or external result"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
