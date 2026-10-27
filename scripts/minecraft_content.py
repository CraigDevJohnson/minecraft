#!/usr/bin/env python3
"""The approved Geyser configurations and the canonical bytes of a managed file, shared with the Crafty adapter.

The Compose-era manifest loader, artifact staging and Compose renderer that lived here were retired in #70;
`minecraft_crafty.py` applies the pinned content from `runtime/crafty/server.json` instead (ADR 0008)."""

from __future__ import annotations

import json


def _approved_geyser(config_version: int, *, dual_transport: bool = False, webrtc_port: int = 19133) -> dict:
    """The Geyser configuration #30 approved: RakNet alone, or both transports with built-in signaling. Geyser 2.12
    (configuration version 11) selects NetherNet alone when no transport is named, so RakNet alone names it from there."""
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
    return configuration


def _managed_file_bytes(managed: dict) -> bytes:
    """canonical-json-v1: sorted keys, compact UTF-8 JSON, and one final LF."""
    return (json.dumps(managed["content"], sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
