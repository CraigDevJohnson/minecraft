"""Offline evidence that unattended observations cannot operate the Minecraft host."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

ACCOUNT = "793680745829"
HOST_ID = "i-0123456789abcdef0"
HOST_IP = "44.240.123.1"
SECRET = "private-token-player-UUID-MOTD"
ALLOWED_OPERATIONS = {
    ("sts", "get-caller-identity"),
    ("ec2", "describe-instances"),
    ("ec2", "describe-instance-status"),
}


class FakeAWS:
    def __init__(self, hosts=(), *, account=ACCOUNT, checks=None, failure=None):
        self.account = account
        self.inventory = {"hosts": list(hosts), "next_token": None}
        self.checks = checks if checks is not None else [{
            "instance_id": HOST_ID, "state": "running", "instance_status": "ok", "system_status": "ok",
        }]
        self.failure = failure
        self.calls = []

    def __call__(self, command, **options):
        self.calls.append((command, options))
        operation = tuple(command[1:3])
        if operation not in ALLOWED_OPERATIONS:
            raise AssertionError("AWS mutation or unapproved metadata access")
        if self.failure:
            raise self.failure
        result = {
            ("sts", "get-caller-identity"): self.account,
            ("ec2", "describe-instances"): self.inventory,
            ("ec2", "describe-instance-status"): self.checks,
        }[operation]
        return subprocess.CompletedProcess(command, 0, json.dumps(result), SECRET)


def host(state="running", **extra):
    return {"instance_id": HOST_ID, "state": state, "public_ipv4": HOST_IP, **extra}


def varint(value):
    result = bytearray()
    while True:
        byte, value = value & 127, value >> 7
        result.append(byte | (128 if value else 0))
        if not value:
            return bytes(result)


def java_packet(**replacements):
    status = {"version": {"name": "Paper 26.2", "protocol": 776},
              "players": {"online": 1, "max": 6, "sample": [{"name": SECRET, "id": SECRET}]},
              "description": SECRET, **replacements}
    text = json.dumps(status).encode()
    body = varint(0) + varint(len(text)) + text
    return varint(len(body)) + body


def bedrock_packet(advertisement=None):
    text = (advertisement or f"MCPE;{SECRET};2193;26.51;1;6;1234567890;{SECRET};Survival;1;19132;19133;").encode()
    return b"\x1c" + bytes(8) + bytes(8) + bytes.fromhex("00ffff00fefefefefdfdfdfd12345678") + struct.pack(">H", len(text)) + text


def signaling_reply(document=None, *, status="200 OK", content_type="application/json", chunked=False):
    body = json.dumps(document if document is not None else {
        "protocol": 2193, "version": "26.51", "players": 1, "maxPlayers": 6, "transportLayer": 2, "connection": 4,
        "onlineAuth": True, "selfSignedAuth": False, "motd": SECRET}).encode()
    if chunked:
        framing, body = "Transfer-Encoding: chunked", b"".join(
            b"%x\r\n%s\r\n" % (len(part), part) for part in (body[:7], body[7:], b""))
    else:
        framing = f"Content-Length: {len(body)}"
    return f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n{framing}\r\n\r\n".encode() + body


class TCPReply:
    def __init__(self, packet):
        self.packet = bytearray(packet)
        self.sent = b""
        self.timeouts = []
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, body):
        self.sent += body

    def recv(self, size):
        self.read_sizes.append(size)
        reply = bytes(self.packet[:min(size, 3)])
        del self.packet[:len(reply)]
        return reply


class UDPReply(TCPReply):
    def connect(self, target):
        self.target = target

    def send(self, body):
        self.sent += body
        return len(body)

    def recv(self, size):
        self.read_sizes.append(size)
        return bytes(self.packet[:size])


class MinecraftHealthTests(unittest.TestCase):
    def reporter(self):
        self.assertTrue((ROOT / "scripts/minecraft_health.py").is_file(), "health reporter is missing")
        return importlib.import_module("minecraft_health")

    def observe(self, aws, *, arguments=(), dns_addresses=(HOST_IP,), tcp_reply=None, udp_reply=None,
                dns_failure=None, tcp_failure=None, udp_failure=None, signaling=None, signaling_failure=None):
        reporter = self.reporter()
        output, errors = io.StringIO(), io.StringIO()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 25565)) for address in dns_addresses]
        java, signaling = tcp_reply or TCPReply(java_packet()), signaling or TCPReply(signaling_reply())

        def connect(target, timeout):
            failure = tcp_failure if target[1] == 25565 else signaling_failure
            if failure:
                raise failure
            return java if target[1] == 25565 else signaling

        with mock.patch.object(reporter.subprocess, "run", side_effect=aws), \
                mock.patch.object(reporter.socket, "getaddrinfo", return_value=addresses, side_effect=dns_failure) as dns, \
                mock.patch.object(reporter.socket, "create_connection", side_effect=connect) as tcp, \
                mock.patch.object(reporter.socket, "socket", return_value=udp_reply or UDPReply(bedrock_packet()),
                                  side_effect=udp_failure) as udp, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = reporter.main(list(arguments))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn(HOST_ID, output.getvalue())
        self.assertNotIn(HOST_IP, output.getvalue())
        return code, json.loads(output.getvalue()), (dns, tcp, udp)

    def assert_read_calls(self, aws, expected):
        self.assertEqual([tuple(command[1:3]) for command, _ in aws.calls], expected)
        for command, options in aws.calls:
            self.assertEqual(command[0], "aws")
            self.assertIn(tuple(command[1:3]), ALLOWED_OPERATIONS)
            self.assertEqual(command[command.index("--region") + 1], "us-west-2")
            self.assertEqual(command[command.index("--output") + 1], "json")
            self.assertIn("--query", command)
            self.assertIn("--no-cli-pager", command)
            self.assertLessEqual(options["timeout"], 30)
            self.assertTrue(options["check"])
            self.assertTrue(options["capture_output"])
            self.assertEqual(options["env"]["AWS_MAX_ATTEMPTS"], "1")
            self.assertEqual(options["env"]["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"], "true")

    def test_absent_host_is_idle_without_application_or_network_claims(self):
        aws = FakeAWS()
        code, report, network = self.observe(aws)
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "absent")
        self.assertEqual(report["status"], "idle")
        self.assertTrue(report["identity_verified"])
        self.assertFalse(report["game_ready"])
        for field in ("ec2_checks_passed", "dns_matches_host", "java_status_ok", "bedrock_discovery_ok", "nethernet_signaling_ok"):
            self.assertIsNone(report[field])
        for dependency in network:
            dependency.assert_not_called()
        self.assert_read_calls(aws, [("sts", "get-caller-identity"), ("ec2", "describe-instances")])
        inventory = aws.calls[1][0]
        for tag in ("Name=tag:Project,Values=minecraft", "Name=tag:Environment,Values=family",
                    "Name=tag:Name,Values=minecraft-family-host"):
            self.assertIn(tag, inventory)
        self.assertEqual(inventory[inventory.index("--max-items") + 1], "2")

    def test_stopped_host_is_idle_and_does_not_start_or_probe_it(self):
        aws = FakeAWS([host("stopped", public_ipv4=None)])
        code, report, network = self.observe(aws)
        self.assertEqual((code, report["state"], report["status"], report["game_ready"]),
                         (0, "stopped", "idle", False))
        for dependency in network:
            dependency.assert_not_called()
        self.assert_read_calls(aws, [("sts", "get-caller-identity"), ("ec2", "describe-instances")])

    def test_identity_mismatch_stops_before_ec2_access(self):
        aws = FakeAWS(account="999999999999")
        code, report, network = self.observe(aws)
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "account_mismatch")
        self.assertFalse(report["identity_verified"])
        for dependency in network:
            dependency.assert_not_called()
        self.assert_read_calls(aws, [("sts", "get-caller-identity")])

    def test_duplicate_or_truncated_inventory_fails_before_status_and_network(self):
        for hosts, token in (([host(), host(instance_id="i-abcdef01234567890")], None),
                             ([host()], SECRET)):
            with self.subTest(hosts=len(hosts), truncated=bool(token)):
                aws = FakeAWS(hosts)
                aws.inventory["next_token"] = token
                code, report, network = self.observe(aws)
                self.assertEqual((code, report["failure"]), (1, "ambiguous_hosts"))
                for dependency in network:
                    dependency.assert_not_called()
                self.assert_read_calls(aws, [("sts", "get-caller-identity"), ("ec2", "describe-instances")])

    def test_aws_errors_and_timeouts_never_emit_their_payload_or_trigger_recovery(self):
        errors = [subprocess.CalledProcessError(1, "aws", output=SECRET, stderr=SECRET),
                  subprocess.TimeoutExpired("aws", 30, output=SECRET, stderr=SECRET),
                  OSError(SECRET)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                aws = FakeAWS(failure=error)
                code, report, network = self.observe(aws)
                self.assertEqual((code, report["failure"]), (1, "aws_read_failed"))
                for dependency in network:
                    dependency.assert_not_called()
                self.assert_read_calls(aws, [("sts", "get-caller-identity")])

    def test_cli_target_overrides_fail_without_any_aws_or_network_calls(self):
        aws = FakeAWS()
        code, report, network = self.observe(aws, arguments=["--region", SECRET])
        self.assertEqual((code, report["failure"]), (1, "unsupported_arguments"))
        self.assertEqual(aws.calls, [])
        for dependency in network:
            dependency.assert_not_called()

    def test_running_host_probes_only_verified_address_with_status_requests_and_safe_output(self):
        aws = FakeAWS([host()])
        java, bedrock, signaling = TCPReply(java_packet()), UDPReply(bedrock_packet()), TCPReply(signaling_reply())
        code, report, (dns, tcp, udp) = self.observe(aws, tcp_reply=java, udp_reply=bedrock, signaling=signaling)
        self.assertEqual((code, report["state"], report["status"], report["game_ready"]),
                         (0, "running", "healthy", True))
        for field in ("identity_verified", "ec2_checks_passed", "dns_matches_host", "java_status_ok", "bedrock_discovery_ok",
                      "nethernet_signaling_ok"):
            self.assertTrue(report[field])
        self.assertEqual(report["java"], {"version": "26.2", "protocol": 776, "online": 1, "limit": 6})
        self.assertEqual(report["bedrock"], {"version": "26.51", "protocol": 2193, "online": 1, "limit": 6})
        self.assertEqual(report["nethernet"], {"version": "26.51", "protocol": 2193, "online": 1, "limit": 6})
        dns.assert_called_once_with("mc.craigdevjohnson.com", 25565, socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(tcp.call_args_list, [mock.call((HOST_IP, 25565), timeout=self.reporter().NETWORK_TIMEOUT),
                                              mock.call((HOST_IP, 19132), timeout=self.reporter().NETWORK_TIMEOUT)])
        # A plain discovery GET: no admission request, no SDP offer.
        self.assertEqual(signaling.sent.split(b"\r\n")[0], b"GET /v1/join HTTP/1.1")
        self.assertNotIn(b"POST", signaling.sent)
        udp.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        self.assertEqual(bedrock.target, (HOST_IP, 19132))
        self.assertTrue(java.sent.endswith(b"\x01\x01\x00"))
        self.assertIn(b"mc.craigdevjohnson.com", java.sent)
        self.assertEqual(bedrock.sent[:1], b"\x01")
        self.assertEqual(len(bedrock.sent), 33)
        self.assertTrue(all(0 < timeout <= self.reporter().NETWORK_TIMEOUT for timeout in java.timeouts + bedrock.timeouts))
        self.assertLessEqual(max(java.read_sizes), self.reporter().MAX_STATUS_BYTES)
        self.assert_read_calls(aws, [("sts", "get-caller-identity"), ("ec2", "describe-instances"),
                                   ("ec2", "describe-instance-status")])
        status = aws.calls[-1][0]
        self.assertEqual(status[status.index("--instance-ids") + 1], HOST_ID)

    def test_signaling_answers_are_bounded_authenticated_discovery_json(self):
        good = signaling_reply()
        for reply in (signaling_reply(status="404 Not Found"), signaling_reply(content_type="text/html"),
                      signaling_reply({"protocol": 2193, "version": "26.51", "players": 1, "maxPlayers": 6, "onlineAuth": False}),
                      signaling_reply({"protocol": 2193, "players": 7, "maxPlayers": 6, "onlineAuth": True}),
                      signaling_reply({"protocol": True, "players": 1, "maxPlayers": 6, "onlineAuth": True}),
                      signaling_reply([]), good[:-1], good.split(b"\r\n\r\n")[0],
                      good.replace(b"Content-Length: ", b"Content-Length: -"),
                      signaling_reply({"padding": SECRET * 1000, "onlineAuth": True})):
            with self.subTest(reply=reply[:40]):
                client = TCPReply(reply)
                code, report, _ = self.observe(FakeAWS([host()]), signaling=client)
                self.assertEqual((code, report["failure"], report["nethernet_signaling_ok"], report["game_ready"]),
                                 (1, "nethernet_signaling_failed", False, False))
                self.assertTrue(all(size <= self.reporter().MAX_SIGNALING_BYTES + 1 for size in client.read_sizes))
        code, report, _ = self.observe(FakeAWS([host()]), signaling=TCPReply(signaling_reply(chunked=True)))
        self.assertEqual((code, report["nethernet"]["protocol"]), (0, 2193))
        code, report, _ = self.observe(FakeAWS([host()]), signaling_failure=ConnectionRefusedError(SECRET))
        self.assertEqual((code, report["failure"]), (1, "nethernet_signaling_failed"))

    def test_a_raknet_only_server_is_not_asked_for_signaling(self):
        manifest = json.loads((ROOT / "runtime" / "crafty" / "server.json").read_text())
        bedrock = manifest["managed_files"][0]["content"]["bedrock"]
        for key in ("transport", "webrtc-port", "signaling"):
            bedrock.pop(key, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.json"
            path.write_text(json.dumps(manifest))
            with mock.patch.object(self.reporter(), "MANIFEST", path):
                code, report, (_, tcp, _) = self.observe(FakeAWS([host()]))
            self.assertEqual((code, report["status"], report["nethernet_signaling_ok"]), (0, "healthy", None))
            self.assertEqual(tcp.call_args_list, [mock.call((HOST_IP, 25565), timeout=self.reporter().NETWORK_TIMEOUT)])
            with mock.patch.object(self.reporter(), "MANIFEST", Path(directory) / "missing.json"):
                code, report, _ = self.observe(FakeAWS([host()]))
            self.assertEqual((code, report["failure"]), (1, "manifest_unreadable"))

    def test_transitional_host_states_fail_without_any_network_or_operational_retries(self):
        for state in ("pending", "stopping", "shutting-down"):
            with self.subTest(state=state):
                aws = FakeAWS([host(state)])
                code, report, network = self.observe(aws)
                self.assertEqual((code, report["state"], report["failure"]), (1, state, "host_transitioning"))
                for dependency in network:
                    dependency.assert_not_called()
                self.assert_read_calls(aws, [("sts", "get-caller-identity"), ("ec2", "describe-instances")])

    def test_stale_missing_or_unhealthy_ec2_checks_prevent_endpoint_probes(self):
        good = FakeAWS().checks[0]
        for checks in ([], [good, good], [dict(good, state="stopped")],
                       [dict(good, instance_id="i-abcdef01234567890")],
                       [dict(good, system_status="impaired")], [dict(good, instance_status="initializing")]):
            with self.subTest(checks=checks):
                aws = FakeAWS([host()], checks=checks)
                code, report, network = self.observe(aws)
                self.assertEqual((code, report["failure"], report["ec2_checks_passed"]), (1, "ec2_checks_failed", False))
                for dependency in network:
                    dependency.assert_not_called()
                self.assert_read_calls(aws, [("sts", "get-caller-identity"), ("ec2", "describe-instances"),
                                           ("ec2", "describe-instance-status")])

    def test_unusable_public_address_is_not_probed_or_published(self):
        for address in (None, "127.0.0.1", "169.254.169.254", "10.0.0.1", "224.0.0.1", SECRET):
            with self.subTest(address=address):
                code, report, network = self.observe(FakeAWS([host(public_ipv4=address)]))
                self.assertEqual((code, report["failure"]), (1, "invalid_public_address"))
                for dependency in network:
                    dependency.assert_not_called()

    def test_dns_mismatch_or_lookup_failure_never_probes_an_unverified_address(self):
        cases = [dict(dns_addresses=()), dict(dns_addresses=("8.8.8.8",)),
                 dict(dns_addresses=(HOST_IP, "8.8.8.8")), dict(dns_failure=socket.gaierror(SECRET))]
        for options in cases:
            with self.subTest(options=options):
                code, report, (_, tcp, udp) = self.observe(FakeAWS([host()]), **options)
                self.assertEqual(code, 1)
                self.assertFalse(report["dns_matches_host"])
                self.assertFalse(report["game_ready"])
                tcp.assert_not_called()
                udp.assert_not_called()

    def test_unreachable_java_or_bedrock_reports_failure_without_raw_errors(self):
        for kind in ("tcp", "udp"):
            with self.subTest(kind=kind):
                code, report, _ = self.observe(FakeAWS([host()]), **{kind + "_failure": OSError(SECRET)})
                self.assertEqual(code, 1)
                self.assertFalse(report["game_ready"])
                expected = "java_status_failed" if kind == "tcp" else "bedrock_discovery_failed"
                self.assertEqual(report["failure"], expected)

    def test_unrecognized_server_version_strings_are_omitted(self):
        packet = java_packet(version={"name": SECRET, "protocol": 776})
        advertisement = f"MCPE;{SECRET};2193;{SECRET};1;6;123;{SECRET};"
        code, report, _ = self.observe(FakeAWS([host()]), tcp_reply=TCPReply(packet),
                                       udp_reply=UDPReply(bedrock_packet(advertisement)))
        self.assertEqual(code, 0)
        self.assertNotIn("version", report["java"])
        self.assertNotIn("version", report["bedrock"])

    def test_java_rejects_oversized_truncated_invalid_and_unbounded_status_values(self):
        packets = [b"", b"\x80" * 6, varint(16385), b"\x05\x01\x03abc", b"\x05\x00\x03ab",
                   java_packet(version={"name": SECRET, "protocol": True}),
                   java_packet(version={"name": SECRET, "protocol": -1}),
                   java_packet(players={"online": True, "max": 6}),
                   java_packet(players={"online": 7, "max": 6}),
                   java_packet(players={"online": 0, "max": 100001}),
                   java_packet(description=SECRET * 2000)]
        for packet in packets:
            with self.subTest(packet_length=len(packet)):
                client = TCPReply(packet)
                code, report, (_, _, udp) = self.observe(FakeAWS([host()]), tcp_reply=client)
                self.assertEqual((code, report["failure"], report["java_status_ok"]), (1, "java_status_failed", False))
                udp.assert_not_called()
                self.assertTrue(all(size <= self.reporter().MAX_STATUS_BYTES for size in client.read_sizes))

    def test_bedrock_rejects_invalid_frames_and_counts_without_emitting_advertisements(self):
        good = bedrock_packet()
        packets = [b"", good[:34], b"\x01" + good[1:], good[:1] + b"\x01" + good[2:],
                   good[:17] + bytes(16) + good[33:], good[:-1], good + bytes(4096),
                   bedrock_packet(f"MCPE;{SECRET};-1;26.51;0;6;"),
                   bedrock_packet(f"MCPE;{SECRET};2193;26.51;7;6;"),
                   bedrock_packet(f"MCPE;{SECRET};2193;26.51;0;100001;"),
                   bedrock_packet(f"MCEE;{SECRET};2193;26.51;0;6;")]
        for packet in packets:
            with self.subTest(packet_length=len(packet)):
                client = UDPReply(packet)
                code, report, _ = self.observe(FakeAWS([host()]), udp_reply=client)
                self.assertEqual((code, report["failure"], report["bedrock_discovery_ok"]),
                                 (1, "bedrock_discovery_failed", False))
                self.assertEqual(client.read_sizes, [self.reporter().MAX_DISCOVERY_BYTES + 1])

    def test_each_socket_probe_has_one_total_deadline_even_when_reads_keep_progressing(self):
        reporter = self.reporter()
        for probe, dependency in ((reporter.java_status, "create_connection"), (reporter.bedrock_discovery, "socket"),
                                  (reporter.nethernet_signaling, "create_connection")):
            with self.subTest(probe=probe.__name__), \
                    mock.patch.object(reporter.time, "monotonic", side_effect=[100, 101, 106]), \
                    mock.patch.object(reporter.socket, dependency) as connect:
                client = connect.return_value.__enter__.return_value
                with self.assertRaises(reporter.HealthError):
                    probe(HOST_IP)
                client.recv.assert_not_called()

    def test_dns_timeout_returns_without_waiting_for_a_stalled_system_resolver(self):
        reporter = self.reporter()
        release = threading.Event()
        started = threading.Event()

        def stalled(*args):
            started.set()
            release.wait(1)
            return []

        try:
            with mock.patch.object(reporter.socket, "getaddrinfo", side_effect=stalled), \
                    mock.patch.object(reporter, "NETWORK_TIMEOUT", 0.03):
                before = time.monotonic()
                with self.assertRaisesRegex(reporter.HealthError, "dns_lookup_failed"):
                    reporter.require_dns_match(HOST_IP)
                self.assertTrue(started.is_set())
                self.assertLess(time.monotonic() - before, 0.5)
        finally:
            release.set()

    def test_standard_credential_environment_is_passed_through_without_mutation_or_output(self):
        reporter = self.reporter()
        environment = {"AWS_ACCESS_KEY_ID": SECRET, "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_SESSION_TOKEN": SECRET,
                       "AWS_PROFILE": "existing-profile", "AWS_CONFIG_FILE": "/private/existing-config"}
        with mock.patch.dict(reporter.os.environ, environment, clear=True):
            aws = FakeAWS()
            code, _, _ = self.observe(aws)
            self.assertEqual(code, 0)
            self.assertEqual(dict(reporter.os.environ), environment)
            for _, options in aws.calls:
                self.assertEqual({key: options["env"][key] for key in environment}, environment)

    def test_malformed_aws_output_stops_before_any_network_or_recovery(self):
        reporter = self.reporter()
        aws = FakeAWS()

        def malformed(command, **options):
            aws(command, **options)
            return subprocess.CompletedProcess(command, 0, SECRET, SECRET)

        code, report, network = self.observe(malformed)
        self.assertEqual((code, report["failure"]), (1, "invalid_aws_response"))
        self.assert_read_calls(aws, [("sts", "get-caller-identity")])
        for dependency in network:
            dependency.assert_not_called()


if __name__ == "__main__":
    unittest.main()
