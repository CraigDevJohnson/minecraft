"""Apply instance allowlist changes and verify private server UUID state."""
from __future__ import annotations

import re
import uuid


class AccessError(Exception):
    """Controlled allowlist diagnostics that contain no player identifiers."""


def canonical_uuid(value):
    if not isinstance(value, str):
        raise AccessError("player UUID must use canonical lowercase UUID syntax")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise AccessError("player UUID must use canonical lowercase UUID syntax") from None
    if str(parsed) != value or parsed.int == 0:
        raise AccessError("player UUID must use canonical lowercase UUID syntax")
    return value


def read_uuids(machine, instance):
    try:
        entries = machine.read_allowlist(instance)
    except Exception:
        raise AccessError("cannot read private server allowlist") from None
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise AccessError("server allowlist is not a valid entry list")
    try:
        identities = {canonical_uuid(entry.get("uuid")) for entry in entries}
    except AccessError:
        raise AccessError("server allowlist contains invalid UUID evidence") from None
    if len(identities) != len(entries):
        raise AccessError("server allowlist contains duplicate UUID evidence")
    return identities


def change_allowlist(machine, instance, operation, edition, player: dict) -> dict:
    """Caller holds lifecycle exclusion and has checked the instance is running."""
    if not isinstance(instance, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", instance):
        raise AccessError("a valid instance ID is required")
    if operation not in ("add", "remove") or edition not in ("java", "bedrock"):
        raise AccessError("unsupported allowlist operation or edition")
    if not isinstance(player, dict):
        raise AccessError("private player input must be a JSON object")
    expected_uuid = canonical_uuid(player.get("uuid"))
    name = player.get("name")
    if edition == "java":
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            raise AccessError("Java player name must contain 3-16 ASCII letters, digits or underscores")
        command = f"whitelist {operation} " + name
    else:
        if (not isinstance(name, str) or not name or name != name.strip()
                or any(not (character.isalnum() or character in " _") for character in name)
                or len(name.encode("utf-16-le")) > 32):
            raise AccessError("Bedrock gamertag must use letters, digits, spaces or underscores within 16 UTF-16 units")
        command = f"fwhitelist {operation} " + ('"' + name + '"' if " " in name else name)
    result = {"instance": instance, "edition": edition, "operation": operation}
    expected_present = operation == "add"
    if (expected_uuid in read_uuids(machine, instance)) == expected_present:
        return {**result, "allowlist": "unchanged"}
    try:
        machine.rcon(instance, command)
    except Exception:
        raise AccessError("allowlist RCON command failed; inspect private server state before retrying") from None
    deadline = machine.monotonic() + 60
    while (expected_uuid in read_uuids(machine, instance)) != expected_present:
        remaining = deadline - machine.monotonic()
        if remaining <= 0:
            raise AccessError("allowlist change verification timed out; inspect private server state before retrying")
        machine.sleep(min(1, remaining))
    return {**result, "allowlist": "updated"}
