# Verified backups and isolated recovery

The installed backup hook publishes a cold recovery point after the lifecycle controller proves a clean stop. It captures the entire mutable `data/` tree, including dimensions, player state, allowlists/operators, server and plugin configuration, and Floodgate keys. It records the actual active content manifest and instance definition, exact runtime versions, source storage identity, UTC creation time, file sizes and SHA-256 checksums. Generated Compose, private deployment/operator files, and lifecycle journals are not copied. Keep the original private deployment output separately for recovery.

## Install and operate

Follow [trusted runtime installation](runtime-installation.md). The reviewed release installs `minecraft-backup`, `minecraft-restore`, and their Python modules. The shared operator example points `backup_hook` at `["/usr/local/sbin/minecraft-backup", "hook"]`; private EULA acceptance and secrets still require operator configuration. Install AWS CLI v2 with support for conditional multipart completion and SHA-256 checksums; the installer checks these capabilities without making AWS requests. The hook uses temporary host credentials and the existing named-instance S3 permissions. Credentials and EC2 control never enter Minecraft's container.

```sh
sudo minecraft-instance --config /etc/minecraft/operator.json backup survival
sudo minecraft-backup status survival --config /etc/minecraft/operator.json
sudo minecraft-backup prune survival --config /etc/minecraft/operator.json
```

`backup` cleanly saves and stops a running instance, then runs the required hook while holding the host lifecycle lock. It leaves Minecraft stopped and does not stop EC2. Session end and pre-update capture use this same path. Retry `backup` or `stop` after failure; an already-stopped pending session is backed up without starting or saving it again. A completed session reuses its recorded recovery point. To reconcile a partial retention operation separately, run `prune` and inspect `status`.

The internal `hook` command accepts only the controller JSON protocol. It verifies the private deployment, retained storage, stopped container and held lifecycle lock. Do not run it directly, modify operation records, or use a success stub. Stop/update and operator EC2 shutdown remain blocked after missing, malformed, failed or timed-out backup completion.

The first #7 deployment installs on a pristine host. Later release replacements retain the existing requirement for a completed safe stop and backup before maintenance; installation never repairs or bypasses a pending session.

## Publication, retries, and retention

Each logical instance/session/content combination has one deterministic recovery identity. Objects are stored under:

- `instances/<id>/objects/<recovery-point>/data.tar.gz`: deterministic cold archive.
- `instances/<id>/completed/<recovery-point>.json`: versioned metadata and completion marker.
- `instances/<id>/retiring/<recovery-point>.json`: durable intent for an interrupted prune.

Uploads use [conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) for both ordinary and multipart objects. The publisher downloads the uploaded archive and compares its complete size and SHA-256 before publishing the completion marker, then reads back the completed point. A previous object is never overwritten to resolve a conflict. A lost receipt reuses the verified point. An interrupted multipart upload or an archive without completion metadata is unavailable for restore; retry the same session. A corrupt conflicting object requires operator investigation rather than automatic overwrite.

Keep the latest 14 valid completed recovery points per instance. Their current objects have no age-only expiry: the latest successful point survives months without play. The backup hook returns immediately after verified publication and leaves retention pending; run `minecraft-backup prune <instance> --config <path>` separately to verify the catalog and apply retention. A damaged catalog prevents deletion. Deletion first removes discovery, then its archive, and retains a private deletion intent until both succeed. Interrupted deletion remains visible through `pending_deletions` and is retryable. Retention work cannot delay or invalidate an independently verified new recovery point.

`status` verifies metadata and archive bytes, reports each valid identity, content checksum, timestamp, age in seconds and archive size, and reports invalid entries, excess completed points and pending deletion intents. No completed point means `completion: unavailable`. No player paths, identities, private storage IDs, configuration values or secrets are printed. Inspection does not repair corrupt backups.

Deleting a current S3 object creates a delete marker in the versioned bucket. Infrastructure expires noncurrent versions after 14 days and incomplete multipart uploads after one day. These are asynchronous S3 lifecycle policies, not immediate physical deletion or a fixed storage cap. Completed-marker failures can leave unreferenced current archives until that session is retried or an operator investigates them; current orphan objects have no automatic age expiry. Storage can temporarily exceed 14 archives. The accepted recovery policy can lose the current session if the retained disk fails during play.

## Disk and execution bounds

