# Development setup

The foundation requires Python 3.11 or newer, Go at the version in [`lambda/idlestop/go.mod`](../lambda/idlestop/go.mod) for the [idle-stop function](operations/idle-stop.md), and public network access for pinned tool and provider downloads. It does not require AWS credentials, Docker, EULA acceptance, or a running Minecraft server.

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

`check` runs Bash syntax checks, ShellCheck, shfmt verification (including the host template), OpenTofu formatting, Python syntax/behavioral checks, gofmt, `go vet` and race-enabled Go tests for `lambda/idlestop`, and backend-free OpenTofu validation and mocked plan assertions for `infra`, `infra/state-bootstrap`, and `infra/recovery-rehearsal`. `fmt` updates shell/template, OpenTofu and Go formatting. `lambda:package` builds the reproducible idle-stop zip. `infra:validate` runs only the isolated OpenTofu validation and assertion path. Python is not statically typechecked; OpenTofu validation checks its configuration types and provider schemas.

OpenTofu runs against a temporary copy of source, with temporary home, data and provider cache directories and an empty CLI configuration. The clean environment discards inherited AWS credentials, OpenTofu variables/arguments and debug settings. AWS configuration paths point to nonexistent files and metadata access is disabled. Local auto-tfvars, overrides, state and initialized backend metadata are not copied. The regression test runs both roots with poisoned private files and CLI arguments and verifies that they remain unchanged. Public pinned provider downloads still require network access.

The approved test seams are infrastructure plan/policy assertions, storage and installer OS boundaries, generated runtime configuration/artifact integrity, instance lifecycle and backup-hook behavior, completed recovery-point publication/retention and isolated restoration, allowlist commands, and operator AWS/SSM orchestration with its DNS updates. Tests exercise public operations with controlled external boundaries; they never access live disks, launch Minecraft, or call AWS or Cloudflare. See [infrastructure operations](operations/infrastructure.md) for the live evidence still required. The separately invoked [ARM64 runtime test](operations/runtime-content.md) proves real local startup and fixture persistence after explicit private EULA acceptance; it is never part of routine CI.

## Shared instance contract

The first server instance is `survival`. Instance IDs begin with a lowercase letter, contain only lowercase letters, digits, and hyphens, and are at most 32 characters. The retained disk mounts at `/srv/minecraft`; mutable data lives at `/srv/minecraft/instances/<id>/data`; backups use `instances/<id>/` within the private environment bucket.

Desired instance configuration and the content manifest stay separate from mutable data. Instance configuration supplies ports, memory, runtime/content revision, manifest, and data paths. Private deployment output supplies the AWS account and region, resource IDs, the public hostname and its Cloudflare zone ID, data-volume and filesystem identities, and bucket names. Store that output under the ignored `operator-config/<environment>/` directory. AWS resource names and tags identify the project, environment, and purpose, adding the stable instance ID only for instance-scoped resources; an EC2 host ID never becomes an instance ID.

Instance start, stop, status, and update operations take an explicit instance ID. Host lifecycle is separate. See [runtime operations and the backup hook](operations/runtime-operations.md), [installation](operations/runtime-installation.md), and [operator host commands](operations/host-commands.md). The [backup/recovery commands](operations/backup-recovery.md) have credential-free coverage, and [#8's dated live proof](operations/live-recovery-rehearsal.md#completed-recovery-proof-2026-10-03) completed authenticated S3 restore, update/recovery, failure handling and temporary cleanup. #23's on-demand mode is activated with [recorded stop/cold-start proof](operations/on-demand-host.md#recorded-live-status-2026-10-04), while DNS repair and the timing decision keep #23 open. #24's sole-S3 mode remains unactivated. Household demonstrations are resolved by owner acceptance; the [server operations design](superpowers/specs/2026-09-07-server-operations-design.md) remains authoritative.
