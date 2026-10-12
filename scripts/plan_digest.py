#!/usr/bin/env python3
"""Print a saved plan's digest, read from `tofu show -json` on stdin.

The digest is the plan's identity for the Deploy workflow: the plan run posts it, and an apply run applies
only a fresh plan with the digest Craig copied from the plan he read (ADR 0006). It covers what the apply
would do: each changed resource's actions, the object it acts on (its id and ARN before the change, so a
delete or update cannot land on a different object than the one planned), its planned values, and each
changed output. It leaves out the rest of what describes the present, such as unchanged resources and the
other values a change starts from (among them when a role was last used and whether the host runs), so
ordinary use and starting or stopping the server between the two runs do not change it unless the plan
changes the host itself. Only the digest leaves this script; the plan's values never reach the log.
"""
from __future__ import annotations

import hashlib
import json
import sys

DIGEST_LENGTH = 16
# The attributes that name the object a change acts on.
IDENTITY = ("id", "arn")


def digest(plan: dict) -> str:
    changes = []
    for change in plan.get("resource_changes") or []:
        detail = change.get("change") or {}
        if detail.get("actions") == ["no-op"]:
            continue
        before = detail.get("before")
        changes.append({
            "address": change.get("address"),
            "actions": detail.get("actions"),
            "acts_on": {key: before.get(key) for key in IDENTITY} if isinstance(before, dict) else None,
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
