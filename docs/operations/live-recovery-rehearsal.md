# Prove live persistence and recovery

Use this procedure only after issue #7 records the approved AWS account, deployment, recovery resources, and cleanup list. The commands mutate live AWS resources. Save raw output outside Git in a mode-0700 directory. Do not put account IDs, resource IDs, player data, private configuration, state, or SSM output in an issue or pull request.

Issue #8 remains incomplete until a deployed `survival` instance passes every check in this guide. Archive extraction alone does not pass.

## Prepare the evidence directory

Set these paths in one Bash or zsh session on the operator computer and keep using that session for the operator commands. Use the repository revision that issue #7 deployed.

```sh
umask 077
set -euo pipefail
repo_dir="$PWD"
evidence_dir="/absolute/private/minecraft/issue-8-$(date -u +%Y%m%dT%H%M%SZ)"
source_dir="/absolute/private/minecraft/family"
source_deployment="$source_dir/deployment.json"
recovery_dir="$evidence_dir/infra/recovery-rehearsal"
mkdir -p "$evidence_dir" "$recovery_dir"
git rev-parse HEAD >"$evidence_dir/git-revision.txt"
git status --short --branch >"$evidence_dir/git-status.txt"
python3 scripts/bootstrap_tools.py
.tools/bin/task check 2>&1 | tee "$evidence_dir/repository-check.txt"
```

Prepare private backend configuration in the work area. The creation commands below copy `infra/recovery-rehearsal` and its sibling `scripts` directory, preserving the module depth so `../../scripts` resolves to `$evidence_dir/scripts`. Start `recovery.tfvars` from `deployment.tfvars.example` and fill its restore-size fields after the final source backup below. Use the source deployment's account, region, AZ, AMI, VPC, subnet, backup bucket, and administrator-created `host_permissions_boundary_arn` as inputs. The updated operator role deletes inline policies only from roles carrying that boundary, so it cannot clean up a recovery stack created before the boundary existed; the administrator must handle that case. The recovery root must create its own temporary security group with no ingress; do not reuse the source game security group. Generate a new filesystem UUID. It must differ from the source UUID.

Record the approved scope in `authorization.txt`. Include the issue comment or approval reference, approved plan hashes, temporary resource classes, and exact cleanup boundary. Do not continue if issue #7 is open or the approved scope omits host replacement, the recovery host and disk, or cleanup.

