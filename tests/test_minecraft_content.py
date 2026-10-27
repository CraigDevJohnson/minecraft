from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_content as content  # noqa: E402


class ApprovedGeyserConfigurationTests(unittest.TestCase):
    """The two Geyser configurations #30 approved, which the Crafty adapter compares a manifest against."""

    def test_raknet_alone_is_implicit_before_configuration_version_11_and_named_from_it(self):
        self.assertNotIn("transport", content._approved_geyser(8)["bedrock"])
        self.assertEqual(content._approved_geyser(11)["bedrock"]["transport"], "raknet")

    def test_dual_transport_adds_built_in_signaling_and_the_selected_webrtc_port(self):
        bedrock = content._approved_geyser(11, dual_transport=True)["bedrock"]
        self.assertEqual(bedrock["transport"], "both")
        self.assertEqual(bedrock["webrtc-port"], 19133)
        self.assertEqual(bedrock["signaling"], {"mode": "builtin"})
        self.assertEqual(content._approved_geyser(11, dual_transport=True, webrtc_port=19134)["bedrock"]["webrtc-port"], 19134)

    def test_every_configuration_keeps_floodgate_authentication_and_player_privacy(self):
        for configuration in (content._approved_geyser(8), content._approved_geyser(11), content._approved_geyser(11, dual_transport=True)):
            with self.subTest(configuration=configuration):
                self.assertEqual(configuration["java"], {"auth-type": "floodgate"})
                self.assertEqual(configuration["bedrock"]["address"], "0.0.0.0")
                self.assertEqual(configuration["bedrock"]["port"], 19132)
                self.assertFalse(configuration["bedrock"]["clone-remote-port"])
                self.assertFalse(configuration["log-player-ip-addresses"])
                self.assertEqual(configuration["saved-user-logins"], [])


class ManagedFileBytesTests(unittest.TestCase):
    def test_canonical_json_v1_sorts_keys_compacts_separators_keeps_unicode_and_ends_with_one_newline(self):
        managed = {"content": {"b": [1, 2], "a": {"z": "ö", "y": True}}}
        self.assertEqual(content._managed_file_bytes(managed), '{"a":{"y":true,"z":"ö"},"b":[1,2]}\n'.encode("utf-8"))

    def test_the_bytes_round_trip_to_the_same_content(self):
        managed = {"content": content._approved_geyser(11, dual_transport=True)}
        self.assertEqual(json.loads(content._managed_file_bytes(managed)), managed["content"])


if __name__ == "__main__":
    unittest.main()
