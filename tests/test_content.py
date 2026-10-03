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

# The exact manifest active before ViaVersion; live recovery points record it (digest ebe2fd62...).
PREVIOUS_MANIFEST = ROOT / "tests/fixtures/content-survival-2026-09-08-managed-config.json"


def write_manifest(directory: str, manifest: dict) -> Path:
    path = Path(directory) / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def with_webrtc_port(manifest: dict, port) -> dict:
    changed = copy.deepcopy(manifest)
    managed = changed["managed_files"][0]
    managed["content"]["bedrock"]["webrtc-port"] = port
    managed["sha256"] = hashlib.sha256(content._managed_file_bytes(managed)).hexdigest()
    return changed


class ContentTests(unittest.TestCase):
    def test_two_instances_own_non_overlapping_same_port_webrtc_bindings(self):
        survival = content.load_definition(ROOT / "runtime/survival.json")
        creative = dict(survival, id="creative", java_port=25566, bedrock_port=19135)
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        creative_manifest = with_webrtc_port(manifest, 19134)
        content._validate_manifest(creative_manifest)
        content._validate_manifest(creative_manifest, recorded=True)
        ports = []
        for definition, selected in ((survival, manifest), (creative, creative_manifest)):
            rendered = content.render_compose(definition, selected,
                data_dir=Path("/private") / definition["id"] / "data", artifacts_dir=Path("/private/artifacts"),
                rcon_password="a-private-fixture-password", eula_accepted=True)
            ports.append(content.published_game_ports(definition, selected))
            port = content.webrtc_port(selected)
            self.assertIn(f"0.0.0.0:{port}:{port}/udp", rendered["services"]["minecraft"]["ports"])
        self.assertFalse(ports[0] & ports[1])
        self.assertEqual(content.webrtc_port(manifest), 19133)

    def test_manifest_port_variation_preserves_every_other_typed_configuration_value(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        for port in (1, 19134, 65535):
            custom = with_webrtc_port(manifest, port)
            content._validate_manifest(custom)
            content._validate_manifest(custom, recorded=True)
        for port in (0, -1, 65536, 19132, True, False, 19134.0, "19134", None):
            custom = with_webrtc_port(manifest, port)
            with self.subTest(port=port), self.assertRaisesRegex(content.ContentError, "configuration schema"):
                content._validate_manifest(custom)
        for mutate in (lambda c: c["bedrock"]["signaling"].update(mode="nxs"),
                       lambda c: c["java"].update({"auth-type": "offline"}),
                       lambda c: c.update({"log-player-ip-addresses": True})):
            custom = with_webrtc_port(manifest, 19134)
            managed = custom["managed_files"][0]
            mutate(managed["content"])
            managed["sha256"] = hashlib.sha256(content._managed_file_bytes(managed)).hexdigest()
            with self.assertRaisesRegex(content.ContentError, "configuration schema"):
                content._validate_manifest(custom, recorded=True)

    def test_custom_webrtc_port_rejects_udp_collision_but_may_share_a_tcp_number(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        parameters = dict(data_dir=Path("/private/data"), artifacts_dir=Path("/private/artifacts"),
                          rcon_password="a-private-fixture-password", eula_accepted=True)
        with self.assertRaises(content.ContentError):
            content.render_compose(dict(definition, bedrock_port=19134), with_webrtc_port(manifest, 19134), **parameters)
        rendered = content.render_compose(definition, with_webrtc_port(manifest, 25565), **parameters)
        self.assertIn("0.0.0.0:25565:25565/udp", rendered["services"]["minecraft"]["ports"])

    def test_managed_configuration_is_recoverable_from_the_exact_manifest(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        expected = (b'{"bedrock":{"address":"0.0.0.0","clone-remote-port":false,"port":19132,'
                    b'"signaling":{"mode":"builtin"},"transport":"both","webrtc-port":19133},'
                    b'"config-version":8,"java":{"auth-type":"floodgate"},'
                    b'"log-player-ip-addresses":false,"saved-user-logins":[]}\n')
        self.assertEqual(hashlib.sha256(expected).hexdigest(),
                         "961fc91c79c38f8e09fd14d1e959b06522ec72b595b933e78fef83563e4b8e42")
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
            lambda c: c.update({"config-version": 8.0}),
            lambda c: c.update({"config-version": 7}),
            lambda c: c.update({"config-version": 9}),
            lambda c: c["bedrock"].update(transport="webrtc"),
            lambda c: c["bedrock"].update(transport="raknet"),
            lambda c: c["bedrock"].update({"webrtc-port": 19132}),
            lambda c: c["bedrock"].update({"webrtc-port": True}),
            lambda c: c["bedrock"]["signaling"].update(mode="nxs"),
            lambda c: c["bedrock"]["signaling"].update(token="unapproved"),
            lambda c: c["bedrock"].pop("signaling"),
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

    def test_manifest_pins_reviewed_crossplay_content_and_geyser_v8_configuration(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        self.assertEqual([(artifact["name"], artifact["path"]) for artifact in manifest["artifacts"]], [
            ("paper", "paper.jar"), ("geyser", "plugins/Geyser-Spigot.jar"),
            ("floodgate", "plugins/floodgate-spigot.jar"), ("viaversion", "plugins/ViaVersion.jar")])
        paper, geyser, floodgate, viaversion = manifest["artifacts"]
        self.assertEqual((paper["version"], paper["build"]), (manifest["minecraft_version"], 129))
        self.assertEqual((geyser["version"], geyser["build"]), ("2.11.3", 1247))
        self.assertEqual((floodgate["version"], floodgate["build"]), ("2.2.5", 141))
        # A GitHub release asset is identified by its exact tag and SHA-256; it has no CI build number.
        self.assertEqual(viaversion["version"], "5.12.0")
        self.assertNotIn("build", viaversion)
        self.assertEqual(viaversion["url"], "https://github.com/ViaVersion/ViaVersion/releases/download/5.12.0/ViaVersion-5.12.0.jar")
        managed = manifest["managed_files"][0]
        self.assertEqual(managed["content"]["config-version"], 8)
        self.assertEqual(managed["sha256"], "961fc91c79c38f8e09fd14d1e959b06522ec72b595b933e78fef83563e4b8e42")
        self.assertNotEqual(content.manifest_digest(manifest),
                            content.manifest_digest(json.loads(PREVIOUS_MANIFEST.read_text())))

    def test_manifest_requires_exactly_paper_geyser_floodgate_and_viaversion(self):
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        unknown = dict(manifest["artifacts"][3], name="protocollib", path="plugins/ProtocolLib.jar")
        via = "https://github.com/ViaVersion/ViaVersion/releases/download/5.12.0/ViaVersion-5.12.0.jar"
        for mutate in (
            lambda m: m["artifacts"].pop(),
            lambda m: m["artifacts"].append(copy.deepcopy(m["artifacts"][3])),
            lambda m: m["artifacts"].append(unknown),
            lambda m: m["artifacts"].__setitem__(3, unknown),
            lambda m: m["artifacts"].insert(0, m["artifacts"].pop()),
            lambda m: m["artifacts"].__setitem__(3, "viaversion"),
            lambda m: m["artifacts"][3].update(path="plugins/ViaVersion-5.12.0.jar"),
            lambda m: m["artifacts"][3].update(path="../ViaVersion.jar"),
            lambda m: m["artifacts"][3].update(name="ViaVersion"),
            lambda m: m["artifacts"][3].update(build=1),
            lambda m: m["artifacts"][3].update(version="latest"),
            lambda m: m["artifacts"][3].update(version="5.12.1"),
            lambda m: m["artifacts"][3].update(url=via.replace("/ViaVersion/ViaVersion/", "/Fork/ViaVersion/")),
            lambda m: m["artifacts"][3].update(url=via.replace("/download/5.12.0/", "/download/5.11.0/")),
            lambda m: m["artifacts"][3].update(url="https://github.com/ViaVersion/ViaVersion/releases/latest/download/ViaVersion.jar"),
            lambda m: m["artifacts"][3].update(url=via.replace("https:", "http:")),
            lambda m: m["artifacts"][3].update(url=via + "?raw=1"),
            lambda m: m["artifacts"][3].update(url="https://hangarcdn.papermc.io/plugins/ViaVersion/ViaVersion/versions/5.12.0/PAPER/ViaVersion-5.12.0.jar"),
            lambda m: m["artifacts"][1].update(url="https://github.com/GeyserMC/Geyser/releases/download/2.11.3/Geyser-Spigot.jar"),
            lambda m: m["artifacts"][1].pop("build"),
            lambda m: m["artifacts"][1].update(build=True),
            lambda m: m["artifacts"][3].update(sha256="0" * 63),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                invalid = copy.deepcopy(manifest)
                mutate(invalid)
                with self.assertRaises(content.ContentError):
                    content.load_manifest(write_manifest(directory, invalid))

    def test_previous_release_manifest_is_only_accepted_as_recorded_recovery_content(self):
        previous = json.loads(PREVIOUS_MANIFEST.read_text())
        current = content.load_manifest(ROOT / "runtime/content.json")
        self.assertEqual(content.manifest_digest(previous), "ebe2fd621e9328dd708aa77918dbd96f8039670cddc3364c4f45e582b931c29e")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(content.ContentError, "exactly Paper, Geyser, Floodgate and ViaVersion"):
                content.load_manifest(write_manifest(directory, previous))
            with self.assertRaises(content.ContentError):
                content.stage_content(previous, Path(directory) / "cache")
            self.assertFalse((Path(directory) / "cache").exists())
        # Recovery metadata may name an earlier reviewed release, but this release never stages it.
        content._validate_manifest(previous, recorded=True)
        content._validate_manifest(current, recorded=True)

        def with_geyser(manifest, configuration):
            mixed = copy.deepcopy(manifest)
            mixed["managed_files"][0]["content"] = copy.deepcopy(configuration)
            mixed["managed_files"][0]["sha256"] = hashlib.sha256(content._managed_file_bytes(mixed["managed_files"][0])).hexdigest()
            return mixed

        tampered = copy.deepcopy(previous["managed_files"][0]["content"])
        tampered["java"]["auth-type"] = "offline"
        for invalid in (
            with_geyser(current, previous["managed_files"][0]["content"]),  # ViaVersion release with Geyser v7
            with_geyser(previous, current["managed_files"][0]["content"]),  # pre-ViaVersion release with Geyser v8
            with_geyser(previous, tampered),
            dict(previous, artifacts=previous["artifacts"] + [current["artifacts"][3], current["artifacts"][3]]),
            dict(previous, artifacts=previous["artifacts"][:2]),
            # Earlier policy structure but not the exact reviewed release (newer Geyser swapped in).
            dict(previous, artifacts=[previous["artifacts"][0], current["artifacts"][1], previous["artifacts"][2]]),
        ):
            with self.subTest(invalid=invalid["artifacts"]), self.assertRaises(content.ContentError):
                content._validate_manifest(invalid, recorded=True)

    def test_raknet_v8_is_recorded_only_and_requires_the_exact_reviewed_manifest(self):
        previous = json.loads((ROOT / "tests/fixtures/content-survival-2026-10-02-bedrock-2650-viaversion.json").read_text())
        self.assertEqual(content.manifest_digest(previous),
                         "72f0640583a5fec446ccd299cbbf35c5aff6815a358eb251cece7975bd148c82")
        content._validate_manifest(previous, recorded=True)
        with self.assertRaises(content.ContentError):
            content._validate_manifest(previous)
        changed = copy.deepcopy(previous)
        changed["revision"] = "unreviewed-raknet-copy"
        with self.assertRaisesRegex(content.ContentError, "exact earlier reviewed"):
            content._validate_manifest(changed, recorded=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(content.ContentError):
                content.stage_content(previous, Path(directory))

    def test_custom_local_ports_publish_both_transports_without_exposing_rcon(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        definition.update(java_port=25566, bedrock_port=29000)
        compose = content.render_compose(definition, content.load_manifest(ROOT / "runtime/content.json"),
            data_dir=Path("/private/data"), artifacts_dir=Path("/private/artifacts"),
            rcon_password="a-private-fixture-password", eula_accepted=True, bind_address="127.0.0.1",
            nethernet_advertise_address="127.0.0.1")
        self.assertEqual(compose["services"]["minecraft"]["ports"], [
            "127.0.0.1:25566:25565/tcp", "127.0.0.1:29000:19132/udp",
            "127.0.0.1:29000:19132/tcp", "127.0.0.1:19133:19133/udp"])
        self.assertEqual(compose["services"]["minecraft"]["environment"]["JVM_OPTS"],
                         "-DgeyserAdvertiseAddresses=127.0.0.1")

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
        self.assertEqual(service["ports"], ["0.0.0.0:25565:25565/tcp", "0.0.0.0:19132:19132/udp",
                                          "0.0.0.0:19132:19132/tcp", "0.0.0.0:19133:19133/udp"])
        self.assertTrue(all(not mount["bind"]["create_host_path"] for mount in service["volumes"]))
        # Every pinned plugin JAR, including ViaVersion, is synced from one read-only staged source.
        self.assertIn({"type": "bind", "source": "/opt/minecraft/content/fixture/plugins", "target": "/plugins",
                       "read_only": True, "bind": {"create_host_path": False}}, service["volumes"])
        self.assertEqual(env["SYNC_SKIP_NEWER_IN_DESTINATION"], "FALSE")

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
                self.assertEqual((staged / "plugins/ViaVersion.jar").read_bytes(), data)
                self.assertEqual(sorted(path.name for path in (staged / "plugins").iterdir()),
                                 ["Geyser-Spigot", "Geyser-Spigot.jar", "ViaVersion.jar", "floodgate-spigot.jar"])
                self.assertEqual(download.call_count, 4)
                content.stage_content(manifest, Path(directory))
                self.assertEqual(download.call_count, 4)
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
