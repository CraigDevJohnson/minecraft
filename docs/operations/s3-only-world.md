# Keep the world in S3 while off

The approved second phase adds `lifecycle_mode="s3-only"`. A completed stop owns no session host, root or data EBS disk. Its world exists in independently verified exact S3 object versions and a durable authoritative recovery selection. Networking, role/profile, backups, budget and state remain retained. Live activation requires the separately reviewed ownership/protection migration and concrete recurring policy; this source change is not proof of actual deletion or recovery.

## Prepare ownership and protection

First complete #8's restore/failure/update proof and #23's reviewed host replacement. Keep the original data disk, world/player checkpoints and completed recovery points available throughout preparation. Update the compatible installed runtime while all instances are stopped; the S3-only controller attests public installed scripts, definitions/content, launchers and service against the pinned bootstrap archive before accepting a final-session receipt. It also verifies actual installed operator/RCON bytes and their trusted private-input certificate against the reviewed digest before private drain, restore or game start. Existing hosts need stopped-game secure reconfiguration to establish that certificate; a new policy alone does not adopt their old files.

The default `data_ownership="retained"` keeps `aws_ebs_volume.data[0]` protected with `prevent_destroy=true`. The compatibility moved block indexes the old `aws_ebs_volume.data` address without replacing the disk. Plan this as a no-change compatibility migration and inspect the unchanged physical volume/UUID/AZ/encryption/size, root, attachment, network, IAM and backup identities.

The separate `aws_ebs_volume.session_data[0]` address has the same physical configuration and tags for the ownership handoff. Enable `data_ownership="session"` only through the specifically reviewed transition. Before it, stop every writer, verify independent recovery and preserve a private backend state/version snapshot. Prepare the exact state-address move of the one recorded volume:

```sh
.tools/bin/tofu -chdir=infra state mv -dry-run \
  'aws_ebs_volume.data[0]' 'aws_ebs_volume.session_data[0]'
```

Review that source/destination and physical ID, the matching private configuration, rollback move, protection change and recurring removal authority before the actual move. Hold the full workstation lifecycle lock during the actual move and subsequent no-change plan verification. The retained resource's protection remains in authored source; moving ownership into the session address is the explicit protection boundary. Do not use an ordinary deletion/apply to perform this ownership migration. If the resulting plan changes the physical volume, attachment, filesystem UUID, networking, IAM, backups or state resources, preserve the volume and repair/roll back the address move before continuing. A candidate `s3-only` mode with retained ownership refuses controller mutation and an absent-session input.

Before deletion authority, deploy the reviewed mode-specific backup policy, effective host-boundary version reads and retirement-tag lifecycle configuration. The operator receipt protector inspects the actual S3 lifecycle rules as well as exact selected object versions; missing access or any conflicting expiry rule blocks deletion. Runtime credentials publish immutable points and read exact recovery versions; they cannot prune, retire, mutate receipts/current selection or permanently delete historical versions. Follow [version retention](s3-only-retention.md) and import the existing point versions through the reviewed [legacy inventory](s3-only-legacy-import.md). Unknown versions are retained for repair and prevent retirement rather than being guessed away.

## Controller inputs and off-state

Use the same owner-only version-3 workstation policy and project-local tools as [on-demand host sessions](on-demand-host.md). Source binding includes the controller, provenance, receipt/store/restore/retention helpers and authored runtime code. AWS operational calls use a temporary assumption of the reviewed operator role from the configured source profile. The policy pins the canonical regular AWS launcher and Session Manager plugin with SHA-256 and device/inode identity. Source-role, receipt/lifecycle and tunnel calls recheck their exact executables after credential refresh; Python import overrides are removed. The SSM tunnel gives the pinned plugin directory priority, then discovers the project-local `.tools/bin/session-manager-plugin` before existing installed-tool paths; it changes no global configuration.

The direct private schema-2 deployment output has `data_ownership="session"`. Off-state `host_id`, `data_volume_id` and `data_filesystem_uuid` are null, with `recovery_receipt_ids` naming the exact current receipt for every configured instance. This mapping is mandatory and is verified against S3; an empty directory or catalog latest never becomes recovery authority. Keep one durable mode-0700 journal directory on the designated workstation, containing operation state, `receipt-<id>.json`, `current-<instance>.json`, legacy inventory and retirement ledgers across operations.

Run the unchanged operator interface with the approved policy checksum:

```sh
python3 scripts/minecraft_host.py --deployment /absolute/private/deployment.json \
  --lifecycle /absolute/private/lifecycle.json --policy-sha256 "$approved_policy_sha256" \
  stop

python3 scripts/minecraft_host.py --deployment /absolute/private/deployment.json \
  --lifecycle /absolute/private/lifecycle.json --policy-sha256 "$approved_policy_sha256" \
  start --instance survival
```

