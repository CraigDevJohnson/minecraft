from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_restore  # noqa: E402
from minecraft_backup import publish  # noqa: E402
from minecraft_content import manifest_digest  # noqa: E402


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def download(self, key, destination, *, max_bytes):
        value = self.objects.get(key)
        if value is None:
            return False
        if len(value) > max_bytes:
            raise minecraft_restore.BackupError("object exceeds its limit")
        Path(destination).write_bytes(value)
        return True

    def upload(self, key, source):
        self.objects.setdefault(key, Path(source).read_bytes())

    def keys(self, prefix):
        return sorted(key for key in self.objects if key.startswith(prefix))

    def delete(self, key):
        self.objects.pop(key, None)


class RestoreMachine:
    def __init__(self, root, manifest, definition):
        self.root = root
        self.definitions = {"survival": definition}
        self.manifests = {"survival": manifest}
        self.deployment = {"data_volume_id": "vol-11111111111111111",
                           "data_filesystem_uuid": "11111111-1111-4111-8111-111111111111"}
        self.container = {"exists": False, "running": False}
        self.storage_verified = False
        self.lock_held = False

    @contextlib.contextmanager
    def lock(self):
        self.lock_held = True
        try:
            yield
        finally:
            self.lock_held = False

    def verify_storage(self):
        if not self.lock_held:
            raise AssertionError("restore storage verification requires the lifecycle lock")
        self.storage_verified = True

    def inspect(self, instance):
        return self.container

    def restore_ownership(self, data):
        pass

    def restore_metadata(self, path):
        # Simulate normalized Linux ownership while retaining real file modes.
        info = path.lstat()
        return SimpleNamespace(st_uid=1000, st_gid=1000, st_mode=info.st_mode)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.target_root = self.directory / "target-volume"
        self.target_root.mkdir()
        self.manifest = json.loads((ROOT / "runtime/content.json").read_text())
        self.definition = json.loads((ROOT / "runtime/survival.json").read_text())
        self.machine = RestoreMachine(self.target_root, self.manifest, self.definition)
        self.store = MemoryStore()
        self.source_deployment = {"backup_bucket_name": "source-backups",
                                  "data_volume_id": "vol-00000000000000000",
                                  "data_filesystem_uuid": "00000000-0000-4000-8000-000000000000"}
        self.checkpoints = {
            "data/world/level.dat": b"survival world checkpoint 73",
            "data/world/playerdata/player.dat": b"inventory: diamond pickaxe",
            "data/world_nether/DIM-1/region/r.0.0.mca": b"nether checkpoint",
            "data/whitelist.json": b'[{"name":"family-player"}]',
            "data/ops.json": b"[]",
            "data/server.properties": b"difficulty=normal\n",
            "data/plugins/floodgate/key.pem": b"private floodgate key",
        }
        identity = {"instance": "survival", "session": "00000000-0000-4000-8000-000000000001",
                    "manifest_sha256": manifest_digest(self.manifest)}
        self.recovery_point = hashlib.sha256(canonical(identity) + b"\n").hexdigest()
        self.archive_key = f"instances/survival/objects/{self.recovery_point}/data.tar.gz"
        self.marker_key = f"instances/survival/completed/{self.recovery_point}.json"
        self.metadata = {"schema_version": 1, "status": "complete", **identity,
                         "definition_sha256": hashlib.sha256(canonical(self.definition)).hexdigest(),
                         "created_at": "2026-09-08T18:00:00Z", "recovery_point": self.recovery_point,
                         "source": {"volume_id": self.source_deployment["data_volume_id"],
                                    "filesystem_uuid": self.source_deployment["data_filesystem_uuid"]},
                         "manifest": self.manifest, "definition": self.definition}
        self.publish()

    def publish(self, *, extra_member=None, replacement=None):
        entries = {}
        for name, value in self.checkpoints.items():
            for parent in Path(name).parents:
                if str(parent) != ".":
                    entries[str(parent)] = {"path": str(parent), "type": "directory"}
            entries[name] = {"path": name, "type": "file", "size_bytes": len(value),
                             "sha256": hashlib.sha256(value).hexdigest()}
        self.metadata["files"] = sorted(entries.values(), key=lambda entry: entry["path"])
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for entry in self.metadata["files"]:
                if replacement and replacement.name == entry["path"]:
                    tar.addfile(replacement)
                    continue
                member = tarfile.TarInfo(entry["path"])
                if entry["type"] == "directory":
                    member.type = tarfile.DIRTYPE
                    tar.addfile(member)
                else:
                    value = self.checkpoints[entry["path"]]
                    member.size = len(value)
                    tar.addfile(member, io.BytesIO(value))
            if extra_member:
                tar.addfile(extra_member)
        payload = archive.getvalue()
        self.metadata["archive"] = {"key": self.archive_key, "size_bytes": len(payload),
                                    "sha256": hashlib.sha256(payload).hexdigest()}
        self.store.objects[self.archive_key] = payload
        self.save_metadata()

    def save_metadata(self):
        self.store.objects[self.marker_key] = canonical(self.metadata)

    def restore(self):
        return minecraft_restore.restore(self.machine, self.source_deployment, "survival",
                                         self.recovery_point, self.store)

    def test_known_world_player_configuration_round_trip_leaves_backup_untouched(self):
        backup_before = dict(self.store.objects)
        result = self.restore()
        target = self.target_root / "instances/survival"
        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["recovery_point"], self.recovery_point)
        for name, expected in self.checkpoints.items():
            self.assertEqual((target / name).read_bytes(), expected)
        self.assertEqual(json.loads((target / "runtime/manifest.json").read_text()), self.manifest)
        self.assertEqual(json.loads((target / "runtime/definition.json").read_text()), self.definition)
        journal = json.loads((target / "operation.json").read_text())
        self.assertEqual(journal["phase"], "prepared")
        self.assertEqual(journal["backup"], "not-required")
        self.assertEqual(journal["restored_from"], self.recovery_point)
        self.assertNotEqual(journal["session"], self.metadata["session"])
        self.assertTrue(self.machine.storage_verified)
        self.assertEqual(self.store.objects, backup_before)

    def test_failed_final_install_leaves_pristine_volume_and_backup_untouched(self):
        backup_before = dict(self.store.objects)
        with mock.patch.object(minecraft_restore.os, "rename", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.restore()
        self.assertEqual(list(self.target_root.iterdir()), [])
        self.assertEqual(self.store.objects, backup_before)

    def test_missing_and_corrupted_inputs_never_alter_the_target(self):
        original_metadata = copy.deepcopy(self.metadata)
        original_objects = dict(self.store.objects)
        for scenario in ("marker-missing", "archive-missing", "marker-corrupt", "manifest-corrupt",
                         "archive-corrupt", "file-checksum-corrupt", "required-file-missing"):
            with self.subTest(scenario=scenario):
                self.metadata = copy.deepcopy(original_metadata)
                self.store.objects = dict(original_objects)
                if scenario == "marker-missing":
                    del self.store.objects[self.marker_key]
                elif scenario == "archive-missing":
                    del self.store.objects[self.archive_key]
                elif scenario == "marker-corrupt":
                    self.store.objects[self.marker_key] = b"{invalid JSON"
                elif scenario == "manifest-corrupt":
                    self.metadata["manifest"]["revision"] = "wrong-version"
                    self.save_metadata()
                elif scenario == "archive-corrupt":
                    self.store.objects[self.archive_key] = b"broken archive"
                elif scenario == "file-checksum-corrupt":
                    entry = next(item for item in self.metadata["files"] if item["path"] == "data/world/level.dat")
                    entry["sha256"] = "0" * 64
                    self.save_metadata()
                else:
                    self.metadata["files"] = [item for item in self.metadata["files"] if item["path"] != "data/ops.json"]
                    self.save_metadata()
                backup_before = dict(self.store.objects)
                with self.assertRaises(minecraft_restore.BackupError):
                    self.restore()
                self.assertEqual(list(self.target_root.iterdir()), [])
                self.assertEqual(self.store.objects, backup_before)

    def test_archive_links_devices_duplicates_and_traversal_are_rejected(self):
        for member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(type=member_type):
                member = tarfile.TarInfo("data/world/level.dat")
                member.type = member_type
                member.linkname = "../../../outside"
                self.publish(replacement=member)
                with self.assertRaises(minecraft_restore.BackupError):
                    self.restore()
                self.assertEqual(list(self.target_root.iterdir()), [])
        for name in ("data/world/level.dat", "../outside", "/tmp/outside"):
            with self.subTest(name=name):
                self.publish(extra_member=tarfile.TarInfo(name))
                with self.assertRaises(minecraft_restore.BackupError):
                    self.restore()
                self.assertEqual(list(self.target_root.iterdir()), [])

    def test_existing_target_and_container_are_preserved(self):
        target = self.target_root / "instances/survival"
        target.mkdir(parents=True)
        checkpoint = target / "existing-world"
        checkpoint.write_bytes(b"keep the existing target")
        with self.assertRaises(minecraft_restore.BackupError):
            self.restore()
        self.assertEqual(checkpoint.read_bytes(), b"keep the existing target")
        checkpoint.unlink()
        target.rmdir()
        self.machine.container = {"exists": True, "running": True}
        with self.assertRaises(minecraft_restore.BackupError):
            self.restore()
        self.assertFalse(target.exists())

    def test_source_volume_and_filesystem_cannot_be_reused_for_restore(self):
        deployment = dict(self.machine.deployment)
        for field in ("data_volume_id", "data_filesystem_uuid"):
            with self.subTest(field=field):
                self.machine.deployment = {**deployment, field: self.source_deployment[field]}
                with self.assertRaises(minecraft_restore.BackupError):
                    self.restore()
                self.assertEqual(list(self.target_root.iterdir()), [])

    def test_wrong_source_and_incompatible_target_reject_before_installation(self):
        original_source = dict(self.source_deployment)
        for scenario in ("source", "manifest", "definition"):
            with self.subTest(scenario=scenario):
                self.source_deployment = dict(original_source)
                self.machine.manifests = {"survival": copy.deepcopy(self.manifest)}
                self.machine.definitions = {"survival": copy.deepcopy(self.definition)}
                if scenario == "source":
                    self.source_deployment["data_volume_id"] = "vol-22222222222222222"
                elif scenario == "manifest":
                    self.machine.manifests["survival"]["revision"] = "a-different-revision"
                else:
                    self.machine.definitions["survival"]["java_port"] = 25566
                with self.assertRaises(minecraft_restore.BackupError):
                    self.restore()
                self.assertEqual(list(self.target_root.iterdir()), [])

    def test_failed_data_write_does_not_create_target_and_can_be_retried(self):
        original = self.machine.restore_ownership
        def fail_after_extracting(data):
            self.assertEqual((data / "world/level.dat").read_bytes(), self.checkpoints["data/world/level.dat"])
            raise minecraft_restore.BackupError("simulated staging filesystem failure")
        self.machine.restore_ownership = fail_after_extracting
        backup_before = dict(self.store.objects)
        with self.assertRaises(minecraft_restore.BackupError):
            self.restore()
        self.assertEqual(list(self.target_root.iterdir()), [])
        self.assertEqual(self.store.objects, backup_before)
        self.machine.restore_ownership = original
        self.assertEqual(self.restore()["status"], "restored")

    def test_published_cold_backup_restores_known_checkpoints_without_changing_source(self):
        source = self.directory / "source-volume/instances/survival"
        for name, contents in self.checkpoints.items():
            filename = source / name
            filename.parent.mkdir(parents=True, exist_ok=True)
            filename.write_bytes(contents)
        (source / "runtime").mkdir()
        (source / "runtime/manifest.json").write_bytes(canonical(self.manifest))
        (source / "runtime/definition.json").write_bytes(canonical(self.definition))
        (source / "operation.json").write_bytes(canonical({"phase": "stopped", "backup": "pending",
            "session": self.metadata["session"], "revision": self.metadata["manifest_sha256"]}))
        request = {"schema_version": 1, "instance": "survival", "session": self.metadata["session"],
                   "data_path": str(source / "data"), "manifest_path": str(source / "runtime/manifest.json"),
                   "definition_path": str(source / "runtime/definition.json"),
                   "manifest_sha256": self.metadata["manifest_sha256"],
                   "destination": {"bucket": "source-backups", "prefix": "instances/survival/"},
                   "source": self.metadata["source"]}
        before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
        self.store.objects.clear()
        workspace = self.directory / "publisher"
        workspace.mkdir()
        receipt = publish(request, self.store, workspace, now="2026-09-08T18:00:00Z")
        self.assertEqual(receipt["recovery_point"], self.recovery_point)
        backup_before = dict(self.store.objects)
        self.restore()
        for name, expected in self.checkpoints.items():
            self.assertEqual((self.target_root / "instances/survival" / name).read_bytes(), expected)
        self.assertEqual(before, {str(path.relative_to(source)): path.read_bytes()
                                  for path in source.rglob("*") if path.is_file()})
        self.assertEqual(self.store.objects, backup_before)


if __name__ == "__main__":
    unittest.main()
