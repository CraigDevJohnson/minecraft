# Minecraft

One family Minecraft server, with a survival and a creative world, for up to six Java and Bedrock players. The server runs under Crafty Controller on a retained AWS host ([ADR 0004](docs/adr/0004-crafty-multiverse-rebuild.md)); the Launcher starts, stops, restores and allowlists it from The Lobby, the Server control workflow or `task server:*` ([ADR 0006](docs/adr/0006-cloud-controls.md)). The repository defines the host, its retained storage and state bootstrap, the pinned server content, the host adapter the Launcher carries, and the deploy and health workflows, with reproducible tools and credential-free checks.

Changes go through feature branches and pull requests in this repository; it is not taking outside contributions. Keep credentials, local configuration, and generated server data out of Git.

Report suspected vulnerabilities using the [security policy](SECURITY.md).

## Start here

- [Crafty Controller host (current build, ADR 0004)](docs/operations/crafty.md)
- [Development setup and commands](docs/development.md)
- [Server operations design](docs/superpowers/specs/2026-09-07-server-operations-design.md)
- [Infrastructure ownership, setup, costs, and handoff](docs/operations/infrastructure.md)
- [State bootstrap and recovery](docs/operations/state-bootstrap.md)
- [Optional idle stop](docs/operations/idle-stop.md)
- [Launcher backend for The Lobby](docs/operations/launcher.md)
- [Cloud controls: stop, start, restore, allowlist and deploy from anywhere](docs/operations/cloud-controls.md)
- [Family device joins, console rollback, and play acceptance](docs/operations/family-device-validation.md)
- [Client compatibility and upstream release gates](docs/operations/client-compatibility-readiness.md)
- [Foundation issue #3](https://github.com/CraigDevJohnson/minecraft/issues/3) and [current issues](https://github.com/CraigDevJohnson/minecraft/issues)

Python 3.11 or newer and Go (the version in `lambda/idlestop/go.mod`, shared by `lambda/launcher`) are the host-language prerequisites. Install the pinned tools and run the shared gate from the repository root:

```sh
python3 scripts/bootstrap_tools.py
.tools/bin/task check
```

These commands may download public tool and provider releases. They do not use AWS credentials, configure a remote backend, apply infrastructure, start Minecraft, or accept its EULA.

The security workflow checks for secrets with Gitleaks, validates GitHub Actions syntax with actionlint, and audits workflows and Dependabot configuration with zizmor. These checks cover the current repository files; add language and dependency scanners when application code and manifests arrive.

Dependabot checks GitHub Actions and the two Go modules under `lambda/` weekly, grouping minor and patch version updates separately from security updates. Version updates wait seven days after release; security updates are not delayed by that cooldown. Scanner CLI releases and their download checksums are pinned in the workflow and require manual updates: verify each replacement against its upstream release before changing the version and SHA256 together.

## Licence

The code is public under the [MIT licence](LICENSE); it is not taking contributions. The Crafty build writes `eula=true` on the strength of Craig's acceptance of the Minecraft EULA for his family server (#5); anyone else running it must read and accept the [Minecraft EULA](https://www.minecraft.net/eula) themselves.