Inject the Cloudflare token through the existing one-command password-manager workflow. The game, API responses and diagnostics never receive raw credential values.

## Stop and deletion

The full workstation lock covers installed-release attestation, private drain, independent marker/archive download and complete inventory inspection, durable receipt/current selection publication with the current private boot/session snapshot rechecked through fresh SSM before and after verification, protection/tag readback and bounded retirement. It preserves the latest fourteen completed points, current selections and active operation receipt versions. Only after that proof does the controller request a graceful EC2 stop and confirm stopped state so Terraform can safely detach storage.

The actual saved removal plan can delete only the recorded session host, orderly attachment and session data address. It checks physical IDs and original state, rejects updates/replacements or protected changes, and pins source/config/tool/backend/private inputs. Immediately before every removal apply, including an interrupted retry, a read-only replay check requires the selected exact versions to retain their existing keep tags, unchanged current selector, complete inventory and actual lifecycle expiry exclusion. Missing or retired protection refuses removal without retagging or replacing it. After apply, independent exact-ID EC2 queries must prove host termination/absence and both recorded root/data disks absent. Only then does the controller publish the null-data handoff and confirm parked DNS. A failed receipt, lifecycle policy, tag, retirement or drain leaves the source disk and host available. A retry after a persisted removal intent can reconcile only exact host/attachment/session-data deletion drift with stopped evidence and durable receipts; it still rechecks selected protection immediately before apply and proves every recorded physical resource absent afterward. Failed deletion proof retains the operation for repair; a DNS failure retains confirmed removal results.

## Start, provenance and restore

Local private JSON/encoding/RCON bundle validation runs before an operation journal or provisioning. Before provisioning and again before the saved create apply, the same read-only replay check verifies existing selected protection. The controller resolves every exact receipt/current selection and downloads/verifies marker/archive content outside the target disk. The operation records immutable receipt IDs/source storage, a distinct random target UUID and a bounded capacity-sized disk, with a 2-GiB floor and 128-GiB ceiling. Capacity includes receipt data/archive requirements and filesystem reserve. Missing, corrupt, incompatible or oversized recovery data blocks provisioning and game start.

The create plan may only create the session host, encrypted gp3 disk and orderly attachment with those recorded values. Successful apply records its actual UTC window (start floored to a second, precise end). Unknown/partial apply outcomes without a proven successful first-use window preserve resources and require explicit repair; they never create another host or disk automatically.

The read-only provenance helper independently checks exact account/role, AZ, sizes, volume/host IDs, unsnapshotted creation, one current attachment, matching CloudTrail create/launch/attach request and response evidence, raw attachment time, first boot, storage configuration, and absence of prior initialization/runtime/mount use. It keeps the mandatory 900-second wait after attachment, a bounded shared observation deadline and ten-minute first-setup certification expiry. The caller then scans every completed-marker version in the configured backup namespaces outside the disk and refuses any point naming the target volume. Missing access, incomplete inventory or malformed metadata fails closed.

Only current valid proof permits the existing guarded zero-fill and explicit initialize commands. No force-format or provenance bypass exists. On a lost initialization observation, retry can recognize only the same volume/UUID through a matching mount; an unformatted or ambiguous partial target remains for repair. Once storage is ready, bootstrap/private runtime configuration is installed through the same digest-bound loopback receiver, with no secret in Run Command or user data.

The exact private receipt is transferred as the fixed `recovery-receipt.json` file, then `minecraft-restore --receipt ... --bootstrap-sha256 ... --timeout 3000 --resume` verifies exact object versions, content/definition and complete target inventory. A retry can recognize only a complete stopped target belonging to the same receipt and target volume/UUID; partial or changed targets remain intact. All configured instance receipts restore before normal instance start. The journal records the game-start phase before invoking the normal idempotent start, so an interrupted readiness observation does not rerun restore after game startup progressed.

## Evidence and rollback

Phase two records actual command-entry-to-controller-ready time and the 900-second provenance floor. It has no 180-second promise: retain the measured longer cold-start result and separately verify endpoint and intended client availability. Offline gates prove orchestration and refusal behavior with controlled boundaries; they do not prove real SSM delivery, no live EBS disks, recovered family checkpoints or timing.

Before closing #24, prove a real no-session-EBS off-state, exact S3-only round trip with distinct disk/UUID and intact world/config/player checkpoints, failure leaving the game stopped, interrupted restore repair and measured readiness. Preserve receipts/state/recovery objects after any failure. Rollback to retained ownership requires a separately reviewed same-ID state/config handoff with protection restored; never delete a partially restored disk or replace a current selection to hide an error. If formats changed, use the independent verified recovery workflow rather than reverting binaries alone.
