import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import minecraft_on_demand as lifecycle
from minecraft_host import HostError


class WorkstationIdentityTests(unittest.TestCase):
    def test_mac_uuid_is_private_normalized_and_independent_of_hostname(self):
        identifier = '12345678-1234-4234-8234-123456789abc'
        expected = 'device-sha256:' + hashlib.sha256(
            ('minecraft-workstation-v1\0darwin\0' + identifier.replace('-', '')).encode()).hexdigest()
        with mock.patch.object(lifecycle.sys, 'platform', 'darwin'), \
                mock.patch.object(lifecycle.subprocess, 'run', return_value=SimpleNamespace(
                    stdout='"IOPlatformUUID" = "' + identifier.upper() + '"')) as run, \
                mock.patch.object(lifecycle.socket, 'gethostname', side_effect=['old-name', 'new-name']) as hostname:
            self.assertEqual(lifecycle.workstation_identity(), expected)
            self.assertEqual(lifecycle.workstation_identity(), expected)
            hostname.assert_not_called()
        self.assertEqual(run.call_args.args[0], ['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'])
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
        self.assertNotIn(identifier, expected)

    def test_other_mac_uuid_has_a_different_device_identity(self):
        values = ['"IOPlatformUUID" = "12345678-1234-4234-8234-123456789abc"',
                  '"IOPlatformUUID" = "22345678-1234-4234-8234-123456789abc"']
        with mock.patch.object(lifecycle.sys, 'platform', 'darwin'), \
                mock.patch.object(lifecycle.subprocess, 'run', side_effect=[SimpleNamespace(stdout=value) for value in values]):
            self.assertNotEqual(lifecycle.workstation_identity(), lifecycle.workstation_identity())

    def test_mac_unavailable_identity_fails_closed(self):
        with mock.patch.object(lifecycle.sys, 'platform', 'darwin'), \
                mock.patch.object(lifecycle.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ioreg', 5)):
            with self.assertRaisesRegex(HostError, 'cannot establish stable'): lifecycle.workstation_identity()

    def test_mac_missing_malformed_duplicate_and_zero_identity_are_refused(self):
        for output in ('', '"IOPlatformUUID" = "invalid-private-hardware-id"',
                       '"IOPlatformUUID" = "00000000-0000-0000-0000-000000000000"',
                       '"IOPlatformUUID" = "12345678-1234-4234-8234-123456789abc"\n' * 2):
            with self.subTest(output_kind=len(output)), mock.patch.object(lifecycle.sys, 'platform', 'darwin'), \
                    mock.patch.object(lifecycle.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
                with self.assertRaises(HostError) as error: lifecycle.workstation_identity()
                self.assertNotIn('invalid-private-hardware-id', str(error.exception))

    def linux_identity(self, value, *, mode=stat.S_IFREG | 0o444, owner=0):
        with tempfile.TemporaryFile() as file:
            file.write(value);file.seek(0)
            descriptor = os.dup(file.fileno())
            with mock.patch.object(lifecycle.sys, 'platform', 'linux'), \
                    mock.patch.object(lifecycle.os, 'open', return_value=descriptor) as opened, \
                    mock.patch.object(lifecycle.os, 'fstat', return_value=SimpleNamespace(st_mode=mode, st_uid=owner)):
                value = lifecycle.workstation_identity()
                opened.assert_called_once_with('/etc/machine-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                return value

    def test_linux_uses_stable_trusted_machine_id(self):
        identifier = '12345678123442348234123456789abc'
        expected = 'device-sha256:' + hashlib.sha256(
            ('minecraft-workstation-v1\0linux\0' + identifier).encode()).hexdigest()
        self.assertEqual(self.linux_identity((identifier + '\n').encode()), expected)

    def test_linux_untrusted_malformed_and_zero_identity_are_refused(self):
        for value, mode, owner in [(b'0' * 32, stat.S_IFREG | 0o444, 0),
                                   (b'not-a-machine-id', stat.S_IFREG | 0o444, 0),
                                   (b'1' * 32 + b' ' * 33 + b'malformed-tail', stat.S_IFREG | 0o444, 0),
                                   (b'1' * 32, stat.S_IFREG | 0o666, 0),
                                   (b'1' * 32, stat.S_IFREG | 0o444, 501),
                                   (b'1' * 32, stat.S_IFIFO | 0o444, 0)]:
            with self.subTest(mode=mode, owner=owner), self.assertRaises(HostError):
                self.linux_identity(value, mode=mode, owner=owner)

    def test_unsupported_platform_and_unavailable_linux_id_fail_closed(self):
        with mock.patch.object(lifecycle.sys, 'platform', 'unsupported'):
            with self.assertRaises(HostError): lifecycle.workstation_identity()
        with mock.patch.object(lifecycle.sys, 'platform', 'linux'), \
                mock.patch.object(lifecycle.os, 'open', side_effect=FileNotFoundError()):
            with self.assertRaises(HostError): lifecycle.workstation_identity()
