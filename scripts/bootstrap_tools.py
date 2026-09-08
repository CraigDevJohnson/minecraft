#!/usr/bin/env python3
"""Install the repository's pinned command-line tools."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "tools" / "lock.json"
BIN_DIR = ROOT / ".tools" / "bin"


def _payload_member(payload: bytes, archive_kind: str, member: str | None) -> bytes:
    if archive_kind == "raw":
        if member is not None:
            raise ValueError("raw payload must not name an archive member")
        return payload
    if not member:
        raise ValueError(f"{archive_kind} payload requires an exact member path")

    if archive_kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                matches = [entry for entry in archive.infolist() if entry.filename == member]
                if len(matches) != 1:
                    raise ValueError(f"expected one regular archive member {member!r}")
                entry = matches[0]
                file_type = stat.S_IFMT(entry.external_attr >> 16)
                if entry.is_dir() or file_type not in (0, stat.S_IFREG):
                    raise ValueError(f"archive member {member!r} is not a regular file")
                return archive.read(entry)
        except zipfile.BadZipFile as error:
            raise ValueError("invalid zip payload") from error

    if archive_kind == "tar.gz":
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                matches = [entry for entry in archive.getmembers() if entry.name == member]
                if len(matches) != 1 or not matches[0].isreg():
                    raise ValueError(f"expected one regular archive member {member!r}")
                extracted = archive.extractfile(matches[0])
                if extracted is None:
                    raise ValueError(f"could not read archive member {member!r}")
                return extracted.read()
        except (tarfile.TarError, EOFError) as error:
            raise ValueError("invalid tar.gz payload") from error

    raise ValueError(f"unsupported archive kind: {archive_kind}")


def install_payload(
    payload: bytes,
    expected_sha256: str,
    archive_kind: str,
    member: str | None,
    destination: Path,
) -> None:
    """Verify and atomically install one executable payload."""
    normalized_hash = expected_sha256.lower()
    if len(normalized_hash) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise ValueError("expected SHA256 must contain exactly 64 hexadecimal characters")
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != normalized_hash:
        raise ValueError(f"SHA256 mismatch: expected {normalized_hash}, received {actual_hash}")

    executable = _payload_member(payload, archive_kind, member)
    if not executable:
        raise ValueError("tool executable is empty")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}."
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(executable)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(0o755)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _platform_key() -> str:
    systems = {"Darwin": "darwin", "Linux": "linux"}
    machines = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "amd64",
        "x86_64": "amd64",
    }
    system = systems.get(platform.system())
    machine = machines.get(platform.machine().lower())
    if system is None or machine is None:
        raise ValueError(
            f"unsupported platform: {platform.system()} {platform.machine()} "
            "(supported: macOS/Linux on arm64/amd64)"
        )
    return f"{system}-{machine}"


def _download_and_install(tool: str, entry: dict[str, Any], destination: Path) -> None:
    url = entry.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"{tool}: URL must use HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()
    install_payload(
        payload,
        entry.get("sha256", ""),
        entry.get("archive_kind", ""),
        entry.get("member"),
        destination,
    )


def main() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    manifest = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("tools"), dict):
        raise ValueError("unsupported tool lock manifest")

    platform_key = _platform_key()
    for tool, configuration in manifest["tools"].items():
        try:
            entry = configuration["platforms"][platform_key]
            version = configuration["version"]
        except (KeyError, TypeError) as error:
            raise ValueError(f"{tool}: missing configuration for {platform_key}") from error
        _download_and_install(tool, entry, BIN_DIR / tool)
        print(f"installed {tool} {version} for {platform_key}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"tool bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
