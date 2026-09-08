from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import minecraft_instance as runtime


class RuntimeLockDirectoryTests(unittest.TestCase):
    def test_first_creation_and_repeat_use_preserve_private_lock_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            directory = runtime.ensure_runtime_lock_directory(parent)
            self.assertEqual(directory, parent / "minecraft")
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            with runtime.operation_lock(directory / "runtime.lock"):
                self.assertEqual(runtime.ensure_runtime_lock_directory(parent), directory)
                with self.assertRaisesRegex(runtime.OperationError, "another runtime"):
                    with runtime.operation_lock(directory / "runtime.lock", timeout=0.01):
                        self.fail("competing process acquired the shared lock")

    def test_public_foreign_or_symlink_parent_cannot_host_the_lock(self):
        for condition in ("public", "foreign", "symlink"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary) / "run"
                parent.mkdir()
                if condition == "public":
                    parent.chmod(0o777)
                elif condition == "symlink":
                    target = Path(temporary) / "untrusted"
                    parent.rename(target)
                    parent.symlink_to(target)
                owner = mock.patch.object(runtime.os, "geteuid", return_value=os.geteuid() + 1) if condition == "foreign" else contextlib.nullcontext()
                with owner, self.assertRaises(runtime.OperationError):
                    runtime.ensure_runtime_lock_directory(parent)
                self.assertFalse((parent / "minecraft").exists())

    def test_existing_untrusted_directory_is_rejected_without_repair(self):
        for condition in ("public", "foreign", "symlink", "file"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary)
                directory = parent / "minecraft"
                if condition == "symlink":
                    directory.symlink_to(parent / "untrusted")
                elif condition == "file":
                    directory.write_text("preserve")
                else:
                    directory.mkdir(mode=0o755 if condition == "public" else 0o700)
                original = os.lstat
                def inspect(path):
                    metadata = original(path)
                    if condition == "foreign" and Path(path) == directory:
                        values = list(metadata)
                        values[4] += 1
                        return os.stat_result(values)
                    return metadata
                with mock.patch.object(runtime.os, "lstat", side_effect=inspect), self.assertRaises(runtime.OperationError):
                    runtime.ensure_runtime_lock_directory(parent)
                self.assertFalse((directory / "runtime.lock").exists())
                if condition == "public":
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_linux_machine_uses_the_private_shared_lock_path(self):
        machine = object.__new__(runtime.LinuxMachine)
        with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                mock.patch.object(runtime, "ensure_runtime_lock_directory", return_value=Path("/run/minecraft")), \
                mock.patch.object(runtime, "operation_lock", return_value=contextlib.nullcontext()) as lock:
            with machine.lock():
                pass
        lock.assert_called_once_with(Path("/run/minecraft/runtime.lock"))


if __name__ == "__main__":
    unittest.main()
