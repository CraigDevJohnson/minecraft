#!/usr/bin/env python3
"""Print a saved plan's digest, read from `tofu show -json` on stdin.

The digest is the plan's identity for the Deploy workflow: the plan run posts it, and an apply run applies
only a fresh plan with the digest Craig copied from the plan he read (ADR 0006). It covers what the apply
would do, each changed resource's and output's actions and planned values, and leaves out what only
describes the present, such as unchanged resources and the values a change starts from, so starting or
stopping the server between the two runs does not change it unless the plan changes the host itself. Only
the digest leaves this script; the plan's values never reach the log.
"""
from __future__ import annotations

import hashlib
import json
import sys

DIGEST_LENGTH = 16


def digest(plan: dict) -> str:
    changes = []
    for change in plan.get("resource_changes") or []:
        detail = change.get("change") or {}
        if detail.get("actions") == ["no-op"]:
            continue
        changes.append({
            "address": change.get("address"),
            "actions": detail.get("actions"),
            "after": detail.get("after"),
            "after_unknown": detail.get("after_unknown"),
            "replace_paths": detail.get("replace_paths"),
        })
    outputs = {
        name: {"actions": detail.get("actions"), "after": detail.get("after"), "after_unknown": detail.get("after_unknown")}
        for name, detail in (plan.get("output_changes") or {}).items()
        if detail.get("actions") != ["no-op"]
    }
    canonical = json.dumps({"resources": sorted(changes, key=lambda change: str(change["address"])), "outputs": outputs},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:DIGEST_LENGTH]


def main() -> int:
    try:
        plan = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("the saved plan could not be read", file=sys.stderr)
        return 1
    if not isinstance(plan, dict):
        print("the saved plan could not be read", file=sys.stderr)
        return 1
    print(digest(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
