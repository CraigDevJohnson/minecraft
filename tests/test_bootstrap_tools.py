from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bootstrap_tools  # noqa: E402
from bootstrap_tools import install_payload  # noqa: E402


def tar_payload(member: tarfile.TarInfo, contents: bytes = b"") -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(contents) if member.isreg() else None)
    return payload.getvalue()


class InstallPayloadTests(unittest.TestCase):
    def test_installs_expected_regular_archive_member(self) -> None:
        member = tarfile.TarInfo("release/tool")
        member.size = len(b"new executable")
        payload = tar_payload(member, b"new executable")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tool"
            install_payload(
                payload,
                hashlib.sha256(payload).hexdigest(),
                "tar.gz",
                "release/tool",
                destination,
            )
            self.assertEqual(destination.read_bytes(), b"new executable")
            self.assertTrue(destination.stat().st_mode & 0o100)

    def test_wrong_hash_preserves_existing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tool"
            destination.write_bytes(b"existing executable")
            with self.assertRaises(ValueError):
                install_payload(b"invalid download", "0" * 64, "raw", None, destination)
            self.assertEqual(destination.read_bytes(), b"existing executable")

    def test_missing_member_preserves_existing_executable(self) -> None:
        member = tarfile.TarInfo("release/other")
        member.size = len(b"wrong file")
        payload = tar_payload(member, b"wrong file")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tool"
            destination.write_bytes(b"existing executable")
            with self.assertRaises(ValueError):
                install_payload(
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    "tar.gz",
                    "release/tool",
                    destination,
                )
            self.assertEqual(destination.read_bytes(), b"existing executable")

    def test_non_regular_member_preserves_existing_executable(self) -> None:
        member = tarfile.TarInfo("release/tool")
        member.type = tarfile.SYMTYPE
        member.linkname = "other"
        payload = tar_payload(member)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tool"
            destination.write_bytes(b"existing executable")
            with self.assertRaises(ValueError):
                install_payload(
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    "tar.gz",
                    "release/tool",
                    destination,
                )
            self.assertEqual(destination.read_bytes(), b"existing executable")

    def test_download_failure_preserves_existing_executable(self) -> None:
        entry = {
            "url": "https://example.invalid/tool",
            "sha256": hashlib.sha256(b"new executable").hexdigest(),
            "archive_kind": "raw",
            "member": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tool"
            destination.write_bytes(b"existing executable")
            with mock.patch.object(
                bootstrap_tools.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                with self.assertRaises(urllib.error.URLError):
                    bootstrap_tools._download_and_install("tool", entry, destination)
            self.assertEqual(destination.read_bytes(), b"existing executable")


if __name__ == "__main__":
    unittest.main()
