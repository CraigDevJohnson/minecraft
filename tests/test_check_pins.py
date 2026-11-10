import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_pins  # noqa: E402

GOOD = "a" * 64
OTHER = "b" * 64


def answers(digests: dict):
    def fetch(url: str) -> dict:
        if url not in digests:
            raise OSError("not found")
        return {"sha256": digests[url], "sha512": "c" * 128, "size": 1}
    return fetch


class CheckPinsTests(unittest.TestCase):
    def run_check(self, content: dict, digests: dict):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "server.json"
            manifest.write_text(json.dumps(content))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                failed = check_pins.check(manifest, fetch=answers(digests))
        return failed, output.getvalue()

    def test_every_pin_in_the_content_is_found_wherever_it_sits(self):
        content = {"artifacts": [{"name": "paper", "url": "https://a.example/paper.jar", "sha256": GOOD}],
                   "mods": {"claims": {"url": "https://a.example/claims.jar", "sha256": OTHER, "name": "Claims"}},
                   "base": {"artifacts": [{"url": "https://a.example/plain.jar", "sha256": GOOD}]},
                   "crafty": {"image": "registry.example/image@sha256:" + GOOD}}
        self.assertEqual(check_pins.pins(content), [
            ("paper", "https://a.example/paper.jar", GOOD),
            ("Claims", "https://a.example/claims.jar", OTHER),
            ("artifacts", "https://a.example/plain.jar", GOOD),
        ])

    def test_the_real_pinned_content_names_downloads(self):
        found = check_pins.pins(json.loads(check_pins.MANIFEST.read_text()))
        self.assertTrue(found)
        for _, url, sha256 in found:
            self.assertTrue(url.startswith("https://"))
            self.assertRegex(sha256, r"^[0-9a-f]{64}$")

    def test_matching_downloads_pass(self):
        failed, output = self.run_check({"artifacts": [{"name": "paper", "url": "https://a.example/p.jar", "sha256": GOOD}]},
                                        {"https://a.example/p.jar": GOOD})
        self.assertEqual(failed, 0)
        self.assertIn("ok       paper", output)

    def test_a_download_that_differs_or_fails_is_counted_with_what_was_downloaded(self):
        content = {"artifacts": [{"name": "paper", "url": "https://a.example/p.jar", "sha256": GOOD},
                                 {"name": "gone", "url": "https://a.example/gone.jar", "sha256": GOOD}]}
        failed, output = self.run_check(content, {"https://a.example/p.jar": OTHER})
        self.assertEqual(failed, 2)
        self.assertIn("downloaded " + OTHER, output)
        self.assertIn("download failed", output)

    def test_content_without_downloads_is_refused(self):
        with self.assertRaises(check_pins.PinError):
            self.run_check({"artifacts": []}, {})

    def test_a_plain_http_pin_is_refused(self):
        with self.assertRaises(check_pins.PinError):
            check_pins.fetch("http://a.example/p.jar")

    def test_a_modrinth_version_answers_its_pin(self):
        described = {"version_number": "1.2.3", "game_versions": ["26.3"], "loaders": ["paper"], "files": [
            {"url": "https://cdn.modrinth.com/data/abc/versions/def/Other.jar", "filename": "Other.jar", "primary": False,
             "hashes": {"sha512": "d" * 128}},
            {"url": "https://cdn.modrinth.com/data/abc/versions/def/Claims.jar", "filename": "Claims.jar", "primary": True,
             "hashes": {"sha512": "c" * 128}}]}
        pin = check_pins.modrinth_pin("AbCd1234", read_json=lambda url: described,
                                      fetch=answers({"https://cdn.modrinth.com/data/abc/versions/def/Claims.jar": GOOD}))
        self.assertEqual((pin["version"], pin["file"], pin["sha256"]), ("1.2.3", "Claims.jar", GOOD))
        self.assertEqual(pin["url"], "https://cdn.modrinth.com/data/abc/versions/def/Claims.jar")

    def test_a_modrinth_file_elsewhere_or_with_another_checksum_is_refused(self):
        elsewhere = {"files": [{"url": "https://files.example/Claims.jar", "filename": "Claims.jar", "hashes": {"sha512": "c" * 128}}]}
        changed = {"files": [{"url": "https://cdn.modrinth.com/data/a/versions/b/Claims.jar", "filename": "Claims.jar",
                              "hashes": {"sha512": "e" * 128}}]}
        for described in (elsewhere, changed, {"files": []}):
            with self.subTest(described=described), self.assertRaises(check_pins.PinError):
                check_pins.modrinth_pin("AbCd1234", read_json=lambda url, described=described: described,
                                        fetch=answers({"https://cdn.modrinth.com/data/a/versions/b/Claims.jar": GOOD}))
        with self.assertRaises(check_pins.PinError):
            check_pins.modrinth_pin("not a version id", read_json=lambda url: {}, fetch=answers({}))


if __name__ == "__main__":
    unittest.main()
