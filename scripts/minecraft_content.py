#!/usr/bin/env python3
"""The approved Geyser configurations and the canonical bytes of a managed file, shared with the Crafty adapter.

The Compose-era manifest loader, artifact staging and Compose renderer that lived here were retired in #70;
`minecraft_crafty.py` applies the pinned content from `runtime/crafty/server.json` instead (ADR 0008)."""

from __future__ import annotations

import json


# Geyser 2.12.1 build 1280 removed RakNet alone (configuration version 12): it loads "raknet" as NetherNet alone.
RAKNET_ALONE_LAST_VERSION = 11
# What Bedrock clients list when Geyser cannot read the MOTD and player limit from Paper. Geyser's own fallback is
# "Geyser" with 100 slots, which Bedrock showed after the Paper 26.3 upgrade, so from version 12 the family server's
# name and limit (server_properties in runtime/crafty/server.json) take its place.
FAMILY_MOTD = {"primary-motd": "Family Minecraft", "secondary-motd": "Family Minecraft", "max-players": 10}


def _approved_geyser(config_version: int, *, dual_transport: bool = False, webrtc_port: int = 19133) -> dict:
    """The Geyser configuration #30 approved: RakNet alone, or both transports with built-in signaling. Geyser 2.12
    (configuration version 11) selects NetherNet alone when no transport is named, so RakNet alone names it from there,
    and version 12 has no RakNet alone at all."""
    if not dual_transport and config_version > RAKNET_ALONE_LAST_VERSION:
        raise ValueError(f"Geyser configuration version {config_version} has no RakNet-alone transport")
    configuration = {
        "config-version": config_version,
        "bedrock": {"address": "0.0.0.0", "port": 19132, "clone-remote-port": False},
        "java": {"auth-type": "floodgate"},
        "log-player-ip-addresses": False,
        "saved-user-logins": [],
    }
    if dual_transport:
        configuration["bedrock"].update({"transport": "both", "webrtc-port": webrtc_port,
                                         "signaling": {"mode": "builtin"}})
    elif config_version >= 11:
        configuration["bedrock"]["transport"] = "raknet"
    if config_version >= 12:
        configuration["motd"] = dict(FAMILY_MOTD)
    return configuration


def _managed_file_bytes(managed: dict) -> bytes:
    """canonical-json-v1: sorted keys, compact UTF-8 JSON, and one final LF."""
    return (json.dumps(managed["content"], sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
