import contextlib
import datetime
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
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

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def now(self):
        return datetime.datetime(2026, 10, 10, 18, 0, 0, tzinfo=datetime.timezone.utc)

    def port_open(self, port):
        return self.api.is_running

    def boot_id(self):
        return "boot-1"

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
            body = self.stored[key]
            return {"ContentLength": len(body), "Metadata": {"sha256": hashlib.sha256(body).hexdigest()}}
        if operation == "list-objects-v2":
            return {"Contents": [{"Key": key} for key in self.stored]}
        if operation == "delete-object":
            del self.stored[key]
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
        self.assertIn(key, host.stored)
        put = next(call for call in host.calls if call[1] == "put-object")
        self.assertIn("--if-none-match", put)
        self.assertEqual(json.loads((self.root / "crafty" / "state" / "family.json").read_text())["key"], key)
        self.assertFalse(any((self.root / "crafty" / "staging").iterdir()))

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
        host.stored[f"{prefix}notes.txt"] = b"keep"
        adapter.prune(config(self.root)["backup"], prefix)
        kept = sorted(host.stored)
        self.assertEqual(len(kept), crafty.KEEP_ARCHIVES + 1)
        self.assertIn(f"{prefix}notes.txt", kept)
        self.assertIn(f"{prefix}20260919T000000Z.tar.gz", kept)
        self.assertNotIn(f"{prefix}20260901T000000Z.tar.gz", kept)

    def test_unexpected_server_path_is_refused(self):
        adapter, _ = self.adapter(FakeAPI())
        with self.assertRaises(crafty.CraftyError):
            adapter.server_dir({"path": "/crafty/servers/../../etc"})
        with self.assertRaises(crafty.CraftyError):
            adapter.server_dir({"path": "/srv/elsewhere"})

    def test_host_status_matches_the_workstation_contract(self):
        api = FakeAPI()
        adapter, _ = self.adapter(api)
        result = adapter.host_status()
        self.assertEqual(result["draining"], False)
        self.assertEqual(result["instances"]["family"], {"instance": "family", "application": "stopped", "backup": "not-required", "ready": False})


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


if __name__ == "__main__":
    unittest.main()
