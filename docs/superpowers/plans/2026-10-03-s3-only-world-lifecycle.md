# S3-only world lifecycle implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved #24 second phase: no session host/root/data EBS while off, with exact independently verified S3 recovery before deletion and restore before play.

**Architecture:** Extend the existing workstation lifecycle with explicit retained/session data ownership, a receipt-bound operation journal and actual saved-plan gate. Reuse the receipt foundation, temporary operator credentials and fixed private transfer. A separate provenance component must prove the new disk before zero-fill/initialize; ambiguous targets remain intact and stopped.

**Tech Stack:** Python standard library, pinned OpenTofu/AWS CLI, SSM, versioned S3.

**Spec:** Approved `operator-config/backlog-completion/on-demand-design-proposal.md` in the originating worktree.

## Global constraints

- No live AWS write or deletion is authorized by this implementation task.
- Keep `prevent_destroy=true` on retained data until the separately reviewed ownership/protection migration; preserve networking/IAM/backups/state.
- One workstation/full-operation lock; durable private receipt/journal outside deleted resources.
- Stop verifies exact final session marker/archive versions and complete inventory before deletion, protects selected versions and publishes the authoritative selection first.
- Start creates a bounded capacity-sized encrypted data disk and a UUID distinct from every source, enforces the mandatory 900-second provenance wait, restores the selected exact receipt, and leaves the game stopped on failure.
- Never choose catalog latest, force-detach, force-format, destroy partial targets, expire current/active points due to inactivity, or grant runtime permanent version deletion.
- Phase-two timing is measured separately; the existing provenance floor prevents a 180-second promise.

### Task 1: Ownership and absent-data handoff

**Files:** `infra/variables.tf`, `infra/host.tf`, `infra/outputs.tf`, `infra/tests/infrastructure.tftest.hcl` (coordinate retention assertions), `scripts/minecraft_host.py`.

**Interfaces:** `lifecycle_mode="s3-only"`; `data_ownership="retained"|"session"`; protected retained `aws_ebs_volume.data[0]` and session `aws_ebs_volume.session_data[0]`; `recovery_receipt_ids` maps every configured instance to a 64-hex receipt. Absent-data schema-2 handoff has null host/data/UUID only with that complete mapping.

- [x] Add compatibility moved block for retained data indexing, preserve exact existing bootstrap bytes and protection.
- [x] Document and verify the separate no-change state-address ownership migration before any removal authority.
- [x] Test absent session data plus retained network/IAM/backups/state, invalid ownership/presence/receipt combinations and idle-stop incompatibility.

### Task 2: Receipt-bound controller and deletion

**Files:** Create `scripts/minecraft_s3_session.py`; modify `scripts/minecraft_on_demand.py`, `scripts/minecraft_host.py`; create `tests/test_s3_session.py`.

**Interfaces:** `S3Session` uses `S3Store(environment_provider=aws.environment)`, `verify_stop_receipt(deployment,drain,journal_dir,bootstrap_sha256,store=store,protect=True,timeout=3000)`, and the shared operation lock. Read-only `verify_current_selection_protection` proves existing keep tags and live lifecycle exclusion before every removal retry and off-state create apply. Receipt journal remains the same durable directory across operations.

- [x] Attest installed runtime against the pinned public release before accepting a receipt’s declared bootstrap digest.
- [x] Consume `host-drain-private`, validate every final session and use exact versions/inventory outside the data disk.
- [x] Save receipt IDs/selection revisions/source storage privately before graceful EC2 stop and the actual saved removal plan.
- [x] Permit only exact session host/attachment/data changes; independently prove all session EBS/host absence and publish valid off-state handoff.
- [x] Test failed/corrupt/missing/incompatible receipt, stale selection and changed source/plan refusing removal.

### Task 3: Fresh disk, guarded restore and interruption

**Files:** `scripts/minecraft_s3_session.py`, `scripts/minecraft_bootstrap_release.py`, `scripts/minecraft_private_transfer.py`, source-digest binding, `tests/test_s3_session.py`.

**Interfaces:** `verify_fresh_volume(aws,deployment,journal,timeout=1800)` consumes `successful_apply_window={started_at_utc,completed_at_utc}`, `source_storage=[{volume_id,filesystem_uuid}]`, `receipt_ids={instance:64hex}`. The caller uses its evidence within ten minutes before guarded zero-fill. Restore command is `minecraft-restore --config /etc/minecraft/operator.json --receipt /etc/minecraft/recovery-receipt.json --bootstrap-sha256 SHA --timeout 3000 --resume INSTANCE POINT`.

- [x] Validate/download the authoritative receipt/archive outside the target before provisioning; persist target capacity/UUID and immutable receipt IDs in the operation.
- [x] Record the actual successful apply window, prove current disk provenance and initial first-boot state, then zero-fill/initialize/mount through existing guarded commands.
- [x] Securely transfer the exact receipt, restore every configured instance and independently recognize only a complete same-receipt target on retry.
- [x] Persist phases before normal start so retries never rerun restore once game startup progressed; preserve ambiguous/partial target disks.
- [x] Test unknown apply outcome, UUID reuse, inadequate capacity, failed provenance/restore/readiness, changed selection, interrupted restore and DNS failure.

### Task 4: Retention integration and final verification

**Files:** Operations/ADR docs and controller caller only; receipt worker owns `minecraft_retention.py`/backup/store files, reviewer owns storage/IAM.

**Interfaces:** `reconcile_retention(store,instance,journal_dir,current_selection=...,active_receipt_ids=...)` runs under the same full-operation lock and preserves latest fourteen/current/active exact versions. Runtime pruning is disabled in S3-only mode.

- [x] Integrate component commits without altering their owned files; include retention/provenance/controller code in source binding and appropriate public release members.
- [x] Run meaningful boundary tests and the combined `.tools/bin/task check` on final state; independently review and fix findings. Final gate: 452 Python tests, Go race/vet and 64 infrastructure assertions; independent bounded review found no safety blocker.
- [x] Commit the complete verified local implementation with `Refs #24`.
- [ ] Parent publishes and integrates the reviewed PR; keep the issue open for actual no-EBS S3 round trip, failure proof, capacity/provenance and measured timing.
