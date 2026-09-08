from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import minecraft_instance as runtime  # noqa: E402


class PrivateConfigurationTests(unittest.TestCase):
    def test_private_regular_file_owned_by_operator_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator.json"
            path.write_text(json.dumps({"eula_accepted": True}))
            path.chmod(0o600)
            self.assertEqual(runtime.private_json(path), {"eula_accepted": True})

    def test_file_owned_by_another_user_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator.json"
            path.write_text("{}")
            path.chmod(0o600)
            with mock.patch.object(runtime.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(runtime.OperationError):
                    runtime.private_json(path)

    def test_public_file_symlink_and_fifo_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public.json"
            public.write_text("{}")
            public.chmod(0o644)
            link = root / "link.json"
            link.symlink_to(public)
            fifo = root / "fifo.json"
            os.mkfifo(fifo, 0o600)
            for path in (public, link, fifo):
                with self.subTest(path=path.name), self.assertRaises(runtime.OperationError):
                    runtime.private_json(path)

    def test_lock_rejects_symlink_or_foreign_owner_before_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("unchanged")
            link = root / "runtime.lock"
            link.symlink_to(target)
            with self.assertRaises(runtime.OperationError):
                with runtime.operation_lock(link):
                    self.fail("acquired a symlink lock")
            self.assertEqual(target.read_text(), "unchanged")
            with mock.patch.object(runtime.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(runtime.OperationError):
                    with runtime.operation_lock(target):
                        self.fail("acquired a lock owned by another user")


if __name__ == "__main__":
    unittest.main()
