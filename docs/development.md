# Development setup

The foundation requires Python 3.11 or newer, Go at the version in [`lambda/idlestop/go.mod`](../lambda/idlestop/go.mod) for the [idle-stop](operations/idle-stop.md) and [launcher](operations/launcher.md) functions, and public network access for pinned tool and provider downloads. It does not require AWS credentials, Docker, EULA acceptance, or a running Minecraft server.

From the repository root, install the exact tool versions recorded in [`tools/lock.json`](../tools/lock.json):

```sh
python3 scripts/bootstrap_tools.py
```

The installer supports macOS and Linux on ARM64 and AMD64. It verifies each official release asset before atomically replacing `.tools/bin/<tool>`.

Use the project-local Task binary for all repository commands:

```sh
.tools/bin/task check
.tools/bin/task fmt
.tools/bin/task infra:validate
```

`check` runs Bash syntax checks, ShellCheck, shfmt verification (including the host template), OpenTofu formatting, Python syntax/behavioral checks, gofmt, `go vet` and race-enabled Go tests for `lambda/idlestop` and `lambda/launcher`, and backend-free OpenTofu validation and mocked plan assertions for `infra` and `infra/state-bootstrap`. `fmt` updates shell/template, OpenTofu and Go formatting. `lambda:package` builds the reproducible idle-stop zip, and `lambda:package:launcher` the launcher zip. `infra:validate` runs only the isolated OpenTofu validation and assertion path. `server:status`, `server:start`, `server:stop`, `server:restore`, `server:mods`, `server:mod-on`, `server:mod-off` and `server:allowlist` send one signed request to the deployed launcher and run nothing locally; `WORLD=<id>` names the world ([cloud controls](operations/cloud-controls.md)). The Pins workflow, not `check`, downloads the files `runtime/crafty/server.json` pins and checks their SHA-256 (`scripts/check_pins.py`), because `check` stays offline. Python is not statically typechecked; OpenTofu validation checks its configuration types and provider schemas.

OpenTofu runs against a temporary copy of source, with temporary home, data and provider cache directories and an empty CLI configuration. The clean environment discards inherited AWS credentials, OpenTofu variables/arguments and debug settings. AWS configuration paths point to nonexistent files and metadata access is disabled. Local auto-tfvars, overrides, state and initialized backend metadata are not copied. The regression test runs both roots with poisoned private files and CLI arguments and verifies that they remain unchanged. Public pinned provider downloads still require network access.

The approved test seams are infrastructure plan/policy assertions, storage and bootstrap OS boundaries, the Crafty adapter against a fake Crafty API and host, the world hosts' first-boot script against a fake operating system, the pin checker against fake downloads, the Launcher package, the workflow guards and policy examples, allowlist commands, and operator AWS/SSM orchestration with its DNS updates. Tests exercise public operations with controlled external boundaries; they never access live disks, launch Minecraft, or call AWS or Cloudflare. See [infrastructure operations](operations/infrastructure.md) for the live evidence still required.

## Shared instance contract

Each world of `runtime/crafty/server.json` (`survival`, `creative`, `adventure`) runs on a host of its own, as that host's one server instance, named by the world's id and run by Crafty Controller ([ADR 0009](adr/0009-one-host-per-world.md)). World, instance and mod IDs begin with a lowercase letter, contain only lowercase letters, digits, and hyphens, and are at most 32 characters. On a world host `/srv/minecraft` is a folder on the root disk; Crafty's configuration, server folders, staging and state live under `/srv/minecraft/crafty/`; archives go to `instances/<world>/crafty/` within the private environment bucket ([Crafty host](operations/crafty.md)). The family host of [ADR 0004](adr/0004-crafty-multiverse-rebuild.md), whose instance was `family` on a retained data disk, is removed from the configuration and retired by the Deploy the [worlds runbook](operations/worlds.md#the-family-hosts-retirement) describes; its archives stay under `instances/family/crafty/`.

The pinned server content (`runtime/crafty/server.json`) stays separate from mutable data; the adapter the Launcher carries applies it at each start ([ADR 0007](adr/0007-the-launcher-carries-the-host-adapter.md), [ADR 0008](adr/0008-the-adapter-applies-the-pinned-server-content.md)). Private deployment output supplies the AWS account and region, resource IDs, the public hostname and its Cloudflare zone ID, each world's host and hostname (`worlds` in the `deployment` output), and bucket names. Store that output under the ignored `operator-config/<environment>/` directory. AWS resource names and tags identify the project, environment, and purpose, adding the stable instance ID only for instance-scoped resources; an EC2 host ID never becomes an instance ID.

Starts, stops, restores, mod switches and allowlist changes go through the Launcher ([cloud controls](operations/cloud-controls.md)); the operator host commands that knew only the family host were removed with it. #8's live recovery proof of 2026-10-03 and #23's on-demand stop and cold-start proof of 2026-10-05 were recorded on the earlier Compose stack ([#8](https://github.com/CraigDevJohnson/minecraft/issues/8), [#23](https://github.com/CraigDevJohnson/minecraft/issues/23)), whose runbooks #70 retired; [CONTEXT.md](../CONTEXT.md) keeps the dated records. Household demonstrations are resolved by owner acceptance; the [server operations design](superpowers/specs/2026-09-07-server-operations-design.md) records the shared requirements that ADR 0004 did not change.
