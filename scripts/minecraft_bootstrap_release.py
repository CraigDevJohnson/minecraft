#!/usr/bin/env python3
"""Package and stage the exact public runtime files under an immutable SHA-256 key."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path


SCRIPTS = ('minecraft_instance.py', 'minecraft_content.py', 'minecraft_access.py', 'minecraft_backup.py',
           'minecraft_backup_store.py', 'minecraft_restore.py', 'check_runtime_install.py',
           'install_runtime.sh', 'install_aws_cli.sh', 'minecraft_private_transfer.py', 'minecraft_retention.py',
           'add_runtime_instance.py')
RUNTIME = ('survival.json', 'content.json', 'creative.json', 'content-raknet.json', 'operator.json.example', 'minecraft@.service')
MEMBERS = frozenset([f'scripts/{name}' for name in SCRIPTS] + [f'runtime/{name}' for name in RUNTIME])
MAX_RELEASE = 8 << 20
SHA256 = re.compile('[0-9a-f]{64}')


class ReleaseError(Exception):
    """Payload-independent bootstrap failure."""


def build_release(root: Path) -> tuple[bytes, str]:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for name in sorted(MEMBERS):
                source = root / name
                if source.is_symlink() or not source.is_file():
                    raise ReleaseError('bootstrap release requires every fixed authored file')
                content = source.read_bytes()
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o755 if name.endswith('.sh') else 0o644
                archive.addfile(member, io.BytesIO(content))
    payload = output.getvalue()
    if len(payload) > MAX_RELEASE:
        raise ReleaseError('bootstrap release exceeds its size limit')
    return payload, hashlib.sha256(payload).hexdigest()


def release_key(digest: str) -> str:
    if not SHA256.fullmatch(digest):
        raise ReleaseError('bootstrap SHA-256 is invalid')
    return f'bootstrap/releases/{digest}/runtime.tar.gz'


def stage_release(payload: bytes, digest: str, destination: Path, *, require_root=True) -> Path:
    if not SHA256.fullmatch(digest) or len(payload) > MAX_RELEASE or hashlib.sha256(payload).hexdigest() != digest:
        raise ReleaseError('bootstrap release digest or size mismatch')
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            for member in archive:
                if not member.isreg() or member.name not in MEMBERS or member.name in files or member.size > MAX_RELEASE:
                    raise ReleaseError('bootstrap release contains an unexpected member')
                stream = archive.extractfile(member)
                if stream is None:
                    raise ReleaseError('bootstrap release member is unreadable')
                files[member.name] = stream.read(MAX_RELEASE + 1)
            if set(files) != MEMBERS or sum(map(len, files.values())) > MAX_RELEASE:
                raise ReleaseError('bootstrap release has an invalid file inventory')
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ReleaseError('bootstrap release archive is invalid') from exc
    owner = 0 if require_root else os.geteuid()
    try:
        if os.geteuid() != owner or destination.is_symlink() or not destination.is_dir():
            raise ReleaseError('bootstrap destination is not a trusted directory')
        metadata = destination.stat()
        if metadata.st_uid != owner or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ReleaseError('bootstrap destination is writable by another account')
        target = destination / digest
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise ReleaseError('bootstrap release destination is occupied')
            for path in [target, *target.rglob('*')]:
                metadata = path.lstat()
                if (not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
                        or metadata.st_uid != owner or stat.S_IMODE(metadata.st_mode) & 0o022):
                    raise ReleaseError('existing bootstrap release has an untrusted path')
            existing = {str(path.relative_to(target)): path for path in target.rglob('*') if not path.is_dir()}
            if set(existing) != MEMBERS:
                raise ReleaseError('existing bootstrap release differs')
            for name, path in existing.items():
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner or stat.S_IMODE(metadata.st_mode) & 0o022 or path.read_bytes() != files[name]:
                    raise ReleaseError('existing bootstrap release differs or is untrusted')
            return target
        with tempfile.TemporaryDirectory(prefix='.release-', dir=destination) as temporary:
            staging = Path(temporary) / 'release'
            staging.mkdir(mode=0o755)
            for name, content in files.items():
                path = staging / name
                path.parent.mkdir(mode=0o755, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o755 if name.endswith('.sh') else 0o644)
            os.rename(staging, target)
        return target
    except OSError as exc:
        raise ReleaseError('bootstrap release could not be staged') from exc



def verify_installed_release(release: Path, installed=Path('/opt/minecraft'), *, launcher_dir=Path('/usr/local/sbin'), service_dir=Path('/etc/systemd/system'), require_root=True) -> str:
    """Attest actual installed public code/config before binding a recovery receipt."""
    if not installed.exists():
        return 'missing'
    owner = 0 if require_root else os.geteuid()
    scripts = ('minecraft_instance.py', 'minecraft_content.py', 'minecraft_access.py', 'minecraft_backup.py', 'minecraft_backup_store.py', 'minecraft_restore.py')
    expected = {f'scripts/{name}': (release / 'scripts' / name).read_bytes() for name in scripts}
    expected.update({f'runtime/{name}': (release / 'runtime' / name).read_bytes() for name in RUNTIME})
    for name in ('minecraft-instance', 'minecraft-backup', 'minecraft-restore'):
        expected['bin/' + name] = ('#!/usr/bin/env bash\nset -euo pipefail\nexec /usr/bin/python3 -B /opt/minecraft/scripts/'
                                  + name.replace('-', '_') + '.py "$@"\n').encode()
    try:
        for path in [installed, *installed.rglob('*')]:
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_uid != owner or stat.S_IMODE(info.st_mode) & 0o022:
                raise ReleaseError('installed runtime contains an untrusted path')
        actual = {str(path.relative_to(installed)): path for path in installed.rglob('*') if not path.is_dir()}
        if set(actual) != set(expected) or any(actual[name].read_bytes() != content for name, content in expected.items()):
            raise ReleaseError('installed runtime differs from the pinned bootstrap release')
        for name in ('minecraft-instance', 'minecraft-backup', 'minecraft-restore'):
            path = launcher_dir / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) & 0o022 or path.read_bytes() != expected['bin/' + name]:
                raise ReleaseError('installed runtime launcher differs from the pinned release')
        service = service_dir / 'minecraft@.service'
        info = service.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) & 0o022 or service.read_bytes() != expected['runtime/minecraft@.service']:
            raise ReleaseError('installed runtime service differs from the pinned release')
        return 'installed'
    except OSError as exc:
        raise ReleaseError('cannot attest installed runtime against its pinned release') from exc

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    package = commands.add_parser('package')
    package.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    package.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--sha256', required=True)
    stage = commands.add_parser('stage')
    stage.add_argument('--bucket', required=True)
    stage.add_argument('--sha256', required=True)
    stage.add_argument('--region', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'package':
            payload, digest = build_release(args.root)
            args.output.write_bytes(payload)
            print(json.dumps({'sha256': digest, 'key': release_key(digest)}))
        elif args.action == 'verify':
            release_key(args.sha256)
            parent = Path('/opt/minecraft-bootstrap')
            staged = parent / args.sha256
            payload, actual = build_release(staged)
            if actual != args.sha256:
                raise ReleaseError('staged bootstrap release does not match its pinned digest')
            stage_release(payload, args.sha256, parent)
            print(json.dumps({'runtime': verify_installed_release(staged), 'bootstrap_sha256': args.sha256}))
        else:
            key = release_key(args.sha256)
            if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', args.region):
                raise ReleaseError('bootstrap region is invalid')
            if not re.fullmatch('[a-z][a-z0-9-]{1,61}[a-z0-9]', args.bucket):
                raise ReleaseError('bootstrap bucket is invalid')
            with tempfile.TemporaryDirectory(prefix='minecraft-bootstrap-') as temporary:
                archive = Path(temporary) / 'runtime.tar.gz'
                result = subprocess.run(['aws', '--region', args.region, 's3api', 'get-object', '--bucket', args.bucket, '--key', key,
                                         '--no-cli-pager', '--no-cli-auto-prompt', str(archive)], capture_output=True, timeout=120)
                if result.returncode:
                    raise ReleaseError('bootstrap release download failed')
                parent = Path('/opt/minecraft-bootstrap')
                if not parent.exists():
                    parent.mkdir(mode=0o755)
                stage_release(archive.read_bytes(), args.sha256, parent)
            print('{"bootstrap_release":"staged"}')
        return 0
    except (ReleaseError, OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, ReleaseError) else 'bootstrap command failed'
        print(json.dumps({'error': reason}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
