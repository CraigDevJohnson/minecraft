"""Apply instance allowlist and operator changes and verify private server UUID state."""
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


def _uuids(entries, what):
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise AccessError(f"server {what} is not a valid entry list")
    try:
        identities = {canonical_uuid(entry.get("uuid")) for entry in entries}
    except AccessError:
        raise AccessError(f"server {what} contains invalid UUID evidence") from None
    if len(identities) != len(entries):
        raise AccessError(f"server {what} contains duplicate UUID evidence")
    return identities


def read_uuids(machine, instance):
    try:
        entries = machine.read_allowlist(instance)
    except Exception:
        raise AccessError("cannot read private server allowlist") from None
    return _uuids(entries, "allowlist")


def read_operator_uuids(machine, instance):
    try:
        entries = machine.read_operators(instance)
    except Exception:
        raise AccessError("cannot read private server operators") from None
    return _uuids(entries, "operators")


def _verified(instance, operation, edition, player, what):
    """The UUID and name of a player change's private input, after the checks every change makes before RCON."""
    if not isinstance(instance, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", instance):
        raise AccessError("a valid instance ID is required")
    if operation not in ("add", "remove") or edition not in ("java", "bedrock"):
        raise AccessError(f"unsupported {what} operation or edition")
    if not isinstance(player, dict):
        raise AccessError("private player input must be a JSON object")
    expected_uuid = canonical_uuid(player.get("uuid"))
    name = player.get("name")
    if edition == "java":
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            raise AccessError("Java player name must contain 3-16 ASCII letters, digits or underscores")
    elif (not isinstance(name, str) or not name or name != name.strip()
            or any(not (character.isalnum() or character in " _") for character in name)
            or len(name.encode("utf-16-le")) > 32):
        raise AccessError("Bedrock gamertag must use letters, digits, spaces or underscores within 16 UTF-16 units")
    return expected_uuid, name


def _converge(machine, instance, command, expected_uuid, expected_present, read, what, timed_out):
    """Send command unless read already shows the UUID as expected, then wait up to 60 seconds for it to; answer
    whether anything changed."""
    if (expected_uuid in read(machine, instance)) == expected_present:
        return False
    try:
        machine.rcon(instance, command)
    except Exception:
        raise AccessError(f"{what} console command failed; inspect private server state before retrying") from None
    deadline = machine.monotonic() + 60
    while (expected_uuid in read(machine, instance)) != expected_present:
        remaining = deadline - machine.monotonic()
        if remaining <= 0:
            raise AccessError(timed_out)
        machine.sleep(min(1, remaining))
    return True


def change_allowlist(machine, instance, operation, edition, player: dict) -> dict:
    """Caller holds lifecycle exclusion and has checked the instance is running."""
    expected_uuid, name = _verified(instance, operation, edition, player, "allowlist")
    if edition == "java":
        command = f"whitelist {operation} " + name
    else:
        command = f"fwhitelist {operation} " + ('"' + name + '"' if " " in name else name)
    changed = _converge(machine, instance, command, expected_uuid, operation == "add", read_uuids, "allowlist",
                        "allowlist change verification timed out; inspect private server state before retrying")
    return {"instance": instance, "edition": edition, "operation": operation, "allowlist": "updated" if changed else "unchanged"}


def change_operators(machine, instance, operation, edition, player: dict) -> dict:
    """Make one verified player an operator of the running server, or no longer one, and confirm it in ops.json.
    Caller holds lifecycle exclusion and has checked the instance is running.

    A Bedrock player goes by the name Floodgate gives them, a dot and the gamertag with its spaces as underscores,
    which the server knows only once they have joined it: until then the change cannot be confirmed here, and
    the next start, which writes ops.json, makes it."""
    expected_uuid, name = _verified(instance, operation, edition, player, "operator")
    target = name if edition == "java" else "." + name.replace(" ", "_")
    command = ("op " if operation == "add" else "deop ") + target
    timed_out = ("operator change verification timed out; a player this world has not seen becomes an operator at its next start"
                 if operation == "add" else
                 "operator change verification timed out; a player this world has not seen lately stays an operator until its next start")
    changed = _converge(machine, instance, command, expected_uuid, operation == "add", read_operator_uuids, "operator", timed_out)
    return {"instance": instance, "edition": edition, "operation": operation, "operators": "updated" if changed else "unchanged"}