The source deployment has a [public hostname](host-commands.md#public-hostname), so its `stop` and `start` commands below set the Cloudflare token for that one command. Without it they finish the lifecycle step but exit 1 with `dns: failed`, which ends this `set -e` session. The recovery deployment has no hostname: host commands against it never change DNS.

## Record the source baseline

Capture the source deployment and storage baseline before a mutation. Select the recovery point after the final source backup as directed below. The examples assume that `$source_deployment` is the private `deployment` output and `$source_dir/operator.json` is the installed operator configuration source.

```sh
python3 scripts/minecraft_host.py \
  --deployment "$source_deployment" status \
  | tee "$evidence_dir/source-status-before.json"

aws ec2 describe-instances \
  --instance-ids "$(jq -r .host_id "$source_deployment")" \
  >"$evidence_dir/source-instance-before.json"
aws ec2 describe-volumes \
  --volume-ids "$(jq -r .data_volume_id "$source_deployment")" \
  >"$evidence_dir/source-volume-before.json"
```

Through SSM, run these commands on the source host and retain the command IDs and private output:

```sh
sudo bash -c 'set -euo pipefail; source /etc/minecraft/storage.conf; exec /usr/local/sbin/minecraft-storage verify --volume-id "$DATA_VOLUME_ID" --filesystem-uuid "$DATA_FILESYSTEM_UUID"'
sudo /usr/local/sbin/minecraft-backup status survival \
  --config /etc/minecraft/operator.json
sudo findmnt --json --target /srv/minecraft
sudo df --block-size=1 --output=source,size,used,avail,target /srv/minecraft
sudo du --bytes --summarize /srv/minecraft/instances/survival
```

Before the tests, record recognizable checkpoints in all three dimensions. Record at least one player inventory or state checkpoint, the allowlist and operator state, the active manifest digest, and a Floodgate or plugin-state checkpoint. Keep player names, UUIDs, coordinates, and raw file content private. A player can confirm the same checkpoint for issue #9, but issue #8 still needs server-side evidence.

## Prove clean stop and start

Time the backup on the source host through SSM. This command cleanly stops `survival`, publishes the recovery point, and leaves EC2 running.

```sh
sudo /usr/bin/time -p -o /root/source-backup-time.txt \
  /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json backup survival
```

Copy `source-backup-time.txt` through SSM. Record the completed archive size from `minecraft-backup status` and `s3api head-object`. Then time the host stop and start. `minecraft_host.py` reports the host, application, and backup separately.

`cloudflare_env` is the private secret-reference file from [host commands](host-commands.md#cloudflare-token).

```sh
public_hostname="$(jq -er .public_hostname "$source_deployment")"

op run --env-file="$cloudflare_env" -- \
  /usr/bin/time -p -o "$evidence_dir/clean-stop-time.txt" \
  python3 scripts/minecraft_host.py \
  --deployment "$source_deployment" stop \
  | tee "$evidence_dir/clean-stop.json"

python3 scripts/minecraft_host.py \
  --deployment "$source_deployment" status \
  | tee "$evidence_dir/stopped-status.json"
dig +short "$public_hostname" @1.1.1.1 | tee "$evidence_dir/hostname-stopped.txt"

op run --env-file="$cloudflare_env" -- \
  /usr/bin/time -p -o "$evidence_dir/clean-start-time.txt" \
  python3 scripts/minecraft_host.py \
  --deployment "$source_deployment" start --instance survival \
  | tee "$evidence_dir/clean-start.json"
aws ec2 describe-instances \
  --instance-ids "$(jq -r .host_id "$source_deployment")" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text \
  >"$evidence_dir/source-address-after-start.txt"
dig +short "$public_hostname" @1.1.1.1 | tee "$evidence_dir/hostname-started.txt"
```

Require the stop result to show `host: stopped`, `draining: true`, `instances.survival.application: stopped`, `instances.survival.backup: complete`, and `dns: parked`. The stopped status must report the application and backup as unavailable or unverified instead of claiming fresh runtime evidence. Require the start result to show `host: running`, `application: running`, `ready: true`, and `dns: updated`.

Compare the source instance, volume, filesystem, manifest, and checkpoint evidence. The EC2 instance ID, EBS volume ID, filesystem UUID, manifest digest, and checkpoints must match the baseline. The public address changes on every start by design: the hostname must resolve to `192.0.2.1` after `stop` and to the host's current `PublicIpAddress` after `start`. Record whether that address differs from the baseline; it normally does. Allow about a minute for the 60-second TTL before each lookup.

## Replace the host and retain the data disk

Drain and stop the source host again, with the token set as above. Create a saved replacement plan from the same private infrastructure directory and state used by issue #7.

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" plan \
  -var-file="$source_dir/deployment.tfvars" \
  -replace='aws_instance.host[0]' \
  -out="$evidence_dir/host-replacement.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" show -no-color \
  "$evidence_dir/host-replacement.tfplan" \
  >"$evidence_dir/host-replacement.txt"
shasum -a 256 "$evidence_dir/host-replacement.tfplan" \
  >"$evidence_dir/host-replacement.tfplan.sha256"
```

Stop if the plan replaces or deletes the data volume, filesystem identity, backup bucket, state bucket, network, or IAM role. The plan may replace the host, its root disk, and the volume attachment. The replacement host must be planned with an auto-assigned address; stop otherwise. The text plan hides that flag when it does not change, so use the plan JSON check in [infrastructure operations](infrastructure.md#normal-replacement-and-explicit-destructive-workflow). Apply only the exact saved plan that Craig approved.

```sh
set -o pipefail || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" apply \
  "$evidence_dir/host-replacement.tfplan" \
  2>&1 | tee "$evidence_dir/host-replacement-apply.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" output -json deployment \
  >"$source_dir/deployment-after-replacement.json" || exit 1
chmod 0600 "$source_dir/deployment-after-replacement.json"
source_deployment="$source_dir/deployment-after-replacement.json"
```

Wait for SSM on the new host ID in `$source_deployment`. Verify the retained mount. Through the approved private transfer path, install this refreshed handoff on the replacement host as `/etc/minecraft/deployment.json`, a root-owned regular file with mode 0600 inside root-owned mode-0700 `/etc/minecraft`. Compare its SHA-256 with `$source_deployment` privately before running `scripts/install_runtime.sh`; the installer reads this on-host file during preflight. Install the same reviewed revision on the new root disk, restore the private operator configuration, and use `$source_deployment` for every subsequent source-host command. Time the normal host start and record the checkpoints:

```sh
op run --env-file="$cloudflare_env" -- \
  /usr/bin/time -p -o "$evidence_dir/replacement-start-time.txt" \
  python3 scripts/minecraft_host.py \
  --deployment "$source_deployment" start --instance survival \
  | tee "$evidence_dir/replacement-start.json"
```

Require a new EC2 instance ID. Require the original EBS volume ID, filesystem UUID, backup bucket, manifest digest, and checkpoint values. Require `dns: updated`, and repeat the two lookups above: the hostname must resolve to the replacement host's `PublicIpAddress`.

Before creating the isolated recovery host, perform the final source backup that will supply the restore. Wait for its completed marker, then select that completed 64-character recovery point. Record its marker and archive object keys, sizes, ETags, and current version IDs with `aws s3api head-object` and `aws s3api list-object-versions`; hash each JSON evidence file after capture. Selecting the point after the final source backup keeps it recent if retention is run before restore.

Set the selected identity in the operator shell from the final completed backup status and save a private transfer copy. Replace the placeholder before running these commands:

```sh
recovery_point="REPLACE_WITH_64_CHARACTER_COMPLETED_ID"
printf '%s\n' "$recovery_point" | grep -Eq '^[0-9a-f]{64}$' || exit 1
printf '%s\n' "$recovery_point" >"$evidence_dir/selected-recovery-point.txt"
chmod 0600 "$evidence_dir/selected-recovery-point.txt"
```

Download the selected completed marker privately from `instances/survival/completed/$recovery_point.json` in the source bucket named by `$source_deployment`. Confirm that its `status` is `complete`, its `instance` is `survival`, and its `recovery_point` matches the selection. Measure the compressed archive from `archive.size_bytes` and the uncompressed recovery set by summing `size_bytes` for every `files[]` entry with `type == "file"`. Compare the archive size with the recorded `head-object` `ContentLength`:

```sh
aws s3api get-object \
  --bucket "$(jq -r .backup_bucket_name "$source_deployment")" \
  --key "instances/survival/completed/$recovery_point.json" \
  "$evidence_dir/selected-recovery-marker.json" \
  >"$evidence_dir/selected-recovery-marker-download.json"
chmod 0600 "$evidence_dir/selected-recovery-marker.json"
jq -e --arg point "$recovery_point" \
  '.status == "complete" and .instance == "survival" and .recovery_point == $point' \
  "$evidence_dir/selected-recovery-marker.json" >/dev/null
archive_bytes="$(jq -er '.archive.size_bytes' "$evidence_dir/selected-recovery-marker.json")"
uncompressed_bytes="$(jq -er '[.files[] | select(.type == "file") | .size_bytes] | add' \
  "$evidence_dir/selected-recovery-marker.json")"
# Raise this ceiling if the reviewed update can grow the restored world's next backup.
max_backup_archive_bytes="$archive_bytes"
python3 - "$archive_bytes" "$uncompressed_bytes" "$max_backup_archive_bytes" <<'PY'
import sys
archive, uncompressed, maximum_backup = (int(value) for value in sys.argv[1:])
total = archive + uncompressed
required_bytes = (total * 11 + 9) // 10
gib = 1 << 30
data_gib = max(2, (required_bytes + gib - 1) // gib)
root_gib = 20 + (22 * maximum_backup + 10 * gib - 1) // (10 * gib)
print(f"Minimum recovery data volume GiB: {data_gib}")
print(f"Minimum recovery root volume GiB: {root_gib}")
PY
```

Record `restore_archive_size_bytes` and `restore_uncompressed_size_bytes` from these measurements in private `recovery.tfvars`. Set `recovery_data_volume_size_gib` to at least the calculated whole-GiB minimum, and increase it if other target files need space. The restore stages the compressed archive and full uncompressed data together on `/srv/minecraft`; source disk size alone is insufficient. Set `max_recovery_backup_archive_size_bytes` to an operator-reviewed ceiling at least as large as the selected archive, allowing for growth from any approved update. Set `recovery_root_volume_size_gib` to at least its calculated minimum: the root disk holds `/tmp`, where backup publication may need both a captured archive and its verification download plus 20 GiB of operating-system/workspace reserve. If the expected backup can exceed the reviewed ceiling, stop and replan. Inspect **both** disk sizes and costs in the saved recovery plan and obtain approval for that exact plan before applying it.

## Create the isolated recovery host

Start the 60-minute operator recovery timer before the first recovery infrastructure command. The disk provenance wait, zero-fill, and zero check below count toward it as named phases; record each, and never shorten them to meet the target. Initialize the recovery root with its separate backend key. Create and inspect a saved plan.

```sh
date -u +%Y-%m-%dT%H:%M:%SZ >"$evidence_dir/recovery-started.txt"
cp -R "$repo_dir/infra/recovery-rehearsal/." "$recovery_dir/"
mkdir -p "$evidence_dir/scripts"
cp -R "$repo_dir/scripts/." "$evidence_dir/scripts/"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" init \
  -input=false -lockfile=readonly \
  -backend-config="$evidence_dir/recovery.backend.hcl"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" plan \
  -var-file="$evidence_dir/recovery.tfvars" \
  -out="$evidence_dir/recovery-create.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" show -no-color \
  "$evidence_dir/recovery-create.tfplan" \
  >"$evidence_dir/recovery-create.txt"
shasum -a 256 "$evidence_dir/recovery-create.tfplan" \
  >"$evidence_dir/recovery-create.tfplan.sha256"
```

The plan may create only the temporary host, root disk, data disk, attachment, temporary no-ingress security group and its reviewed outbound web rules, instance role and profile, inline policies, and temporary backup bucket with its protection settings. The recovery host takes its own auto-assigned public IPv4 address; the plan creates no Elastic IP. The source VPC, subnet, bucket, data disk, host, and state are inputs; the source game security group must not be attached to the recovery host. The recovery security group must have no ingress and only the approved TCP 80/443 egress needed for package, AWS API, and SSM access. Stop if the plan manages any source resource. Source S3 access must remain read-only. Write, delete, and multipart access must name only the temporary recovery bucket.

Apply only the exact saved plan that Craig approved. Record its UTC window and caller for the disk provenance check, and save both sensitive outputs with mode 0600.

```sh
set -o pipefail || exit 1
caller_arn="$(aws sts get-caller-identity --query Arn --output text)" || exit 1
printf 'apply-start %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$caller_arn" \
  >>"$evidence_dir/recovery-apply-window.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" apply \
  "$evidence_dir/recovery-create.tfplan" \
  2>&1 | tee "$evidence_dir/recovery-create-apply.txt" || exit 1
printf 'apply-end %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >>"$evidence_dir/recovery-apply-window.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" output -json deployment \
  >"$evidence_dir/recovery-deployment.json" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" output -json cleanup_inventory \
  >"$evidence_dir/recovery-cleanup-inventory.json" || exit 1
chmod 0600 "$evidence_dir/recovery-deployment.json" \
  "$evidence_dir/recovery-cleanup-inventory.json"
```

Cloud-init waits up to 300 seconds for the host's public IPv4 address to appear through IMDSv2 before package installation. The address is assigned at launch, so the wait normally passes at once. If it fails, inspect the instance's public address and the subnet route rather than bypassing bootstrap. The first cloud-init run then stops at the blank-disk storage gate. Through SSM, read `DATA_VOLUME_ID` and `DATA_FILESYSTEM_UUID` from root-only `/etc/minecraft/storage.conf` and compare both with the approved recovery deployment output. Stop if either value differs. A new encrypted EBS volume does not read back as zeros, so it must be zero-filled. This is first setup: the recovery apply created both this volume and this host. Apply every zero-fill rule in [infrastructure operations](infrastructure.md#disk-setup-and-readiness-handoff-to-5-and-7), using `data_volume_id`, `host_id`, and `backup_bucket_name` from `recovery-deployment.json` and the newest `apply-start`/`apply-end` pair in `recovery-apply-window.txt` (a recreation appends a new pair; earlier pairs never qualify).

Wait at least 15 minutes after the recovery volume's `AttachTime` before trusting an empty or partial history. This provenance wait runs from `apply-end` to the passing provenance section. Then, from the operator computer, append a timestamped provenance section and review it:

```sh
set -o pipefail || exit 1
recovery_volume_id="$(jq -er .data_volume_id "$evidence_dir/recovery-deployment.json")" || exit 1
recovery_host_id="$(jq -er .host_id "$evidence_dir/recovery-deployment.json")" || exit 1
recovery_bucket="$(jq -er .backup_bucket_name "$evidence_dir/recovery-deployment.json")" || exit 1
provenance="$evidence_dir/recovery-disk-provenance.txt"
export TZ=UTC
{
  printf '== %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" &&
    aws ec2 describe-volumes --volume-ids "$recovery_volume_id" --output json \
      --query 'Volumes[0].{Snapshot:SnapshotId,Source:SourceVolumeId,Created:CreateTime,Attachments:Attachments[].[InstanceId,AttachTime]}' &&
    aws cloudtrail lookup-events --output text \
      --lookup-attributes AttributeKey=ResourceName,AttributeValue="$recovery_volume_id" \
      --query 'Events[].[EventTime,EventName,ReadOnly,Username,EventId,Resources[].ResourceName]' &&
    aws cloudtrail lookup-events --output text \
      --lookup-attributes AttributeKey=ResourceName,AttributeValue="$recovery_host_id" \
      --query "Events[?ReadOnly=='false'].[EventTime,EventName,Username,EventId,Resources[].ResourceName]" &&
    printf 'completed recovery point source volumes, all marker versions:\n' &&
    aws s3api list-object-versions --bucket "$recovery_bucket" --prefix instances/ \
      --query 'Versions[].[Key,VersionId]' --output json |
    jq -r '.[]? | select(.[0] | test("/completed/[0-9a-f]{64}[.]json$")) | @tsv' |
    while IFS=$'\t' read -r key version; do
      aws s3api get-object --bucket "$recovery_bucket" --key "$key" --version-id "$version" \
        "$evidence_dir/recovery-marker.json" </dev/null >/dev/null &&
        jq -er .source.volume_id "$evidence_dir/recovery-marker.json" || exit 1
    done
} >>"$provenance" || exit 1
cat "$provenance"
```

Stop if any command fails or is denied. In the newest section, require an empty `Snapshot` and `Source`, a `Created` time inside the newest `apply-start`/`apply-end` pair in `recovery-apply-window.txt`, with the recovery host's `RunInstances` in the same pair, and one attachment, to the recovery host, whose `AttachTime` equals, to the second, `responseElements.attachTime` from that apply's raw `AttachVolume` response with matching volume/instance IDs; use the [response-time normalization](infrastructure.md#disk-setup-and-readiness-handoff-to-5-and-7), not request-completion `EventTime`; exactly one `CreateVolume` inside that window, with the `Username` of the recovery host's single `RunInstances` event; at least one `AttachVolume`, each to the recovery host, with the apply's own `AttachVolume` found in the volume or host listing; among the host's write events, no root-disk replacement, `ModifyInstanceAttribute`, or other attachment of the recovery volume; no other volume event whose `ReadOnly` is `false`; and no listed source volume equal to the recovery volume. If an expected event is missing, wait and rerun the block, which appends a new section; if it stays missing, stop. For an `AttachVolume` without an instance resource, use the event-ID lookup in infrastructure operations and keep the raw event private.

Through SSM, run the host-side check from infrastructure operations on the recovery host and retain its command ID and private output. Any evidence of prior storage or runtime use is a stop. Transfer the deployment handoffs and install the runtime only after initialization below. Immediately before zero-fill, rerun the provenance block and the host-side check together and require the same single attachment with an identical `AttachTime`; if more than 10 minutes pass before zero-fill starts, rerun both again.

The recovery disk is sized from the marker, so compute its zero-fill and zero-check bounds from that size. Session Manager ends an interactive session after 20 idle minutes by default, so run both as transient systemd units, as below, or through Run Command with `executionTimeout` above the combined bounds of the commands in that invocation. Then, through SSM, zero-fill and initialize that exact disk:

```sh
# Set both from the reviewed recovery-deployment.json, the volume the provenance block passed.
expected_volume_id='<recovery data_volume_id>'
expected_filesystem_uuid='<recovery data_filesystem_uuid>'
for unit in zero-fill initialize; do
  sudo env EXPECTED_VOLUME_ID="$expected_volume_id" EXPECTED_FILESYSTEM_UUID="$expected_filesystem_uuid" UNIT="$unit" \
    bash -c 'set -euo pipefail; source /etc/minecraft/storage.conf
      [[ "$DATA_VOLUME_ID" == "$EXPECTED_VOLUME_ID" && "$DATA_FILESYSTEM_UUID" == "$EXPECTED_FILESYSTEM_UUID" ]] ||
        { echo "storage.conf does not match the reviewed recovery output: stop" >&2; exit 1; }
      exec systemd-run --unit="minecraft-storage-$UNIT" --collect --wait -p Type=oneshot /usr/local/sbin/minecraft-storage "$UNIT" \
        --volume-id "$DATA_VOLUME_ID" --confirm-volume-id "$EXPECTED_VOLUME_ID" --filesystem-uuid "$EXPECTED_FILESYSTEM_UUID"' || exit 1
done
sudo systemctl restart minecraft-storage.service || exit 1
sudo systemctl start docker.service || exit 1
```

Each unit keeps running if the session is lost, and `--collect` removes it once it exits. After reconnecting, wait while a unit is active, then read both outcomes from the journal and save them privately. Their start and finish times are the zero-fill and zero-check phase times:

```sh
systemctl status minecraft-storage-zero-fill.service minecraft-storage-initialize.service --no-pager
sudo journalctl -u minecraft-storage-zero-fill.service -u minecraft-storage-initialize.service \
  -o short-iso --no-pager
```

Require `disk zero-filled; run initialize separately` before initialize, and `filesystem initialized or already matches; run mount separately` before mounting. If the session was lost, set `expected_volume_id` and `expected_filesystem_uuid` again from the reviewed output, then rerun the loop only for the units that have not succeeded (`for unit in initialize` after a successful zero-fill; rerunning zero-fill after it succeeded is safe but slow, and after initialize it refuses), and continue with the storage and Docker commands. A timeout is a failure, never a reason to force-format. A timed-out zero-fill can return late and leave the disk partly zeroed: wait for its unit to exit and rerun zero-fill to completion. Initialize's whole-device check remains the gate.

Before installing the runtime, transfer `$evidence_dir/recovery-deployment.json` to `/etc/minecraft/deployment.json` on the recovery host, the refreshed `$source_deployment` to `/etc/minecraft/source-deployment.json`, and `$evidence_dir/selected-recovery-point.txt` to `/etc/minecraft/recovery-point`. All three must be root-owned regular files with mode 0600 inside root-owned mode-0700 `/etc/minecraft`; compare each on-host SHA-256 with its private operator copy. Install a reviewed AWS CLI v2 with conditional object/multipart upload and SHA-256 support first, and confirm `aws --version` reports `aws-cli/2`. Then follow [runtime installation](runtime-installation.md): `scripts/install_runtime.sh` checks CLI capabilities and reads the target deployment file during preflight. Supply a fresh RCON secret and recorded EULA acceptance in target-private configuration. Configure the target operator file from the recovery deployment output and stage the selected point's original definition and manifest as described in [step 3 of the isolated restore procedure](backup-recovery.md#restore-onto-an-isolated-host). Its backup destination is the temporary recovery bucket. The restore command reads the source bucket only through `--source-deployment`.

Before an update or clean stop backs up the restored world, verify that Python's temporary directory is `/tmp` on the sized replaceable root disk and that its available bytes can hold two copies of the reviewed maximum backup archive with 10 percent headroom. A different `TMPDIR`, tmpfs, or insufficient free space invalidates the root-disk plan; stop and revise it. Repeat this check after recreating the recovery host.

```sh
sudo python3 -c 'import tempfile; print(tempfile.gettempdir())'
findmnt --target /tmp
df --block-size=1 --output=source,size,avail,target /tmp
```

## Restore and verify the selected recovery point

On the recovery host, run:

```sh
recovery_point="$(sudo cat /etc/minecraft/recovery-point)" || exit 1
printf '%s\n' "$recovery_point" | grep -Eq '^[0-9a-f]{64}$' || exit 1
sudo /usr/bin/time -p -o /root/restore-time.txt \
  /usr/local/sbin/minecraft-restore \
  --config /etc/minecraft/operator.json \
  --source-deployment /etc/minecraft/source-deployment.json \
  survival "$recovery_point"

sudo /usr/bin/time -p -o /root/restored-start-time.txt \
  /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json start survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json status survival
sudo df --block-size=1 --output=source,size,used,avail,target /srv/minecraft
sudo du --bytes --summarize /srv/minecraft/instances/survival
```

Copy the timing and storage output into the private evidence directory through SSM. Verify the active manifest digest and every recorded world, player, access, Floodgate, and plugin checkpoint after Minecraft reports ready. Record the UTC time of the last confirmed checkpoint. Compute total operator recovery time from the initial timestamp to that confirmation, reporting the provenance wait, zero-fill, and zero check as separate phases. If the total exceeds 60 minutes, report the measured time and the slowest phase; do not shorten those phases to meet the target.

Repeat `head-object` and `list-object-versions` for the selected source marker and archive. Their version IDs, sizes, and ETags must match the baseline.

## Rehearse update recovery on the isolated host

Use the current pinned manifest for an idempotent update rehearsal unless Craig separately approves a compatible update candidate. Run the supported update command, verify readiness, and compare the checkpoints.

```sh
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json update survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json status survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json stop survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json status survival
```

Require the target stop to finish with `application: stopped` and `backup: complete`; if it fails, do not destroy the running recovery host. Do not infer rollback from reinstalling old binaries. To test recovery from an update, remove and recreate only the approved temporary recovery resources. Verify the first removal against its cleanup inventory using the checks below, and retain that inventory and evidence privately. Record the recreation apply's window in `recovery-apply-window.txt` as above, and capture the new recovery deployment output and a new `recovery-cleanup-inventory.json` with mode 0600 for the recreated resources. After the new root host is ready, zero-fill, initialize, and mount the new blank target disk using the same guarded sequence above, including the provenance wait and the provenance and host-side checks for the new volume and host. Transfer and verify the new `/etc/minecraft/deployment.json`, refreshed `/etc/minecraft/source-deployment.json`, and selected `/etc/minecraft/recovery-point` handoffs as described above. Install the reviewed AWS CLI v2 before repeating runtime installation. Restage a fresh RCON secret, recorded EULA acceptance, and target `/etc/minecraft/operator.json` configuration from the new recovery deployment output. Restage the selected point's original definition and manifest using the isolated restore procedure linked above. Repeat the restore command block with the staged identity on the new host, then verify its recorded manifest and checkpoints again. Never run this rehearsal against the family data directory.

## Prove failed backups remain visible

After the update rehearsal and checkpoint verification, remove only the temporary backup write policy. Save and inspect the plan before applying it. The source read policy, host, disk, and both buckets must remain unchanged.

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" plan \
  -var-file="$evidence_dir/recovery.tfvars" \
  -var=backup_writes_enabled=false \
  -out="$evidence_dir/recovery-disable-backups.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" show -no-color \
  "$evidence_dir/recovery-disable-backups.tfplan" \
  >"$evidence_dir/recovery-disable-backups.txt"
shasum -a 256 "$evidence_dir/recovery-disable-backups.tfplan" \
  >"$evidence_dir/recovery-disable-backups.tfplan.sha256"
set -o pipefail || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" apply \
  "$evidence_dir/recovery-disable-backups.tfplan" \
  2>&1 | tee "$evidence_dir/recovery-disable-backups-apply.txt" || exit 1
```

Wait for the instance-profile credentials and IAM policy change to take effect. Confirm through SSM that `s3api put-object` to a unique key in the temporary recovery prefix receives `AccessDenied`. Do not test against the source bucket. Then stop the restored instance. The backup hook must fail after the clean application stop.

```sh
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json stop survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json status survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json start survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json host-drain
```

Require all four commands to preserve the failed state:

- `stop` exits nonzero after Minecraft exits cleanly.
- `status` reports `application: stopped` and `backup: failed`.
- `start` exits nonzero because the previous session lacks a completed backup.
- `host-drain` exits nonzero and does not certify safe EC2 shutdown.

Inspect private SSM output for the expected S3 authorization failure. Do not edit `operation.json`. Verify again that no source S3 object or version changed. The temporary recovery bucket may contain the successful backup made by the update rehearsal.

## Record stopped costs and clean up

Stop the source host through `minecraft_host.py --deployment "$source_deployment" stop`, with the token set as above, and require `dns: parked`. Inventory the resources that continue to incur charges while compute is stopped:

- the source root and retained data EBS volumes;
- current and noncurrent S3 backup objects;
- the versioned state bucket and lock objects;
- the account budget resource.

A stopped host has no public IPv4 address and no address charge. Confirm that `aws ec2 describe-addresses` lists no project Elastic IP. Record actual sizes and current region pricing. Do not copy the estimates from the design as measured cost evidence.

Compare the recovery state with `recovery-cleanup-inventory.json`. Create a destroy plan and inspect every resource address and ID.

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" plan -destroy \
  -var-file="$evidence_dir/recovery.tfvars" \
  -var=backup_writes_enabled=false \
  -out="$evidence_dir/recovery-destroy.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" show -no-color \
  "$evidence_dir/recovery-destroy.tfplan" \
  >"$evidence_dir/recovery-destroy.txt"
shasum -a 256 "$evidence_dir/recovery-destroy.tfplan" \
  >"$evidence_dir/recovery-destroy.tfplan.sha256"
```

The plan must delete only resources in the approved cleanup inventory, including the temporary no-ingress security group and its outbound rules. Apply only the saved destroy plan that Craig approved.

```sh
set -o pipefail || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" apply \
  "$evidence_dir/recovery-destroy.tfplan" \
  2>&1 | tee "$evidence_dir/recovery-destroy-apply.txt" || exit 1
```

Check every entry in the private cleanup inventory against AWS after destroy, and save each response or exact not-found error with its exit status in the evidence directory. Query these IDs and names from `$evidence_dir/recovery-cleanup-inventory.json`:

| Temporary resource | AWS query | Required result |
| --- | --- | --- |
| Host | `aws ec2 describe-instances --instance-ids <host_id>` | Absent or `terminated`; no running or stopped instance. |
| Root and data disks | `aws ec2 describe-volumes --volume-ids <root_volume_id> <data_volume_id>` | Both absent. Check each ID separately if the combined request fails. |
| No-ingress security group and its rules | `aws ec2 describe-security-groups --group-ids <security_group_id>` | Group absent; its ingress and egress rules cannot remain attached. |
| Temporary backup bucket and all versions | `aws s3api head-bucket --bucket <backup_bucket_name>` | Bucket absent (`404` or `NoSuchBucket`); `403` is inconclusive. |
| Instance profile | `aws iam get-instance-profile --instance-profile-name <instance_profile_name>` | `NoSuchEntity`. |
| Role and inline policies | `aws iam get-role --role-name <iam_role_name>` and `aws iam get-role-policy --role-name <iam_role_name> --policy-name <name>` for `session-manager`, `source-backup-read-only`, `temporary-recovery-backup-read`, and `temporary-recovery-backup-writes` | `NoSuchEntity` for the role and every policy. The write policy may already have been removed by the failed-backup test. |

An authorization error, timeout, or stale result is not proof of cleanup. Also require the recovery OpenTofu state to contain no resource addresses:

```sh
if ! "$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" state list \
  >"$evidence_dir/recovery-state-after-destroy.txt" \
  2>"$evidence_dir/recovery-state-after-destroy-error.txt"; then
  printf 'Recovery state could not be inspected; cleanup is unverified.\n' >&2
  exit 1
fi
if test -s "$evidence_dir/recovery-state-after-destroy.txt"; then
  printf 'Recovery state still contains resources; cleanup is incomplete.\n' >&2
  exit 1
fi
```

Do not report cleanup as successful until every query and the state check pass. Then verify that the source host, source retained volume, state, bucket, and selected recovery point remain intact, and that the hostname's record is unchanged by the rehearsal host.

## Confirm source-bucket lifecycle expiry

The retained source backup bucket has the lifecycle rule; the temporary recovery bucket is destroyed with the recovery host and cannot supply delayed evidence. Use operator credentials for these checks, not the recovery host's source-read-only role. Keep the recovery resources destroyed while waiting. Record the source bucket's versioning and effective lifecycle configuration privately:

```sh
source_bucket="$(jq -er .backup_bucket_name "$source_deployment")" || exit 1
if ! aws s3api get-bucket-versioning --bucket "$source_bucket" \
  >"$evidence_dir/source-bucket-versioning.json"; then exit 1; fi
if ! aws s3api get-bucket-lifecycle-configuration --bucket "$source_bucket" \
  >"$evidence_dir/source-bucket-lifecycle.json"; then exit 1; fi
if ! jq -e '.Status == "Enabled"' \
  "$evidence_dir/source-bucket-versioning.json" >/dev/null; then exit 1; fi
if ! jq -e '
  ([.Rules[] | select(.Status == "Enabled")] | length == 1) and
  ([.Rules[] | select(
    .ID == "remove-superseded-versions-and-incomplete-uploads" and
    .Status == "Enabled" and
    ((.Filter == null and (.Prefix == null or .Prefix == "")) or
     .Filter == {} or .Filter == {"Prefix":""}) and
    .NoncurrentVersionExpiration.NoncurrentDays == 14 and
    .NoncurrentVersionExpiration.NewerNoncurrentVersions == null and
    .AbortIncompleteMultipartUpload.DaysAfterInitiation == 1 and
    .Expiration == null and (.Transitions == null or .Transitions == []) and
    (.NoncurrentVersionTransitions == null or .NoncurrentVersionTransitions == [])
  )] | length == 1)' "$evidence_dir/source-bucket-lifecycle.json" >/dev/null; then exit 1; fi
```

Configuration proves that the rule is installed, not that S3 has carried out either action. For each action, first look for a previously recorded, safe candidate: an exact noncurrent version ID with its successor version or delete-marker ID and successor creation time, or an already abandoned multipart upload with its exact upload ID and initiation time. Save successful `list-object-versions --prefix <exact-key>` or `list-multipart-uploads --prefix <exact-key>` output before waiting. Do not select an active production upload or infer noncurrent age from the old version's `LastModified`. Record both candidates in a mode-0600 `$evidence_dir/lifecycle-targets.json` with the source `bucket`, `noncurrent` fields `key`, `version_id`, `successor_version_id`, and `successor_created_at`, and `multipart` fields `key`, `upload_id`, and `initiated_at`. A candidate that was not observed before expiry cannot prove an actual lifecycle deletion.

If either safe candidate is unavailable, obtain separate concrete approval before creating a complete probe in the **retained source bucket**, outside `instances/`. Add the approval reference, unique `lifecycle-probe/<run-id>/` prefix, exact allowed S3 write/list/delete/abort actions, cost bound, delayed observation period, and cleanup of exact version and upload IDs to `authorization.txt`. The recovery role must not gain source write access. A probe adds S3 request and object storage charges, including one 5 MiB multipart part until S3 aborts it; use [current S3 pricing](https://aws.amazon.com/s3/pricing/) for the approved estimate. Do not keep EC2, EBS, or the temporary bucket for the 14-day observation. If this extra scope is not approved, leave actual expiry **pending** and do not claim Issue #8 complete.

For an approved probe, use a fresh prefix and save every response privately. The two writes to the same tiny key make the first exact version noncurrent; the incomplete upload has one part and is never completed or manually aborted before the observation:

```sh
probe_prefix="lifecycle-probe/$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import uuid; print(uuid.uuid4())')/" || exit 1
version_key="${probe_prefix}noncurrent.txt"
upload_key="${probe_prefix}incomplete.bin"
printf 'first\n' >"$evidence_dir/lifecycle-first.txt" || exit 1
printf 'second\n' >"$evidence_dir/lifecycle-second.txt" || exit 1
aws s3api put-object --output json --bucket "$source_bucket" --key "$version_key" \
  --body "$evidence_dir/lifecycle-first.txt" --if-none-match '*' \
  >"$evidence_dir/lifecycle-first-put.json" || exit 1
aws s3api put-object --output json --bucket "$source_bucket" --key "$version_key" \
  --body "$evidence_dir/lifecycle-second.txt" \
  >"$evidence_dir/lifecycle-second-put.json" || exit 1
aws s3api list-object-versions --output json --bucket "$source_bucket" --prefix "$version_key" \
  >"$evidence_dir/lifecycle-versions-before.json" || exit 1

dd if=/dev/zero of="$evidence_dir/lifecycle-part.bin" bs=1048576 count=5 || exit 1
aws s3api create-multipart-upload --output json --bucket "$source_bucket" --key "$upload_key" \
  >"$evidence_dir/lifecycle-upload-create.json" || exit 1
upload_id="$(jq -er '.UploadId | select(type == "string" and length > 0)' \
  "$evidence_dir/lifecycle-upload-create.json")" || exit 1
aws s3api upload-part --output json --bucket "$source_bucket" --key "$upload_key" \
  --upload-id "$upload_id" --part-number 1 \
  --body "$evidence_dir/lifecycle-part.bin" \
  >"$evidence_dir/lifecycle-part-upload.json" || exit 1
aws s3api list-parts --output json --bucket "$source_bucket" --key "$upload_key" \
  --upload-id "$upload_id" >"$evidence_dir/lifecycle-parts-before.json" || exit 1
aws s3api list-multipart-uploads --output json --bucket "$source_bucket" --prefix "$upload_key" \
  >"$evidence_dir/lifecycle-uploads-before.json" || exit 1
```

Confirm the first version is listed as noncurrent, the second as current, and the upload ID has one 5 MiB part. Save both exact version IDs and S3-reported successor `LastModified` and upload `Initiated` timestamps; local command time is not the lifecycle clock:

```sh
old_version_id="$(jq -er '.VersionId | select(type == "string" and length > 0)' \
  "$evidence_dir/lifecycle-first-put.json")" || exit 1
successor_version_id="$(jq -er '.VersionId | select(type == "string" and length > 0)' \
  "$evidence_dir/lifecycle-second-put.json")" || exit 1
test "$old_version_id" != "$successor_version_id" || exit 1
jq -e --arg key "$version_key" --arg old "$old_version_id" \
  --arg current "$successor_version_id" \
  '([.Versions[]? | select(.Key == $key and .VersionId == $old and .IsLatest == false)] | length == 1) and
   ([.Versions[]? | select(.Key == $key and .VersionId == $current and .IsLatest == true)] | length == 1)' \
  "$evidence_dir/lifecycle-versions-before.json" >/dev/null || exit 1
jq -e --arg key "$upload_key" --arg id "$upload_id" \
  '.Key == $key and .UploadId == $id and
   ([.Parts[]? | select(.PartNumber == 1 and .Size == 5242880)] | length == 1)' \
  "$evidence_dir/lifecycle-parts-before.json" >/dev/null || exit 1
jq -e --arg key "$upload_key" --arg id "$upload_id" \
  '[.Uploads[]? | select(.Key == $key and .UploadId == $id and
    (.Initiated | type == "string") and (.Initiated | length > 0))] | length == 1' \
  "$evidence_dir/lifecycle-uploads-before.json" >/dev/null || exit 1
successor_at="$(jq -er --arg id "$successor_version_id" \
  '.Versions[] | select(.VersionId == $id) | .LastModified |
   select(type == "string" and length > 0)' \
  "$evidence_dir/lifecycle-versions-before.json")" || exit 1
initiated_at="$(jq -er --arg key "$upload_key" --arg id "$upload_id" \
  '.Uploads[] | select(.Key == $key and .UploadId == $id) | .Initiated |
   select(type == "string" and length > 0)' \
  "$evidence_dir/lifecycle-uploads-before.json")" || exit 1
jq -n --arg bucket "$source_bucket" --arg version_key "$version_key" \
  --arg old "$old_version_id" --arg successor "$successor_version_id" \
  --arg successor_at "$successor_at" --arg upload_key "$upload_key" \
  --arg upload_id "$upload_id" --arg initiated_at "$initiated_at" \
  '{bucket:$bucket, origin:"approved-probe",
    noncurrent:{key:$version_key, version_id:$old, successor_version_id:$successor,
                successor_created_at:$successor_at},
    multipart:{key:$upload_key, upload_id:$upload_id, initiated_at:$initiated_at}}' \
  >"$evidence_dir/lifecycle-targets.json" || exit 1
