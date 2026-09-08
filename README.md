# Minecraft

Foundation for one fresh family survival server for up to six Java and Bedrock players. The repository defines the replaceable host, retained storage, state bootstrap, and safe mount prerequisites, with reproducible tools and credential-free checks. The game runtime and live AWS deployment are separate deliveries.

Use feature branches and pull requests for changes. Keep credentials, local configuration, and generated server data out of Git.

Report suspected vulnerabilities using the [security policy](SECURITY.md).

## Start here

- [Development setup and commands](docs/development.md)
- [Server operations design](docs/superpowers/specs/2026-09-07-server-operations-design.md)
- [Infrastructure ownership, setup, costs, and handoff](docs/operations/infrastructure.md)
- [State bootstrap and recovery](docs/operations/state-bootstrap.md)
- [Foundation issue #3](https://github.com/CraigDevJohnson/minecraft/issues/3) and [current issues](https://github.com/CraigDevJohnson/minecraft/issues)

Python 3.11 or newer is the only host-language prerequisite. Install the pinned tools and run the shared gate from the repository root:

```sh
python3 scripts/bootstrap_tools.py
.tools/bin/task check
```

These commands may download public tool and provider releases. They do not use AWS credentials, configure a remote backend, apply infrastructure, start Minecraft, or accept its EULA.

The security workflow checks for secrets with Gitleaks, validates GitHub Actions syntax with actionlint, and audits workflows and Dependabot configuration with zizmor. These checks cover the current repository files; add language and dependency scanners when application code and manifests arrive.

Dependabot checks GitHub Actions dependencies weekly, grouping minor and patch version updates separately from security updates. Version updates wait seven days after release; security updates are not delayed by that cooldown. Scanner CLI releases and their download checksums are pinned in the workflow and require manual updates: verify each replacement against its upstream release before changing the version and SHA256 together.
