from __future__ import annotations

import base64
import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import minecraft_bootstrap_release as release


class BootstrapReleaseTests(unittest.TestCase):
    def test_packaged_cli_contract_matches_controller_options_and_source_bytes(self):
        payload, digest = release.build_release(ROOT)
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            for name in release.MEMBERS:
                self.assertEqual(archive.extractfile(name).read(), (ROOT / name).read_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            staged = release.stage_release(payload, digest, Path(temporary), require_root=False)
            commands = (
                (staged / 'scripts/add_runtime_instance.py', [], ('--definition', '--manifest', '--deployment', '--operator')),
                (staged / 'scripts/minecraft_restore.py', [], ('--receipt', '--bootstrap-sha256', '--resume', '--timeout')),
                (staged / 'scripts/minecraft_private_transfer.py', ['verify-inputs'], ('--sha256',)),
                (ROOT / 'scripts/minecraft_bootstrap_release.py', ['stage'], ('--region', '--bucket', '--sha256')),
                (ROOT / 'scripts/minecraft_bootstrap_release.py', ['verify'], ('--sha256',)),
            )
            for script, arguments, options in commands:
                with self.subTest(script=script.name, arguments=arguments):
                    result = subprocess.run([sys.executable, str(script), *arguments, '--help'],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for option in options:
                        self.assertIn(option, result.stdout)

    def test_installed_runtime_attestation_rejects_drift_and_untrusted_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            installed=root/'installed'; launchers=root/'launchers'; services=root/'services'
            self.assertEqual(release.verify_installed_release(ROOT,installed,launcher_dir=launchers,service_dir=services,require_root=False),'missing')
            for folder in ('scripts','runtime','bin'):(installed/folder).mkdir(parents=True,mode=0o755)
            launchers.mkdir(); services.mkdir()
            for name in ('minecraft_instance.py','minecraft_content.py','minecraft_access.py','minecraft_backup.py','minecraft_backup_store.py','minecraft_restore.py','minecraft_retention.py'):
                shutil.copy2(ROOT/'scripts'/name,installed/'scripts'/name)
            for name in release.RUNTIME:shutil.copy2(ROOT/'runtime'/name,installed/'runtime'/name)
            for name in ('minecraft-instance','minecraft-backup','minecraft-restore'):
                content=('#!/usr/bin/env bash\nset -euo pipefail\nexec /usr/bin/python3 -B /opt/minecraft/scripts/'+name.replace('-','_')+'.py "$@"\n')
                for target in (installed/'bin'/name,launchers/name):target.write_text(content); target.chmod(0o755)
            shutil.copy2(ROOT/'runtime/minecraft@.service',services/'minecraft@.service')
            self.assertEqual(release.verify_installed_release(ROOT,installed,launcher_dir=launchers,service_dir=services,require_root=False),'installed')
            (installed/'scripts/minecraft_backup.py').write_text('unreviewed installed release')
            with self.assertRaises(release.ReleaseError):release.verify_installed_release(ROOT,installed,launcher_dir=launchers,service_dir=services,require_root=False)

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
