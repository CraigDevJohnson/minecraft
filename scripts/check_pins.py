#!/usr/bin/env python3
"""Download every file the pinned server content names and check its SHA-256.

The repository's own checks run offline, so they can only tell that a pin is well formed. The Pins workflow runs
this where the downloads are possible, and it fails when a URL does not answer the file its pin names.

With --modrinth-version it prints the pin for one Modrinth version instead, for adding a mod Modrinth hosts: the
file's URL and name, and the SHA-256 of the file as downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "runtime" / "crafty" / "server.json"
USER_AGENT = "CraigMinecraft/1.0 (https://github.com/CraigDevJohnson/minecraft)"
MODRINTH_API = "https://api.modrinth.com/v2/version/"
MODRINTH_FILES = "cdn.modrinth.com"
MODRINTH_VERSION = re.compile(r"[A-Za-z0-9]{8}")
SHA256 = re.compile(r"[0-9a-f]{64}")
# The largest pinned file is Paper, under 100 MiB. Nothing a pin names should come near this.
DOWNLOAD_LIMIT = 512 * 1024 * 1024


class PinError(Exception):
    """A pin that cannot be checked."""


def pins(value, label: str = "") -> list:
    """Every (label, url, sha256) in the content: each object that carries both a url and a sha256."""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("url"), str) and isinstance(value.get("sha256"), str):
            found.append((str(value.get("name") or label or value["url"]), value["url"], value["sha256"]))
        for key, item in value.items():
            found += pins(item, str(key))
    elif isinstance(value, list):
        for item in value:
            found += pins(item, label)
    return found


def fetch(url: str) -> dict:
    """The SHA-256, SHA-512 and size of what url answers."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise PinError("a pin must be an HTTPS URL")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    sha256, sha512, size = hashlib.sha256(), hashlib.sha512(), 0
    with urllib.request.urlopen(request, timeout=60) as response:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > DOWNLOAD_LIMIT:
                raise PinError("the download exceeded its size limit")
            sha256.update(chunk)
            sha512.update(chunk)
    return {"sha256": sha256.hexdigest(), "sha512": sha512.hexdigest(), "size": size}


def annotate(level: str, title: str, text: str) -> None:
    """Show text on the run's summary page too, where GitHub Actions runs this."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={title}::{text}")


def check(manifest: Path, *, fetch=fetch) -> int:
    """Check every pin in the manifest, and answer how many failed."""
    found = pins(json.loads(manifest.read_text()))
    if not found:
        raise PinError("the pinned content names no downloads")
    failed = 0
    for label, url, expected in found:
        try:
            actual = fetch(url)["sha256"]
        except (OSError, PinError) as exc:
            actual = f"download failed ({exc})"
        if actual == expected:
            print(f"ok       {label}  {expected}")
            continue
        failed += 1
        print(f"MISMATCH {label}  pinned {expected}  downloaded {actual}  {url}")
        annotate("error", "Pin mismatch", f"{label}: pinned {expected}, downloaded {actual}")
    return failed


def read_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read(1024 * 1024))


def modrinth_pin(version: str, *, read_json=read_json, fetch=fetch) -> dict:
    """The pin for a Modrinth version's primary file, checked against the SHA-512 Modrinth publishes for it."""
    if not MODRINTH_VERSION.fullmatch(version):
        raise PinError("a Modrinth version id is eight letters or digits")
    described = read_json(MODRINTH_API + version)
    files = described.get("files") or []
    chosen = next((item for item in files if item.get("primary")), files[0] if files else None)
    if chosen is None:
        raise PinError("the Modrinth version has no file")
    url, name = str(chosen.get("url", "")), str(chosen.get("filename", ""))
    if urllib.parse.urlsplit(url).netloc != MODRINTH_FILES or not re.fullmatch(r"[A-Za-z0-9._+-]+\.jar", name):
        raise PinError("the Modrinth version's file is not a jar on Modrinth's own file host")
    downloaded = fetch(url)
    if downloaded["sha512"] != chosen.get("hashes", {}).get("sha512"):
        raise PinError("the download does not match the SHA-512 Modrinth publishes")
    return {"version": str(described.get("version_number", "")), "file": name, "url": url, "sha256": downloaded["sha256"],
            "game_versions": described.get("game_versions", []), "loaders": described.get("loaders", [])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--modrinth-version", help="print the pin for this Modrinth version id instead")
    args = parser.parse_args(argv)
    try:
        if args.modrinth_version:
            pin = modrinth_pin(args.modrinth_version)
            print(json.dumps(pin, indent=2, sort_keys=True))
            annotate("notice", "Modrinth pin", json.dumps(pin, sort_keys=True))
            return 0
        failed = check(args.manifest)
    except (OSError, ValueError, PinError) as exc:
        print(f"cannot check the pins: {exc}", file=sys.stderr)
        annotate("error", "Pins", f"cannot check the pins: {exc}")
        return 1
    if failed:
        print(f"{failed} pinned download(s) did not match", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
