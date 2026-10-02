#!/usr/bin/env python3
"""Build the idle-stop Lambda for ARM64 and write a reproducible deployment zip."""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "lambda" / "idlestop"
DEFAULT_OUTPUT = ROOT / ".build" / "idle-stop.zip"
# A fixed timestamp and mode keep the archive bytes, and so the Lambda code hash, stable across rebuilds.
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def build(output: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="idle-stop-") as directory:
        binary = Path(directory) / "bootstrap"
        environment = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64", "GOFLAGS": "-mod=readonly"}
        subprocess.run(
            ["go", "build", "-trimpath", "-tags", "lambda.norpc", "-ldflags", "-s -w -buildid=", "-o", str(binary), "."],
            cwd=SOURCE,
            env=environment,
            check=True,
            timeout=600,
        )
        info = zipfile.ZipInfo("bootstrap", date_time=ZIP_TIME)
        info.external_attr = 0o100755 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".partial")
        with zipfile.ZipFile(partial, "w") as archive:
            archive.writestr(info, binary.read_bytes(), compresslevel=9)
        partial.replace(output)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    digest = build(args.output.resolve())
    print(f"{args.output.resolve()} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
