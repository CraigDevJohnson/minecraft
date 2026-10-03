#!/usr/bin/env python3
"""Add one retained-host instance without stopping or replacing an existing world."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import stat
import tempfile

from minecraft_content import load_definition, load_manifest, published_game_ports
from minecraft_instance import (LinuxMachine, OperationError, command, ensure_runtime_lock_directory,
                                operation_lock, private_json)

MODULES = ('minecraft_instance.py', 'minecraft_content.py', 'minecraft_access.py',
           'minecraft_backup.py', 'minecraft_backup_store.py', 'minecraft_restore.py')
LAUNCHERS = ('minecraft-instance', 'minecraft-backup', 'minecraft-restore')


def trusted(path: Path, *, directory=False, private=False) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise OperationError('additive installation requires absolute managed paths')
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        expected_directory = item != path or directory
        if (not (stat.S_ISDIR(info.st_mode) if expected_directory else stat.S_ISREG(info.st_mode))
                or info.st_uid != os.geteuid() or info.st_mode & 0o022
                or (item == path and private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise OperationError('additive installation paths must be owned by root and never redirected or writable by others')


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def encoded(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def generation(source: Path, install: Path) -> Path:
    files = {}
    for name in MODULES:
        path = source / 'scripts' / name
        trusted(path)
        content = path.read_bytes()
        compile(content, name, 'exec')
        files[name] = content
    digest = hashlib.sha256(json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                                       sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    releases = install / 'releases'
    if not releases.exists() and not releases.is_symlink():
        releases.mkdir(mode=0o755)
    trusted(releases, directory=True)
    sync_directory(install)
    target = releases / digest
    if target.exists() or target.is_symlink():
        trusted(target, directory=True)
        trusted(target / 'scripts', directory=True)
        if {p.name for p in target.iterdir()} != {'scripts'} or {p.name for p in (target / 'scripts').iterdir()} != set(files):
            raise OperationError('existing additive generation has a different file inventory')
        for name, content in files.items():
            trusted(target / 'scripts' / name)
            if (target / 'scripts' / name).read_bytes() != content:
                raise OperationError('existing additive generation differs from its digest')
        sync_directory(target / 'scripts')
        sync_directory(target)
        sync_directory(releases)
        return target
    with tempfile.TemporaryDirectory(prefix='.add-instance-', dir=releases) as temporary:
        staged = Path(temporary) / 'release'
        (staged / 'scripts').mkdir(parents=True, mode=0o755)
        staged.chmod(0o755)
        for name, content in files.items():
            atomic_write(staged / 'scripts' / name, content, 0o644)
        sync_directory(staged)
        os.rename(staged, target)
        sync_directory(releases)
    return target


def add_instance(definition: Path, manifest: Path, deployment: Path, operator: Path, *,
                 source_root=Path(__file__).resolve().parents[1], install=Path('/opt/minecraft'),
                 launchers=Path('/usr/local/sbin'), run_parent=Path('/run'),
                 retained=Path('/srv/minecraft'), require_root=True) -> dict:
    if require_root and (os.geteuid() != 0 or platform.system() != 'Linux' or platform.machine() != 'aarch64'):
        raise OperationError('additive runtime installation requires root on the reviewed Linux ARM64 host')
    for path in (source_root, install, install / 'runtime', launchers):
        trusted(path, directory=True)
    for path in (definition, manifest):
        trusted(path)
    trusted(operator, private=True)
    trusted(deployment, private=True)
    with operation_lock(ensure_runtime_lock_directory(run_parent) / 'runtime.lock'):
        old_operator = private_json(operator)
        trusted(Path(old_operator['rcon_password_file']), private=True)
        deployed = Path(old_operator['deployment'])
        trusted(deployed, private=True)
        if deployed == deployment:
            raise OperationError('new deployment input must be separate from the existing private deployment')
        old = private_json(deployed)
        candidate = private_json(deployment)
        if old.get('lifecycle_mode', 'retained-host') != 'retained-host':
            raise OperationError('additive installation supports the retained host; pinned on-demand releases use their existing installer')
        if old_operator.get('eula_accepted') is not True:
            raise OperationError('existing private EULA acceptance is required before adding an instance')
        desired = load_definition(definition)
        content = load_manifest(manifest)
        name = desired['id']
        if definition.name != name + '.json' or desired['manifest'] != manifest.name:
            raise OperationError('new definition and manifest filenames must match the new instance catalog')
        public_definition = install / 'runtime' / definition.name
        public_manifest = install / 'runtime' / manifest.name
        if public_definition == public_manifest:
            raise OperationError('new definition and manifest must be separate files')
        existing = old['instances']
        definitions = old_operator['definitions']
        if (not isinstance(existing, dict) or not existing or not isinstance(definitions, dict)
                or set(existing) != set(definitions) or not isinstance(candidate.get('instances'), dict)):
            raise OperationError('existing deployment and operator catalogs must agree')
        already = name in existing
        expected_names = set(existing) if already else set(existing) | {name}
        if (set(candidate['instances']) != expected_names
                or {key: value for key, value in old.items() if key != 'instances'} != {key: value for key, value in candidate.items() if key != 'instances'}
                or any(candidate['instances'].get(key) != value for key, value in existing.items())
                or candidate['instances'][name] != {'data_path': f'/srv/minecraft/instances/{name}/data', 'backup_prefix': f'instances/{name}/'}):
            raise OperationError('new deployment may only add one independent data and backup namespace')
        for configured, filename in definitions.items():
            path = Path(filename)
            if path.parent != install / 'runtime':
                raise OperationError('existing desired definitions must remain in the managed runtime catalog')
            trusted(path)
            current_definition = load_definition(path)
            current_manifest = path.parent / current_definition['manifest']
            trusted(current_manifest)
            if configured != name and published_game_ports(current_definition, load_manifest(current_manifest)) & published_game_ports(desired, content):
                raise OperationError('new desired game ports overlap an existing instance')
        old_machine = LinuxMachine(old_operator)
        old_machine.root = retained
        old_machine.verify_storage()
        for configured in definitions:
            if configured != name and old_machine.active_game_ports(configured) & published_game_ports(desired, content):
                raise OperationError('new game ports overlap a running instance')
        targets = {public_definition: definition.read_bytes(), public_manifest: manifest.read_bytes()}
        for path, data in targets.items():
            if path.exists() or path.is_symlink():
                trusted(path)
                if path.read_bytes() != data:
                    raise OperationError('additive installation never replaces existing desired files')
        for name_cli in LAUNCHERS:
            trusted(launchers / name_cli)
        if not already:
            target = retained / 'instances' / name
            if target.exists() or target.is_symlink() or command(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project=minecraft-{name}']).strip():
                raise OperationError('new instance namespace already contains data or a container')
        merged = {**old_operator, 'definitions': {**definitions, name: str(public_definition)}}
        with tempfile.TemporaryDirectory(prefix='.add-instance-check-', dir=operator.parent) as temporary:
            staged = Path(temporary)
            atomic_write(staged / 'deployment.json', encoded(candidate), 0o600)
            for path, data in targets.items():
                atomic_write(staged / path.name, data, 0o644)
            LinuxMachine({**merged, 'deployment': str(staged / 'deployment.json'),
                          'definitions': {**definitions, name: str(staged / definition.name)}})
        release = generation(source_root, install)
        updates = {deployed: (encoded(candidate), 0o600), operator: (encoded(merged), 0o600)}
        for name_cli in LAUNCHERS:
            script = release / 'scripts' / (name_cli.replace('-', '_') + '.py')
            updates[launchers / name_cli] = (f'#!/usr/bin/env bash\nset -euo pipefail\nexec /usr/bin/python3 -B {shlex.quote(str(script))} "$@"\n'.encode(), 0o755)
        if already:
            if (old_operator != merged or candidate != old
                    or any(not path.exists() or path.read_bytes() != data for path, data in targets.items())
                    or any(path.read_bytes() != data for path, (data, _) in updates.items() if path.parent == launchers)):
                raise OperationError('already configured instance differs; use the normal stopped release/update procedure')
            return {'instance': name, 'status': 'already-configured', 'generation': release.name}
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in updates}
        added = []
        try:
            for path, data in targets.items():
                if not path.exists():
                    atomic_write(path, data, 0o644)
                    added.append(path)
            for path, (data, mode) in updates.items():
                atomic_write(path, data, mode)
        except BaseException as error:
            failed = False
            for path, (data, mode) in before.items():
                try:
                    atomic_write(path, data, mode)
                except OSError:
                    failed = True
            if not failed:
                for path in added:
                    path.unlink(missing_ok=True)
            message = ('additive publication failed; inspect catalogs because rollback was incomplete' if failed
                       else 'additive publication failed; prior catalogs and launchers were restored')
            raise OperationError(message) from error
        return {'instance': name, 'status': 'configured', 'generation': release.name}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--operator', type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = add_instance(args.definition, args.manifest, args.deployment, args.operator)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OperationError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, OperationError) else 'invalid additive runtime inputs or publication result'
        print(json.dumps({'error': message}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
