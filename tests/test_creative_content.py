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
from minecraft_instance import LinuxMachine

RAKNET_DIGEST = "72f0640583a5fec446ccd299cbbf35c5aff6815a358eb251cece7975bd148c82"


class CreativeContentTests(unittest.TestCase):
    def render(self, definition, manifest, **options):
        return content.render_compose(
            definition, manifest, data_dir=Path("/private/data"),
            artifacts_dir=Path("/private/content"), rcon_password="a-private-fixture-password",
            eula_accepted=True, **options)["services"]["minecraft"]

    def test_exact_reviewed_raknet_manifest_loads_for_launch(self):
        manifest = content.load_manifest(ROOT / "runtime/content-raknet.json")
        self.assertEqual(content.manifest_digest(manifest), RAKNET_DIGEST)
        content._validate_manifest(manifest, recorded=True)

    def test_actual_controller_loads_survival_and_creative_catalog_with_legacy_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            survival = json.loads((ROOT / "runtime/survival.json").read_text())
            survival["manifest"] = "content-raknet.json"
            (root / "survival.json").write_text(json.dumps(survival))
            for name in ("creative.json", "content-raknet.json"):
                (root / name).write_bytes((ROOT / "runtime" / name).read_bytes())
            deployment = root / "deployment.json"
            deployment.write_text(json.dumps({"instances": {
                instance: {"data_path": f"/srv/minecraft/instances/{instance}/data",
                           "backup_prefix": f"instances/{instance}/"}
                for instance in ("survival", "creative")}}))
            deployment.chmod(0o600)
            machine = LinuxMachine({"deployment": str(deployment), "definitions": {
                instance: str(root / f"{instance}.json") for instance in ("survival", "creative")}})
            self.assertEqual(set(machine.definitions), {"survival", "creative"})
            self.assertEqual({content.manifest_digest(m) for m in machine.manifests.values()}, {RAKNET_DIGEST})

    def test_creative_definition_renders_creative_mode_and_only_raknet_ports(self):
        definition = content.load_definition(ROOT / "runtime/creative.json")
        manifest = content.load_manifest(ROOT / "runtime" / definition["manifest"])
        service = self.render(definition, manifest, nethernet_advertise_address="44.240.123.1")
        self.assertEqual((definition["java_port"], definition["bedrock_port"], definition["memory"],
                          definition["max_players"], definition["view_distance"],
                          definition["simulation_distance"]), (25566, 19134, "1G", 6, 6, 4))
        self.assertEqual(service["environment"]["MODE"], "creative")
        self.assertEqual(service["environment"]["FORCE_GAMEMODE"], "TRUE")
        self.assertNotIn("JVM_OPTS", service["environment"])
        self.assertEqual(service["ports"], ["0.0.0.0:25566:25565/tcp", "0.0.0.0:19134:19132/udp"])
        self.assertEqual(content.published_game_ports(definition, manifest),
                         frozenset({("tcp", 25566), ("udp", 19134)}))

    def test_existing_survival_definition_renders_legacy_ports_without_forced_mode(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        manifest = content.load_manifest(ROOT / "runtime/content-raknet.json")
        service = self.render(definition, manifest)
        self.assertEqual(service["environment"]["MODE"], "survival")
        self.assertNotIn("FORCE_GAMEMODE", service["environment"])
        self.assertNotIn("JVM_OPTS", service["environment"])
        self.assertEqual(service["ports"], ["0.0.0.0:25565:25565/tcp", "0.0.0.0:19132:19132/udp"])

    def test_default_dual_render_remains_byte_identical_to_reviewed_baseline(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        manifest = content.load_manifest(ROOT / "runtime/content.json")
        rendered = content.render_compose(
            definition, manifest, data_dir=Path("/private/data"), artifacts_dir=Path("/private/content"),
            rcon_password="a-private-fixture-password", eula_accepted=True,
            nethernet_advertise_address="44.240.123.1")
        canonical = json.dumps(rendered, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(),
                         "c3426d826138a5680d8bbdff0d47ebee21ef67ba230c4abece2e425b3095c698")

    def test_explicit_survival_mode_preserves_default_render(self):
        definition = content.load_definition(ROOT / "runtime/survival.json")
        for filename in ("content.json", "content-raknet.json"):
            manifest = content.load_manifest(ROOT / "runtime" / filename)
            self.assertEqual(self.render(definition, manifest),
                             self.render(dict(definition, gamemode="survival"), manifest))

    def test_gamemode_rejects_wrong_types_and_unapproved_values_at_definition_boundary(self):
        definition = json.loads((ROOT / "runtime/survival.json").read_text())
        for mode in (None, True, False, 0, 1, [], {}, "Creative", "adventure", "spectator", "creative\nMODE=survival"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "instance.json"
                path.write_text(json.dumps(dict(definition, gamemode=mode)))
                with self.assertRaisesRegex(content.ContentError, "gamemode"):
                    content.load_definition(path)

    def test_legacy_changes_and_digest_spoof_are_rejected_before_staging_or_rendering(self):
        original = json.loads((ROOT / "runtime/content-raknet.json").read_text())
        definition = content.load_definition(ROOT / "runtime/survival.json")
        for mutate in (lambda m: m.update(revision=m["revision"] + "-creative"),
                       lambda m: m.update(approved_sha256=RAKNET_DIGEST),
                       lambda m: m["artifacts"][0].update(sha256="0" * 64),
                       lambda m: m["artifacts"][0].update(build=m["artifacts"][0]["build"] + 1)):
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.subTest(manifest=changed["revision"]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps(changed))
                with self.assertRaises(content.ContentError):
                    content.load_manifest(path)
                with mock.patch("urllib.request.urlopen") as download:
                    with self.assertRaises(content.ContentError):
                        content.stage_content(changed, Path(directory) / "cache")
                    download.assert_not_called()
                with self.assertRaises(content.ContentError):
                    self.render(definition, changed)

    def test_legacy_configuration_tamper_is_rejected_even_with_a_matching_configuration_hash(self):
        manifest = json.loads((ROOT / "runtime/content-raknet.json").read_text())
        managed = manifest["managed_files"][0]
        managed["content"]["java"]["auth-type"] = "offline"
        managed["sha256"] = hashlib.sha256(content._managed_file_bytes(managed)).hexdigest()
        with self.assertRaisesRegex(content.ContentError, "configuration schema"):
            content._validate_manifest(manifest)

    def test_legacy_staging_still_verifies_artifact_bytes(self):
        manifest = content.load_manifest(ROOT / "runtime/content-raknet.json")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"corrupted jar")) as download:
            with self.assertRaisesRegex(content.ContentError, "downloaded artifact checksum"):
                content.stage_content(manifest, Path(directory))
            self.assertEqual(download.call_count, 1)
            self.assertEqual(download.call_args.args[0].full_url, manifest["artifacts"][0]["url"])

    def test_older_v7_manifest_remains_recorded_only(self):
        manifest = json.loads((ROOT / "tests/fixtures/content-survival-2026-09-08-managed-config.json").read_text())
        content._validate_manifest(manifest, recorded=True)
        with self.assertRaises(content.ContentError):
            content._validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