chmod 0600 "$evidence_dir/lifecycle-targets.json" || exit 1
```

Recheck the selected source recovery point's marker and archive key, current version IDs, sizes, and ETags against the pre-probe evidence. The probe must never write or delete an `instances/` key.

[S3 calculates noncurrent age from the successor's creation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro-lifecycle-rules.html), adds 14 days, then rounds eligibility up to the next midnight UTC. An incomplete upload becomes [eligible for abort after one day](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html) from initiation. [Lifecycle actions are asynchronous](https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-lifecycle.html): eligibility is not proof of deletion. Record both eligibility times. On each follow-up, restore the private `$evidence_dir` and `$source_deployment` paths, require the recorded target bucket to match the deployment, and run the corresponding check only after its eligibility time. First check the incomplete upload with successful, fully paginated listings; an upload that was completed also disappears from the multipart listing but leaves an object version:

```sh
lifecycle_targets="$evidence_dir/lifecycle-targets.json"
source_bucket="$(jq -er '.bucket | select(type == "string" and length > 0)' \
  "$lifecycle_targets")" || exit 1
expected_source_bucket="$(jq -er '.backup_bucket_name | select(type == "string" and length > 0)' \
  "$source_deployment")" || exit 1
if test "$source_bucket" != "$expected_source_bucket"; then
  printf 'Lifecycle target bucket differs from the source deployment.\n' >&2
  exit 1
