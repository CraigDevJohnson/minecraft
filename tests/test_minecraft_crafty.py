import contextlib
import datetime
import errno
import hashlib
import http.client
import io
import json
import os
import socket
import stat
import subprocess
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
        self.console = []
        self.on_console = None
        self.command = "java -Xms1024M -Xmx5120M -jar paper.jar nogui"
        self.patches = []
        self.keeps_patches = True
        self.log = []

    def request(self, method, path, body=None, text=None):
        if path.endswith("/stdin"):
            if not self.is_running:
                raise crafty.CraftyError("Crafty API POST request was refused")
            self.console.append((path.split("/")[-2], text))
            if self.on_console:
                self.on_console(text)
            return {"status": "ok"}
        if path == "/api/v2/servers" and method == "POST":
            self.created.append(body)
            self.no_servers = False
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
            self.log.append(action)
            self.pending = (action, self.start_after if action == "start_server" else self.stop_after)
            return {"status": "ok"}
        if path == f"/api/v2/servers/{SERVER_ID}" and method == "GET":
            return {"status": "ok", "data": {"server_id": SERVER_ID, "execution_command": self.command}}
        if path == f"/api/v2/servers/{SERVER_ID}" and method == "PATCH":
            self.patches.append(body)
            self.log.append("patch")
            if self.keeps_patches:
                self.command = body["execution_command"]
            return {"status": "ok"}
        raise AssertionError(path)


def member(name: str, kind=tarfile.REGTYPE, data: bytes = b"", linkname: str = "") -> tuple:
    """One archive member and its content, for archives a stop would never write."""
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, linkname, 0o755 if kind == tarfile.DIRTYPE else 0o644
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data if kind == tarfile.REGTYPE else None


