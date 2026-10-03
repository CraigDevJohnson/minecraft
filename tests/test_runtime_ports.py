"""Shared-host allocation uses running containers' actual ports, preserving stored rotations."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import minecraft_content as content
import minecraft_instance as runtime


def machine(root):
    result = object.__new__(runtime.LinuxMachine)
    result.root = Path(root)
    result.config = {"eula_accepted": True, "rcon_password_file": "/private/fixture-rcon"}
    definition = content.load_definition(ROOT / "runtime/survival.json")
    result.definitions = {"survival": definition,
                          "creative": dict(definition, id="creative", java_port=25566, bedrock_port=19135)}
    manifest = content.load_manifest(ROOT / "runtime/content.json")
    second = copy.deepcopy(manifest)
    managed = second["managed_files"][0]
    managed["content"]["bedrock"]["webrtc-port"] = 19134
    managed["sha256"] = hashlib.sha256(content._managed_file_bytes(managed)).hexdigest()
    result.manifests = {"survival": manifest, "creative": second}
    result.verify_storage = mock.Mock()
    return result


class RuntimePortTests(unittest.TestCase):
    def test_active_binding_projection_uses_actual_ports_and_ignores_unpublished_entries(self):
        selected = machine("/private/fixture")
        selected.inspect = mock.Mock(return_value={"running": True, "container_id": "fixture-creative"})
        bindings = {"19133/udp": [{"HostIp": "0.0.0.0", "HostPort": "19133"}],
                    "25565/tcp": [{"HostIp": "0.0.0.0", "HostPort": "28100"}],
                    "25575/tcp": None, "25575/udp": []}
        with mock.patch.object(runtime, "command", return_value=json.dumps(bindings)) as command:
            self.assertEqual(selected.active_game_ports("creative"), frozenset({("udp", 19133), ("tcp", 28100)}))
        command.assert_called_once_with(["docker", "inspect", "--format", "{{json .HostConfig.PortBindings}}", "fixture-creative"])
        self.assertEqual(content.webrtc_port(selected.manifests["creative"]), 19134)

    def test_invalid_published_binding_refuses_preparation_with_safe_diagnostics(self):
        selected = machine("/private/fixture")
        selected.inspect = mock.Mock(return_value={"running": True, "container_id": "fixture"})
        for bindings in ([], {"0/udp": [{"HostPort": "19133"}]},
                         {"19133/udp": [{"HostPort": "private-value"}]},
                         {"19133/udp": [{"HostPort": 19133}]},
                         {"19133/udp": [{"HostPort": "65536"}]}, {"19133/udp": False}):
            with self.subTest(bindings=bindings), mock.patch.object(runtime, "command", return_value=json.dumps(bindings)), \
                    self.assertRaisesRegex(runtime.OperationError, "active port bindings") as failure:
                selected.active_game_ports("creative")
            self.assertNotIn("private-value", str(failure.exception))

    def test_running_instance_old_active_port_blocks_a_conflicting_new_start(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = machine(directory)
            selected.inspect = mock.Mock(return_value={"running": True, "container_id": "fixture-creative"})
            # Creative's desired manifest says 19134, but its running container still owns 19133.
            with mock.patch.object(runtime, "command", return_value=json.dumps({"19133/udp": [{"HostPort": "19133"}]})), \
                    mock.patch.object(runtime, "ec2_public_ipv4") as metadata, \
                    mock.patch.object(content, "stage_content") as stage, \
                    self.assertRaisesRegex(runtime.OperationError, "overlap"):
                selected.prepare("survival")
            selected.inspect.assert_called_once_with("creative")
            metadata.assert_not_called()
            stage.assert_not_called()
            self.assertFalse((Path(directory) / "instances").exists())

    def test_stopped_instance_can_reuse_ports_and_tcp_udp_numbers_are_independent(self):
        for running, bindings in ((False, None), (True, {"19133/tcp": [{"HostPort": "19133"}]})):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as directory:
                selected = machine(directory)
                if not running:
                    selected.manifests["creative"] = copy.deepcopy(selected.manifests["survival"])
                selected.inspect = mock.Mock(return_value={"running": running, "container_id": "fixture-creative"})
                with mock.patch.object(runtime, "command", return_value=json.dumps(bindings)), \
                        mock.patch.object(runtime, "ec2_public_ipv4", return_value="8.8.8.8"), \
                        mock.patch.object(runtime, "private_text", return_value="a-private-fixture-password"), \
                        mock.patch.object(runtime.os, "chown"), \
                        mock.patch.object(content, "stage_content", return_value=Path(directory) / "artifacts"):
                    selected.prepare("survival")
                self.assertTrue((selected.runtime("survival") / "compose.json").is_file())
                selected.inspect.assert_called_once_with("creative")
