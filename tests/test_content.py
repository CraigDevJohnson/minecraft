from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import minecraft_content as content


class ContentTests(unittest.TestCase):
    def test_managed_configuration_is_recoverable_from_the_exact_manifest(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        expected = (b'{"bedrock":{"address":"0.0.0.0","clone-remote-port":false,"port":19132},'
                    b'"config-version":7,"java":{"auth-type":"floodgate"},'
                    b'"log-player-ip-addresses":false,"saved-user-logins":[]}\n')
        artifact_bytes = b"fixture artifact contents"
        for artifact in manifest["artifacts"]:
            artifact["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            recovered = Path(directory) / "recovered-manifest.json"
            recovered.write_text(json.dumps(manifest))
            with mock.patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(artifact_bytes)):
                staged = content.stage_content(content.load_manifest(recovered), Path(directory) / "cache")
            self.assertEqual((staged / "plugins/Geyser-Spigot/config.yml").read_bytes(), expected)

    def test_corrupted_managed_cache_is_rejected_without_silent_replacement(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        artifact_bytes = b"fixture artifact contents"
        for artifact in manifest["artifacts"]:
            artifact["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(artifact_bytes)):
                staged = content.stage_content(manifest, Path(directory))
            target = staged / "plugins/Geyser-Spigot/config.yml"
            target.write_bytes(b"unexpected configuration bytes\n")
            with self.assertRaisesRegex(content.ContentError, "configuration checksum"):
                content.stage_content(manifest, Path(directory))
            self.assertEqual(target.read_bytes(), b"unexpected configuration bytes\n")

    def test_configuration_schema_protects_authentication_network_and_privacy(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        original_revision = content.manifest_digest(manifest)
        for mutate in (
            lambda c: c["java"].update({"auth-type": "offline"}),
            lambda c: c["bedrock"].update(port="19132"),
            lambda c: c["bedrock"].update({"clone-remote-port": True}),
            lambda c: c.update({"config-version": 7.0}),
            lambda c: c.update({"log-player-ip-addresses": True}),
            lambda c: c.update({"saved-user-logins": ["unapproved-fixture"]}),
            lambda c: c["java"].clear(),
            lambda c: c.update({"unexpected-config-key": True}),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                invalid = copy.deepcopy(manifest)
                managed = invalid["managed_files"][0]
                mutate(managed["content"])
                encoded = (json.dumps(managed["content"], sort_keys=True, separators=(",", ":")) + "\n").encode()
                managed["sha256"] = hashlib.sha256(encoded).hexdigest()
                self.assertNotEqual(content.manifest_digest(invalid), original_revision)
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps(invalid))
                with self.assertRaisesRegex(content.ContentError, "configuration schema"):
                    content.load_manifest(path)

    def test_managed_configuration_integrity_and_file_identity_are_required(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        for mutate in (
            lambda m: m["managed_files"][0].update(sha256="0" * 64),
            lambda m: m["managed_files"][0].update(path="../unrelated.yml"),
            lambda m: m["managed_files"][0].update(format="unknown-encoding"),
            lambda m: m.update(managed_files=[]),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                invalid = copy.deepcopy(manifest)
                mutate(invalid)
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps(invalid))
                with self.assertRaises(content.ContentError):
                    content.load_manifest(path)

    def test_render_requires_acceptance_and_generates_private_authenticated_runtime(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        parameters = dict(data_dir=Path("/srv/minecraft/instances/survival/data"), artifacts_dir=Path("/opt/minecraft/content/fixture"), rcon_password="a-private-fixture-password", eula_accepted=False)
        with self.assertRaisesRegex(content.ContentError, "EULA"):
            content.render_compose(definition, manifest, **parameters)
        parameters["eula_accepted"] = True
        result = content.render_compose(definition, manifest, **parameters)
        self.assertEqual(result, content.render_compose(definition, manifest, **parameters))
        service = result["services"]["minecraft"]
        env = service["environment"]
        self.assertEqual(service["restart"], "no")
        self.assertEqual(service["platform"], "linux/arm64")
        self.assertEqual(env["ONLINE_MODE"], "TRUE")
        self.assertEqual(env["ENABLE_WHITELIST"], "TRUE")
        self.assertEqual(env["ENFORCE_WHITELIST"], "TRUE")
        self.assertEqual(env["CUSTOM_SERVER_PROPERTIES"], "rcon.ip=127.0.0.1")
        self.assertEqual(env["ENFORCE_SECURE_PROFILE"], "FALSE")
        self.assertEqual(env["CUSTOM_SERVER"], "/runtime/paper.jar")
        self.assertEqual(env["MAX_PLAYERS"], "6")
        self.assertNotIn("OPS", env)
        self.assertEqual(service["ports"], ["0.0.0.0:25565:25565/tcp", "0.0.0.0:19132:19132/udp"])
        self.assertTrue(all(not mount["bind"]["create_host_path"] for mount in service["volumes"]))

    def test_definition_rejects_invalid_identity_and_ports(self):
        definition = json.loads((ROOT / "runtime/survival.json").read_text())
        for field, value in (("id", "../survival"), ("java_port", 70000), ("memory", "5G\nJAVA_OPTS=bad")):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "definition.json"
                path.write_text(json.dumps(dict(definition, **{field: value})))
                with self.assertRaises(content.ContentError):
                    content.load_definition(path)

    def test_artifacts_are_checked_before_use_and_cached_bytes_are_rechecked(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        data = b"fixture artifact contents"
        for artifact in manifest["artifacts"]:
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(data)) as download:
                staged = content.stage_content(manifest, Path(directory))
                self.assertEqual((staged / "paper.jar").read_bytes(), data)
                self.assertEqual(download.call_count, 3)
                content.stage_content(manifest, Path(directory))
                self.assertEqual(download.call_count, 3)
                (staged / "paper.jar").write_bytes(b"corrupted")
                with self.assertRaisesRegex(content.ContentError, "checksum"):
                    content.stage_content(manifest, Path(directory))
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong bytes")):
                with self.assertRaisesRegex(content.ContentError, "checksum"):
                    content.stage_content(manifest, Path(directory) / "fresh")

    def test_staging_rejects_a_symlink_in_managed_configuration(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        data = b"fixture artifact contents"
        for artifact in manifest["artifacts"]:
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(data)):
                staged = content.stage_content(manifest, Path(directory) / "cache")
            unrelated = Path(directory) / "unrelated.txt"
            unrelated.write_text("preserve")
            config = staged / "plugins/Geyser-Spigot/config.yml"
            config.unlink()
            config.symlink_to(unrelated)
            with self.assertRaisesRegex(content.ContentError, "symlink"):
                content.stage_content(manifest, Path(directory) / "cache")
            self.assertEqual(unrelated.read_text(), "preserve")

    def test_manifest_rejects_moving_image_and_artifact_selectors(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        for mutate in (
            lambda m: m.update(image="itzg/minecraft-server:java25"),
            lambda m: m["artifacts"][0].update(url="https://example.com/latest/server.jar"),
            lambda m: m["artifacts"][0].update(sha256="not-a-checksum"),
        ):
            invalid = copy.deepcopy(manifest)
            mutate(invalid)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps(invalid))
                with self.assertRaises(content.ContentError):
                    content.load_manifest(path)


if __name__ == "__main__":
    unittest.main()
