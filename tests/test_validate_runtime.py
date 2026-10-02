"""Credential-free checks of the explicit runtime validator's protocol expectations."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest

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
