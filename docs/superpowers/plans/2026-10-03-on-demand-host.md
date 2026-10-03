# On-demand host implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add opt-in host/root removal after a verified drain and operator start that recreates and configures the host while preserving the data disk.

**Architecture:** Keep existing infrastructure ownership and runtime commands. Add versioned off-state deployment output and a workstation controller that serializes the full operation, pins approved configuration and release bytes, gates the actual saved plan, and journals interruptions. Private files cross only an SSM loopback tunnel to a digest-bound receiver.

**Tech Stack:** Python standard library, AWS CLI v2/Session Manager plugin, pinned OpenTofu, Ubuntu ARM64/Docker.

**Spec:** `operator-config/backlog-completion/on-demand-design-proposal.md` in the originating worktree, approved in chat on 2026-10-03.

## Global constraints

- Existing deployments retain stop/start behavior; reject `idle_stop` with on-demand mode.
- Phase one retains the protected data volume, network, IAM, backups, and state.
- One designated workstation; one lock over the complete operation; private durable journal.
- Credentials never appear in Git, user data, Terraform values, Run Command, bootstrap release, diagnostics, or public status.
- Runtime release is immutable under a SHA-256 prefix with read-only host permission.
- The 180-second startup target requires a live measurement; offline tests do not establish it.
- This implementation authorizes no AWS mutation; initial migration and recurring policy need concrete saved-plan review.

### Task 1: Session infrastructure and deployment handoff

**Files:** `infra/host.tf`, `infra/variables.tf`, `infra/outputs.tf`, `infra/iam.tf`, `infra/idle-stop.tf`, `infra/tests/infrastructure.tftest.hcl`, `tests/test_infrastructure_policy.py`.

**Interfaces:** `lifecycle_mode="retained-host"|"on-demand-host"`; `session_present=true|false`; deployment schema 2 has null `host_id` only when absent in on-demand mode. Moved blocks preserve host/attachment state when introducing count.

- [x] Exercise mocked off-state and idle-stop incompatibility assertions.
- [x] Add count/moved blocks and retain existing `prevent_destroy` protection.
- [x] Verify protected resources still exist when the session is absent.

### Task 2: Immutable runtime and secure private transfer

**Files:** `scripts/minecraft_bootstrap_release.py`, `scripts/minecraft_private_transfer.py`, `tests/test_bootstrap_release.py`, `tests/test_private_transfer.py`, optional bootstrap rendering in `infra/host.tf`.

**Interfaces:** `build_bundle(deployment,operator_path,secret_path)->(bytes,sha256)`; receiver binds loopback and publishes only fixed mode-0600 private files after exact digest/schema/size/path validation. `stage_release(payload,sha256,destination)` validates deterministic fixed-member release; the host downloads only its SHA-256 key.

- [x] Test wrong digest, excessive body, symlink paths, invalid file inventory, malformed JSON, hostile archive members, and identical retries.
- [x] Package only authored scripts/runtime; install pinned AWS CLI and helpers without secrets.
- [x] Test client/receiver diagnostics never print supplied secret bytes.

### Task 3: Guarded operator lifecycle

**Files:** `scripts/minecraft_on_demand.py`, `scripts/minecraft_host.py`, `tests/test_on_demand_host.py`, `tests/test_minecraft_host.py`.

**Interfaces:** `OnDemandSession` implements `start`, `stop`, `status`, `repair_dns`; invokes existing `HostSession` after validating a present v2 handoff. Controller policy pins source/inputs/backend/release digests and workstation. Plan gate allows only `aws_instance.host[0]` and `aws_volume_attachment.data[0]` create/delete operations.

- [x] Test concurrent refusal and private journal resume after interruption.
- [x] Test account/backend/state lineage/input/release binding and reject every non-session plan change.
- [x] Test failed drain leaves host and disk available; failed transfer/install/readiness never reports ready.
- [x] Implement strict saved-plan apply with independent state reconciliation after a partial apply.
- [x] Test removal proof includes host termination, root disappearance, and retained-disk identity; DNS failure remains repairable.

### Task 4: Documentation and final validation

**Files:** `docs/adr/0003-on-demand-host-sessions.md`, `docs/operations/on-demand-host.md`, `docs/operations/host-commands.md`, `CONTEXT.md`.

- [x] Record accepted architecture, migration/rollback and recurring policy preparation commands.
- [x] Run `.tools/bin/task fmt`, `.tools/bin/task check`, inspect task-owned diff, and commit using Conventional Commits.
- [ ] Publish PR with `Refs #23`, wait for current-revision CI, report live proof still required without closing the issue.