fi
upload_key="$(jq -er '.multipart.key | select(type == "string" and length > 0)' \
  "$lifecycle_targets")" || exit 1
upload_id="$(jq -er '.multipart.upload_id | select(type == "string" and length > 0)' \
  "$lifecycle_targets")" || exit 1
if ! aws s3api list-multipart-uploads --bucket "$source_bucket" \
  --prefix "$upload_key" --output json >"$evidence_dir/lifecycle-uploads-after.json"; then
  printf 'Multipart lifecycle listing failed; result unverified.\n' >&2
  exit 1
fi
if ! jq -e 'type == "object" and (.Uploads == null or (.Uploads | type == "array"))' \
  "$evidence_dir/lifecycle-uploads-after.json" >/dev/null; then
  printf 'Multipart lifecycle listing invalid; result unverified.\n' >&2
  exit 1
fi
if ! aws s3api list-object-versions --bucket "$source_bucket" \
  --prefix "$upload_key" --output json >"$evidence_dir/lifecycle-upload-versions-after.json"; then
  printf 'Multipart object-version listing failed; result unverified.\n' >&2
  exit 1
fi
if ! jq -e --arg key "$upload_key" '
  type == "object" and
  ([(.Versions[]?, .DeleteMarkers[]?) | select(.Key == $key)] | length == 0)' \
  "$evidence_dir/lifecycle-upload-versions-after.json" >/dev/null; then
  printf 'Multipart key has an object or delete marker; abort result unverified.\n' >&2
  exit 1
