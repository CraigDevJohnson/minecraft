import contextlib
import datetime
import hashlib
import io
import json
import os
import socket
import threading
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_crafty as crafty  # noqa: E402

SERVER_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"


def config(root: Path) -> dict:
    return {
        "schema_version": 1, "runtime": "crafty", "crafty_url": "https://127.0.0.1:8443",
        "token_file": "/etc/minecraft/crafty-api.token", "data_root": str(root),
        "instances": {"family": {"server_name": "family", "java_port": 25565}},
        "backup": {"bucket": "family-backups", "region": "us-west-2"},
        "timeouts": {"start": 60, "stop": 60},
    }


class FakeAPI:
    def __init__(self, running=False, start_after=1, stop_after=1):
        self.is_running = running
        self.start_after = start_after
        self.stop_after = stop_after
        self.pending = None
        self.actions = []
        self.created = []
        self.no_servers = False

    def request(self, method, path, body=None):
        if path == "/api/v2/servers" and method == "POST":
            self.created.append(body)
            return {"status": "ok", "data": {"new_server_id": SERVER_ID}}
        if path == "/api/v2/servers":
            if self.no_servers:
                return {"status": "ok", "data": []}
            return {"status": "ok", "data": [{"server_id": SERVER_ID, "server_name": "family", "path": f"/crafty/servers/{SERVER_ID}"}]}
        if path.endswith("/stats"):
            if self.pending:
                action, remaining = self.pending
                if remaining <= 0:
                    self.is_running = action == "start_server"
                    self.pending = None
                else:
                    self.pending = (action, remaining - 1)
            return {"status": "ok", "data": {"running": self.is_running}}
        if "/action/" in path:
            action = path.rsplit("/", 1)[-1]
            self.actions.append(action)
            self.pending = (action, self.start_after if action == "start_server" else self.stop_after)
            return {"status": "ok"}
        raise AssertionError(path)