Use a private working directory outside the source instance, preferably on an adequately sized separate filesystem. Python uses `TMPDIR` for backup/status/prune work; each invocation creates a mode-0700 temporary child. Do not put a nested workspace mount beneath `/srv/minecraft`: storage verification refuses nested mounts. Allow roughly twice the largest compressed archive plus 64 MiB for publication/catalog verification, alongside the retained world. Restoration needs its compressed archive plus the complete uncompressed recovery set free on the target retained volume. On the recommended 5 GiB data disk that fits a world of up to about 2 GiB uncompressed; restore a larger one onto a larger new disk sized from the marker ([data disk capacity](data-disk-capacity.md)). Interrupted process termination may leave a private temporary directory; inspect and remove only that confirmed inactive workspace.

The current bounds are 100 GiB for compressed archives and total uncompressed file bytes, 200,000 inventory entries, and 16 MiB metadata. Actual available disk may be much smaller. Full catalog verification downloads every retained archive, so transfer time and S3 read/traffic charges grow with the retained set. The host hook defaults to 900 seconds and allows at most 3000 seconds through `backup_timeout`; the S3 client shares a deadline across all requests. If a world exceeds measured workspace or execution limits, stop and revise capacity/verification strategy before relying on normal host shutdown. No change here increases provisioned disk or grants billable resources.

## Restore onto an isolated host

Restoration prepares the same named instance on a **different verified retained volume and filesystem**, using target-private deployment/configuration. It refuses any existing instance directory or container, even a stopped one. It never replaces survival in place. A clone retaining the source filesystem UUID is also rejected.

1. Through the separately authorized #7/#8 deployment procedure, prepare an isolated host and blank retained filesystem. Install the reviewed runtime. Leave its instance entirely pristine. Supply target-private deployment, operator configuration, fresh RCON secret and recorded EULA acceptance. Restrict access to the recovery test host until validation succeeds.
2. Retain the original private source deployment output and select a 64-character recovery identity from the verified backup status. `--source-deployment` must match the source volume and filesystem recorded in the point, so after a data disk replacement keep the pre-replacement output for as long as any retained recovery point names the old volume. Give the recovery operator temporary access to read that source backup bucket; the default role for a different environment can access only its own bucket. Review any required source-bucket permission separately in the concrete deployment plan.
3. Retrieve the selected `completed/<identity>.json` into a mode-0700 operator directory, using the AWS CLI with temporary credentials. Its `manifest` and `definition` objects provide the exact original runtime inputs. Write these objects to private JSON files: keep the definition unchanged, store its manifest under the filename named by `definition.manifest`, and point the target operator configuration's `definitions.survival` at that definition. Do not run arbitrary code from metadata. The restore command independently validates these inputs and checks their identity against the verified backup before publishing a target.
4. Run the supported restore command on the target host:

```sh
sudo minecraft-restore --config /etc/minecraft/operator.json \
  --source-deployment /etc/minecraft/source-deployment.json survival RECOVERY_POINT
sudo minecraft-instance --config /etc/minecraft/operator.json start survival
sudo minecraft-instance --config /etc/minecraft/operator.json status survival
```

The command validates metadata schema and identity, exact content/definition compatibility, archive size/checksum, inventory completeness, every file checksum, and safe archive paths/types. It rejects links, devices, duplicate entries and traversal. All extraction and ownership preparation happens in private staging before a final atomic directory rename. Failure before publication leaves the selected target absent; retry after correcting the cause. No archive is extracted directly into the requested target.

The result includes the recorded runtime inputs and a fresh prepared lifecycle record. Normal startup checks the restored inputs and requires private acceptance/secret, storage verification and an absent prior container before generating target Compose. It downloads the exact pinned artifacts and starts through the existing one-writer controller. Source world data and S3 backup objects are untouched. Review world landmarks, all dimensions, player inventory/state, access settings and plugin/Floodgate state after startup. Exact runtime pins must remain downloadable; schema validation cannot promise future upstream artifact availability.

Finish the restored session with the normal guarded backup/stop procedure using its own configured destination. Promotion into the active family environment is a separate, explicitly reviewed operation.

## Exact-version receipts for S3-only storage

