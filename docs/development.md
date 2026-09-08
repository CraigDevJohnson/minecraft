# Development setup

The foundation requires Python 3.11 or newer and public network access for pinned tool and provider downloads. It does not require AWS credentials, Docker, EULA acceptance, or a running Minecraft server.

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

`check` runs Bash syntax checks, ShellCheck, shfmt verification, OpenTofu formatting verification, Python unit tests, and backend-free OpenTofu initialization and validation. `fmt` updates shell and OpenTofu formatting. `infra:validate` runs only the isolated OpenTofu initialization and validation path.

OpenTofu uses a temporary `TF_DATA_DIR` for validation. Both initialization and validation explicitly use an empty temporary CLI configuration, so per-user settings such as provider development overrides and credentials helpers do not affect these checks. The check removes inherited AWS credentials, disables instance metadata access, and points AWS configuration paths at nonexistent files. It cannot alter an operator's initialized backend or use an AWS identity, although it downloads the locked public provider.

## Shared instance contract

The first server instance is `survival`. Instance IDs begin with a lowercase letter, contain only lowercase letters, digits, and hyphens, and are at most 32 characters. The retained disk mounts at `/srv/minecraft`; mutable data lives at `/srv/minecraft/instances/<id>/data`; backups use `instances/<id>/` within the private environment bucket.

Desired instance configuration and the content manifest stay separate from mutable data. Instance configuration supplies ports, memory, runtime/content revision, manifest, and data paths. Private deployment output supplies the AWS account and region, resource IDs, Elastic IP, data-volume and filesystem identities, and bucket names. Store that output under the ignored `operator-config/<environment>/` directory. AWS resource names and tags identify the project, environment, and purpose, adding the stable instance ID only for instance-scoped resources; an EC2 host ID never becomes an instance ID.

Instance start, stop, status, backup, restore, and update operations always take an explicit instance ID. Host lifecycle is separate. Runtime implementation and AWS resource creation belong to later issues; the [server operations design](superpowers/specs/2026-09-07-server-operations-design.md) is authoritative.