class FakeHost:
    def __init__(self, api):
        self.api = api
        self.clock = 0.0
        self.calls = []
        self.stored = {}
        self.metadata = {}
        self.compose = None
        self.compose_failures = 0
        self.on_compose_up = None
        self.address = "34.212.10.20"
        self.artifacts = {}
        self.failing_downloads = set()
        self.working = set()  # (st_dev, st_ino) of the folders a game works in

    def works_in(self, folder):
        if self.working is None:
            raise OSError("the process table could not be read")
        info = os.stat(folder, follow_symlinks=False)
        return (info.st_dev, info.st_ino) in self.working

    def read_compose(self):
        return self.compose.read_bytes() if self.compose.exists() else None

    def write_compose(self, content):
        self.calls.append(("compose", "write"))
        self.compose.write_bytes(content)

    def compose_up(self):
        self.calls.append(("compose", "up"))
        if self.compose_failures:
            self.compose_failures -= 1
            raise crafty.CraftyError("docker compose did not bring Crafty up")
        if self.on_compose_up:
            self.on_compose_up()

    def public_ipv4(self):
        self.calls.append(("imds",))
        if self.address is None:
            raise crafty.CraftyError("cannot read the host's public IPv4 for NetherNet")
        return self.address

    def fetch(self, url, sha256, target):
        self.calls.append(("fetch", url))
        data = self.artifacts[url]
        if url in self.failing_downloads or hashlib.sha256(data).hexdigest() != sha256:
            raise crafty.CraftyError("download failed")
        target.write_bytes(data)

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

    def archived(self, adapter, host):
        """Stop the running server, storing its archive, then start the host again, as before a launcher restore."""
        adapter.host_drain()
        host.boot = "boot-2"

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
        self.archived(adapter, host)
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
        self.archived(adapter, host)
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
        with self.assertRaisesRegex(crafty.CraftyError, "stop the server"):
            adapter.restore("family", key)  # still running
        self.archived(adapter, host)
        (folder / "level.dat").write_bytes(b"current")
        with self.assertRaisesRegex(crafty.CraftyError, "this instance's archives"):
            adapter.restore("family", "instances/other/crafty/20261010T180000Z.tar.gz")
        host.stored[key + ".sha256"] = b"0" * 64 + b"  x\n"
        with self.assertRaisesRegex(crafty.CraftyError, "does not match its recorded SHA-256"):
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

    def test_restore_refuses_to_swap_under_a_server_started_meanwhile(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        (folder / "level.dat").write_bytes(b"current")
        download = host.download

        def download_then_started(*arguments):
            download(*arguments)
            api.is_running = True

        host.download = download_then_started
        with self.assertRaisesRegex(crafty.CraftyError, "started during the restore"):
            adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual((folder / "level.dat").read_bytes(), b"current")

    def test_restore_puts_the_original_folder_back_when_the_swap_fails(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
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

    def test_restore_waits_for_crafty_to_boot(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        (folder / "level.dat").write_bytes(b"corrupted")
        original, attempts = api.request, []

        def request(method, path, *rest):
            attempts.append(path)
            if len(attempts) < 3:
                raise crafty.CraftyError("Crafty API GET request failed")
            return original(method, path, *rest)

        api.request = request
        started = host.clock
        adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual((folder / "level.dat").read_bytes(), b"first session")
        self.assertEqual(host.clock - started, 10)

    def test_restore_refuses_while_the_host_is_draining(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        adapter.host_drain()  # the same boot: the launcher has not stopped the host yet
        (folder / "level.dat").write_bytes(b"current")
        key = "instances/family/crafty/20261010T180000Z.tar.gz"
        with self.assertRaisesRegex(crafty.CraftyError, "^host is draining; finish stopping the host before restoring$"):
            adapter.restore("family", key)
        self.assertEqual((folder / "level.dat").read_bytes(), b"current")
        self.assertNotIn(("s3", "cp-down", key), host.calls)

    def test_restore_removes_earlier_leftovers_and_nothing_else(self):
        self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        staging = self.root / "crafty" / "staging"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "level.dat").write_bytes(b"kept")
        for name in ("archived-family-20261001T000000Z", "archived-family-20261002T000000Z", "restore-abc_1234"):
            (staging / name / "world").mkdir(parents=True)
            (staging / name / "world" / "level.dat").write_bytes(b"old")
        # Another instance's folder, other work, folders named by hand, a file and a link are never removed, nor
        # anything a link points to. Nor is a replaced-* folder an earlier adapter left without checking its world.
        kept = ["content-abc12345", "archived-family-two-20261001T000000Z", "archived-family-20261003T000000Z", "restore-notes",
                "restore-old", "restore-old-copy", "restore-abc123456", "replaced-family-20261001T000000Z"]
        (staging / kept[0]).mkdir()
        (staging / kept[1]).mkdir()
        (staging / kept[2]).symlink_to(outside)
        (staging / kept[3]).write_bytes(b"a file")
        for name in kept[4:]:
            (staging / name).mkdir()
            (staging / name / "level.dat").write_bytes(b"kept")
        result = adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual(set(result), {"instance", "restored", "replaced"})
        self.assertEqual(result["replaced"], str(staging / "archived-family-20261010T180000Z"))
        self.assertEqual(sorted(path.name for path in staging.iterdir()), sorted([*kept, "archived-family-20261010T180000Z"]))
        self.assertEqual((outside / "level.dat").read_bytes(), b"kept")

    def test_restore_keeps_a_replaced_world_that_no_archive_holds(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        staging = self.root / "crafty" / "staging"
        # Played again, then the host went down without a drain: this world is in no archive.
        (folder / "level.dat").write_bytes(b"third session, never archived")
        host.now = lambda: datetime.datetime(2026, 10, 11, 8, 0, tzinfo=datetime.timezone.utc)
        first = adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual(first["replaced"], str(staging / "unarchived-family-20261011T080000Z"))
        # The restored world is its archive's, so the next restore may remove the folder it replaces, but never
        # the unarchived one.
        host.now = lambda: datetime.datetime(2026, 10, 11, 8, 30, tzinfo=datetime.timezone.utc)
        second = adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual(second["replaced"], str(staging / "archived-family-20261011T083000Z"))
        host.now = lambda: datetime.datetime(2026, 10, 11, 9, 0, tzinfo=datetime.timezone.utc)
        adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual(sorted(path.name for path in staging.iterdir()),
                         ["archived-family-20261011T090000Z", "unarchived-family-20261011T080000Z"])
        self.assertEqual((staging / "unarchived-family-20261011T080000Z" / "level.dat").read_bytes(), b"third session, never archived")

    def test_restore_keeps_a_world_played_from_crafty_while_the_archive_downloaded(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        staging = self.root / "crafty" / "staging"
        download = host.download

        def played_meanwhile(*arguments):
            # Started, played and stopped from Crafty's own UI while the archive streamed: stopped again at the swap.
            (folder / "level.dat").write_bytes(b"played during the download")
            download(*arguments)

        host.download = played_meanwhile
        host.now = lambda: datetime.datetime(2026, 10, 11, 8, 0, tzinfo=datetime.timezone.utc)
        first = adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
        self.assertEqual(first["replaced"], str(staging / "unarchived-family-20261011T080000Z"))
        self.assertEqual((staging / "unarchived-family-20261011T080000Z" / "level.dat").read_bytes(), b"played during the download")

    def test_restore_keeps_the_folder_it_moved_aside_when_crafty_starts_the_server_at_the_swap(self):
        # Started from Crafty's page just after the restore's last look. Crafty reports it at once, or only after its
        # half-second CPU sample, when only the host sees the game in its folder; or the look cannot be answered.
        for case in ("Crafty reports it", "only the host sees it", "the look fails", "no archive held the world"):
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                folder = self.played_server()
                api = FakeAPI(running=True)
                adapter, host = self.adapter(api)
                self.archived(adapter, host)
                if case == "no archive held the world":
                    (folder / "level.dat").write_bytes(b"played since")
                staging = self.root / "crafty" / "staging"

                def started_at_the_swap():
                    if case == "Crafty reports it":
                        api.is_running = True
                    elif case == "the look fails":
                        host.working = None
                    else:
                        info = folder.stat()
                        host.working.add((info.st_dev, info.st_ino))
                    return datetime.datetime(2026, 10, 11, 8, 0, tzinfo=datetime.timezone.utc)

                host.now = started_at_the_swap
                with self.assertRaises(crafty.CraftyError) as caught:
                    adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
                self.assertEqual(str(caught.exception), "the server started as the restore swapped folders; kept "
                                 "staging/unarchived-family-20261011T080000Z; stop it, check which world it played, restore again")
                kept = staging / "unarchived-family-20261011T080000Z"
                self.assertEqual(sorted(path.name for path in staging.iterdir()), [kept.name])
                played = b"played since" if case == "no archive held the world" else b"first session"
                self.assertEqual((kept / "level.dat").read_bytes(), played)
                self.assertTrue((folder / "level.dat").exists())
                # Stopped and restored again, that folder stays.
                api.is_running, host.working = False, set()
                host.now = lambda: datetime.datetime(2026, 10, 11, 9, 0, tzinfo=datetime.timezone.utc)
                adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
                self.assertEqual((kept / "level.dat").read_bytes(), played)

    def test_restore_refuses_a_game_crafty_has_not_reported_yet(self):
        # Started from Crafty's page within its half-second CPU sample of the last look, or while the restore read
        # the world: refused before the swap, with the world left in place.
        for case in ("only the host sees it", "Crafty reports it while the world is read"):
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                folder = self.played_server()
                api = FakeAPI(running=True)
                adapter, host = self.adapter(api)
                self.archived(adapter, host)
                if case == "only the host sees it":
                    info = folder.stat()
                    host.working.add((info.st_dev, info.st_ino))
                else:
                    unchanged = adapter.unchanged

                    def started_meanwhile(instance):
                        api.is_running = True
                        return unchanged(instance)

                    adapter.unchanged = started_meanwhile
                with self.assertRaises(crafty.CraftyError) as caught:
                    adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
                self.assertEqual(str(caught.exception), "the server was started during the restore; stop it and restore again")
                self.assertEqual((folder / "level.dat").read_bytes(), b"first session")
                self.assertEqual(list((self.root / "crafty" / "staging").iterdir()), [])

    def test_the_swap_failure_names_its_folder_within_the_launcher_s_limit(self):
        # The launcher drops an adapter reason over 200 bytes; instance names have at most 32 characters.
        name = "unarchived-" + "x" * 32 + "-20261011T080000Z"
        reason = f"the server started as the restore swapped folders; kept staging/{name}; stop it, check which world it played, restore again"
        self.assertLessEqual(len(json.dumps(reason)) - 2, 200)
        self.assertTrue(crafty.INSTANCE_ID.fullmatch("x" * 32))

    def test_restore_keeps_a_world_played_before_the_adapter_recorded_anything(self):
        # A fresh disk whose server was played from Crafty's own UI before the adapter's first start: no state
        # proves that play is in S3.
        folder = self.played_server()
        adapter, host = self.adapter(FakeAPI())
        self.assertEqual(adapter.backup_state("family"), "not-required")
        staging = self.root / "crafty" / "staging"
        key = self.stored_archive(host, [member("family", tarfile.DIRTYPE), member("family/level.dat", data=b"restored")])
        host.now = lambda: datetime.datetime(2026, 10, 12, 8, 0, tzinfo=datetime.timezone.utc)
        first = adapter.restore("family", key)
        self.assertEqual(first["replaced"], str(staging / "unarchived-family-20261012T080000Z"))
        host.now = lambda: datetime.datetime(2026, 10, 12, 8, 30, tzinfo=datetime.timezone.utc)
        adapter.restore("family", key)
        self.assertEqual(sorted(path.name for path in staging.iterdir()),
                         ["archived-family-20261012T083000Z", "unarchived-family-20261012T080000Z"])
        self.assertEqual((staging / "unarchived-family-20261012T080000Z" / "level.dat").read_bytes(), b"first session")
        self.assertEqual((folder / "level.dat").read_bytes(), b"restored")

    def stored_archive(self, host, members) -> str:
        """Store an archive of members with its checksum, as a stop stores its own."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            for info, data in members:
                bundle.addfile(info, None if data is None else io.BytesIO(data))
        key = "instances/family/crafty/20261011T180000Z.tar.gz"
        host.stored[key] = buffer.getvalue()
        host.stored[key + ".sha256"] = f"{hashlib.sha256(buffer.getvalue()).hexdigest()}  20261011T180000Z.tar.gz\n".encode()
        return key

    def test_restore_refuses_special_files_and_links_out_of_the_folder(self):
        folder = self.played_server()
        adapter, host = self.adapter(FakeAPI())
        staging = self.root / "crafty" / "staging"
        base = [member("family", tarfile.DIRTYPE), member("family/level.dat", data=b"restored")]
        refused = {
            "a FIFO": member("family/pipe", tarfile.FIFOTYPE),
            "a character device": member("family/null", tarfile.CHRTYPE),
            "a block device": member("family/disk", tarfile.BLKTYPE),
            "a link up and out": member("family/escape", tarfile.SYMTYPE, linkname="../../../outside"),
            "an absolute link": member("family/passwd", tarfile.SYMTYPE, linkname="/etc/passwd"),
            "a hard link out": member("family/hard", tarfile.LNKTYPE, linkname="../outside"),
            "an absolute hard link": member("family/shadow", tarfile.LNKTYPE, linkname="/etc/shadow"),
        }
        for case, bad in refused.items():
            with self.subTest(case=case):
                key = self.stored_archive(host, [*base, bad])
                with self.assertRaisesRegex(crafty.CraftyError, "^the archive could not be unpacked$"):
                    adapter.restore("family", key)
                self.assertEqual((folder / "level.dat").read_bytes(), b"first session")
                self.assertEqual(list(staging.iterdir()), [])
        # Links that stay inside the folder are the server's own and come back, with the owners and modes stored.
        key = self.stored_archive(host, [*base, member("family/latest", tarfile.SYMTYPE, linkname="level.dat"),
                                         member("family/copy", tarfile.LNKTYPE, linkname="family/level.dat")])
        adapter.restore("family", key)
        self.assertEqual((folder / "latest").readlink(), Path("level.dat"))
        self.assertEqual((folder / "copy").read_bytes(), b"restored")
        self.assertEqual(stat.S_IMODE((folder / "level.dat").stat().st_mode), 0o644)
        # An absolute name lands inside the folder being unpacked, never at that path.
        planted = self.root / "outside" / "planted"
        key = self.stored_archive(host, [*base, member(str(planted), data=b"planted")])
        adapter.restore("family", key)
        self.assertFalse(planted.exists())
        self.assertEqual((folder / "level.dat").read_bytes(), b"restored")

    def test_restored_members_keep_their_owners(self):
        # The adapter runs as root, and the server's files belong to the account Crafty runs it as: the "data"
        # filter alone would make every restored file root's.
        info, _ = member("family/level.dat", data=b"x")
        info.uid, info.gid, info.uname, info.gname = 1000, 1000, "crafty", "crafty"
        kept = crafty.restored_member(info, str(self.root))
        self.assertEqual((kept.uid, kept.gid, kept.uname, kept.gname), (1000, 1000, "crafty", "crafty"))

    def test_a_full_disk_while_unpacking_says_to_grow_the_disk(self):
        folder = self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        (folder / "level.dat").write_bytes(b"current")
        staging = self.root / "crafty" / "staging"
        for number, raised in ((errno.ENOSPC, crafty.CraftyError), (errno.EIO, OSError)):
            def fail(*arguments, number=number):
                raise OSError(number, os.strerror(number))

            with self.subTest(errno=number), unittest.mock.patch.object(tarfile.TarFile, "makefile", fail):
                with self.assertRaises(raised) as caught:
                    adapter.restore("family", "instances/family/crafty/20261010T180000Z.tar.gz")
                if raised is crafty.CraftyError:
                    self.assertEqual(str(caught.exception),
                                     "the data disk filled up while unpacking the archive; grow the disk, then restore again")
                else:
                    self.assertNotIsInstance(caught.exception, crafty.CraftyError)
                self.assertEqual((folder / "level.dat").read_bytes(), b"current")
                self.assertEqual(list(staging.glob("restore-*")), [])

    def test_the_cli_restore_prints_one_json_object(self):
        self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        self.archived(adapter, host)
        path = self.root / "operator.json"
        path.write_text(json.dumps(config(self.root)))
        key = "instances/family/crafty/20261010T180000Z.tar.gz"
        arguments = ["--config", str(path), "restore", "family", "--key", key]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = crafty.main(arguments, api=api, host=host)
        self.assertEqual((code, json.loads(output.getvalue())), (0, {
            "instance": "family", "restored": key, "replaced": str(self.root / "crafty" / "staging" / "archived-family-20261010T180000Z")}))
        # An AWS CLI that never exits is killed, and the answer is still one JSON object.
        host.download = lambda *arguments: crafty.Host.download(host, *arguments)
        process = unittest.mock.Mock(stdout=io.BytesIO(host.stored[key]))
        process.wait.side_effect = [subprocess.TimeoutExpired("aws", 1800), 0]
        output = io.StringIO()
        with unittest.mock.patch.object(crafty.subprocess, "Popen", return_value=process), contextlib.redirect_stdout(output):
            code = crafty.main(arguments, api=api, host=host)
        self.assertEqual((code, json.loads(output.getvalue())), (1, {"error": "AWS download did not finish"}))
        process.kill.assert_called_once_with()

    def test_a_server_started_during_the_archive_fails_the_stop(self):
        self.played_server()
        api = FakeAPI(running=True)
        adapter, host = self.adapter(api)
        upload = host.upload

        def upload_then_started(*arguments):
            upload(*arguments)
            api.is_running = True  # started from Crafty's own UI while the archive uploaded

        host.upload = upload_then_started
        with self.assertRaises(crafty.CraftyError):
            adapter.host_drain()
        self.assertEqual(adapter.backup_state("family"), "failed")
        self.assertEqual(host.stored, {})  # the doubtful upload is not left as a recovery point

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


# Made-up players: a Java account and a Bedrock gamertag with its Floodgate UUID.
JAVA = {"uuid": "6b1f3c2e-9d4a-4f5e-8a7b-0c1d2e3f4a5b", "name": "Sample_Java"}
BEDROCK = {"uuid": "00000000-0000-0000-0009-01eed05d1009", "name": "Sample Player"}
RENAMED = {"uuid": "2f4e6a8c-1b3d-4e5f-9a0b-c1d2e3f4a5b6", "name": "Old_Name"}

def jar(artifact: dict) -> bytes:
    return f"{artifact['name']} {artifact['version']}".encode()


def content_manifest(transport: str = "both") -> dict:
    """The repository's manifest with stand-in jars, and the Geyser configuration of transport."""
    manifest = json.loads((ROOT / "runtime" / "crafty" / "server.json").read_text())
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = hashlib.sha256(jar(artifact)).hexdigest()
    geyser = manifest["managed_files"][0]
    geyser["content"] = crafty._approved_geyser(8, dual_transport=transport == "both")
    geyser["sha256"] = hashlib.sha256(crafty._managed_file_bytes(geyser)).hexdigest()
    return manifest


# The Compose file scripts/install_crafty.sh wrote before NetherNet, byte for byte: a RakNet manifest leaves it alone.
INSTALLED_COMPOSE = """services:
  crafty:
    image: registry.gitlab.com/crafty-controller/crafty-4@sha256:7b6e87514259549647e15ceeaf73d238792e6d77e2c79fc744d5badc8d6e997c
    platform: linux/arm64
    restart: unless-stopped
    stop_grace_period: 180s
    environment:
      TZ: Etc/UTC
    ports:
      - "25565:25565/tcp"
      - "19132:19132/udp"
      - "127.0.0.1:8443:8443/tcp"
    volumes:
      - /srv/minecraft/crafty/backups:/crafty/backups
      - /srv/minecraft/crafty/logs:/crafty/logs
      - /srv/minecraft/crafty/servers:/crafty/servers
      - /srv/minecraft/crafty/config:/crafty/app/config
      - /srv/minecraft/crafty/import:/crafty/import
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
"""

ADVERTISED = "java -Xms1024M -Xmx5120M -DgeyserAdvertiseAddresses=34.212.10.20 -jar paper.jar nogui"


class ContentTests(unittest.TestCase):
    """A start first brings the stopped server to the pinned content (ADR 0008)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = self.root / "crafty" / "servers" / SERVER_ID
        (self.server / "survival").mkdir(parents=True)
        (self.server / "survival" / "level.dat").write_bytes(b"world")
        self.manifest = content_manifest()
        self.raknet = content_manifest("raknet")
        # The server as the first import staged it: the RakNet release's jars and Geyser configuration.
        (self.server / "plugins" / "Geyser-Spigot").mkdir(parents=True)
        for artifact in self.raknet["artifacts"]:
            (self.server / artifact["path"]).write_bytes(jar(artifact))
        (self.server / crafty.GEYSER_CONFIG).write_bytes(crafty._managed_file_bytes(self.raknet["managed_files"][0]))

    def tearDown(self):
        self.temporary.cleanup()

    def adapter(self, api=None, manifest=None):
        api = api or FakeAPI()
        host = FakeHost(api)
        host.compose = self.root / "compose.yaml"
        host.compose.write_bytes(crafty.render_compose(self.raknet, self.root / "crafty"))
        for artifact in (manifest or self.manifest)["artifacts"]:
            host.artifacts[artifact["url"]] = jar(artifact)
        return crafty.Adapter(config(self.root), api, host, manifest or self.manifest), api, host

    def geyser_config(self) -> bytes:
        return (self.server / crafty.GEYSER_CONFIG).read_bytes()

    def changes(self, host) -> list:
        return [call for call in host.calls if call[0] in ("imds", "fetch", "compose")]

    def test_a_nethernet_start_applies_the_config_ports_and_address_before_the_server_starts(self):
        adapter, api, host = self.adapter()
        result = adapter.start("family")
        self.assertTrue(result["ready"])
        self.assertEqual(self.geyser_config(), crafty._managed_file_bytes(self.manifest["managed_files"][0]))
        compose = host.compose.read_text()
        for port in ('"25565:25565/tcp"', '"19132:19132/udp"', '"19132:19132/tcp"', '"19133:19133/udp"', '"127.0.0.1:8443:8443/tcp"'):
            self.assertIn(port, compose)
        self.assertEqual(api.command, ADVERTISED)
        self.assertEqual(api.log, ["patch", "start_server"])
        # The jars already match, so nothing is downloaded.
        self.assertEqual(self.changes(host), [("imds",), ("compose", "write"), ("compose", "up")])
        state = json.loads((self.root / "crafty" / "state" / "family-content.json").read_text())
        self.assertEqual(state, {"artifacts": sorted(a["path"] for a in self.manifest["artifacts"]),
                                 "revision": self.manifest["revision"]})

    def test_a_start_with_the_content_in_place_changes_nothing(self):
        adapter, api, host = self.adapter()
        adapter.start("family")
        api.is_running = False
        config_inode = (self.server / crafty.GEYSER_CONFIG).stat().st_ino
        host.calls.clear()
        api.log.clear()
        adapter.start("family")
        self.assertEqual(self.changes(host), [("imds",)])
        self.assertEqual(api.log, ["start_server"])
        self.assertEqual((self.server / crafty.GEYSER_CONFIG).stat().st_ino, config_inode)

    def test_a_new_address_replaces_the_one_crafty_launched_with(self):
        api = FakeAPI()
        api.command = "java -Xms1024M -Xmx5120M -DgeyserAdvertiseAddresses=34.1.2.3 -jar paper.jar nogui"
        adapter, api, host = self.adapter(api)
        adapter.start("family")
        self.assertEqual(api.command, ADVERTISED)

    def test_a_failed_address_lookup_refuses_the_start_and_changes_nothing(self):
        adapter, api, host = self.adapter()
        host.address = None
        before = host.compose.read_bytes()
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual(self.geyser_config(), crafty._managed_file_bytes(self.raknet["managed_files"][0]))
        self.assertEqual(host.compose.read_bytes(), before)
        self.assertEqual(api.log, [])

    def test_a_raknet_manifest_needs_no_address_and_drops_it_from_the_launch(self):
        api = FakeAPI()
        api.command = ADVERTISED
        adapter, api, host = self.adapter(api, self.raknet)
        adapter.start("family")
        self.assertEqual(api.command, "java -Xms1024M -Xmx5120M -jar paper.jar nogui")
        # The installed Compose file is the one wanted; Docker confirms it once, and the record says so after.
        self.assertEqual(self.changes(host), [("compose", "up")])
        api.is_running = False
        host.calls.clear()
        adapter.start("family")
        self.assertEqual(self.changes(host), [])

    def test_a_new_paper_needs_the_world_archived_as_it_is(self):
        manifest = content_manifest()
        paper = manifest["artifacts"][0]
        self.assertEqual(paper["name"], "paper")
        paper["version"] = "26.2.1"
        paper["sha256"] = hashlib.sha256(jar(paper)).hexdigest()
        adapter, api, host = self.adapter(manifest=manifest)
        with self.assertRaisesRegex(crafty.CraftyError, "archive"):
            adapter.start("family")
        self.assertEqual((self.server / "paper.jar").read_bytes(), b"paper 26.2")
        self.assertEqual(self.changes(host), [("imds",)])
        # Once the last archive holds the folder as it is, the new Paper goes in, before anything else.
        adapter.record("family", backup="complete", fingerprint=crafty.fingerprint(self.server))
        adapter.start("family")
        self.assertEqual((self.server / "paper.jar").read_bytes(), b"paper 26.2.1")
        self.assertEqual(api.actions, ["start_server"])

    def test_jars_an_earlier_manifest_pinned_go_and_others_stay(self):
        state = self.root / "crafty" / "state" / "family-content.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"artifacts": [a["path"] for a in self.raknet["artifacts"]] + ["plugins/Old.jar"]}))
        (self.server / "plugins" / "Old.jar").write_bytes(b"old")
        (self.server / "plugins" / "Unmanaged.jar").write_bytes(b"kept")
        adapter, api, host = self.adapter()
        adapter.start("family")
        self.assertFalse((self.server / "plugins" / "Old.jar").exists())
        self.assertEqual((self.server / "plugins" / "Unmanaged.jar").read_bytes(), b"kept")
        # The record keeps it, so a restored archive that still holds it loses it again on the next start.
        self.assertIn("plugins/Old.jar", json.loads(state.read_text())["artifacts"])
        api.is_running = False
        (self.server / "plugins" / "Old.jar").write_bytes(b"old")
        adapter.start("family")
        self.assertFalse((self.server / "plugins" / "Old.jar").exists())
        self.assertEqual((self.server / "plugins" / "Unmanaged.jar").read_bytes(), b"kept")

    def test_a_start_with_nothing_to_change_still_records_the_applied_content(self):
        state = self.root / "crafty" / "state" / "family-content.json"
        adapter, api, host = self.adapter(manifest=self.raknet)
        adapter.start("family")
        self.assertEqual(json.loads(state.read_text()), {"artifacts": sorted(a["path"] for a in self.raknet["artifacts"]),
                                                         "revision": self.raknet["revision"]})
        # A later manifest that renames a pinned jar then removes the old name.
        manifest = content_manifest("raknet")
        core = next(artifact for artifact in manifest["artifacts"] if artifact["path"] == "plugins/multiverse-core-5.8.1.jar")
        core["path"], core["version"] = "plugins/multiverse-core-5.9.0.jar", "5.9.0"
        core["sha256"] = hashlib.sha256(jar(core)).hexdigest()
        api.is_running = False
        adapter, api, host = self.adapter(api, manifest)
        adapter.start("family")
        self.assertFalse((self.server / "plugins" / "multiverse-core-5.8.1.jar").exists())
        self.assertEqual((self.server / "plugins" / "multiverse-core-5.9.0.jar").read_bytes(), jar(core))

    def test_an_unreadable_record_of_applied_content_changes_nothing(self):
        state = self.root / "crafty" / "state" / "family-content.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"artifacts": ["../../etc/passwd"]}))
        adapter, api, host = self.adapter()
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual(self.geyser_config(), crafty._managed_file_bytes(self.raknet["managed_files"][0]))
        self.assertEqual(api.log, [])

    def test_a_failed_download_changes_nothing_in_the_server_folder(self):
        manifest = content_manifest()
        for artifact in manifest["artifacts"]:
            if artifact["name"] in ("geyser", "floodgate"):
                artifact["version"] += "-next"
                artifact["sha256"] = hashlib.sha256(jar(artifact)).hexdigest()
        adapter, api, host = self.adapter(manifest=manifest)
        host.failing_downloads = {manifest["artifacts"][2]["url"]}
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual((self.server / "plugins" / "Geyser-Spigot.jar").read_bytes(), b"geyser 2.11.3")
        self.assertEqual(self.geyser_config(), crafty._managed_file_bytes(self.raknet["managed_files"][0]))
        self.assertEqual(list((self.root / "crafty" / "staging").iterdir()), [])
        self.assertEqual(api.log, [])

    def test_a_server_started_during_the_downloads_keeps_its_jars(self):
        manifest = content_manifest()
        geyser = manifest["artifacts"][1]
        geyser["version"] += "-next"
        geyser["sha256"] = hashlib.sha256(jar(geyser)).hexdigest()
        adapter, api, host = self.adapter(manifest=manifest)
        fetch = host.fetch
        host.fetch = lambda *args: (fetch(*args), setattr(api, "is_running", True))
        with self.assertRaisesRegex(crafty.CraftyError, "while its content was downloading"):
            adapter.start("family")
        self.assertEqual((self.server / "plugins" / "Geyser-Spigot.jar").read_bytes(), b"geyser 2.11.3")
        self.assertEqual(self.geyser_config(), crafty._managed_file_bytes(self.raknet["managed_files"][0]))
        self.assertEqual(api.log, [])

    def test_a_cut_off_download_is_an_adapter_error(self):
        host = crafty.Host()
        with unittest.mock.patch.object(crafty, "download", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaisesRegex(crafty.CraftyError, "cannot download Geyser.jar"):
                host.fetch("https://example.invalid/geyser", "0" * 64, self.root / "Geyser.jar")

    def test_downloaded_jars_keep_the_owner_and_mode_of_the_ones_they_replace(self):
        manifest = content_manifest()
        geyser = manifest["artifacts"][1]
        geyser["version"] += "-next"
        geyser["sha256"] = hashlib.sha256(jar(geyser)).hexdigest()
        target = self.server / geyser["path"]
        target.chmod(0o664)
        adapter, api, host = self.adapter(manifest=manifest)
        adapter.start("family")
        self.assertEqual(target.read_bytes(), jar(geyser))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o664)
        self.assertEqual([name for name in os.listdir(self.server / "plugins") if name.startswith(".")], [])

    def test_a_link_planted_for_a_pinned_file_is_replaced_never_followed(self):
        outside = self.root / "outside"
        outside.write_bytes(b"keep")
        config_path = self.server / crafty.GEYSER_CONFIG
        config_path.unlink()
        config_path.symlink_to(outside)
        jar_path = self.server / "plugins" / "Geyser-Spigot.jar"
        jar_path.unlink()
        jar_path.symlink_to(outside)
        adapter, api, host = self.adapter()
        adapter.start("family")
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertFalse(config_path.is_symlink())
        self.assertEqual(config_path.read_bytes(), crafty._managed_file_bytes(self.manifest["managed_files"][0]))
        self.assertFalse(jar_path.is_symlink())
        self.assertEqual(jar_path.read_bytes(), b"geyser 2.11.3")

    def test_a_folder_swapped_for_a_link_fails_the_start(self):
        elsewhere = self.root / "elsewhere"
        (self.server / "plugins").rename(elsewhere)
        (self.server / "plugins").symlink_to(elsewhere)
        adapter, api, host = self.adapter()
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual((elsewhere / "Geyser-Spigot" / "config.yml").read_bytes(),
                         crafty._managed_file_bytes(self.raknet["managed_files"][0]))
        self.assertEqual(api.log, [])

    def test_compose_crafty_cannot_come_up_with_puts_the_previous_file_back(self):
        adapter, api, host = self.adapter()
        host.compose_failures = 1
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual(host.compose.read_bytes(), crafty.render_compose(self.raknet, self.root / "crafty"))
        self.assertEqual(self.changes(host), [("imds",), ("compose", "write"), ("compose", "up"), ("compose", "write"), ("compose", "up")])
        self.assertEqual(api.log, [])

    def test_the_start_waits_until_the_recreated_crafty_lists_the_server(self):
        adapter, api, host = self.adapter()
        host.on_compose_up = lambda: setattr(api, "no_servers", True)
        host.sleep = lambda seconds: (setattr(api, "no_servers", False), FakeHost.sleep(host, seconds))
        adapter.start("family")
        self.assertEqual(api.actions, ["start_server"])

    def test_a_compose_file_docker_never_applied_is_applied_on_the_next_start(self):
        adapter, api, host = self.adapter()
        # A start cut off after writing the file, before Docker recreated Crafty.
        host.compose.write_bytes(crafty.render_compose(self.manifest, self.root / "crafty"))
        adapter.start("family")
        self.assertEqual(self.changes(host), [("imds",), ("compose", "up")])

    def test_a_retry_docker_cannot_apply_puts_back_the_file_it_last_applied(self):
        adapter, api, host = self.adapter(manifest=self.raknet)
        adapter.start("family")
        api.is_running = False
        applied = host.compose.read_bytes()
        # A start cut off after writing the new file, before Docker applied it; then Docker fails on the retry.
        adapter, api, host = self.adapter(api)
        host.compose.write_bytes(crafty.render_compose(self.manifest, self.root / "crafty"))
        host.compose_failures = 1
        with self.assertRaisesRegex(crafty.CraftyError, "previous Compose file is back"):
            adapter.start("family")
        self.assertEqual(host.compose.read_bytes(), applied)
        self.assertEqual(self.changes(host), [("imds",), ("compose", "up"), ("compose", "write"), ("compose", "up")])

    def test_staged_downloads_never_take_their_names_from_the_manifest(self):
        manifest = content_manifest()
        geyser = manifest["artifacts"][1]
        geyser["name"], geyser["version"] = "../../escape", geyser["version"] + "-next"
        geyser["sha256"] = hashlib.sha256(jar(geyser)).hexdigest()
        adapter, api, host = self.adapter(manifest=manifest)
        adapter.start("family")
        self.assertEqual((self.server / "plugins" / "Geyser-Spigot.jar").read_bytes(), jar(geyser))
        self.assertFalse((self.root / "crafty" / "escape.jar").exists())
        self.assertEqual([path.name for path in self.root.rglob("escape*")], [])

    def test_docker_failing_on_an_unchanged_compose_file_leaves_it_alone(self):
        adapter, api, host = self.adapter(manifest=self.raknet)
        host.compose_failures = 1
        with self.assertRaisesRegex(crafty.CraftyError, "did not bring Crafty up"):
            adapter.start("family")
        self.assertEqual(self.changes(host), [("compose", "up")])
        self.assertEqual(host.compose.read_bytes(), crafty.render_compose(self.raknet, self.root / "crafty"))
        self.assertEqual(api.log, [])

    def test_a_server_started_while_crafty_restarts_fails_the_start(self):
        adapter, api, host = self.adapter()
        host.on_compose_up = lambda: setattr(api, "is_running", True)
        with self.assertRaisesRegex(crafty.CraftyError, "restarted"):
            adapter.start("family")
        self.assertEqual(api.log, [])

    def test_a_server_started_while_the_launch_command_changes_fails_the_start(self):
        adapter, api, host = self.adapter()
        adapter.start("family")
        api.is_running = False
        host.address = "34.212.10.21"
        request = api.request

        def started_meanwhile(method, path, body=None, text=None):
            if method == "PATCH":
                api.is_running = True
            return request(method, path, body, text)

        api.request = started_meanwhile
        with self.assertRaisesRegex(crafty.CraftyError, "while its content was being applied"):
            adapter.start("family")
        self.assertEqual(api.actions, ["start_server"])

    def test_crafty_must_keep_the_new_launch_command(self):
        api = FakeAPI()
        api.keeps_patches = False
        adapter, api, host = self.adapter(api)
        with self.assertRaises(crafty.CraftyError):
            adapter.start("family")
        self.assertEqual(api.actions, [])

    def test_a_running_server_keeps_its_content(self):
        adapter, api, host = self.adapter(FakeAPI(running=True))
        adapter.start("family")
        self.assertEqual(self.changes(host), [])
        self.assertEqual(self.geyser_config(), crafty._managed_file_bytes(self.raknet["managed_files"][0]))

    def test_the_launch_command_carries_one_address_before_the_jar(self):
        self.assertEqual(crafty.launch_command("java -Xmx5G -jar paper.jar nogui", "34.212.10.20"),
                         "java -Xmx5G -DgeyserAdvertiseAddresses=34.212.10.20 -jar paper.jar nogui")
        self.assertEqual(crafty.launch_command("java -jar paper.jar -DgeyserAdvertiseAddresses=1.1.1.1 nogui", None),
                         "java -jar paper.jar nogui")
        for command, address in (("java paper.jar", "34.212.10.20"), ("java -jar a.jar -jar b.jar", "34.212.10.20"),
                                 ("java -jar paper.jar", "10.0.0.5"), ("java -jar paper.jar", "203.0.113.9"),
                                 ("java -jar paper.jar", "34.212.10.20 -Devil=1")):
            with self.assertRaises(crafty.CraftyError):
                crafty.launch_command(command, address)

    def test_a_raknet_compose_file_is_the_one_installed_before_nethernet(self):
        self.assertEqual(crafty.render_compose(self.raknet, Path("/srv/minecraft/crafty")), INSTALLED_COMPOSE.encode())

    def test_only_the_approved_geyser_configurations_are_accepted(self):
        self.assertEqual(crafty.bedrock_transport(self.manifest), "both")
        self.assertEqual(crafty.bedrock_transport(self.raknet), "raknet")
        for change in ({"webrtc-port": 19134}, {"signaling": {"mode": "xbox"}}, {"transport": "nethernet"}):
            manifest = content_manifest()
            manifest["managed_files"][0]["content"]["bedrock"].update(change)
            with self.assertRaises(crafty.CraftyError):
                crafty.bedrock_transport(manifest)

    def test_the_cli_start_applies_the_manifest_beside_the_adapter_and_fails_without_it(self):
        path = self.root / "operator.json"
        path.write_text(json.dumps(config(self.root)))
        manifest = self.root / "server.json"
        manifest.write_text(json.dumps(self.manifest))
        _, api, host = self.adapter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = crafty.main(["--config", str(path), "start", "family"], api=api, host=host, manifest=self.root / "missing.json")
        self.assertEqual((code, json.loads(output.getvalue())), (1, {"error": "cannot read the pinned server content"}))
        self.assertEqual(api.log, [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = crafty.main(["--config", str(path), "start", "family"], api=api, host=host, manifest=manifest)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ready"])
        self.assertEqual(api.log, ["patch", "start_server"])

    def test_the_cli_renders_the_compose_file_the_installer_writes(self):
        output = io.BytesIO()
        with unittest.mock.patch.object(sys, "stdout", unittest.mock.Mock(buffer=output)):
            code = crafty.main(["render-compose", "--manifest", str(ROOT / "runtime" / "crafty" / "server.json"),
                                "--data-root", "/srv/minecraft"])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), crafty.render_compose(crafty.load_manifest(ROOT / "runtime" / "crafty" / "server.json"),
                                                                  Path("/srv/minecraft/crafty")))


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server_dir = self.root / "crafty" / "servers" / SERVER_ID
        self.server_dir.mkdir(parents=True)
        self.allowlist = self.server_dir / "whitelist.json"
        self.write([])
        self.log = self.server_dir / "logs" / "latest.log"
        self.log.parent.mkdir()
        # An earlier session's answers, which must never count for a later reload.
        self.log.write_text("[09:59:58] [Server thread/INFO]: Reloaded the whitelist\n"
                            "[09:59:59] [Server thread/INFO]: There are no whitelisted players\n")
        self.commands = {}
        self.answers = True  # whether the server answers reload and list in its log
        self.memory = None  # the list the server holds after its last reload; None for the file's
        self.api = FakeAPI(running=True)
        self.api.on_console = self.play
        self.host = FakeHost(self.api)
        self.adapter = crafty.Adapter(config(self.root), self.api, self.host)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, entries):
        self.allowlist.write_text(json.dumps(entries))

    def entries(self):
        return json.loads(self.allowlist.read_text())

    def server_applies(self, commands):
        """Each console line in commands changes whitelist.json the way the game would."""
        self.commands = commands

    def say(self, text):
        with self.log.open("a") as log:
            log.write(f"[10:00:00] [Server thread/INFO]: {text}\n")

    def play(self, line):
        """Play the server: apply the line's change, then answer a reload or a list in the log as the game does."""
        if line in self.commands:
            self.write(self.commands[line](self.entries()))
        if not self.answers:
            return
        if line == "whitelist reload":
            self.memory = self.loaded()
            self.say("Reloaded the whitelist")
        elif line == "whitelist list":
            names = [entry["name"] for entry in (self.loaded() if self.memory is None else self.memory)]
            self.say(f"There are {len(names)} whitelisted player(s): {', '.join(names)}" if names else "There are no whitelisted players")

    def loaded(self):
        """The list the game would load from whitelist.json: nothing from a file it cannot read."""
        try:
            return self.entries()
        except ValueError:
            return []

    def server_files(self):
        return sorted(path.name for path in self.server_dir.iterdir())

    def test_java_add_sends_one_console_line_and_confirms_the_uuid(self):
        self.server_applies({"whitelist add Sample_Java": lambda entries: entries + [JAVA]})
        result = self.adapter.allowlist("family", "add", "java", dict(JAVA))
        self.assertEqual(result, {"instance": "family", "edition": "java", "operation": "add", "allowlist": "updated"})
        self.assertEqual(self.api.console, [(SERVER_ID, "whitelist add Sample_Java")])

    def test_a_listed_player_is_unchanged_without_a_console_line(self):
        self.write([JAVA])
        result = self.adapter.allowlist("family", "add", "java", dict(JAVA))
        self.assertEqual(result["allowlist"], "unchanged")
        self.assertEqual(self.api.console, [])

    def test_java_remove_by_uuid_waits_for_the_entry_to_leave(self):
        self.write([JAVA, BEDROCK])
        self.server_applies({"whitelist remove Sample_Java": lambda entries: [e for e in entries if e != JAVA]})
        result = self.adapter.allowlist("family", "remove", "java", dict(JAVA))
        self.assertEqual(result["allowlist"], "updated")
        self.assertEqual(self.entries(), [BEDROCK])

    def test_bedrock_changes_go_through_floodgate_with_the_gamertag_quoted(self):
        self.server_applies({'fwhitelist add "Sample Player"': lambda entries: entries + [BEDROCK]})
        result = self.adapter.allowlist("family", "add", "bedrock", dict(BEDROCK))
        self.assertEqual(result, {"instance": "family", "edition": "bedrock", "operation": "add", "allowlist": "updated"})
        self.assertEqual(self.api.console, [(SERVER_ID, 'fwhitelist add "Sample Player"')])

    def test_a_change_the_server_never_makes_times_out_without_naming_the_player(self):
        with self.assertRaises(crafty.CraftyError) as caught:
            self.adapter.allowlist("family", "add", "java", dict(JAVA))
        self.assertIn("timed out", str(caught.exception))
        self.assertNotIn("Sample_Java", str(caught.exception))
        self.assertNotIn(JAVA["uuid"], str(caught.exception))
        self.assertEqual(self.host.clock, 60)

    def test_a_name_mojang_no_longer_knows_leaves_the_file_and_the_server_reloads_it(self):
        self.write([JAVA, RENAMED])
        self.allowlist.chmod(0o640)
        synced = []
        real_fsync = os.fsync

        def fsync(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with unittest.mock.patch.object(crafty.os, "fsync", fsync):
            result = self.adapter.allowlist("family", "remove", "java", {"name": "old_name"})
        self.assertEqual(result, {"instance": "family", "edition": "java", "operation": "remove", "allowlist": "updated"})
        self.assertEqual(self.entries(), [JAVA])
        self.assertEqual(self.api.console, [(SERVER_ID, "whitelist reload"), (SERVER_ID, "whitelist list")])
        self.assertEqual(self.memory, [JAVA])
        self.assertEqual(self.allowlist.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.server_files(), ["logs", "whitelist.json"])
        self.assertEqual(synced, [False, True], "the file, then the folder holding the rename")

    def test_the_removal_never_writes_over_a_change_made_while_it_ran(self):
        self.write([JAVA, RENAMED])
        real_fsync = os.fsync

        def fsync(descriptor):
            real_fsync(descriptor)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                self.write([JAVA, RENAMED, BEDROCK])  # an add from the Crafty console, mid-removal

        with unittest.mock.patch.object(crafty.os, "fsync", fsync), self.assertRaises(crafty.CraftyError):
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertEqual(self.entries(), [JAVA, RENAMED, BEDROCK])
        self.assertEqual(self.api.console, [])
        self.assertEqual(self.server_files(), ["logs", "whitelist.json"])

    def test_the_removal_fails_when_the_server_writes_the_entry_back_before_its_reload(self):
        self.write([JAVA, RENAMED])
        self.server_applies({"whitelist reload": lambda entries: entries + [RENAMED]})
        with self.assertRaises(crafty.CraftyError) as caught:
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertIn("still holds", str(caught.exception))
        self.assertNotIn("Old_Name", str(caught.exception))

    def test_the_rewrite_never_follows_a_link_swapped_in_for_its_temporary_file(self):
        self.write([JAVA, RENAMED])
        self.allowlist.chmod(0o2660)
        victim = self.root / "victim"
        victim.write_text("host file")
        victim.chmod(0o600)
        real_fsync = os.fsync

        def swap(descriptor):
            real_fsync(descriptor)
            for partial in [path for path in self.server_dir.iterdir() if path.name.startswith(".whitelist.")]:
                partial.rename(self.server_dir / "moved")
                partial.symlink_to(victim)

        # The rename puts the link in place of whitelist.json, which the check after the reload refuses to follow.
        with unittest.mock.patch.object(crafty.os, "fsync", swap), self.assertRaises(OSError):
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertEqual(victim.stat().st_mode & 0o7777, 0o600)
        self.assertEqual(victim.read_text(), "host file")
        self.assertEqual((self.server_dir / "moved").stat().st_mode & 0o7777, 0o660)

    def test_a_name_the_allowlist_does_not_hold_is_reported_not_listed(self):
        self.write([JAVA])
        result = self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertEqual(result["allowlist"], "not-listed")
        self.assertEqual(self.entries(), [JAVA])
        # An earlier removal may have changed the file without its reload reaching the server.
        self.assertEqual(self.api.console, [(SERVER_ID, "whitelist reload"), (SERVER_ID, "whitelist list")])

    def test_a_retry_finishes_a_removal_whose_reload_never_reached_the_server(self):
        self.write([JAVA])
        self.memory = [JAVA, RENAMED]
        self.assertEqual(self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})["allowlist"], "not-listed")
        self.assertEqual(self.memory, [JAVA])

    def test_a_reload_the_server_never_answers_fails_without_naming_the_player(self):
        self.write([JAVA, RENAMED])
        self.answers = False
        with self.assertRaises(crafty.CraftyError) as caught:
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertIn("did not confirm", str(caught.exception))
        self.assertNotIn("old_name", str(caught.exception).lower())
        self.assertEqual(self.host.clock, 60)
        self.assertEqual(self.api.console, [(SERVER_ID, "whitelist reload"), (SERVER_ID, "whitelist list")])

    def test_a_server_still_holding_the_name_after_its_reload_fails_the_removal(self):
        # As if the server loaded some other file: it answers, but its list keeps the entry.
        for start, player in (([JAVA, RENAMED], "updated"), ([JAVA], "not-listed")):
            with self.subTest(player=player):
                self.write(start)
                self.api.on_console = lambda line: self.say("Reloaded the whitelist" if line == "whitelist reload"
                                                            else "There are 2 whitelisted player(s): Sample_Java, Old_Name")
                with self.assertRaises(crafty.CraftyError) as caught:
                    self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
                self.assertIn("still holds", str(caught.exception))
                self.assertNotIn("old_name", str(caught.exception).lower())

    def test_only_whole_server_lines_after_the_reload_count(self):
        # Each case would confirm the removal if any of its imitated, early or partly written lines counted.
        reload = "[10:00:00] [Server thread/INFO]: Reloaded the whitelist\n"
        empty = "[10:00:00] [Server thread/INFO]: There are no whitelisted players\n"
        cases = {
            "a list from before the reload": empty + reload,
            "a reload imitated in chat or with /say": (
                "[10:00:00] [Async Chat Thread - #0/INFO]: <Sample_Java> ]: Reloaded the whitelist\n"
                "[10:00:00] [Async Chat Thread - #0/INFO]: [Not Secure] <Sample_Java> Reloaded the whitelist\n"
                "[10:00:00] [Server thread/INFO]: [Sample_Java] Reloaded the whitelist\n"
                "[10:00:00] [Server thread/INFO]: [.Sample_Player] Reloaded the whitelist\n"
                "[10:00:00] [Server thread/INFO]: Reloaded the whitelist and more\n" + empty),
            "a list imitated in chat or with /say": reload + (
                "[10:00:00] [Async Chat Thread - #0/INFO]: <Sample_Java> There are no whitelisted players\n"
                "[10:00:00] [Server thread/INFO]: [Sample_Java] There are no whitelisted players\n"),
            "a list not yet whole": reload + "[10:00:00] [Server thread/INFO]: There are 2 whitelisted player(s): Sample_Java",
        }
        self.answers = False
        for case, text in cases.items():
            with self.subTest(case=case):
                self.write([JAVA, RENAMED])
                self.log.write_text("")

                def answer(line, text=text):
                    if line == "whitelist list":
                        with self.log.open("a") as log:
                            log.write(text)
                self.api.on_console = answer
                with self.assertRaises(crafty.CraftyError) as caught:
                    self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
                self.assertIn("did not confirm", str(caught.exception))

    def test_an_answer_under_a_logger_name_still_counts(self):
        self.write([JAVA, RENAMED])
        self.answers = False

        def answer(line):
            if line == "whitelist list":
                self.say("[net.minecraft.server.MinecraftServer] Reloaded the whitelist")
                self.say("[net.minecraft.server.MinecraftServer] There are 1 whitelisted player(s): Sample_Java")
        self.api.on_console = answer
        self.assertEqual(self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})["allowlist"], "updated")

    def test_the_removal_fails_when_the_file_holds_the_entry_again_after_the_reload(self):
        self.write([JAVA, RENAMED])

        def rewrite(line):
            self.play(line)
            if line == "whitelist list":  # the server's list lacks the name, but the file holds it again
                self.write([JAVA, RENAMED])
        self.api.on_console = rewrite
        with self.assertRaises(crafty.CraftyError) as caught:
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertIn("changed during the removal", str(caught.exception))
        self.assertNotIn("old_name", str(caught.exception).lower())

    def test_a_log_lost_after_the_rewrite_says_to_inspect_before_retrying(self):
        self.write([JAVA, RENAMED])
        real_write = crafty._Console.write_allowlist

        def write_then_roll(console, entries, expected):
            real_write(console, entries, expected)
            self.log.rename(self.log.parent / "2026-10-12-1.log")  # as log4j renames before it creates the new log

        with unittest.mock.patch.object(crafty._Console, "write_allowlist", write_then_roll), \
                self.assertRaises(crafty.CraftyError) as caught:
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertIn("inspect private server state", str(caught.exception))
        self.log.write_text("")
        self.assertEqual(self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})["allowlist"], "not-listed")

    def test_the_reload_is_confirmed_across_a_log_rollover(self):
        self.write([JAVA, RENAMED])

        def roll(line):
            if line == "whitelist reload":
                self.log.rename(self.log.parent / "2026-10-12-1.log")
                self.log.write_text("")
            self.play(line)
        self.api.on_console = roll
        self.assertEqual(self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})["allowlist"], "updated")

    def test_a_linked_or_missing_log_is_never_read(self):
        elsewhere = self.root / "elsewhere.log"
        elsewhere.write_text("[10:00:00] [Server thread/INFO]: Reloaded the whitelist\n"
                             "[10:00:00] [Server thread/INFO]: There are no whitelisted players\n")
        self.log.unlink()
        for linked in (True, False):
            with self.subTest(linked=linked):
                if linked:
                    self.log.symlink_to(elsewhere)
                else:
                    self.log.unlink()
                self.write([JAVA, RENAMED])
                self.api.console.clear()
                with self.assertRaises(crafty.CraftyError):
                    self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
                self.assertEqual(self.api.console, [], "nothing is sent without a log to confirm it")
                self.assertEqual(self.entries(), [JAVA, RENAMED])

    def test_only_a_java_removal_may_name_a_player_without_a_uuid(self):
        self.write([RENAMED])
        for operation, edition, player in (("add", "java", {"name": "Old_Name"}), ("remove", "bedrock", {"name": "Old_Name"}),
                                           ("remove", "java", {"name": "Old_Name", "extra": 1}), ("remove", "java", {"name": "no;way"})):
            with self.subTest(operation=operation, edition=edition, player=player), self.assertRaises(crafty.CraftyError):
                self.adapter.allowlist("family", operation, edition, player)
        self.assertEqual(self.entries(), [RENAMED])
        self.assertEqual(self.api.console, [])

    def test_a_name_listed_twice_is_refused(self):
        self.write([RENAMED, {"uuid": JAVA["uuid"], "name": "old_name"}])
        with self.assertRaises(crafty.CraftyError):
            self.adapter.allowlist("family", "remove", "java", {"name": "Old_Name"})
        self.assertEqual(self.api.console, [])

    def test_refuses_a_stopped_server_a_closed_port_or_a_draining_host(self):
        self.api.is_running = False
        with self.assertRaises(crafty.CraftyError):
            self.adapter.allowlist("family", "add", "java", dict(JAVA))
        self.api.is_running = True
        self.host.port_open = lambda port: False
        with self.assertRaises(crafty.CraftyError):
            self.adapter.allowlist("family", "add", "java", dict(JAVA))
        self.host.port_open = lambda port: True
        state = self.root / "crafty" / crafty.STATE_DIR
        state.mkdir(parents=True)
        (state / "host-drain.json").write_text(json.dumps({"boot_id": "boot-1"}))
        with self.assertRaises(crafty.CraftyError):
            self.adapter.allowlist("family", "add", "java", dict(JAVA))
        self.assertEqual(self.api.console, [])

    def test_a_linked_allowlist_file_is_never_read_or_replaced(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps([RENAMED]))
        self.allowlist.unlink()
        self.allowlist.symlink_to(target)
        for player, operation in ((dict(JAVA), "add"), ({"name": "Old_Name"}, "remove")):
            with self.subTest(operation=operation), self.assertRaises(crafty.CraftyError):
                self.adapter.allowlist("family", operation, "java", player)
        self.assertEqual(json.loads(target.read_text()), [RENAMED])
        self.assertEqual(self.api.console, [])

    def test_bad_identity_evidence_is_refused(self):
        self.write([{"uuid": "not-a-uuid", "name": "Old_Name"}])
        for player, operation in ((dict(JAVA), "add"), ({"name": "Old_Name"}, "remove")):
            with self.subTest(operation=operation), self.assertRaises(crafty.CraftyError):
                self.adapter.allowlist("family", operation, "java", player)
        self.assertEqual(self.api.console, [])

    def test_console_refuses_anything_but_one_line(self):
        for line in ("", "whitelist add A\nop A", "say a\rop A", "say \x00"):
            with self.subTest(line=line), self.assertRaises(crafty.CraftyError):
                self.adapter.console({"server_id": SERVER_ID}, line)
        self.assertEqual(self.api.console, [])


