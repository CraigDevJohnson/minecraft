from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import minecraft_instance as runtime


class RetainedPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.instance = self.root / "instances" / "survival"
        (self.instance / "data").mkdir(parents=True)
        (self.instance / "runtime").mkdir()
        device = self.root.stat().st_dev
        self.mount = {"target": str(self.root), "maj:min": f"{os.major(device)}:{os.minor(device)}",
                      "fsroot": "/", "options": "rw,nosuid,nodev"}

    def check(self):
        def findmnt(argv, **kwargs):
            self.assertEqual(argv[:5], ["findmnt", "--kernel", "--json", "--submounts", "--mountpoint"])
            return json.dumps({"filesystems": [self.mount]})
        with mock.patch.object(runtime, "command", side_effect=findmnt):
            runtime.verify_retained_paths(self.root)

    def test_same_filesystem_data_and_control_paths_are_accepted(self):
        self.check()

    def test_nested_root_disk_and_same_device_bind_mounts_are_rejected(self):
        for path, device in (("data", "259:0"), ("runtime", self.mount["maj:min"]),
                             ("data/world", self.mount["maj:min"])):
            with self.subTest(path=path, device=device):
                self.mount["children"] = [{"target": str(self.instance / path), "maj:min": device}]
                with self.assertRaisesRegex(runtime.OperationError, "nested mounts"):
                    self.check()

    def test_symlink_to_another_data_directory_is_rejected(self):
        data = self.instance / "data"
        data.rmdir()
        data.symlink_to(self.instance / "runtime", target_is_directory=True)
        with self.assertRaisesRegex(runtime.OperationError, "symlink"):
            self.check()

    def test_mismatched_or_readonly_root_snapshot_is_rejected(self):
        for field, value in (("maj:min", "987:654"), ("options", "ro"), ("fsroot", "/other")):
            with self.subTest(field=field):
                previous = self.mount[field]
                self.mount[field] = value
                with self.assertRaises(runtime.OperationError):
                    self.check()
                self.mount[field] = previous


if __name__ == "__main__":
    unittest.main()
