"""Credential-free checks of the explicit runtime validator's protocol expectations."""
from __future__ import annotations

import json
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_runtime as runtime


def read_varint(stream) -> int:
    value = 0
    for shift in range(0, 35, 7):
        byte = stream.recv(1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
    raise AssertionError("varint too long")


class StatusServer:
    """A loopback Java status responder; ``via`` emulates ViaVersion's protocol echo for 26.3."""

    def __init__(self, *, via: bool, server_protocol: int = 776):
        self.via, self.server_protocol = via, server_protocol
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.handshakes = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                read_varint(connection)  # handshake length
                packet_id, protocol = read_varint(connection), read_varint(connection)
                address = connection.recv(read_varint(connection)).decode()
                port, = struct.unpack(">H", connection.recv(2))
                next_state = read_varint(connection)
                self.handshakes.append((packet_id, protocol, address, port, next_state))
                self.assert_request(connection)
                answered = protocol if self.via and protocol in (776, 777) else self.server_protocol
                status = json.dumps({"version": {"name": "Paper 26.2", "protocol": answered},
                                     "players": {"max": 6, "online": 0}, "description": "fixture"}).encode()
                body = runtime.varint(0) + runtime.varint(len(status)) + status
                connection.sendall(runtime.varint(len(body)) + body)

    @staticmethod
    def assert_request(connection):
        if (read_varint(connection), read_varint(connection)) != (1, 0):
            raise AssertionError("status request expected")

    def close(self):
        self.listener.close()


class ValidateRuntimeTests(unittest.TestCase):
    def test_native_probe_uses_and_requires_the_manifest_owned_port(self):
        manifest = runtime.content.load_manifest(ROOT / "runtime/content.json")
        manifest = copy.deepcopy(manifest)
        managed = manifest["managed_files"][0]
        managed["content"]["bedrock"]["webrtc-port"] = 19134
        managed["sha256"] = hashlib.sha256(runtime.content._managed_file_bytes(managed)).hexdigest()
        expected = {"status": "passed", "scope": "isolated_bundled_native_transport", "udp_port": 19134,
                    "reliable_echo": True, "unreliable_echo": True,
                    "geyser_admission_verified": False, "bedrock_join_verified": False}
        for reported_port in (19134, 19133):
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                    runtime, "run", side_effect=["", json.dumps(dict(expected, udp_port=reported_port))]) as execute:
                if reported_port == 19134:
                    self.assertEqual(runtime.native_transport_probe(manifest, Path(directory), Path(directory)), expected)
                else:
                    with self.assertRaisesRegex(RuntimeError, "native UDP"):
                        runtime.native_transport_probe(manifest, Path(directory), Path(directory))
                self.assertEqual(execute.call_args_list[1].args[0][-1], "19134")

    def test_local_preflight_checks_the_selected_manifest_port_and_reports_occupation(self):
        with mock.patch.object(runtime.socket, "socket") as create:
            listener = create.return_value.__enter__.return_value
            runtime.require_local_webrtc_port(19134)
            listener.bind.assert_called_once_with(("127.0.0.1", 19134))
            listener.bind.side_effect = OSError("already allocated")
            with self.assertRaisesRegex(RuntimeError, "19134 is occupied"):
                runtime.require_local_webrtc_port(19134)

    def test_paired_allocator_excludes_selected_native_udp_and_java_tcp_ports(self):
        sockets = []
        for port in (19134, 25566, 29000):
            tcp, udp = mock.MagicMock(), mock.MagicMock()
            tcp.__enter__.return_value = tcp
            udp.__enter__.return_value = udp
            tcp.getsockname.return_value = ("127.0.0.1", port)
            sockets.extend((tcp, udp))
        with mock.patch.object(runtime.socket, "socket", side_effect=sockets):
            self.assertEqual(runtime.free_signaling_port(19134, 25566), 29000)
        sockets[-1].bind.assert_called_once_with(("127.0.0.1", 29000))

    def test_nethernet_discovery_and_denial_leave_authenticated_transport_unverified(self):
        metadata = {"protocol": 2193, "version": "26.51", "maxPlayers": 6, "players": 0,
                    "transportLayer": 2, "connection": 4, "onlineAuth": True, "selfSignedAuth": False}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.assert_path("/v1/join")
                body = json.dumps(metadata).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.assert_path("/v1/join/native-probe")
                if self.rfile.read(int(self.headers["Content-Length"])) != b"v=0\r\n":
                    raise AssertionError("unexpected synthetic request")
                self.send_response(401)
                self.end_headers()

            def assert_path(self, expected):
                if self.path != expected:
                    raise AssertionError("unexpected signaling path")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = runtime.nethernet_signaling(server.server_port)
            self.assertEqual(result["discovery"], metadata)
            self.assertEqual(result["unauthenticated_admission"], "denied")
            self.assertEqual(result["authenticated_geyser_peer"], "unverified")
            self.assertEqual(result["real_bedrock_join"], "unverified")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_nethernet_metadata_refuses_generic_http_stale_protocol_and_disabled_authentication(self):
        valid = {"protocol": 2193, "version": "26.51", "maxPlayers": 6, "players": 0,
                 "transportLayer": 2, "connection": 4, "onlineAuth": True, "selfSignedAuth": False}
        for invalid in ({}, [], dict(valid, protocol=2169), dict(valid, maxPlayers="6"),
                        dict(valid, transportLayer=1), dict(valid, connection=3),
                        dict(valid, onlineAuth=False), dict(valid, selfSignedAuth=True),
                        dict(valid, players=True), dict(valid, players=7)):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                runtime.check_nethernet_metadata(invalid)

    def test_signaling_port_is_free_for_both_protocols(self):
        port = runtime.free_signaling_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", port))
            udp.bind(("127.0.0.1", port))

    def test_handshake_requests_status_with_the_exact_client_protocol(self):
        self.assertEqual(runtime.varint(777), b"\x89\x06")
        self.assertEqual(runtime.varint(776), b"\x88\x06")
        self.assertEqual(runtime.handshake(777, 25565),
                         b"\x10\x00\x89\x06\x09127.0.0.1\x63\xdd\x01")
        self.assertEqual(runtime.JAVA_PROTOCOLS, {"26.2": 776, "26.3": 777})

    def test_java_status_requires_native_26_2_and_viaversion_26_3_answers(self):
        server = StatusServer(via=True)
        self.addCleanup(server.close)
        statuses = runtime.java_statuses(server.port)
        self.assertEqual(statuses, {"26.2": {"name": "Paper 26.2", "protocol": 776},
                                    "26.3": {"name": "Paper 26.2", "protocol": 777}})
        self.assertEqual([(protocol, address, next_state) for _, protocol, address, _, next_state in server.handshakes],
                         [(776, "127.0.0.1", 1), (777, "127.0.0.1", 1)])

    def test_java_status_without_viaversion_is_rejected(self):
        server = StatusServer(via=False)
        self.addCleanup(server.close)
        with self.assertRaisesRegex(RuntimeError, "26.3"):
            runtime.java_statuses(server.port)

    def test_java_status_rejects_a_server_that_does_not_speak_26_2(self):
        server = StatusServer(via=False, server_protocol=775)
        self.addCleanup(server.close)
        self.assertEqual(runtime.java_status(server.port, 776)["protocol"], 775)
        with self.assertRaisesRegex(RuntimeError, "26.2"):
            runtime.java_statuses(server.port)

    def test_java_status_rejects_malformed_responses(self):
        for response in (b"\x05\x01\x03abc", b"\x7f\x00", b""):
            with self.subTest(response=response):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.addCleanup(listener.close)
                listener.bind(("127.0.0.1", 0))
                listener.listen()

                def answer():
                    connection, _ = listener.accept()
                    with connection:
                        connection.recv(64)
                        connection.sendall(response)

                thread = threading.Thread(target=answer, daemon=True)
                thread.start()
                with self.assertRaises(RuntimeError):
                    runtime.java_status(listener.getsockname()[1], 777)
                thread.join(5)

    def test_bedrock_discovery_must_advertise_the_pinned_geyser_default(self):
        current = "MCPE;Dedicated Server;2193;26.51;0;6;1234567890;Geyser;Survival;1;19132;19133;"
        self.assertEqual(runtime.check_bedrock_discovery(current),
                         {"protocol": 2193, "version": "26.51", "online": 0, "max_players": 6})
        for stale in ("MCPE;Dedicated Server;2169;26.45;0;6;1;Geyser;Survival;1;19132;19133;",
                      "MCPE;Dedicated Server;2193;26.45;0;6;1;Geyser;Survival;1;19132;19133;",
                      "MCPE;Dedicated Server;2193;26.51;zero;6;1;Geyser;Survival;1;19132;19133;",
                      "MCPE;Dedicated Server;2193",
                      "MCEE;Dedicated Server;2193;26.51;0;6;1;Geyser;Survival;1;19132;19133;"):
            with self.subTest(stale=stale), self.assertRaises(RuntimeError):
                runtime.check_bedrock_discovery(stale)

    def test_crossplay_plugins_include_viaversion(self):
        loaded = "Server Plugins (3):\nBukkit Plugins:\n - floodgate, Geyser-Spigot, ViaVersion"
        runtime.check_plugins(loaded)
        for missing in ("Server Plugins (2):\nBukkit Plugins:\n - floodgate, Geyser-Spigot",
                        "Server Plugins (2):\nBukkit Plugins:\n - Geyser-Spigot, ViaVersion"):
            with self.subTest(missing=missing), self.assertRaisesRegex(RuntimeError, "plugins"):
                runtime.check_plugins(missing)


if __name__ == "__main__":
    unittest.main()
