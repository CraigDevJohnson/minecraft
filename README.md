# Minecraft

One fresh family survival server for up to six Java and Bedrock players. The repository defines the replaceable host, retained storage, state bootstrap, pinned crossplay runtime, guarded session commands, verified cold backup publication, and isolated restore, with reproducible tools and credential-free checks. Live AWS deployment and demonstrated S3 recovery remain separate deliveries.

Use feature branches and pull requests for changes. Keep credentials, local configuration, and generated server data out of Git.

Report suspected vulnerabilities using the [security policy](SECURITY.md).

## Start here

- [Crafty Controller host (current build, ADR 0004)](docs/operations/crafty.md)
- [Development setup and commands](docs/development.md)
- [Server operations design](docs/superpowers/specs/2026-09-07-server-operations-design.md)
- [Infrastructure ownership, setup, costs, and handoff](docs/operations/infrastructure.md)
- [State bootstrap and recovery](docs/operations/state-bootstrap.md)
- [Runtime installation and private configuration](docs/operations/runtime-installation.md)
- [Instance commands and backup-hook contract](docs/operations/runtime-operations.md)
- [Backup commands, retention, and isolated recovery](docs/operations/backup-recovery.md)
- [Live persistence and recovery rehearsal](docs/operations/live-recovery-rehearsal.md)
- [Optional idle stop](docs/operations/idle-stop.md)
- [Launcher backend for The Lobby](docs/operations/launcher.md)
- [Cloud controls: stop, start, allowlist and deploy from anywhere](docs/operations/cloud-controls.md)
- [Pinned content and explicit local runtime validation](docs/operations/runtime-content.md)
- [Family device joins, console rollback, and play acceptance](docs/operations/family-device-validation.md)
- [Creative world, separate ports and shared-host operation](docs/operations/creative-world.md)
- [Client compatibility and upstream release gates](docs/operations/client-compatibility-readiness.md)
- [Foundation issue #3](https://github.com/CraigDevJohnson/minecraft/issues/3) and [current issues](https://github.com/CraigDevJohnson/minecraft/issues)

Python 3.11 or newer and Go (the version in `lambda/idlestop/go.mod`, shared by `lambda/launcher`) are the host-language prerequisites. Install the pinned tools and run the shared gate from the repository root:

```sh
python3 scripts/bootstrap_tools.py
.tools/bin/task check
```

These commands may download public tool and provider releases. They do not use AWS credentials, configure a remote backend, apply infrastructure, start Minecraft, or accept its EULA.

The security workflow checks for secrets with Gitleaks, validates GitHub Actions syntax with actionlint, and audits workflows and Dependabot configuration with zizmor. These checks cover the current repository files; add language and dependency scanners when application code and manifests arrive.

Dependabot checks GitHub Actions dependencies weekly, grouping minor and patch version updates separately from security updates. Version updates wait seven days after release; security updates are not delayed by that cooldown. Scanner CLI releases and their download checksums are pinned in the workflow and require manual updates: verify each replacement against its upstream release before changing the version and SHA256 together.
