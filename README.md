# Minecraft

[![Validate](https://github.com/CraigDevJohnson/minecraft/actions/workflows/validate.yml/badge.svg)](https://github.com/CraigDevJohnson/minecraft/actions/workflows/validate.yml)
[![Security](https://github.com/CraigDevJohnson/minecraft/actions/workflows/security.yml/badge.svg)](https://github.com/CraigDevJohnson/minecraft/actions/workflows/security.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

One family's Minecraft worlds, survival, creative and adventure, for up to six Java and Bedrock players. Each world runs on an AWS host of its own, under Crafty Controller, with its own mods and its own address, and only the worlds being played run ([ADR 0009](docs/adr/0009-one-host-per-world.md)). The Launcher starts, stops and restores a world, switches its mods and keeps the family's one allowlist, asked from The Lobby, the Server control workflow or `task server:*` ([ADR 0006](docs/adr/0006-cloud-controls.md)). The repository defines the world hosts and the script that sets each one up, the backup bucket and state bootstrap, the pinned server content with its mods, the host adapter the Launcher carries, and the deploy, pin-check and health workflows, with reproducible tools and credential-free checks. The earlier single family host ([ADR 0004](docs/adr/0004-crafty-multiverse-rebuild.md)) and its data disk are removed from the configuration and retired by the Deploy the [worlds runbook](docs/operations/worlds.md#the-family-hosts-retirement) describes; its archives stay in the backup bucket.

Changes go through feature branches and pull requests in this repository; it is not taking outside contributions ([contributing](CONTRIBUTING.md)). Keep credentials, local configuration, and generated server data out of Git.

Report suspected vulnerabilities using the [security policy](SECURITY.md).

## Start here

- [Worlds: mods, adding a world, the first Deploy and the proof run](docs/operations/worlds.md)
- [Crafty Controller on a world host](docs/operations/crafty.md)
- [Development setup and commands](docs/development.md)
- [Server operations design](docs/superpowers/specs/2026-09-07-server-operations-design.md)
- [Infrastructure ownership, setup, costs, and handoff](docs/operations/infrastructure.md)
- [State bootstrap and recovery](docs/operations/state-bootstrap.md)
- [Idle stop: an empty world stops itself](docs/operations/idle-stop.md)
- [Launcher backend for The Lobby](docs/operations/launcher.md)
- [Cloud controls: stop, start, restore, allowlist and deploy from anywhere](docs/operations/cloud-controls.md)
- [Family device joins, console rollback, and play acceptance](docs/operations/family-device-validation.md)
- [Client compatibility and upstream release gates](docs/operations/client-compatibility-readiness.md)
- [Changelog](CHANGELOG.md)
- [Foundation issue #3](https://github.com/CraigDevJohnson/minecraft/issues/3) and [current issues](https://github.com/CraigDevJohnson/minecraft/issues)

Python 3.11 or newer and Go (the version in `lambda/idlestop/go.mod`, shared by `lambda/launcher`) are the host-language prerequisites. Install the pinned tools and run the shared gate from the repository root:

```sh
python3 scripts/bootstrap_tools.py
.tools/bin/task check
```

These commands may download public tool and provider releases. They do not use AWS credentials, configure a remote backend, apply infrastructure, start Minecraft, or accept its EULA.

The security workflow checks for secrets with Gitleaks, validates GitHub Actions syntax with actionlint, and audits workflows and Dependabot configuration with zizmor. These checks cover the current repository files; add language and dependency scanners when application code and manifests arrive. The Pins workflow downloads every file `runtime/crafty/server.json` pins and checks its SHA-256 whenever that file changes, because the shared gate runs offline.

Dependabot checks GitHub Actions, the two Go modules under `lambda/` and the AWS provider in both OpenTofu roots (`infra` and `infra/state-bootstrap`) weekly, grouping minor and patch version updates separately from security updates and labelling its pull requests `dependencies`. Version updates wait seven days after release; security updates are not delayed by that cooldown. Minor and patch updates merge on their own once the required checks pass (`.github/workflows/dependabot-auto-merge.yml`); major updates, minor updates of a 0.x dependency, and every GitHub Actions update wait for Craig's review. A merged provider update reaches AWS only through the next reviewed plan and apply. OpenTofu itself, Task, ShellCheck and shfmt are pinned in `tools/lock.json` and updated by hand. Scanner CLI releases and their download checksums are pinned in the workflow and require manual updates: verify each replacement against its upstream release before changing the version and SHA256 together.

## Licence

The code is public under the [MIT licence](LICENSE); it is not taking contributions. The Crafty build writes `eula=true` on the strength of Craig's acceptance of the Minecraft EULA for his family server (#5); anyone else running it must read and accept the [Minecraft EULA](https://www.minecraft.net/eula) themselves.
