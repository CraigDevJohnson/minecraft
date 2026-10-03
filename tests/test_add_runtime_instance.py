from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import add_runtime_instance as installer
from minecraft_instance import OperationError, operation_lock


class AddRuntimeInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.install = self.root / 'opt/minecraft'
        self.launchers = self.root / 'usr/local/sbin'
        self.private = self.root / 'etc/minecraft'
        self.retained = self.root / 'srv/minecraft'
        self.run = self.root / 'run'
        for path in (self.install / 'scripts', self.install / 'runtime', self.launchers, self.private, self.run):
            path.mkdir(parents=True, mode=0o700)
        for name in installer.MODULES:
            (self.install / 'scripts' / name).write_bytes(b'prior reviewed controller: ' + name.encode())
        (self.install / 'runtime/survival.json').write_bytes((ROOT / 'runtime/survival.json').read_bytes())
        (self.install / 'runtime/content.json').write_bytes((ROOT / 'runtime/content-raknet.json').read_bytes())
        for name in installer.LAUNCHERS:
            path = self.launchers / name
            path.write_text('#!/bin/sh\necho prior-reviewed-launcher\n')
            path.chmod(0o755)
        for relative in ('instances/survival/data/world/level.dat', 'instances/survival/runtime/compose.json'):
            path = self.retained / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'existing running survival checkpoint')
        self.deployment = self.private / 'deployment.json'
        self.operator = self.private / 'operator.json'
        self.candidate = self.private / 'candidate.json'
        secret = self.private / 'rcon.secret'
        secret.write_text('PRIVATE_FIXTURE_RCON_VALUE')
        secret.chmod(0o600)
        self.old = {'schema_version': 1, 'host_id': 'fixture-host', 'data_volume_id': 'fixture-volume',
                    'data_filesystem_uuid': 'fixture-filesystem', 'backup_bucket_name': 'PRIVATE_FIXTURE_BUCKET',
                    'mount_path': '/srv/minecraft', 'instances': {'survival': {
                        'data_path': '/srv/minecraft/instances/survival/data', 'backup_prefix': 'instances/survival/'}}}
        self.config = {'deployment': str(self.deployment), 'eula_accepted': True,
                       'definitions': {'survival': str(self.install / 'runtime/survival.json')},
                       'rcon_password_file': str(self.private / 'rcon.secret'),
                       'backup_hook': [str(self.launchers / 'minecraft-backup'), 'hook'], 'warning_seconds': 17}
        self.new = copy.deepcopy(self.old)
        self.new['instances']['creative'] = {'data_path': '/srv/minecraft/instances/creative/data', 'backup_prefix': 'instances/creative/'}
        for path, value in ((self.deployment, self.old), (self.operator, self.config), (self.candidate, self.new)):
            path.write_bytes(installer.encoded(value))
            path.chmod(0o600)
        # Only the host OS ancestry is replaced; managed fixture metadata remains real.
        platform_ancestors = set(self.root.parents) | set(ROOT.parents)
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            if path in platform_ancestors:
                return SimpleNamespace(st_uid=os.geteuid(), st_mode=stat.S_IFDIR | 0o755)
            return original(path, *args, **kwargs)
        patch = mock.patch.object(Path, 'lstat', metadata)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(installer.LinuxMachine, 'verify_storage')
        self.storage = patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(installer.LinuxMachine, 'active_game_ports', return_value=frozenset({('tcp', 25565), ('udp', 19132)}))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(installer, 'command', return_value='')
        self.command = patch.start(); self.addCleanup(patch.stop)

    def add(self):
        return installer.add_instance(ROOT / 'runtime/creative.json', ROOT / 'runtime/content-raknet.json',
                                      self.candidate, self.operator, source_root=ROOT, install=self.install,
                                      launchers=self.launchers, run_parent=self.run, retained=self.retained,
                                      require_root=False)

    def preserved(self):
        files = [*self.install.joinpath('scripts').iterdir(), self.install / 'runtime/survival.json',
                 self.install / 'runtime/content.json', *self.retained.rglob('*')]
        return {str(path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in files if path.is_file()}

    def controls(self):
        return {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in (self.operator, self.deployment, *(self.launchers / name for name in installer.LAUNCHERS))}

    def test_addition_preserves_running_survival_and_private_fields_then_reuses_identical_generation(self):
        before = self.preserved()
        result = self.add()
        self.assertEqual(result['status'], 'configured')
        self.assertEqual(before, self.preserved())
        self.assertFalse((self.retained / 'instances/creative').exists())
        self.assertEqual(json.loads(self.deployment.read_text()), self.new)
        merged = json.loads(self.operator.read_text())
        self.assertEqual({key: value for key, value in merged.items() if key != 'definitions'},
                         {key: value for key, value in self.config.items() if key != 'definitions'})
        self.assertEqual(merged['definitions'], {**self.config['definitions'], 'creative': str(self.install / 'runtime/creative.json')})
        generation = self.install / 'releases' / result['generation']
        for name in installer.MODULES:
            self.assertEqual((generation / 'scripts' / name).read_bytes(), (ROOT / 'scripts' / name).read_bytes())
        for name in installer.LAUNCHERS:
            self.assertIn(str(generation / 'scripts' / (name.replace('-', '_') + '.py')), (self.launchers / name).read_text())
            child = subprocess.run([sys.executable, '-B', str(generation / 'scripts' / (name.replace('-', '_') + '.py')), '--help'],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
        self.command.assert_called_once_with(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=minecraft-creative'])
        controls = self.controls()
        retry = self.add()
        self.assertEqual(retry['status'], 'already-configured')
        self.assertEqual(result['generation'], retry['generation'])
        self.assertEqual(controls, self.controls())
        self.assertEqual(before, self.preserved())

    def test_existing_source_identity_or_catalog_changes_refuse_before_publication(self):
        for mutate in ('source', 'existing', 'extra'):
            candidate = copy.deepcopy(self.new)
            if mutate == 'source': candidate['data_volume_id'] = 'different-volume'
            elif mutate == 'existing': candidate['instances']['survival']['backup_prefix'] = 'wrong/'
            else: candidate['instances']['third'] = {'data_path': 'wrong', 'backup_prefix': 'wrong'}
            self.candidate.write_bytes(installer.encoded(candidate))
            before = self.controls()
            with self.subTest(mutate=mutate), self.assertRaises(OperationError): self.add()
            self.assertEqual(before, self.controls())
            self.assertFalse((self.install / 'runtime/creative.json').exists())
            self.assertFalse((self.install / 'releases').exists())

    def test_private_map_or_launcher_publication_failure_restores_prior_bytes_and_modes(self):
        for target in (self.operator, self.launchers / 'minecraft-backup'):
            before, survival = self.controls(), self.preserved()
            original = installer.atomic_write
            failed = False
            def publish(path, data, mode):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    raise OSError('controlled publication failure')
                return original(path, data, mode)
            with self.subTest(target=target.name), mock.patch.object(installer, 'atomic_write', side_effect=publish), self.assertRaisesRegex(OperationError, 'restored'):
                self.add()
            self.assertEqual(before, self.controls())
            self.assertEqual(survival, self.preserved())
            self.assertFalse((self.install / 'runtime/creative.json').exists())
            self.assertFalse((self.install / 'runtime/content-raknet.json').exists())
            self.assertFalse((self.retained / 'instances/creative').exists())

    def test_generation_directory_sync_failure_refuses_shared_control_publication(self):
        before, survival = self.controls(), self.preserved()
        original = installer.sync_directory
        def synchronize(path):
            if path == self.install / 'releases':
                raise OSError('controlled generation durability failure')
            return original(path)
        with mock.patch.object(installer, 'sync_directory', side_effect=synchronize), self.assertRaises(OSError):
            self.add()
        self.assertEqual(before, self.controls())
        self.assertEqual(survival, self.preserved())
        self.assertFalse((self.install / 'runtime/creative.json').exists())
        self.assertFalse((self.retained / 'instances/creative').exists())
        self.assertEqual(self.add()['status'], 'configured')

    def test_interrupted_release_parent_creation_is_synced_again_before_retry_publication(self):
        original_sync, original_write = installer.sync_directory, installer.atomic_write
        synchronized = False
        fail = True
        def synchronize(path):
            nonlocal fail, synchronized
            if path == self.install:
                if fail:
                    fail = False
                    raise OSError('controlled release parent sync failure')
                synchronized = True
            return original_sync(path)
        def publish(path, content, mode):
            if path.parent == self.launchers and not synchronized:
                raise AssertionError('generation parent was not durably established on retry')
            return original_write(path, content, mode)
        before = self.controls()
        with mock.patch.object(installer, 'sync_directory', side_effect=synchronize), mock.patch.object(installer, 'atomic_write', side_effect=publish):
            with self.assertRaises(OSError): self.add()
            self.assertEqual(before, self.controls())
            self.assertEqual(self.add()['status'], 'configured')
        self.assertTrue(synchronized)

    def test_symlink_writable_catalog_and_occupied_new_namespace_refuse_without_changing_controls(self):
        path = self.install / 'runtime/creative.json'
        path.symlink_to(self.install / 'runtime/survival.json')
        before = self.controls()
        with self.assertRaises(OperationError): self.add()
        self.assertEqual(before, self.controls()); path.unlink()
        self.install.joinpath('runtime').chmod(0o777)
        with self.assertRaises(OperationError): self.add()
        self.assertEqual(before, self.controls()); self.install.joinpath('runtime').chmod(0o700)
        target = self.retained / 'instances/creative'
        target.mkdir(); (target / 'untracked').write_bytes(b'preserve untracked data')
        with self.assertRaises(OperationError): self.add()
        self.assertEqual((target / 'untracked').read_bytes(), b'preserve untracked data')
        self.assertEqual(before, self.controls())

    def test_shared_lock_contention_refuses_before_any_catalog_change(self):
        directory = installer.ensure_runtime_lock_directory(self.run)
        before = self.controls()
        original = operation_lock
        with original(directory / 'runtime.lock'):
            with mock.patch.object(installer, 'operation_lock', side_effect=lambda path: original(path, timeout=0.01)):
                with self.assertRaisesRegex(OperationError, 'another runtime operation'): self.add()
        self.assertEqual(before, self.controls())

    def test_s3_only_lifecycle_cannot_use_the_retained_host_installation_contract(self):
        current = {**self.old, 'lifecycle_mode': 's3-only'}
        self.deployment.write_bytes(installer.encoded(current))
        before = self.controls()
        with self.assertRaisesRegex(OperationError, 'retained host'): self.add()
        self.assertEqual(before, self.controls())


if __name__ == '__main__':
    unittest.main()
