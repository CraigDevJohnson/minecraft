#!/usr/bin/env python3
"""Build a Lambda function (the idle stop or the launcher) for ARM64 and write a reproducible deployment zip.

The launcher's zip also carries the host adapter it installs on the host before each start or allowlist change
(docs/adr/0007-the-launcher-carries-the-host-adapter.md), in its adapter folder.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = {
    "idle-stop": ROOT / "lambda" / "idlestop",
    "launcher": ROOT / "lambda" / "launcher",
}
# The files scripts/install_crafty.sh installs as the host adapter, the same list as adapterFiles in
# lambda/launcher/hostadapter.go; tests/test_launcher_package.py keeps the three in step.
ADAPTER_FILES = ("minecraft_access.py", "minecraft_content.py", "minecraft_crafty.py")
# A fixed timestamp and mode keep the archive bytes, and so the Lambda code hash, stable across rebuilds.
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def bundled(source: Path) -> list[tuple[str, Path]]:
    """Answer the files a function's zip carries beside its binary, as (name in the zip, file) pairs."""
    if source == FUNCTIONS["launcher"]:
        return [(f"adapter/{name}", ROOT / "scripts" / name) for name in ADAPTER_FILES]
    return []


def write_archive(output: Path, binary: Path, files: list[tuple[str, Path]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    with zipfile.ZipFile(partial, "w") as archive:
        for name, path, mode in [("bootstrap", binary, 0o100755), *((name, path, 0o100644) for name, path in files)]:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    partial.replace(output)


def build(output: Path, source: Path = FUNCTIONS["idle-stop"]) -> str:
    with tempfile.TemporaryDirectory(prefix="lambda-") as directory:
        binary = Path(directory) / "bootstrap"
        environment = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64", "GOFLAGS": "-mod=readonly"}
        subprocess.run(
            ["go", "build", "-trimpath", "-buildvcs=false", "-tags", "lambda.norpc", "-ldflags", "-s -w -buildid=", "-o", str(binary), "."],
            cwd=source,
            env=environment,
            check=True,
            timeout=600,
        )
        write_archive(output, binary, bundled(source))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function", choices=sorted(FUNCTIONS), default="idle-stop")
    parser.add_argument("--output", type=Path, help="default: .build/<function>.zip")
    args = parser.parse_args(argv)
    output = (args.output or ROOT / ".build" / f"{args.function}.zip").resolve()
    digest = build(output, FUNCTIONS[args.function])
    print(f"{output} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
