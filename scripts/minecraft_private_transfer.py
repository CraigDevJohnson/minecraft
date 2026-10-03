#!/usr/bin/env python3
"""Digest-bound private file transfer over an SSM loopback tunnel; never logs payloads."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path


FILES = frozenset(('deployment.json', 'operator.json', 'rcon.secret'))
OPTIONAL_FILES = frozenset(('recovery-receipt.json',))
INPUT_CERTIFICATE = 'private-inputs.sha256'
MAX_FILE = 1 << 20
MAX_BUNDLE = 3 * MAX_FILE
SHA256 = re.compile(r'[0-9a-f]{64}')


class TransferError(Exception):
    """A fixed diagnostic containing no payload bytes."""


def private_file(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise TransferError('private input must be an owner-only regular file')
            data = stream.read(MAX_FILE + 1)
        if not data or len(data) > MAX_FILE:
            raise TransferError('private input exceeds its size limit')
        return data
    except OSError as exc:
        raise TransferError('cannot read trusted private input') from exc


def build_bundle(deployment: dict, operator_path: Path, secret_path: Path, *, receipt=None) -> tuple[bytes, str]:
    try:
        files = {'deployment.json': json.dumps(deployment, sort_keys=True),
                 'operator.json': private_file(operator_path).decode('utf-8'),
                 'rcon.secret': private_file(secret_path).decode('ascii')}
        if receipt is not None:
            files['recovery-receipt.json'] = json.dumps(receipt, sort_keys=True)
        bundle = json.dumps({'schema_version': 1, 'files': files}, sort_keys=True, separators=(',', ':')).encode()
    except (ValueError, UnicodeError, TypeError) as exc:
        raise TransferError('private input encoding is invalid') from exc
    validate_bundle(bundle, hashlib.sha256(bundle).hexdigest())
    return bundle, hashlib.sha256(bundle).hexdigest()


def validate_bundle(bundle: bytes, expected_digest: str) -> dict[str, bytes]:
    if not SHA256.fullmatch(expected_digest) or len(bundle) > MAX_BUNDLE or hashlib.sha256(bundle).hexdigest() != expected_digest:
        raise TransferError('private bundle digest or size does not match')
    try:
        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError
                result[name] = value
            return result
        value = json.loads(bundle, object_pairs_hook=unique)
        if set(value) != {'schema_version', 'files'} or value['schema_version'] != 1 or not FILES <= set(value['files']) <= FILES | OPTIONAL_FILES:
            raise ValueError
        files = {name: content.encode('utf-8') for name, content in value['files'].items()}
        if any(not content or len(content) > MAX_FILE for content in files.values()):
            raise ValueError
        if not isinstance(json.loads(files['deployment.json']), dict) or not isinstance(json.loads(files['operator.json']), dict):
            raise ValueError
        if 'recovery-receipt.json' in files and not isinstance(json.loads(files['recovery-receipt.json']), dict):
            raise ValueError
        if not re.fullmatch(rb'[A-Za-z0-9_-]{16,128}\n?', files['rcon.secret']):
            raise ValueError
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise TransferError('private bundle has invalid files or schema') from exc
    return files


def receive_bundle(bundle: bytes, expected_digest: str, destination: Path, *, require_root=True) -> None:
    files = validate_bundle(bundle, expected_digest)
    inputs = hashlib.sha256(files['operator.json'] + b'\0' + files['rcon.secret']).hexdigest()
    files[INPUT_CERTIFICATE] = (inputs + '\n').encode()
    expected_owner = 0 if require_root else os.geteuid()
    if os.geteuid() != expected_owner:
        raise TransferError('private receiver requires the intended owner')
    try:
        metadata = destination.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != expected_owner or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise TransferError('private destination must be an owner-only trusted directory')
        for name in files:
            path = destination / name
            if path.exists() or path.is_symlink():
                current = path.lstat()
                if not stat.S_ISREG(current.st_mode) or current.st_uid != expected_owner or stat.S_IMODE(current.st_mode) != 0o600:
                    raise TransferError('private destination contains an untrusted file')
        # All input and destination checks precede publication. Runtime remains stopped
        # until the caller confirms all three files and completes installation.
        with tempfile.TemporaryDirectory(prefix='.transfer-', dir=destination) as temporary:
            staging = Path(temporary)
            for name, content in files.items():
                descriptor = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            for name in sorted(files):
                os.replace(staging / name, destination / name)
            descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise TransferError('private file publication failed; retry the same bundle before starting') from exc


def verify_installed_inputs(expected_digest: str, destination=Path('/etc/minecraft'), *, require_root=True) -> None:
    expected_owner = 0 if require_root else os.geteuid()
    if not SHA256.fullmatch(expected_digest) or os.geteuid() != expected_owner:
        raise TransferError('private input verification requires the intended owner and digest')
    try:
        info = destination.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) != 0o700:
            raise TransferError('installed private directory is untrusted')
        actual = hashlib.sha256(private_file(destination / 'operator.json') + b'\0' + private_file(destination / 'rcon.secret')).hexdigest()
        certificate = private_file(destination / INPUT_CERTIFICATE)
        if actual != expected_digest or certificate != (expected_digest + '\n').encode():
            raise TransferError('installed private inputs differ from the reviewed policy; explicit reconfiguration required')
    except OSError as exc:
        raise TransferError('cannot verify installed private inputs') from exc


def send_bundle(port: int, bundle: bytes) -> None:
    if type(port) is not int or not 1024 <= port <= 65535 or len(bundle) > MAX_BUNDLE:
        raise TransferError('invalid local transfer request')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('POST', '/private-bundle', body=bundle, headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        if response.status != 204 or response.read(1):
            raise TransferError('private receiver refused the bundle')
    except (OSError, http.client.HTTPException) as exc:
        raise TransferError('cannot reach the private loopback receiver') from exc
    finally:
        connection.close()


def serve(expected_digest: str, port: int, timeout: int, destination=Path('/etc/minecraft'), *, require_root=True) -> None:
    if not SHA256.fullmatch(expected_digest) or not 1024 <= port <= 65535 or not 1 <= timeout <= 300:
        raise TransferError('invalid receiver bounds')
    completed = False
    deadline = time.monotonic() + timeout

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal completed
            self.connection.settimeout(min(10, max(0.01, deadline - time.monotonic())))
            try:
                length = self.headers.get('Content-Length', '')
                if self.path != '/private-bundle' or not length.isdecimal() or not 0 < int(length) <= MAX_BUNDLE or self.headers.get('Transfer-Encoding'):
                    raise TransferError('invalid bounded private request')
                content = self.rfile.read(int(length))
                if len(content) != int(length):
                    raise TransferError('incomplete private request')
                receive_bundle(content, expected_digest, destination, require_root=require_root)
                completed = True
                self.send_response(204)
            except (TransferError, OSError):
                self.send_response(400)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *arguments):
            pass

    try:
        with http.server.HTTPServer(('127.0.0.1', port), Handler) as server:
            while not completed and time.monotonic() < deadline:
                server.timeout = min(1, max(0.01, deadline - time.monotonic()))
                server.handle_request()
        if not completed:
            raise TransferError('private receiver expired without successful publication')
    except OSError as exc:
        raise TransferError('private receiver could not bind its loopback port') from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['receive', 'verify-inputs'])
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--port', type=int, default=19140)
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args(argv)
    try:
        if args.action == 'verify-inputs':
            verify_installed_inputs(args.sha256)
            print('{"private_inputs":"verified"}')
        else:
            serve(args.sha256, args.port, args.timeout)
            print('{"private_files":"installed"}')
        return 0
    except TransferError as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
