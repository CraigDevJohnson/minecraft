# Instance lifecycle and backup integration

The `minecraft-instance` command runs as root on the reviewed Linux host after [installation](runtime-installation.md). It consumes private operator/deployment configuration. Every instance operation requires an explicit ID; initial deployment contains only `survival`.

```sh
sudo minecraft-instance --config /etc/minecraft/operator.json start survival
sudo minecraft-instance --config /etc/minecraft/operator.json status survival
sudo minecraft-instance --config /etc/minecraft/operator.json stop survival
sudo minecraft-instance --config /etc/minecraft/operator.json update survival
```

Start verifies retained EBS storage and operator acceptance, checks exact artifacts, and pulls the immutable ARM64 image. It verifies storage again before creating writable data and immediately before starting Compose. It waits for container health and RCON readiness. Missing storage cannot silently become a root-disk world. Compose has no automatic restart policy. Repeated start checks readiness without another writer; changed content requires `update`.

Stop warns players, requires `save-all flush` confirmation, sends `stop`, and waits for a clean container exit before calling the required backup hook. It never uses Docker's forced stop/kill fallback. Failure returns nonzero. Results distinguish application and backup states without printing player identities or secrets.

## Persistent state and retries

Commands and installation share a bounded Linux lock, serializing the small configured host through cold backup and allowlist changes. Competing commands fail after ten seconds and can be retried. Compose is the only runtime owner.

The retained disk stores `instances/<id>/operation.json` beside mutable `data/` and generated `runtime/`. It records the session identity, manifest digest, clean-stop progress and backup outcome using atomic writes and filesystem synchronization. Container UID/GID 1000 owns data; root owns adjacent control files. Generated Compose is private because it contains RCON credentials and EULA acceptance. Never edit operation metadata or invoke Compose directly to bypass a failed state.

| Situation | Outcome |
| --- | --- |
| Readiness fails while Minecraft runs | Start fails; inspect private logs and use safe stop. |
| Save or clean shutdown fails | No backup success is claimed; investigate and retry stop. |
| Clean stop succeeds but backup fails | Pending session persists. Fix/install the hook and retry stop without restarting or resaving the world. |
| Completed stop is repeated | Reports stopped/complete without another backup. |
| Compose fails before another container execution starts | The journal retains the prior container execution and session. Inspection can restore the completed backup state and permit retry when that execution is unchanged; a new failed execution still requires investigation. |
| Start follows a pending session backup | Refused until stop/backup succeeds. |
| Existing data has no operation record, or Minecraft exited unexpectedly | Ordinary operation refuses to adopt data or certify a crash as a clean save. Investigate and follow the reviewed recovery procedure. |

The [operator host commands](host-commands.md) control EC2 separately. Instance stop never stops the host. `host-drain` records a durable start barrier, rejects unconfigured running containers, and finishes every instance's safe stop and backup, including already-stopped instances with pending backups. Only then may the operator request EC2 stop. If AWS stop fails, the barrier remains for retry. Concurrent start cannot cancel it during the same Linux boot. An actual host stop/start gives a new boot ID; a subsequent start still requires valid storage and completed earlier backups. There is no automatic same-boot cancellation.

`host-status` reports configured instances and the barrier. A pristine instance without data or operation state has `backup: not-required`, which is not a recovery point. An already-stopped EC2 host cannot provide fresh application or backup verification.

## Required backup hook for #6

Private `backup_hook` is an argument array starting with an absolute executable, such as `["/usr/local/sbin/minecraft-backup"]`. The example leaves it `null`; no successful no-op hook is installed. The hook runs on the host with existing instance-role S3 access. The container gains neither AWS credentials nor EC2 permissions.

One request is supplied on standard input:

```json
{
  "schema_version": 1,
  "instance": "survival",
  "session": "operation UUID",
  "data_path": "/srv/minecraft/instances/survival/data",
  "manifest_path": "/srv/minecraft/instances/survival/runtime/manifest.json",
  "manifest_sha256": "canonical content manifest SHA-256",
  "definition_path": "/srv/minecraft/instances/survival/runtime/definition.json",
  "destination": {"bucket": "private bucket", "prefix": "instances/survival/"}
}
```

`minecraft_content.manifest_digest` hashes UTF-8 JSON with sorted keys and compact separators. This differs from individual JAR checksums. The source is stopped and the lifecycle lock stays held until the hook returns. The hook must not invoke another instance command and contend for that lock.

Exit zero alone is insufficient. Standard output must be one JSON object:

```json
{
  "schema_version": 1,
  "status": "complete",
  "instance": "survival",
  "session": "the same operation UUID",
  "manifest_sha256": "the same manifest SHA-256",
  "recovery_point": "identity of a verified complete S3 recovery point"
}
```

#6 must verify S3 publication before issuing this receipt, include the complete data directory plus supplied manifest/definition, and make retries for the same session/content idempotent. Missing, malformed, mismatched, failed or timed-out receipts block normal stop/update success. A timed-out hook process group is terminated; Minecraft has already exited cleanly. Diagnostics are not copied into public results.

The default hook timeout is 900 seconds, maximum 3000, keeping the initial single-instance stop within the one-hour SSM envelope. Larger worlds/additional instances require revisiting measured bounds. Test doubles prove orchestration; #6/#8 establish real backup/recovery.

## Controlled updates and validation

Review the new exact manifest and compatible desired definition outside mutable data. For an existing instance, `update` first stops and backs up the active revision, then stages and starts the new revision. Failed backup blocks launch. Active and desired manifests are separate; previous active manifests are retained by digest under runtime history before replacement. The hook receives the actual active definition as well.

After a failed content upgrade, do not assume reinstalling old binaries reverses world-format changes. Restore the verified pre-update recovery point into an isolated target through #6, using its matching old manifest/definition; #8 proves live recovery. Replacing operation scripts follows [staged installation maintenance](runtime-installation.md).

See [content and explicit ARM64 validation](runtime-content.md) for pins and real test evidence, and [allowlist management](allowlist.md) for private identities. Craig accepted the EULA for this server and local tests on 2026-09-08; agents may prepare private local test configuration from that decision. Shared defaults and routine CI continue to carry no acceptance.