fi
if jq -e --arg key "$upload_key" --arg id "$upload_id" \
  '[.Uploads[]? | select(.Key == $key and .UploadId == $id)] | length == 0' \
  "$evidence_dir/lifecycle-uploads-after.json" >/dev/null; then
  printf '%s multipart observed absent\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$evidence_dir/lifecycle-multipart-status.txt"
else
  printf '%s multipart pending\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$evidence_dir/lifecycle-multipart-status.txt"
  exit 1
fi
```

After the noncurrent version becomes eligible, separately check its exact ID:

```sh
lifecycle_targets="$evidence_dir/lifecycle-targets.json"
source_bucket="$(jq -er '.bucket | select(type == "string" and length > 0)' \
  "$lifecycle_targets")" || exit 1
expected_source_bucket="$(jq -er '.backup_bucket_name | select(type == "string" and length > 0)' \
  "$source_deployment")" || exit 1
if test "$source_bucket" != "$expected_source_bucket"; then
  printf 'Lifecycle target bucket differs from the source deployment.\n' >&2
  exit 1
fi
version_key="$(jq -er '.noncurrent.key | select(type == "string" and length > 0)' \
  "$lifecycle_targets")" || exit 1
old_version_id="$(jq -er '.noncurrent.version_id | select(type == "string" and length > 0)' \
  "$lifecycle_targets")" || exit 1