The approved [#24 design](https://github.com/CraigDevJohnson/minecraft/issues/24) requires a verified receipt before the live data disk can be removed. The receipt and restore foundations alone do not enable host or data-volume deletion. The separately reviewed lifecycle migration, recurring deletion policy, S3 retention and version-read permissions, and live recovery evidence are still required.

`minecraft-instance host-drain-private` runs the same guarded drain under the runtime lock and adds a private `recovery` map containing each final session, content digest and exact recovery point. It requires a tracked, cleanly stopped session with a completed backup for every instance. Keep this response in the private operator journal. The ordinary `host-drain` response stays unchanged.

The operator controller calls `minecraft_backup.verify_stop_receipt` with that evidence, the source deployment, the reviewed bootstrap bundle digest and an existing operator-owned mode-0700 journal directory. It independently reads the exact completed marker and archive versions and verifies every archived file. Receipts record those version IDs and hashes, source account/host/disk/filesystem and deployment digest, content and definition digests, bootstrap digest and capacity requirements. Mode-0600 local receipts survive disk deletion. S3 stores immutable receipts under `instances/<id>/receipts/<receipt-id>.json` and advances `receipts/current.json` with a conditional write against the observed ETag. A sequence and predecessor bind successive selections. Missing or corrupt selection, a stale local selection, incomplete final-session backup, or an unavailable exact version blocks removal; catalog `latest` is never a fallback.

Receipt verification accepts an explicit `timeout` of 1 to 3000 seconds, defaulting to 3000 for each configured instance. Each instance receives a fresh store operation, including when the caller injects a configured store, so earlier instances do not consume its verification budget. This bounded interval covers that instance's full marker/archive verification and receipt publication; timeout failure preserves the host and disk.

After the lifecycle controller creates and verifies a distinct target disk and filesystem, it transfers the selected private receipt with the existing private runtime configuration and invokes:

```sh
sudo minecraft-restore --config /etc/minecraft/operator.json \
  --receipt /etc/minecraft/recovery-receipt.json \
  --bootstrap-sha256 REVIEWED_BUNDLE_SHA256 --resume survival RECOVERY_POINT
```

Restore reads the recorded object versions even if the current keys change. It requires exact target content/definition compatibility and sufficient free capacity for the archive, extracted files and 1 GiB of working margin. It installs the recovery data atomically and leaves Minecraft stopped. `--resume` recognizes an occupied target only when its prepared record names the same receipt and target storage and every world/player/configuration file still matches the recorded inventory and hash. A partial, changed or ambiguous target is preserved for repair. The default restore command continues to refuse occupied targets.

An authoritative sole-copy receipt must retain access to its selected versions for as long as the server is off. The existing 14-day noncurrent-version expiry can remove a pinned version after a later write or delete marker. The [phase-two retention proposal](s3-only-retention.md) protects the exact selected versions and retires only a bounded, independently verified obsolete set; its lifecycle and IAM changes remain unimplemented. Runtime credentials must not receive permanent version deletion. The existing 15-minute fresh-volume provenance wait remains required; these foundations do not establish a three-minute S3-only startup or close #24.

## Evidence and live handoff

Credential-free validation covers publication/restore round trips with recognizable world/player/configuration/plugin checkpoints, missing/corrupt/incompatible input, unsafe archive paths/types, interrupted uploads, receipt loss, retention after long inactivity, prune retries, isolated target failure, and lifecycle/EC2 shutdown gates. AWS process tests verify command contracts and bounds using a fake CLI. They do not establish live S3 behavior.

The separately invoked `scripts/validate_recovery.py` performs a real local ARM64 Minecraft startup and recovery check after private EULA acceptance. Its storage and object-store boundaries are local test doubles. Routine `task check` never starts Minecraft. On 2026-09-08, this explicit command passed using ignored, mode-0600 private acceptance input:

```sh
python3 scripts/validate_recovery.py \
  --operator-config operator-config/recovery-validation-acceptance.json \
  --output-dir operator-config/recovery-proof-2026-09-08
```

That run used the previous content manifest `ebe2fd621e9328dd708aa77918dbd96f8039670cddc3364c4f45e582b931c29e`; it is pending re-validation for revision `survival-2026-10-02-bedrock-2650-viaversion` (see [runtime content](runtime-content.md#recorded-runtime-result)). The private report recorded two healthy ARM64 starts and clean stops, diamond-block checkpoints in all three dimensions, eight synthetic player/config/plugin/Floodgate file checkpoints, upload-failure retry, and unchanged source data and selected backup. Local restore/start/check/stop took 38.582 seconds. Storage identity, S3, root ownership, hook dispatch, and host installation were simulated; this interval is not a deployed recovery-time claim. Use a fresh output directory for each run.

After the authorized deployment in [#7](https://github.com/CraigDevJohnson/minecraft/issues/7), follow the [live persistence and recovery rehearsal](live-recovery-rehearsal.md). [#8](https://github.com/CraigDevJohnson/minecraft/issues/8) must demonstrate a real S3 round trip onto a distinct retained volume, validate family checkpoints after runtime startup, measure operator-initiated recovery time against the proposed 60-minute target, measure disk and transfer requirements, and confirm lifecycle expiry and temporary-credential permissions. Code checks and local runtime recovery do not close that live acceptance gate.
