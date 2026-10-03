"""Current EC2 NAT advertisement at the metadata and Compose boundaries."""
from __future__ import annotations

import io
import http.client
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
from validate_lifecycle import LocalMachine
from validate_recovery import RecoveryMachine


class NetherNetAddressTests(unittest.TestCase):
    def test_native_prepare_refreshes_the_advertisement_on_each_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = object.__new__(runtime.LinuxMachine)
            machine.root = Path(directory)
            machine.config = {"eula_accepted": True, "rcon_password_file": "/private/fixture-rcon"}
            machine.definitions = {"survival": content.load_definition(ROOT / "runtime/survival.json")}
            machine.manifests = {"survival": content.load_manifest(ROOT / "runtime/content.json")}
            machine.verify_storage = mock.Mock()
            with mock.patch.object(runtime, "private_text", return_value="a-private-fixture-password"), \
                    mock.patch.object(runtime, "ec2_public_ipv4", side_effect=["8.8.8.8", "8.8.4.4"]), \
                    mock.patch.object(runtime, "command"), mock.patch.object(runtime.os, "chown"), \
                    mock.patch.object(content, "stage_content", return_value=Path(directory) / "artifacts"):
                for address in ("8.8.8.8", "8.8.4.4"):
                    machine.prepare("survival")
                    compose = json.loads((machine.runtime("survival") / "compose.json").read_text())
                    self.assertEqual(compose["services"]["minecraft"]["environment"]["JVM_OPTS"],
                                     f"-DgeyserAdvertiseAddresses={address}")

    def test_metadata_requests_use_imdsv2_fixed_paths_no_proxy_and_bounded_reads(self):
        opener = mock.Mock()
        opener.open.side_effect = [io.BytesIO(b"fixture-token"), io.BytesIO(b"8.8.8.8\n")]
        with mock.patch.object(runtime.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(runtime.ec2_public_ipv4(), "8.8.8.8")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], runtime.RefuseMetadataRedirects)
        token_request, address_request = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(token_request.full_url, "http://169.254.169.254/latest/api/token")
        self.assertEqual(token_request.get_method(), "PUT")
        self.assertEqual(token_request.get_header("X-aws-ec2-metadata-token-ttl-seconds"), "60")
        self.assertEqual(address_request.full_url, "http://169.254.169.254/latest/meta-data/public-ipv4")
        self.assertEqual(address_request.get_header("X-aws-ec2-metadata-token"), "fixture-token")
        self.assertEqual([call.kwargs["timeout"] for call in opener.open.call_args_list], [5, 5])
        self.assertIsNone(runtime.RefuseMetadataRedirects().redirect_request(None, 302, "", {}, "https://example.com"))

    def test_invalid_metadata_and_request_failures_refuse_advertisement(self):
        for token, address in ((b"", b"8.8.8.8"), (b"token\n", b"8.8.8.8"),
                               (b"x" * 4097, b"8.8.8.8"), (b"token", b"127.0.0.1"),
                               (b"token", b"10.0.0.1"), (b"token", b"224.0.0.1"),
                               (b"token", b"::1"), (b"token", b"unavailable")):
            with self.subTest(token_size=len(token), address=address):
                opener = mock.Mock()
                opener.open.side_effect = [io.BytesIO(token), io.BytesIO(address)]
                with mock.patch.object(runtime.urllib.request, "build_opener", return_value=opener), \
                        self.assertRaisesRegex(runtime.OperationError, "current EC2 public IPv4"):
                    runtime.ec2_public_ipv4()
        for exception in (OSError("private diagnostic"), http.client.IncompleteRead(b"private diagnostic"),
                          http.client.BadStatusLine("private diagnostic")):
            opener = mock.Mock()
            opener.open.side_effect = exception
            with mock.patch.object(runtime.urllib.request, "build_opener", return_value=opener), \
                    self.assertRaises(runtime.OperationError) as failure:
                runtime.ec2_public_ipv4()
            self.assertNotIn("private diagnostic", str(failure.exception))

    def test_real_controller_uses_metadata_but_local_recovery_never_calls_it(self):
        with mock.patch.object(runtime, "ec2_public_ipv4", side_effect=["8.8.8.8", "8.8.4.4"]) as lookup:
            machine = object.__new__(runtime.LinuxMachine)
            self.assertEqual(machine.nethernet_advertise_address(), "8.8.8.8")
            self.assertEqual(machine.nethernet_advertise_address(), "8.8.4.4")
            self.assertEqual(object.__new__(LocalMachine).nethernet_advertise_address(), "127.0.0.1")
            self.assertEqual(object.__new__(RecoveryMachine).nethernet_advertise_address(), "127.0.0.1")
            self.assertEqual(lookup.call_count, 2)

    def test_compose_allows_only_public_ipv4_or_the_isolated_loopback_override(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        arguments = dict(data_dir=Path("/private/data"), artifacts_dir=Path("/private/artifacts"),
                         rcon_password="a-private-fixture-password", eula_accepted=True)
        for address in ("8.8.8.8", "8.8.4.4"):
            environment = content.render_compose(definition, manifest, **arguments,
                nethernet_advertise_address=address)["services"]["minecraft"]["environment"]
            self.assertEqual(environment["JVM_OPTS"], f"-DgeyserAdvertiseAddresses={address}")
        for address in (134744072, "127.0.0.1", "10.0.0.1", "224.0.0.1", "hostname.example", "8.8.8.8:19133", "8.8.8.8,8.8.4.4"):
            with self.subTest(address=address), self.assertRaises(content.ContentError):
                content.render_compose(definition, manifest, **arguments, nethernet_advertise_address=address)
        for java, bedrock in ((19132, 19132), (25565, 19133)):
            with self.assertRaises(content.ContentError):
                content.render_compose(dict(definition, java_port=java, bedrock_port=bedrock), manifest, **arguments)