class ConsoleTests(unittest.TestCase):
    """The console action an operator runs in a Session Manager shell on the host."""

    ANSWER = "[10:00:00] [Server thread/INFO]: There are 1 whitelisted player(s): Sample_Java"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = self.root / "crafty" / "servers" / SERVER_ID / "logs" / "latest.log"
        self.log.parent.mkdir(parents=True)
        self.log.write_text('[09:59:59] [Server thread/INFO]: Done (3.2s)! For help, type "help"\n')
        self.api = FakeAPI(running=True)
        self.api.on_console = lambda line: self.say(self.ANSWER + "\n")
        self.host = FakeHost(self.api)
        self.adapter = crafty.Adapter(config(self.root), self.api, self.host)

    def tearDown(self):
        self.temporary.cleanup()

    def say(self, text):
        with self.log.open("a") as log:
            log.write(text)

    def test_the_line_goes_to_the_console_and_what_the_server_logged_next_comes_back(self):
        result = self.adapter.console_command("family", "whitelist list")
        self.assertEqual(result, {"instance": "family", "sent": "whitelist list", "log": [self.ANSWER]})
        self.assertEqual(self.api.console, [(SERVER_ID, "whitelist list")])
        self.assertEqual(self.host.clock, crafty.CONSOLE_WAIT)

    def test_the_log_read_back_is_bounded_and_whole_lines_only(self):
        self.api.on_console = lambda line: self.say("".join(f"[10:00:00] [Server thread/INFO]: line {n}\n" for n in range(300)) + "[10:00:01] partly")
        log = self.adapter.console_command("family", "list")["log"]
        self.assertEqual(len(log), crafty.CONSOLE_LOG_LINES)
        self.assertEqual(log[-1], "[10:00:00] [Server thread/INFO]: line 199")
        long = "[10:00:00] [Server thread/INFO]: " + "x" * 1000
        self.api.on_console = lambda line: self.say((long + "\n") * 100)  # about 100 KB at once
        log = self.adapter.console_command("family", "list")["log"]
        self.assertEqual(log, [long] * (crafty.CONSOLE_LOG_LIMIT // (len(long) + 1)))

    def test_the_lines_after_a_log_rollover_come_from_the_new_log(self):
        def roll(line):
            self.log.rename(self.log.parent / "2026-10-12-1.log")  # as log4j renames before it creates the new log
            self.log.write_text(self.ANSWER + "\n")

        self.api.on_console = roll
        self.assertEqual(self.adapter.console_command("family", "whitelist list")["log"], [self.ANSWER])

    def test_refuses_a_held_lock_a_draining_host_a_stopped_server_more_than_one_line_or_a_linked_log(self):
        def held(path):
            raise crafty.CraftyError("another lifecycle command is running; retry when it finishes")

        self.host.lock = held
        with self.assertRaisesRegex(crafty.CraftyError, "another lifecycle command"):
            self.adapter.console_command("family", "list")
        del self.host.lock
        self.api.is_running = False
        with self.assertRaisesRegex(crafty.CraftyError, "^start the server before sending console commands$"):
            self.adapter.console_command("family", "list")
        self.api.is_running = True
        for line in ("", "say a\nop A", "say a\rop A", "say \x00"):
            with self.subTest(line=line), self.assertRaisesRegex(crafty.CraftyError, "one line"):
                self.adapter.console_command("family", line)
        elsewhere = self.root / "elsewhere.log"
        elsewhere.write_text("")
        self.log.unlink()
        self.log.symlink_to(elsewhere)
        with self.assertRaisesRegex(crafty.CraftyError, "server log"):
            self.adapter.console_command("family", "list")
        crafty.write_durably(self.root / "crafty" / crafty.STATE_DIR / "host-drain.json", {"boot_id": "boot-1"})
        with self.assertRaisesRegex(crafty.CraftyError, "^host is draining; start the host again before sending console commands$"):
            self.adapter.console_command("family", "list")
        self.assertEqual(self.api.console, [])

    def test_the_cli_prints_one_json_object(self):
        path = self.root / "operator.json"
        path.write_text(json.dumps(config(self.root)))
        for running, code, answer in ((True, 0, {"instance": "family", "sent": "whitelist list", "log": [self.ANSWER]}),
                                      (False, 1, {"error": "start the server before sending console commands"})):
            self.api.is_running = running
            output = io.StringIO()
            with self.subTest(running=running), contextlib.redirect_stdout(output):
                result = crafty.main(["--config", str(path), "console", "family", "whitelist list"], api=self.api, host=self.host)
            self.assertEqual((result, json.loads(output.getvalue())), (code, answer))


# The first admin login a fresh Crafty writes, in these tests only: made up, and named so no scanner takes it for a real one.
FIRST_ADMIN_PHRASE = "made-up-first-phrase"


class FakeFreshCrafty(FakeAPI):
    """A freshly installed Crafty: its first admin login, and the API key endpoints a session may use."""

    SESSION, KEY = "session." + "s" * 40, "key." + "k" * 40

    def __init__(self):
        super().__init__()
        self.token = None  # the adapter sets the session, then the key
        self.answers = {"login": {"token": self.SESSION, "user_id": "1", "page": "/panel/dashboard"}, "key": {"id": 7}, "token": self.KEY}
        self.sent = []

    def request(self, method, path, body=None, text=None):
        self.sent.append((method, path, body, self.token))
        if path == "/api/v2/auth/login" and method == "POST":
            if body != {"username": "admin", "password": FIRST_ADMIN_PHRASE}:
                raise crafty.CraftyError("Crafty API POST request failed")  # Crafty answers 401
            return {"status": "ok", "data": self.answers["login"]}
        if self.token != (self.SESSION if path.startswith("/api/v2/users/") else self.KEY):
            raise crafty.CraftyError(f"Crafty API {method} request failed")
        if path == "/api/v2/users/1/key" and method == "PATCH":
            return {"status": "ok", "data": self.answers["key"]}
        if path == "/api/v2/users/1/key/7" and method == "GET":
            return {"status": "ok", "data": self.answers["token"]}
        return super().request(method, path, body, text)


class SetupAPIKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.creds = self.root / "crafty" / "config" / "default-creds.txt"
        self.creds.parent.mkdir(parents=True)
        # As Crafty 4.11 writes it on a fresh install.
        self.login = json.dumps({"username": "admin", "password": FIRST_ADMIN_PHRASE, "info": "This is NOT where you change your password."}, indent=4)
        self.creds.write_text(self.login)
        self.creds.chmod(0o600)
        (self.root / "etc").mkdir()
        self.token_file, self.admin_file = self.root / "etc" / "crafty-api.token", self.root / "etc" / "crafty-admin.json"
        self.path = self.root / "operator.json"
        self.path.write_text(json.dumps(dict(config(self.root), token_file=str(self.token_file))))
        self.api = FakeFreshCrafty()

    def tearDown(self):
        self.temporary.cleanup()

    def setup_api_key(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = crafty.main(["--config", str(self.path), "setup-api-key"], api=self.api, host=FakeHost(self.api))
        return code, output.getvalue()

    def assertNothingSetUp(self, output):
        self.assertIn("error", json.loads(output))
        for secret in (FIRST_ADMIN_PHRASE, FakeFreshCrafty.SESSION, FakeFreshCrafty.KEY):
            self.assertNotIn(secret, output)
        self.assertFalse(os.path.lexists(self.token_file))
        self.assertFalse(os.path.lexists(self.admin_file))
        self.assertEqual(self.creds.read_text(), self.login)

    def test_the_key_is_stored_for_root_alone_and_the_first_login_leaves_crafty(self):
        output = io.StringIO()
        # Run as the host runs it: main makes the client, which has no token yet.
        with unittest.mock.patch.object(crafty, "CraftyAPI", return_value=self.api) as client, contextlib.redirect_stdout(output):
            code = crafty.main(["--config", str(self.path), "setup-api-key"], host=FakeHost(self.api))
        client.assert_called_once_with("https://127.0.0.1:8443", None)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"key": "lifecycle", "token_file": str(self.token_file), "admin_file": str(self.admin_file)})
        for secret in (FIRST_ADMIN_PHRASE, FakeFreshCrafty.SESSION, FakeFreshCrafty.KEY):
            self.assertNotIn(secret, output.getvalue())
        self.assertEqual(self.token_file.read_text(), FakeFreshCrafty.KEY)
        self.assertEqual(self.admin_file.read_text(), self.login)
        for path in (self.token_file, self.admin_file):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(os.path.lexists(self.creds))
        self.assertEqual(self.api.sent, [
            ("POST", "/api/v2/auth/login", {"username": "admin", "password": FIRST_ADMIN_PHRASE}, None),
            ("PATCH", "/api/v2/users/1/key", {"name": "lifecycle", "full_access": True, "server_permissions_mask": "00000000",
                                              "crafty_permissions_mask": "000"}, FakeFreshCrafty.SESSION),
            ("GET", "/api/v2/users/1/key/7", None, FakeFreshCrafty.SESSION),
            ("GET", "/api/v2/servers", None, FakeFreshCrafty.KEY)])

    def test_an_existing_token_file_or_admin_file_is_never_replaced(self):
        cases = ((self.token_file, False, "the API key is already set up"),
                 (self.token_file, True, "the API key is already set up"),
                 (self.admin_file, False, f"{self.admin_file} holds an earlier Crafty login; move it away, then run setup-api-key again"))
        for path, link, error in cases:
            with self.subTest(path=path.name, link=link):
                if link:
                    path.symlink_to(self.root / "elsewhere")  # dangling, so exists() alone would miss it
                else:
                    path.write_text("earlier")
                # A key set up earlier has moved the first login away; an admin file alone blocks a fresh setup.
                moved = path == self.token_file
                if moved:
                    self.creds.unlink()
                code, output = self.setup_api_key()
                self.assertEqual((code, json.loads(output)), (1, {"error": error}))
                self.assertFalse(os.path.lexists(self.root / "elsewhere"))
                self.assertEqual(path.is_symlink(), link)
                if not link:
                    self.assertEqual(path.read_text(), "earlier")
                self.assertEqual(self.api.sent, [])
                if moved:
                    self.assertFalse(os.path.lexists(self.creds))
                    self.creds.write_text(self.login)
                self.assertEqual(self.creds.read_text(), self.login)
                path.unlink()

    def test_a_setup_cut_off_after_storing_the_token_finishes_the_move(self):
        checked = [("GET", "/api/v2/servers", None, FakeFreshCrafty.KEY)]
        # The token was stored, then the setup stopped before the login left Crafty's config folder, or after an
        # identical copy of it was stored but before default-creds.txt was gone for good.
        for copied in (False, True):
            with self.subTest(copied=copied):
                self.api = FakeFreshCrafty()
                self.token_file.write_text(FakeFreshCrafty.KEY + "\n")
                if copied:
                    self.admin_file.write_text(self.login)
                    self.admin_file.chmod(0o600)
                synced, fsync = [], os.fsync

                def recorded(descriptor):
                    synced.append((os.readlink(f"/proc/self/fd/{descriptor}"), os.path.lexists(self.creds)))
                    fsync(descriptor)

                with unittest.mock.patch.object(crafty.os, "fsync", recorded):
                    code, output = self.setup_api_key()
                self.assertEqual((code, json.loads(output)), (0, {"token_file": str(self.token_file), "admin_file": str(self.admin_file)}))
                # The copy, written now or by the cut-off run, is on disk before the login it copies is removed.
                self.assertIn((os.path.realpath(self.admin_file), True), synced)
                self.assertNotIn(FIRST_ADMIN_PHRASE, output)
                self.assertEqual(self.api.sent, checked)  # only the stored key's check, no new key
                self.assertEqual(self.token_file.read_text(), FakeFreshCrafty.KEY + "\n")
                self.assertEqual(self.admin_file.read_text(), self.login)
                self.assertEqual(stat.S_IMODE(self.admin_file.stat().st_mode), 0o600)
                self.assertFalse(os.path.lexists(self.creds))
                # Run once more, there is nothing left to do.
                code, output = self.setup_api_key()
                self.assertEqual((code, json.loads(output)), (1, {"error": "the API key is already set up"}))
                for path in (self.token_file, self.admin_file):
                    path.unlink()
                self.creds.write_text(self.login)

    def test_a_token_this_crafty_does_not_accept_leaves_its_first_login_in_place(self):
        # An empty token from a cut-off write, a link, or the token of the Crafty a lost data disk held.
        refusal = (f"Crafty does not accept the key in {self.token_file}; once Crafty has started, run setup-api-key again, "
                   "and if it still refuses, remove that file first")
        for case in ("empty", "a link", "another Crafty's"):
            with self.subTest(case=case):
                self.api = FakeFreshCrafty()
                if case == "a link":
                    elsewhere = self.root / "elsewhere"
                    elsewhere.write_text(FakeFreshCrafty.KEY)
                    self.token_file.symlink_to(elsewhere)
                else:
                    self.token_file.write_text("" if case == "empty" else "other." + "k" * 40)
                code, output = self.setup_api_key()
                self.assertEqual((code, json.loads(output)), (1, {"error": refusal}))
                self.assertEqual(self.creds.read_text(), self.login)
                self.assertFalse(os.path.lexists(self.admin_file))
                self.assertNotIn(FakeFreshCrafty.KEY, output)
                if case != "another Crafty's":
                    self.assertEqual(self.api.sent, [])  # no token that could not be Crafty's is ever sent
                self.token_file.unlink()
        # Once that file is gone, the setup runs from the start.
        code, _ = self.setup_api_key()
        self.assertEqual(code, 0)
        self.assertEqual(self.token_file.read_text(), FakeFreshCrafty.KEY)

    def test_finishing_the_move_never_replaces_another_admin_file_or_follows_a_link(self):
        self.token_file.write_text(FakeFreshCrafty.KEY)
        self.admin_file.write_text("earlier")
        code, output = self.setup_api_key()
        refusal = f"{self.admin_file} is not a plain copy of the login in default-creds.txt; move it away, then run setup-api-key again"
        self.assertEqual((code, json.loads(output)), (1, {"error": refusal}))
        self.assertEqual(self.admin_file.read_text(), "earlier")
        self.assertEqual(self.creds.read_text(), self.login)
        # A link counts as another file, even to a copy of this same login.
        self.admin_file.unlink()
        copy = self.root / "copy.json"
        copy.write_text(self.login)
        self.admin_file.symlink_to(copy)
        code, output = self.setup_api_key()
        self.assertEqual((code, json.loads(output)), (1, {"error": refusal}))
        self.assertTrue(self.admin_file.is_symlink())
        self.assertEqual(self.creds.read_text(), self.login)
        self.admin_file.unlink()
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_text(self.login)
        self.creds.unlink()
        self.creds.symlink_to(elsewhere)
        code, output = self.setup_api_key()
        self.assertEqual(code, 1)
        self.assertIn("default-creds.txt does not hold Crafty's first admin login", json.loads(output)["error"])
        self.assertNotIn(FIRST_ADMIN_PHRASE, output)
        self.assertFalse(os.path.lexists(self.admin_file))
        self.assertTrue(self.creds.is_symlink())

    def test_without_crafty_s_first_login_nothing_is_asked(self):
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_text(self.login)
        cases = {"missing": None, "not JSON": "admin " + FIRST_ADMIN_PHRASE, "no password": json.dumps({"username": "admin"}),
                 "a link": elsewhere}
        for case, content in cases.items():
            with self.subTest(case=case):
                self.creds.unlink(missing_ok=True)
                if isinstance(content, Path):
                    self.creds.symlink_to(content)
                elif content is not None:
                    self.creds.write_text(content)
                code, output = self.setup_api_key()
                self.assertEqual(code, 1)
                self.assertIn("default-creds.txt does not hold Crafty's first admin login", json.loads(output)["error"])
                self.assertNotIn(FIRST_ADMIN_PHRASE, output)
                self.assertEqual(self.api.sent, [])
                self.assertFalse(os.path.lexists(self.token_file))

    def test_answers_crafty_should_never_give_store_nothing(self):
        cases = {
            "a login without a token": ("login", {"user_id": "1"}),
            "a user ID that is a path": ("login", {"token": FakeFreshCrafty.SESSION, "user_id": "1/../2"}),
            "a session token on two lines": ("login", {"token": FakeFreshCrafty.SESSION + "\nX: 1", "user_id": "1"}),
            "no key ID": ("key", {}),
            "a key ID that is true": ("key", {"id": True}),
            "a key ID that is a path": ("key", {"id": "7/../8"}),
            "no token": ("token", None),
            "a token that is not text": ("token", {"token": FakeFreshCrafty.KEY}),
            "a token with a space": ("token", "key " + "k" * 40),
            "a token Crafty then refuses": ("token", "other." + "k" * 40),
        }
        for case, (step, answer) in cases.items():
            with self.subTest(case=case):
                self.api = FakeFreshCrafty()
                self.api.answers[step] = answer
                code, output = self.setup_api_key()
                self.assertEqual(code, 1)
                self.assertNothingSetUp(output)

    def test_a_refused_login_says_to_retry_once_crafty_has_started(self):
        self.login = json.dumps({"username": "admin", "password": "changed-since-install"})
        self.creds.write_text(self.login)
        code, output = self.setup_api_key()
        self.assertEqual((code, json.loads(output)), (1, {
            "error": "Crafty did not accept its first admin login; once Crafty has started, run setup-api-key again"}))
        self.assertNothingSetUp(output)
        self.assertEqual(len(self.api.sent), 1)


class CraftyAPITests(unittest.TestCase):
    def api(self, answer):
        api = crafty.CraftyAPI("https://127.0.0.1:8443/", "token")
        sent = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener:
            def open(self, request, timeout):
                sent.append(request)
                return Response(answer)

        api.opener = Opener()
        return api, sent

    def test_console_lines_travel_as_the_raw_request_body(self):
        api, sent = self.api(b'{"status": "ok"}')
        api.request("POST", f"/api/v2/servers/{SERVER_ID}/stdin", text="whitelist add Sample_Java")
        self.assertEqual(sent[0].full_url, f"https://127.0.0.1:8443/api/v2/servers/{SERVER_ID}/stdin")
        self.assertEqual(sent[0].data, b"whitelist add Sample_Java")
        self.assertEqual(sent[0].get_header("Content-type"), "text/plain; charset=utf-8")
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer token")

    def test_a_request_without_a_token_carries_no_authorization(self):
        api, sent = self.api(b'{"status": "ok", "data": {}}')
        api.token = None
        api.request("POST", "/api/v2/auth/login", {"username": "admin", "password": FIRST_ADMIN_PHRASE})
        self.assertFalse(sent[0].has_header("Authorization"))
        self.assertEqual(json.loads(sent[0].data), {"username": "admin", "password": FIRST_ADMIN_PHRASE})

    def test_a_stopped_server_refuses_the_console_line(self):
        api, _ = self.api(b'{"status": "error", "error": "SERVER_NOT_RUNNING"}')
        with self.assertRaises(crafty.CraftyError):
            api.request("POST", f"/api/v2/servers/{SERVER_ID}/stdin", text="whitelist reload")


class HostTests(unittest.TestCase):
    def test_works_in_finds_a_process_in_its_folder_wherever_the_folder_moved(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder, moved, other = Path(scratch) / "server", Path(scratch) / "moved", Path(scratch) / "other"
            folder.mkdir()
            other.mkdir()
            game = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=folder)
            try:
                host = crafty.Host()
                self.assertTrue(host.works_in(folder))
                folder.rename(moved)
                self.assertTrue(host.works_in(moved))
                self.assertFalse(host.works_in(other))
            finally:
                game.kill()
                game.wait()
            self.assertFalse(host.works_in(moved))

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


    def test_download_streams_from_the_aws_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "aws"
            fake.write_text(f"#!/bin/sh\necho \"$@\" > {directory}/args\nprintf hello\n")
            fake.chmod(0o755)
            received = []
            with unittest.mock.patch.dict(os.environ, {"PATH": f"{directory}:{os.environ['PATH']}"}):
                crafty.Host().download("us-west-2", "bucket", "a/b.tar.gz", lambda source: received.append(source.read()))
            self.assertEqual(received, [b"hello"])
            self.assertIn("s3 cp s3://bucket/a/b.tar.gz -", (Path(directory) / "args").read_text())

    def test_a_download_that_never_finishes_is_killed_and_reported(self):
        process = unittest.mock.Mock(stdout=io.BytesIO(b""))
        process.wait.side_effect = [subprocess.TimeoutExpired("aws", 1800), 0]
        with unittest.mock.patch.object(crafty.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(crafty.CraftyError, "^AWS download did not finish$"):
                crafty.Host().download("us-west-2", "bucket", "a/b.tar.gz", lambda source: source.read())
        process.kill.assert_called_once_with()


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

    def player_file(self, directory, value, mode=0o600):
        path = Path(directory) / "player.json"
        path.write_text(json.dumps(value))
        path.chmod(mode)
        return path

    def test_cli_runs_the_launchers_allowlist_command(self):
        # The exact command line lambda/launcher/allowlist.go sends, answered with the four fields it checks.
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(config(Path(directory)))
            server_dir = Path(directory) / "crafty" / "servers" / SERVER_ID
            server_dir.mkdir(parents=True)
            (server_dir / "whitelist.json").write_text("[]")
            api = FakeAPI(running=True)
            api.on_console = lambda line: (server_dir / "whitelist.json").write_text(json.dumps([JAVA]))
            player = self.player_file(directory, JAVA)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = crafty.main(["--config", str(path), "allowlist", "family", "add", "--edition", "java",
                                    "--player-file", str(player)], api=api, host=FakeHost(api))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"instance": "family", "edition": "java", "operation": "add", "allowlist": "updated"})

    def test_cli_refuses_a_shared_or_malformed_player_file_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(config(Path(directory)))
            for value, mode in ((JAVA, 0o644), ("[1, 2]", 0o600), ({"uuid": "bad", "name": "Sample_Java"}, 0o600)):
                api = FakeAPI(running=True)
                player = self.player_file(directory, value, mode)
                output = io.StringIO()
                with self.subTest(value=value, mode=mode), contextlib.redirect_stdout(output):
                    code = crafty.main(["--config", str(path), "allowlist", "family", "remove", "--edition", "java",
                                        "--player-file", str(player)], api=api, host=FakeHost(api))
                self.assertEqual(code, 1)
                self.assertIn("error", json.loads(output.getvalue()))
                self.assertNotIn("Sample_Java", output.getvalue())
                self.assertEqual(api.console, [])

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
        # Craig approved both Bedrock transports with built-in signaling on 2026-10-03 (#30), activated by #26.
        self.assertEqual(crafty.bedrock_transport(manifest), "both")
        self.assertEqual(manifest["managed_files"][0]["sha256"], "961fc91c79c38f8e09fd14d1e959b06522ec72b595b933e78fef83563e4b8e42")

    def test_managed_file_outside_the_server_folder_is_refused(self):
        manifest = json.loads((ROOT / "runtime" / "crafty" / "server.json").read_text())
        manifest["managed_files"][0]["path"] = "../../etc/cron.d/x"
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(manifest, handle)
            handle.flush()
            with self.assertRaises(crafty.CraftyError):
                crafty.load_manifest(Path(handle.name))

    def test_an_artifact_name_that_is_not_a_plain_word_is_refused(self):
        manifest = json.loads((ROOT / "runtime" / "crafty" / "server.json").read_text())
        manifest["artifacts"].append(dict(manifest["artifacts"][2], name="../../servers/x", path="plugins/Extra.jar"))
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(manifest, handle)
            handle.flush()
            with self.assertRaisesRegex(crafty.CraftyError, "plain unique name"):
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
            host = FakeHost(api)
            adapter = crafty.Adapter(config(Path(directory)), api, host)
            folder = Path(directory) / "crafty" / "servers" / SERVER_ID
            waits, sleep = [], host.sleep

            def unpack_later(seconds):  # Crafty unpacks in the background after the request returns
                waits.append(seconds)
                sleep(seconds)
                if len(waits) == 2:
                    folder.mkdir(parents=True)
                    (folder / "paper.jar").write_bytes(b"jar")

            host.sleep = unpack_later
            self.assertEqual(adapter.import_server("family", "family.zip", 5), {"instance": "family", "server_id": SERVER_ID})
            self.assertEqual(len(waits), 2)
            (body,) = api.created
            self.assertEqual(body["name"], "family")
            imported = body["minecraft_java_create_data"]["import_server_create_data"]
            self.assertEqual((imported["archive_name"], imported["jarfile"], imported["mem_max"], imported["server_properties_port"]),
                             ("family.zip", "paper.jar", 5, 25565))

    def test_import_server_fails_when_crafty_never_unpacks_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            api.no_servers = True
            adapter = crafty.Adapter(config(Path(directory)), api, FakeHost(api))
            with self.assertRaises(crafty.CraftyError):
                adapter.import_server("family", "family.zip", 5)

    def test_import_server_rerun_resumes_waiting_for_an_unfinished_import(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()  # the server record exists, but its files are not unpacked yet
            host = FakeHost(api)
            adapter = crafty.Adapter(config(Path(directory)), api, host)
            folder = Path(directory) / "crafty" / "servers" / SERVER_ID
            sleep = host.sleep

            def unpack_later(seconds):
                sleep(seconds)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "paper.jar").write_bytes(b"jar")

            host.sleep = unpack_later
            self.assertEqual(adapter.import_server("family", "family.zip", 5), {"instance": "family", "server_id": SERVER_ID})
            self.assertEqual(api.created, [])

    def test_import_server_refuses_an_existing_server_or_a_path(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            adapter = crafty.Adapter(config(Path(directory)), api, FakeHost(api))
            folder = Path(directory) / "crafty" / "servers" / SERVER_ID
            folder.mkdir(parents=True)
            (folder / "paper.jar").write_bytes(b"jar")
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
