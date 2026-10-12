#!/usr/bin/env python3
"""Mask the private inputs' values in the Deploy workflow's log.

Prints a GitHub `::add-mask::` command for every quoted value of six or more characters in the files named on
the command line (deployment.tfvars and backend.hcl). OpenTofu writes its errors to stderr, straight to the
log, and a provider error can quote an input, such as the backup bucket's name; once masked, the log shows
*** in its place and the rest of the error stays readable.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

QUOTED = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
MINIMUM_LENGTH = 6


def values(text: str) -> set[str]:
    """The quoted values as OpenTofu would print them, with their escapes undone."""
    found = (re.sub(r"\\(.)", r"\1", value) for value in QUOTED.findall(text))
    return {value for value in found if len(value) >= MINIMUM_LENGTH}


def main(paths: list[str]) -> int:
    if not paths:
        print("usage: mask_private_inputs.py FILE...", file=sys.stderr)
        return 2
    for value in sorted(set().union(*(values(Path(path).read_text()) for path in paths))):
        print(f"::add-mask::{value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
