from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import minecraft_private_transfer as transfer


class PrivateTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.operator = self.root / 'operator-source.json'
        self.operator.write_text('{"deployment":"/etc/minecraft/deployment.json"}')
        self.operator.chmod(0o600)
        self.secret = self.root / 'secret-source'
        self.secret.write_text('PRIVATE_SECRET_BYTES_NEVER_PRINT')
        self.secret.chmod(0o600)
        self.destination = self.root / 'installed'
        self.destination.mkdir(mode=0o700)
        self.bundle, self.digest = transfer.build_bundle({'host_id': 'private-host'}, self.operator, self.secret)

    def test_digest_bound_publication_preserves_owner_only_files_and_retry(self):
        for _ in range(2):
            transfer.receive_bundle(self.bundle, self.digest, self.destination, require_root=False)
        self.assertEqual((self.destination / 'rcon.secret').read_text(), self.secret.read_text())
        for name in transfer.FILES:
            self.assertEqual((self.destination / name).stat().st_mode & 0o777, 0o600)
        certificate = self.destination / transfer.INPUT_CERTIFICATE
        expected = hashlib.sha256(self.operator.read_bytes() + b'\0' + self.secret.read_bytes()).hexdigest()
        self.assertEqual(certificate.read_text(), expected + '\n')
        self.assertEqual(certificate.stat().st_mode & 0o777, 0o600)
        transfer.verify_installed_inputs(expected, self.destination, require_root=False)

    def test_installed_digest_requires_current_files_and_trusted_certificate(self):
        expected = hashlib.sha256(self.operator.read_bytes() + b'\0' + self.secret.read_bytes()).hexdigest()
        with self.assertRaises(transfer.TransferError):
            transfer.verify_installed_inputs(expected, self.destination, require_root=False)
        transfer.receive_bundle(self.bundle, self.digest, self.destination, require_root=False)
        for name in ('operator.json', 'rcon.secret', transfer.INPUT_CERTIFICATE):
            path = self.destination / name
            saved = path.read_bytes()
            path.write_bytes(b'changed installed input')
            with self.subTest(name=name), self.assertRaises(transfer.TransferError):
                transfer.verify_installed_inputs(expected, self.destination, require_root=False)
            path.write_bytes(saved)
        certificate = self.destination / transfer.INPUT_CERTIFICATE
        certificate.chmod(0o644)
        with self.assertRaises(transfer.TransferError):
            transfer.verify_installed_inputs(expected, self.destination, require_root=False)

    def test_new_verification_cli_option_is_available_to_the_actual_subprocess(self):
        result = subprocess.run([sys.executable, transfer.__file__, 'verify-inputs', '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verify-inputs', result.stdout)
        self.assertIn('--sha256', result.stdout)

    def test_invalid_digest_schema_inventory_and_payload_leave_destination_unchanged(self):
        cases = [(self.bundle, '0' * 64), (b'{}', hashlib.sha256(b'{}').hexdigest()),
                 (b'x' * (transfer.MAX_BUNDLE + 1), '0' * 64)]
        value = json.loads(self.bundle)
        for mutation in ('extra', 'missing', 'bad-secret', 'not-json', 'bom-json'):
            changed = json.loads(self.bundle)
            if mutation == 'extra': changed['files']['../attack'] = 'x'
            if mutation == 'missing': del changed['files']['operator.json']
            if mutation == 'bad-secret': changed['files']['rcon.secret'] = 'invalid secret'
            if mutation == 'not-json': changed['files']['operator.json'] = 'invalid'
            if mutation == 'bom-json': changed['files']['operator.json'] = '\ufeff{}'
            payload = json.dumps(changed).encode()
            cases.append((payload, hashlib.sha256(payload).hexdigest()))
        for payload, digest in cases:
            with self.subTest(digest=digest), self.assertRaises(transfer.TransferError):
                transfer.receive_bundle(payload, digest, self.destination, require_root=False)
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_untrusted_inputs_and_destinations_refused_before_any_write(self):
        self.secret.chmod(0o644)
        with self.assertRaises(transfer.TransferError):
            transfer.build_bundle({}, self.operator, self.secret)
        self.secret.chmod(0o600)
        (self.destination / 'rcon.secret').symlink_to(self.secret)
        with self.assertRaises(transfer.TransferError):
            transfer.receive_bundle(self.bundle, self.digest, self.destination, require_root=False)
        self.assertEqual(self.secret.read_text(), 'PRIVATE_SECRET_BYTES_NEVER_PRINT')
        self.assertFalse((self.destination / 'deployment.json').exists())

    def test_loopback_receiver_one_success_automatic_shutdown_and_no_payload_logs(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        errors = []
        def receiver():
            try:
                transfer.serve(self.digest, port, 5, self.destination, require_root=False)
            except Exception as exc:
                errors.append(exc)
        output = io.StringIO()
        with redirect_stdout(output):
            worker = threading.Thread(target=receiver)
            worker.start()
            for _ in range(50):
                try:
                    transfer.send_bundle(port, self.bundle)
                    break
                except transfer.TransferError:
                    threading.Event().wait(0.01)
            worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(output.getvalue(), '')
        with self.assertRaises(transfer.TransferError):
            transfer.send_bundle(port, self.bundle)

    def test_duplicate_json_fields_rejected(self):
        payload = self.bundle.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')
        with self.assertRaises(transfer.TransferError):
            transfer.receive_bundle(payload, hashlib.sha256(payload).hexdigest(), self.destination, require_root=False)