class FakeHost:
    def __init__(self, api):
        self.api = api
        self.clock = 0.0
        self.calls = []
        self.stored = {}
        self.metadata = {}

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def now(self):
        return datetime.datetime(2026, 10, 10, 18, 0, 0, tzinfo=datetime.timezone.utc)

    def port_open(self, port):
        return self.api.is_running

    boot = "boot-1"

    def boot_id(self):
        return self.boot

    def lock(self, path):
        return contextlib.nullcontext()

    def download(self, region, bucket, key, read):
        self.calls.append(("s3", "cp-down", key))
        read(io.BytesIO(self.stored[key]))

    def upload(self, region, bucket, key, expected_size, write):
        self.calls.append(("s3", "cp", key))
        sink = io.BytesIO()
        write(sink)
        self.stored[key] = sink.getvalue()

    def storage(self, root):
        return {"size_bytes": 100, "available_bytes": 60, "used_percent": 40}

    def aws(self, region, *arguments):
        self.calls.append(arguments)
        operation = arguments[1]
        key = arguments[arguments.index("--key") + 1] if "--key" in arguments else None
        if operation == "put-object":
            body = Path(arguments[arguments.index("--body") + 1]).read_bytes()
            self.stored[key] = body
            return {}
        if operation == "head-object":
            return {"ContentLength": len(self.stored[key]), "Metadata": self.metadata.get(key, {})}
        if operation == "get-object":
            if key not in self.stored:
                raise crafty.CraftyError("AWS get-object failed")
            Path(arguments[-1]).write_bytes(self.stored[key])
            return {}
        if operation == "list-objects-v2":
            prefix = arguments[arguments.index("--prefix") + 1]
            return {"Contents": [{"Key": key} for key in self.stored if key.startswith(prefix)]}
        if operation == "delete-object":
            self.stored.pop(key, None)
            return {}
        raise AssertionError(operation)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        server_dir = self.root / "crafty" / "servers" / SERVER_ID
        (server_dir / "survival").mkdir(parents=True)
        (server_dir / "survival" / "level.dat").write_bytes(b"world")

    def tearDown(self):
        self.temporary.cleanup()

    def adapter(self, api):
        host = FakeHost(api)
        return crafty.Adapter(config(self.root), api, host), host

    def test_start_waits_for_crafty_and_the_java_port(self):
        api = FakeAPI()
        adapter, _ = self.adapter(api)
        result = adapter.start("family")
        self.assertEqual(api.actions, ["start_server"])
        self.assertEqual(result["application"], "running")
        self.assertTrue(result["ready"])
        self.assertEqual(result["backup"], "pending")
        self.assertEqual(result["storage"]["used_percent"], 40)

    def test_start_waits_for_crafty_to_boot(self):
        api = FakeAPI()
        original, attempts = api.request, []

        def request(method, path):
            attempts.append(path)
            if len(attempts) < 3:
                raise crafty.CraftyError("Crafty API GET request failed")
            return original(method, path)

        api.request = request
        adapter, _ = self.adapter(api)
        self.assertTrue(adapter.start("family")["ready"])

    def test_start_of_a_running_server_sends_no_action(self):
        api = FakeAPI(running=True)
        adapter, _ = self.adapter(api)
        adapter.start("family")
        self.assertEqual(api.actions, [])

    def test_start_times_out_without_reporting_ready(self):
        api = FakeAPI(start_after=10_000)
        adapter, _ = self.adapter(api)
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")

    def test_drain_stops_then_stores_a_verified_archive(self):
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        result = adapter.host_drain()
        self.assertEqual(api.actions, ["stop_server"])
        self.assertEqual(result, {"draining": True, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "complete"}}})
        key = "instances/family/crafty/20261010T180000Z.tar.gz"
        self.assertIn(("s3", "cp", key), host.calls)
        digest = hashlib.sha256(host.stored[key]).hexdigest()
        self.assertEqual(host.stored[f"{key}.sha256"], f"{digest}  20261010T180000Z.tar.gz\n".encode())
        put = next(call for call in host.calls if call[1] == "put-object")
        self.assertIn("--if-none-match", put)
        state = json.loads((self.root / "crafty" / "state" / "family.json").read_text())
        self.assertEqual((state["key"], state["sha256"]), (key, digest))
        self.assertFalse(any((self.root / "crafty" / "staging").iterdir()))
        with tarfile.open(fileobj=io.BytesIO(host.stored[key])) as bundle:
            self.assertEqual(bundle.extractfile("family/survival/level.dat").read(), b"world")

    def test_drain_blocks_starts_until_the_host_reboots(self):
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()
        self.assertTrue(adapter.host_status()["draining"])
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual(api.actions, ["stop_server"])
        host.boot = "boot-2"
        self.assertFalse(adapter.host_status()["draining"])
        self.assertTrue(adapter.start("family")["ready"])

    def test_shutdown_drain_after_an_operator_stop_does_not_archive_again(self):
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()
        stored = dict(host.stored)
        result = adapter.host_drain()
        self.assertEqual(host.stored, stored)
        self.assertEqual(result["instances"]["family"], {"instance": "family", "application": "stopped", "backup": "complete"})

    def played_server(self):
        folder = self.root / "crafty" / "servers" / SERVER_ID
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "level.dat").write_bytes(b"first session")
        return folder

    def test_shutdown_drain_archives_again_after_play_from_the_crafty_ui(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()
        # The EC2 stop failed, and the admin then played and stopped from Crafty's own UI.
        (folder / "level.dat").write_bytes(b"second session")
        host.now = lambda: datetime.datetime(2026, 10, 10, 19, 0, 0, tzinfo=datetime.timezone.utc)
        adapter.host_drain()
        self.assertEqual(sum(call[:2] == ("s3", "cp") for call in host.calls), 2)

    def test_restore_replaces_the_server_folder_with_a_verified_archive(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()
        key = "instances/family/crafty/20261010T180000Z.tar.gz"
        (folder / "level.dat").write_bytes(b"corrupted")
        (folder / "stray.txt").write_bytes(b"x")
        result = adapter.restore("family", key)
        self.assertEqual((folder / "level.dat").read_bytes(), b"first session")
        self.assertFalse((folder / "stray.txt").exists())
        self.assertEqual(result["restored"], key)
        self.assertEqual((Path(result["replaced"]) / "level.dat").read_bytes(), b"corrupted")

    def test_restore_reads_the_checksum_older_archives_keep_in_metadata(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()
        key = "instances/family/crafty/20261010T180000Z.tar.gz"
        host.metadata[key] = {"sha256": host.stored.pop(key + ".sha256").split()[0].decode()}
        (folder / "level.dat").write_bytes(b"corrupted")
        adapter.restore("family", key)
        self.assertEqual((folder / "level.dat").read_bytes(), b"first session")

    def test_restore_refuses_a_running_server_a_bad_checksum_or_a_foreign_key(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        key = "instances/family/crafty/20261010T180000Z.tar.gz"
        with self.assertRaises(crafty.CraftyError):
            adapter.restore("family", key)  # still running
        adapter.host_drain()
        (folder / "level.dat").write_bytes(b"current")
        with self.assertRaises(crafty.CraftyError):
            adapter.restore("family", "instances/other/crafty/20261010T180000Z.tar.gz")
        host.stored[key + ".sha256"] = b"0" * 64 + b"  x\n"
        with self.assertRaises(crafty.CraftyError):
            adapter.restore("family", key)
        self.assertEqual((folder / "level.dat").read_bytes(), b"current")
        self.assertEqual(list((self.root / "crafty" / "staging").glob("restore-*")), [])

    def test_retried_drain_archives_a_server_that_has_no_recorded_state(self):
        self.played_server()
        api = FakeAPI()  # already stopped: the first drain was interrupted after Crafty's UI session
        adapter, host = self.adapter(api)
        crafty.write_durably(self.root / "crafty" / "state" / "host-drain.json", {"boot_id": "boot-1"})
        self.assertEqual(adapter.host_drain()["instances"]["family"]["backup"], "complete")
        self.assertEqual(sum(call[:2] == ("s3", "cp") for call in host.calls), 1)

    def test_an_upload_that_cannot_be_verified_is_removed_from_s3(self):
        self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        original = host.aws

        def aws(region, *arguments):
            if arguments[1] == "put-object":
                raise crafty.CraftyError("AWS put-object failed")
            return original(region, *arguments)

        host.aws = aws
        with self.assertRaises(crafty.CraftyError):
            adapter.host_drain()
        self.assertEqual(host.stored, {})
        self.assertEqual(adapter.backup_state("family"), "failed")

    def test_prune_counts_only_archives_with_a_checksum(self):
        adapter, host = self.adapter(FakeAPI())
        prefix = "instances/family/crafty/"
        verified = [f"{prefix}202610{day:02d}T000000Z.tar.gz" for day in range(1, 16)]
        orphans = [f"{prefix}20261020T000000Z.tar.gz", f"{prefix}20261021T000000Z.tar.gz"]
        for key in verified:
            host.stored[key], host.stored[key + ".sha256"] = b"a", b"b"
        for key in orphans:
            host.stored[key] = b"a"
        adapter.prune({"bucket": "family-backups", "region": "us-west-2"}, prefix)
        kept = sorted(key for key in host.stored if key.endswith(".tar.gz") and key + ".sha256" in host.stored)
        self.assertEqual(kept, verified[1:])
        self.assertNotIn(verified[0], host.stored)

    def test_restore_puts_the_original_folder_back_when_the_swap_fails(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()
        (folder / "level.dat").write_bytes(b"current")
        real, failed = Path.rename, []

        def rename(path, target):
            if Path(target) == folder and not failed:
                failed.append(path)
                raise OSError("disk error")
            return real(path, target)

        with unittest.mock.patch.object(Path, "rename", rename):
            with self.assertRaises(OSError):
                adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual((folder / "level.dat").read_bytes(), b"current")

    def test_failed_prune_keeps_the_verified_backup(self):
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        original = host.aws

        def aws(region, *arguments):
            if arguments[1] == "list-objects-v2" and arguments[arguments.index("--prefix") + 1].endswith("/"):
                raise crafty.CraftyError("AWS list-objects-v2 failed")
            return original(region, *arguments)

        host.aws = aws
        with contextlib.redirect_stderr(io.StringIO()):
            result = adapter.host_drain()
        self.assertEqual(result["instances"]["family"]["backup"], "complete")
        self.assertEqual(adapter.backup_state("family"), "complete")

    def test_failed_upload_records_a_failed_backup(self):
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        host.aws = lambda region, *arguments: (_ for _ in ()).throw(crafty.CraftyError("AWS put-object failed"))
        with self.assertRaises(crafty.CraftyError):
            adapter.host_drain()
        self.assertEqual(adapter.backup_state("family"), "failed")
        self.assertEqual(adapter.status("family")["backup"], "failed")

    def test_server_that_never_stops_is_not_archived(self):
        api = FakeAPI(running=True, stop_after=10_000)
        adapter, host = self.adapter(api)
        with self.assertRaises(crafty.CraftyError):
            adapter.host_drain()
        self.assertFalse(host.stored)

    def test_prune_keeps_the_newest_archives(self):
        api = FakeAPI()
        adapter, host = self.adapter(api)
        prefix = "instances/family/crafty/"
        for day in range(1, 20):
            host.stored[f"{prefix}202609{day:02d}T000000Z.tar.gz"] = b"x"
            host.stored[f"{prefix}202609{day:02d}T000000Z.tar.gz.sha256"] = b"x"
        host.stored[f"{prefix}notes.txt"] = b"keep"
        adapter.prune(config(self.root)["backup"], prefix)
        kept = sorted(host.stored)
        self.assertEqual(len(kept), 2 * crafty.KEEP_ARCHIVES + 1)
        self.assertNotIn(f"{prefix}20260901T000000Z.tar.gz.sha256", kept)
        self.assertIn(f"{prefix}notes.txt", kept)
        self.assertIn(f"{prefix}20260919T000000Z.tar.gz", kept)
        self.assertNotIn(f"{prefix}20260901T000000Z.tar.gz", kept)

    def test_unexpected_server_path_is_refused(self):
        adapter, _ = self.adapter(FakeAPI())
        with self.assertRaises(crafty.CraftyError):
            adapter.server_dir({"path": "/crafty/servers/../../etc"})
        with self.assertRaises(crafty.CraftyError):
            adapter.server_dir({"path": "/srv/elsewhere"})
        with self.assertRaises(crafty.CraftyError):
            adapter.server_dir({"path": "/crafty/servers//etc"})

    def test_host_status_matches_the_workstation_contract(self):
        api = FakeAPI()
        adapter, _ = self.adapter(api)
        result = adapter.host_status()
        self.assertEqual(result["draining"], False)
        self.assertEqual(result["instances"]["family"], {"instance": "family", "application": "stopped", "backup": "not-required", "ready": False})


class HostTests(unittest.TestCase):
    def test_lifecycle_lock_refuses_a_second_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "lifecycle.lock"
            host = crafty.Host()
            with host.lock(path):
                with self.assertRaises(crafty.CraftyError):
                    with host.lock(path):
                        pass
            with host.lock(path):
                pass

    def test_upload_streams_to_the_aws_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "aws"
            received = Path(directory) / "received"
            fake.write_text(f"#!/bin/sh\necho \"$@\" > {received}.args\ncat > {received}\n")
            fake.chmod(0o755)
            with unittest.mock.patch.dict(os.environ, {"PATH": f"{directory}:{os.environ['PATH']}"}):
                crafty.Host().upload("us-west-2", "bucket", "a/b.tar.gz", 5, lambda sink: sink.write(b"hello"))
            self.assertEqual(received.read_bytes(), b"hello")
            self.assertIn("s3 cp - s3://bucket/a/b.tar.gz", Path(f"{received}.args").read_text())


class ConfigTests(unittest.TestCase):
    def write(self, value):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(value, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_valid_configuration_loads(self):
        self.assertEqual(crafty.load_config(self.write(config(Path("/srv/minecraft"))))["runtime"], "crafty")

    def test_only_loopback_api_is_accepted(self):
        for url in ("https://10.0.0.5:8443", "http://127.0.0.1:8443", "https://localhost:8443"):
            value = config(Path("/srv/minecraft"))
            value["crafty_url"] = url
            with self.subTest(url=url), self.assertRaises(crafty.CraftyError):
                crafty.load_config(self.write(value))

    def test_cli_reports_errors_as_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = crafty.main(["--config", "/nonexistent/operator.json", "host-status"])
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(output.getvalue()))

    def test_cli_runs_host_status_through_the_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(config(Path(directory)))
            api = FakeAPI()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = crafty.main(["--config", str(path), "host-status"], api=api, host=FakeHost(api))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["instances"]["family"]["application"], "stopped")


class ManifestTests(unittest.TestCase):
    def test_repository_manifest_is_valid_and_pinned(self):
        manifest = crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json")
        names = {artifact["name"] for artifact in manifest["artifacts"]}
        self.assertTrue({"paper", "geyser", "floodgate", "viaversion", "multiverse-core", "multiverse-inventories",
                         "multiverse-portals", "multiverse-netherportals"} <= names)
        self.assertTrue(manifest["server_properties"]["white-list"])
        self.assertTrue(manifest["server_properties"]["online-mode"])

    def test_managed_file_outside_the_server_folder_is_refused(self):
        manifest = json.loads((ROOT / "runtime" / "crafty" / "server.json").read_text())
        manifest["managed_files"][0]["path"] = "../../etc/cron.d/x"
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(manifest, handle)
            handle.flush()
            with self.assertRaises(crafty.CraftyError):
                crafty.load_manifest(Path(handle.name))

    def test_unpinned_image_is_refused(self):
        manifest = json.loads((ROOT / "runtime" / "crafty" / "server.json").read_text())
        manifest["crafty"]["image"] = "registry.gitlab.com/crafty-controller/crafty-4:latest"
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(manifest, handle)
            handle.flush()
            with self.assertRaises(crafty.CraftyError):
                crafty.load_manifest(Path(handle.name))

    def test_stage_import_writes_verified_files_and_refuses_reuse(self):
        manifest = crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json")
        fetched = []

        def fetch(url, sha256, target):
            fetched.append(url)
            target.write_bytes(b"jar")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "family"
            crafty.stage_import(manifest, destination, fetch=fetch)
            self.assertEqual(len(fetched), len(manifest["artifacts"]))
            # Crafty 4.11 compares the first line exactly, so a trailing newline blocks every start.
            self.assertEqual((destination / "eula.txt").read_text(), "eula=true")
            properties = (destination / "server.properties").read_text()
            self.assertIn("white-list=true\n", properties)
            self.assertIn("level-name=survival\n", properties)
            self.assertTrue((destination / "plugins" / "Geyser-Spigot" / "config.yml").is_file())
            with self.assertRaises(crafty.CraftyError):
                crafty.stage_import(manifest, destination, fetch=fetch)

    def test_failed_stage_import_leaves_nothing_behind(self):
        manifest = crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json")

        def fetch(url, sha256, target):
            raise crafty.CraftyError("download failed")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "family"
            with self.assertRaises(crafty.CraftyError):
                crafty.stage_import(manifest, destination, fetch=fetch)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_stage_import_archive_holds_the_server_files_at_its_root(self):
        manifest = crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "family"
            archive = Path(directory) / "upload" / "family.zip"
            crafty.stage_import(manifest, destination, fetch=lambda url, sha256, target: target.write_bytes(b"jar"),
                                archive=archive)
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
            self.assertLessEqual({"paper.jar", "eula.txt", "server.properties", "plugins/Geyser-Spigot/config.yml"}, names)

    def test_stage_import_refuses_a_planted_archive_link(self):
        manifest = crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json")
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "family.zip"
            archive.symlink_to(Path(directory) / "elsewhere")  # dangling, so exists() alone would miss it
            with self.assertRaises(crafty.CraftyError):
                crafty.stage_import(manifest, Path(directory) / "family", archive=archive,
                                    fetch=lambda url, sha256, target: target.write_bytes(b"jar"))
            self.assertFalse((Path(directory) / "elsewhere").exists())

    def test_stage_import_refuses_a_linked_archive_folder(self):
        manifest = crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json")
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "elsewhere").mkdir()
            (Path(directory) / "upload").symlink_to(Path(directory) / "elsewhere")
            with self.assertRaises(crafty.CraftyError):
                crafty.stage_import(manifest, Path(directory) / "family", archive=Path(directory) / "upload" / "family.zip",
                                    fetch=lambda url, sha256, target: target.write_bytes(b"jar"))
            self.assertEqual(list((Path(directory) / "elsewhere").iterdir()), [])

    def test_import_server_creates_one_server_from_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            api.no_servers = True
            adapter = crafty.Adapter(config(Path(directory)), api, FakeHost(api))
            self.assertEqual(adapter.import_server("family", "family.zip", 5), {"instance": "family", "server_id": SERVER_ID})
            (body,) = api.created
            self.assertEqual(body["name"], "family")
            imported = body["minecraft_java_create_data"]["import_server_create_data"]
            self.assertEqual((imported["archive_name"], imported["jarfile"], imported["mem_max"], imported["server_properties_port"]),
                             ("family.zip", "paper.jar", 5, 25565))

    def test_import_server_refuses_an_existing_server_or_a_path(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            adapter = crafty.Adapter(config(Path(directory)), api, FakeHost(api))
            with self.assertRaises(crafty.CraftyError):
                adapter.import_server("family", "family.zip", 5)
            api.no_servers = True
            with self.assertRaises(crafty.CraftyError):
                adapter.import_server("family", "../family.zip", 5)
            self.assertEqual(api.created, [])


class HostPortTests(unittest.TestCase):
    def answer(self, reply: bytes) -> bool:
        with socket.create_server(("127.0.0.1", 0)) as listener:
            def serve():
                connection, _ = listener.accept()
                with connection:
                    connection.recv(16)
                    connection.sendall(reply)
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                return crafty.Host().port_open(listener.getsockname()[1])
            finally:
                thread.join()

    def test_a_server_that_answers_the_ping_is_open(self):
        self.assertTrue(self.answer(b"\xff\x00"))

    def test_a_proxy_that_accepts_and_closes_is_not_open(self):
        self.assertFalse(self.answer(b""))


if __name__ == "__main__":
    unittest.main()