if ! aws s3api list-object-versions --bucket "$source_bucket" \
  --prefix "$version_key" --output json >"$evidence_dir/lifecycle-versions-after.json"; then
  printf 'Version lifecycle listing failed; result unverified.\n' >&2
  exit 1
fi
if ! jq -e 'type == "object" and (.Versions == null or (.Versions | type == "array"))' \
  "$evidence_dir/lifecycle-versions-after.json" >/dev/null; then
  printf 'Version lifecycle listing invalid; result unverified.\n' >&2
  exit 1
fi
if jq -e --arg key "$version_key" --arg id "$old_version_id" \
  '[.Versions[]? | select(.Key == $key and .VersionId == $id)] | length == 0' \
  "$evidence_dir/lifecycle-versions-after.json" >/dev/null; then
  printf '%s noncurrent version observed absent\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$evidence_dir/lifecycle-noncurrent-status.txt"
else
  printf '%s noncurrent version pending\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$evidence_dir/lifecycle-noncurrent-status.txt"
  exit 1
fi
```

If either ID is still present, record **pending**, allow S3 more time, and repeat that action's read-only check. An authorization error, failed/incomplete listing, missing before snapshot, or manual delete/abort is not lifecycle success. If the observation cannot be completed, record an exception and manually clean up only approved probe resources by exact version IDs and upload ID; do not count that cleanup as lifecycle expiry. After successful observation, also remove the probe's surviving current version by its exact version ID and verify the probe prefix has no versions, delete markers, or incomplete uploads. Never clean up a pre-existing backup version as part of the probe. Verify that source backup objects and the current completed recovery point remain intact, allowing for separately recorded normal backup/retention activity during the wait. Keep the private before/after listings, exact IDs, UTC times, approval, and cleanup receipts; report only redacted outcomes.

## Publish the redacted result

Add a dated result below after the live run. Keep identifiers and player data in the private evidence directory. Include hashes of the private evidence index so the result remains tied to the tested revision.

Record these fields:

- Git revision and UTC test interval;
- clean stop, backup, clean start, replacement start, provenance wait, zero-fill, zero check, restore, and total recovery times;
- compressed backup bytes, restored bytes, and source and target disk use;
- old and new host distinction plus unchanged EBS, filesystem, manifest, and checkpoint results;
- hostname results: parked on `192.0.2.1` after each stop, and on the host's current address after each start and after replacement;
- recovery-disk provenance and host-side check results;
- failed-backup result and blocked restart/drain results;
- source backup integrity before and after the rehearsal;
- stopped-state billable resource classes;
- temporary cleanup result;
- source-bucket lifecycle configuration and versioning check, candidate or approved probe origin, 1-day multipart and 14-day noncurrent eligibility times, observed results or pending/exception status, exact-probe cleanup result, and unchanged source backup result;
- pass or measured exception for the 60-minute target;
- private evidence index filename and SHA-256.

### Live results

No deployed run has been completed. Issue #8 remains blocked by issue #7 as of 2026-09-18.
