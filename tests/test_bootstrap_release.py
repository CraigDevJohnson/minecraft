from __future__ import annotations

import base64
import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import minecraft_bootstrap_release as release


class BootstrapReleaseTests(unittest.TestCase):
    def test_rendered_session_bootstrap_fits_ec2_user_data_limit(self):
        bootstrap = (ROOT / 'scripts/host-bootstrap.sh.tftpl').read_text()
        bootstrap = bootstrap.replace('${storage_script_base64}', base64.b64encode((ROOT / 'scripts/minecraft_storage.py').read_bytes()).decode())
        bootstrap = bootstrap.replace('${data_volume_id}', 'vol-0123456789abcdef0').replace('${data_filesystem_uuid}', '12345678-1234-4234-8234-123456789abc')
        session = (ROOT / 'scripts/on-demand-bootstrap.sh.tftpl').read_text()
        for key, name in [('cli_installer_base64', 'install_aws_cli.sh'), ('release_script_base64', 'minecraft_bootstrap_release.py')]:
            session = session.replace('${' + key + '}', base64.b64encode((ROOT / 'scripts' / name).read_bytes()).decode())
        self.assertNotIn('${', session)
        self.assertLessEqual(len(gzip.compress((bootstrap + session).encode())), 16384)

    def test_fixed_authored_bundle_is_deterministic_and_identical_retry_is_safe(self):
        first, digest = release.build_release(ROOT)
        self.assertEqual((first, digest), release.build_release(ROOT))
        self.assertEqual(release.release_key(digest), f'bootstrap/releases/{digest}/runtime.tar.gz')
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            parent.chmod(0o755)
            installed = release.stage_release(first, digest, parent, require_root=False)
            self.assertEqual(installed, release.stage_release(first, digest, parent, require_root=False))
            self.assertEqual((installed / 'runtime/content.json').read_bytes(), (ROOT / 'runtime/content.json').read_bytes())
            self.assertFalse((installed / 'operator-config').exists())

    def test_bad_digest_and_hostile_members_never_create_release(self):
        payload, digest = release.build_release(ROOT)
        cases = [(payload, '0' * 64)]
        for name, kind in (('../outside', tarfile.REGTYPE), ('runtime/content.json', tarfile.SYMTYPE)):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside'
                archive.addfile(member)
            hostile = stream.getvalue()
            cases.append((hostile, hashlib.sha256(hostile).hexdigest()))
        for content, sha in cases:
            with tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary)
                with self.assertRaises(release.ReleaseError):
                    release.stage_release(content, sha, parent, require_root=False)
                self.assertEqual(list(parent.iterdir()), [])

    def test_existing_release_rejects_writable_nested_directory(self):
        payload, digest = release.build_release(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            installed = release.stage_release(payload, digest, parent, require_root=False)
            (installed / 'scripts').chmod(0o777)
            with self.assertRaises(release.ReleaseError):
                release.stage_release(payload, digest, parent, require_root=False)

    def test_existing_release_drift_and_symlink_refused(self):
        payload, digest = release.build_release(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            installed = release.stage_release(payload, digest, parent, require_root=False)
            target = installed / 'runtime/content.json'
            target.unlink()
            target.symlink_to(ROOT / 'runtime/content.json')
            with self.assertRaises(release.ReleaseError):
                release.stage_release(payload, digest, parent, require_root=False)
