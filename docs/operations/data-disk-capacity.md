# Data disk capacity

The retained data disk, `aws_ebs_volume.data[0]` mounted at `/srv/minecraft`, takes its size from the required private input `data_volume_size_gib`. EBS grows a volume online and never shrinks one, so the disk starts small and grows when the [usage warning](#usage-report-and-warning) says so. gp3 gives the same 3,000 IOPS and 125 MiB/s at any size: a smaller disk is not a slower one. Keep volume IDs, UUIDs, and command output in the private operator directory, never in an issue or pull request.

## What the disk holds

| Path under `/srv/minecraft` | Size, October 2026 | Grows with |
| --- | --- | --- |
| `instances/survival/data/`: all dimensions, player data, allowlist and operators, plugin data, the Floodgate key, logs, and Paper's re-downloadable `libraries/`, `cache/`, and `versions/` | about 0.2–0.3 GB | Exploration of new chunks, logs |
| `instances/survival/runtime/content/<manifest digest>/`: the pinned server and plugin JARs and the managed Geyser configuration | about 0.1 GB per content revision | Every content update; old revisions are never pruned |
| `instances/survival/runtime/` records, `operation.json`, `host-drain.json` | kilobytes | — |
| `.minecraft-restore-*`: restore staging, holding the archive and the full uncompressed set until the final rename | archive + uncompressed, about 0.5 GB | World size; present only during a restore |
| ext4 journal, inode tables, and the 5 percent of blocks reserved for root | about 8 percent of the disk | Disk size |

The backup workspace is not on this disk. `minecraft-backup` works in Python's temporary directory, `/tmp` on the 20 GiB root disk, which also holds Docker images and logs, `/opt/minecraft`, and `/etc/minecraft`.

## Why 5 GiB

- After `mkfs.ext4` with its defaults, Minecraft (UID 1000) can use about 4.6 GiB, roughly nine times the current 0.5 GB. Root-run tools can also use the reserved blocks, so Minecraft meets a full disk first and maintenance and restore keep room. `tune2fs -m` can change the reserve later, while mounted.
- Growth is an estimate. At `view_distance` 8, newly explored terrain adds tens of MB per hour of exploration, and building near existing terrain adds little. Six family players plausibly add well under a few GB a year. The usage warning, not this estimate, triggers growth.
- A restore needs (archive + uncompressed) × 1.1 free on its target disk. On a new 5 GiB disk that works until the data directory reaches about 2 GiB uncompressed. A larger world restores onto a larger new disk sized from its marker, as in step 4 of the [replacement](#replace-the-disk-with-a-smaller-one).
- 3 GiB would save $0.16 a month more and leave little restore headroom. 8 GiB costs $0.24 a month more for headroom not needed yet. At $0.08/GB-month, each 5 GiB costs $0.40 a month.

`data_volume_size_gib` accepts a whole number from 2 through 128. Below 2 GiB a restore has no headroom. The backup program refuses recovery sets over 100 GiB, so a much larger disk could not be protected; raising the cap means revisiting that limit.

No code depends on the size. `minecraft-storage mount` and `verify` check the EBS serial, the ext4 UUID, and the mount, and backup and restore have their own byte limits. Only the zero-fill and zero-check bounds scale with it: 403 seconds for 5 GiB.

## A smaller size is always a stop

The variable has no default, so a deployment that has not set it fails to plan instead of planning a shrink. An existing deployment sets its current size, 40 for the first deployment, before any plan with this revision.

`prevent_destroy` does not catch a shrink. The pinned AWS provider plans a smaller `size` as an in-place update. The apply then calls `ModifyVolume`, AWS rejects it, and other changes in that apply may already have happened. Treat **any plan in which `aws_ebs_volume.data[0]` `size` decreases** as a stop, whether it shows as an update or as a replacement. A smaller disk comes only from the [replacement](#replace-the-disk-with-a-smaller-one) below.

## Usage report and warning

`minecraft_host.py start` and `status` print the host's `storage` report and add `storage_warning` once `used_percent` reaches 80; see [host commands](host-commands.md#data-disk-usage). The warning never fails a command or blocks a start. At 5 GiB it leaves about 0.9 GiB, and growing takes minutes. A full disk can lose chunk saves, so grow the disk when the warning first appears. Each content update also adds about 0.1 GB that is never pruned.

## Grow the disk

Growing needs no downtime and does not replace the host: `size` is not part of the host's user data.

1. In the private tfvars, raise `data_volume_size_gib`, for example from 5 to 10. Never lower it.
2. Make a saved plan from the private infrastructure copy used for the last apply, as in [infrastructure operations](infrastructure.md#private-deployment-inputs-and-output). It must show only `aws_ebs_volume.data[0]` updated in place, with `size` increasing. Anything else, including a host replacement, is a stop. Apply only the saved plan that Craig approved.
3. Wait for the new size to reach the host, with `data_volume_id` set from the private `deployment` output. It does once the modification is `optimizing`, usually within seconds:

   ```sh
   aws ec2 describe-volumes-modifications --volume-ids "$data_volume_id" \
     --query 'VolumesModifications[0].[ModificationState,Progress,OriginalSize,TargetSize]' --output text
   ```

4. Through SSM on the running host, grow the filesystem. Minecraft can keep running. If the host is stopped, do this after its next start:

   ```sh
   sudo bash -c 'set -euo pipefail; source /etc/minecraft/storage.conf; exec /usr/local/sbin/minecraft-storage grow --volume-id "$DATA_VOLUME_ID" --filesystem-uuid "$DATA_FILESYSTEM_UUID"'
   ```

5. Run the host `status` command and confirm the larger `storage.size_bytes`.

`grow` finds the disk by its EBS serial and requires the expected ext4 UUID mounted read-write at `/srv/minecraft`. It then runs `resize2fs` on the whole unpartitioned disk, which needs no `growpart`, verifies the mount again, and prints the old and new filesystem sizes as `df` counts them. It only ever extends the filesystem to the disk. `filesystem not grown` means the disk is no larger than the filesystem: wait for `optimizing` and rerun. It refuses an unmounted, mismatched, or ambiguous disk. It needs no `--confirm-volume-id` because it destroys nothing. A failed or timed-out `resize2fs`, bounded at 300 seconds, leaves the filesystem mounted: rerun `grow`. Boot never grows the filesystem; the operator performs every filesystem change.

AWS accepts another modification of the same volume only after the previous one is `completed`, and [at most four in a rolling 24 hours](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-modify-volume.html). A small gp3 increase usually completes in minutes, but AWS promises only best effort.

## Replace the disk with a smaller one

EBS cannot shrink a volume or create a smaller one from a snapshot. The replacement is a new blank disk, created by OpenTofu together with a new host, then a restore of the latest verified recovery point with the existing `minecraft-restore`. Restore requires a target volume and filesystem UUID that differ from the recovery point's source, and an absent target instance directory. A new disk with a **new UUID** meets both. A file copy is not used: it skips restore's checksum and inventory checks and needs the old disk mounted by hand, outside the storage tool's rules.

OpenTofu forgets the old volume and never destroys it. `tofu state rm 'aws_ebs_volume.data[0]'` keeps the address and its `prevent_destroy`. The apply only detaches the old volume, which stays `available` and unmodified as the rollback copy until it is [retired](#retire-the-old-volume). Both disks bill until then.

The family cannot play from step 3 until step 11. Every state change and AWS change needs Craig's approval of that exact action or saved plan. A pending host change shares this apply and its one runtime reinstall; follow that change's own plan checks as well.

Before the window:

- The reviewed revision is merged, and the host already runs its `runtime/content.json` and `runtime/survival.json`. Restore refuses a manifest or definition whose digest differs from the recovery point, and a release loads only its own [content policy](runtime-content.md). A host on earlier content is [updated](runtime-installation.md#replace-a-reviewed-installed-release) first. Steps 1 and 4 check this before any state change.
- A new filesystem UUID is generated privately and differs from the current one: `python3 -c 'import uuid; print(uuid.uuid4())'`.
- Private mode-0600 copies of the operator configuration, with its recorded EULA acceptance, and of the RCON secret exist. The replacement deletes the old root disk and its `/etc/minecraft`. Otherwise plan a new RCON secret.

Use one Bash or zsh session on the operator computer, in the reviewed checkout. Stop at the first command that fails:

```sh
umask 077
set -o pipefail
repo_dir="$PWD"
private_dir="/absolute/private/minecraft/family"
deployment="$private_dir/deployment.json"
tofu_dir="$private_dir/infrastructure/infra"
```

`$private_dir` holds the infrastructure copy, backend configuration, tfvars, and `deployment` output described in [infrastructure operations](infrastructure.md#private-deployment-inputs-and-output). Print the reviewed checkout's content digests, which steps 1 and 4 compare against:

```sh
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "scripts")
from minecraft_content import load_definition, load_manifest, manifest_digest
print("manifest_sha256  ", manifest_digest(load_manifest(Path("runtime/content.json"))))
print("definition_sha256", manifest_digest(load_definition(Path("runtime/survival.json"))))
PY
```

**1. Checkpoint.** Start `survival` with the [host command](host-commands.md). Through SSM, write a random value into the world's command storage over RCON. Record it privately with the hashes of the allowlist, operators, and Floodgate key, and the active manifest digest:

```sh
rcon() {
  sudo docker compose --project-name minecraft-survival \
    --file /srv/minecraft/instances/survival/runtime/compose.json exec -T minecraft rcon-cli "$1"
}
instance_dir=/srv/minecraft/instances/survival
checkpoint="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
rcon "data modify storage operations:checkpoint value set value \"$checkpoint\""
rcon "data get storage operations:checkpoint value"
sudo sha256sum "$instance_dir/data/whitelist.json" "$instance_dir/data/ops.json" \
  "$instance_dir/data/plugins/floodgate/key.pem"
sudo python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["revision"])' "$instance_dir/operation.json"
```

The last line is the active manifest digest. It must equal the checkout's `manifest_sha256`. If it differs, stop and update the instance to the reviewed content before this window.

**2. Measure.** Through SSM, record the old disk's use:

```sh
sudo df --block-size=1 --output=source,size,used,avail,pcent,target /srv/minecraft
sudo du --bytes --summarize /srv/minecraft/instances/survival/data /srv/minecraft/instances/survival/runtime
sudo du --bytes --max-depth=1 /srv/minecraft/instances/survival/data | sort -n | tail -15
sudo sh -c 'du --bytes --summarize /srv/minecraft/instances/survival/runtime/content/*'
```

**3. Final backup, then stop.** Through SSM, time the backup. It cleanly stops Minecraft and publishes the recovery point. The last command prints that session's completed recovery point, which must be the newest in `status`:

```sh
sudo /usr/bin/time -p -o /root/final-backup-time.txt \
  /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json backup survival
sudo /usr/local/sbin/minecraft-backup status survival --config /etc/minecraft/operator.json
sudo python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["recovery_point"])' \
  /srv/minecraft/instances/survival/operation.json
```

Record its marker and archive `head-object` output and version IDs, as in the [recovery rehearsal](live-recovery-rehearsal.md#replace-the-host-and-retain-the-data-disk). Then stop the host, with the Cloudflare token set for the command when the deployment has a [public hostname](host-commands.md#public-hostname). The result must show `host: stopped`, `draining: true`, and `instances.survival` with `application: stopped` and `backup: complete`. No second backup runs:

```sh
python3 scripts/minecraft_host.py --deployment "$deployment" stop
```

**4. Capacity and compatibility gate.** Download the selected marker and print its content digests:

```sh
recovery_point="REPLACE_WITH_64_CHARACTER_COMPLETED_ID"
printf '%s\n' "$recovery_point" | grep -Eq '^[0-9a-f]{64}$' || exit 1
aws s3api get-object --bucket "$(jq -er .backup_bucket_name "$deployment")" \
  --key "instances/survival/completed/$recovery_point.json" \
  "$private_dir/selected-recovery-marker.json" >/dev/null || exit 1
jq -e --arg point "$recovery_point" \
  '.status == "complete" and .instance == "survival" and .recovery_point == $point' \
  "$private_dir/selected-recovery-marker.json" >/dev/null || exit 1
jq -r '"manifest_sha256   " + .manifest_sha256, "definition_sha256 " + .definition_sha256' \
  "$private_dir/selected-recovery-marker.json"
```

Both must equal the checkout's digests. If either differs, stop: nothing has changed yet, so the host can start again on the old disk. Staging the point's recorded definition and manifest for step 9, as in [step 3 of the isolated restore](backup-recovery.md#restore-onto-an-isolated-host), works only for inputs this release still loads; an earlier content manifest is not one. Then compute the smallest disk the restore fits on, with `content_bytes` set from step 2 to the size of the content directory named by the active manifest digest:

```sh
content_bytes="REPLACE_WITH_CURRENT_CONTENT_REVISION_BYTES"
archive_bytes="$(jq -er '.archive.size_bytes' "$private_dir/selected-recovery-marker.json")"
uncompressed_bytes="$(jq -er '[.files[] | select(.type == "file") | .size_bytes] | add' \
  "$private_dir/selected-recovery-marker.json")"
python3 - "$archive_bytes" "$uncompressed_bytes" "$content_bytes" <<'PY'
import sys
archive, uncompressed, content = (int(value) for value in sys.argv[1:])
gib = 1 << 30
# Restore staging with 10 percent headroom, the staged content, and 1 GiB to play in.
needed = ((archive + uncompressed) * 11 + 9) // 10 + content + gib
# ext4 overhead and the root reserve leave about 92 percent of the disk usable.
print(f"Minimum data_volume_size_gib: {max(2, (needed * 100 + 92 * gib - 1) // (92 * gib))}")
PY
```

The new `data_volume_size_gib` must be at least that minimum. Otherwise choose the next size up.

**5. Private safety copies.** `deployment-before-shrink.json` becomes the restore's `--source-deployment`:

```sh
old_volume_id="$(jq -er .data_volume_id "$deployment")" || exit 1
cp -p "$deployment" "$private_dir/deployment-before-shrink.json" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" state pull >"$private_dir/state-before-shrink.json" || exit 1
cp -p "$private_dir/infrastructure.tfvars" "$private_dir/infrastructure-before-shrink.tfvars" || exit 1
aws ec2 describe-volumes --volume-ids "$old_volume_id" >"$private_dir/old-volume-before-shrink.json" || exit 1
```

**6. Point the configuration at a new disk.** Refresh the private copy of `infra` and `scripts` at the reviewed revision from an empty directory, so no stale file remains, and initialize it:

```sh
mv "$private_dir/infrastructure" "$private_dir/infrastructure-before-shrink" || exit 1
mkdir -p "$private_dir/infrastructure" || exit 1
cp -R "$repo_dir/infra" "$repo_dir/scripts" "$private_dir/infrastructure/" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" init \
  -input=false -lockfile=readonly -backend-config="$private_dir/infrastructure.backend.hcl" || exit 1
```

Before changing state, prove this revision plans at all. With the private tfvars still naming the old size and UUID, run an unsaved plan. It must succeed and may show only the host replacement and, for a deployment still on an Elastic IP, the Elastic IP destroys:

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" plan -var-file="$private_dir/infrastructure.tfvars" || exit 1
```

Then, in the private tfvars, set `data_volume_size_gib` to the new size and `data_filesystem_uuid` to the new UUID. Show Craig the dry run. With his approval of this state change, remove the old volume from state. From here until a saved plan is approved, any failure is handled by [re-importing the old volume](#roll-back):

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" state rm -dry-run 'aws_ebs_volume.data[0]'
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" state rm 'aws_ebs_volume.data[0]'
```

Make the saved plan. `-replace='aws_instance.host[0]'` is mandatory. The new volume's ID is unknown at plan time, so without it the provider plans the host as an in-place update: the old host, launched without an auto-assigned address, would lose its Elastic IP and its reach. An in-place host change is a stop. The flag also makes this apply create the host, which the first-setup zero-fill rule requires:

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" plan \
  -var-file="$private_dir/infrastructure.tfvars" -replace='aws_instance.host[0]' \
  -out="$private_dir/disk-replacement.tfplan" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" show -no-color \
  "$private_dir/disk-replacement.tfplan" >"$private_dir/disk-replacement-plan.txt" || exit 1
shasum -a 256 "$private_dir/disk-replacement.tfplan" >"$private_dir/disk-replacement.tfplan.sha256"
grep -n "$old_volume_id" "$private_dir/disk-replacement-plan.txt"
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" show -json "$private_dir/disk-replacement.tfplan" |
  jq -e '[.resource_changes[] | select(.address == "aws_instance.host[0]")] | length == 1
    and .[0].change.actions == ["delete","create"]
    and .[0].change.after.associate_public_ip_address == true
    and .[0].change.after.root_block_device[0].volume_size == 20' || exit 1
```

The text plan hides attributes that do not change, including the public-address flag while an Elastic IP is still attached, so the JSON check above is the gate for the new host. Do not save that JSON; it contains sensitive values.

The plan must show exactly:

- `aws_ebs_volume.data[0]` created: encrypted gp3 at the new size;
- `aws_instance.host[0]` replaced, never updated in place. The JSON check above proves its auto-assigned address and 20 GiB root disk;
- `aws_volume_attachment.data[0]` replaced. Its prior `volume_id` is the old volume, and `grep` must find the old ID nowhere else;
- for a deployment whose state still holds the Elastic IP, `aws_eip_association.host` and `aws_eip.host` destroyed.

Anything else is a stop: an S3, IAM, network, or budget change, any other destroy, or any resource that creates, updates, or destroys the old volume. If Craig does not approve the plan, [re-import the old volume](#roll-back) at once.

For a deployment that still has an Elastic IP, park the hostname before the apply releases that address. With `public_hostname` and `cloudflare_zone_id` added to a private copy of the old deployment output, the `dns` command parks the stopped host and must print `dns: parked` or `dns: current`.

**7. Apply** only the saved plan that Craig approved, in full and with no `-target`, recording its window for the zero-fill check:

```sh
caller_arn="$(aws sts get-caller-identity --query Arn --output text)" || exit 1
printf 'apply-start %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$caller_arn" \
  >>"$private_dir/infrastructure-apply-window.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" apply "$private_dir/disk-replacement.tfplan" || exit 1
printf 'apply-end %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >>"$private_dir/infrastructure-apply-window.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" output -json deployment >"$deployment" || exit 1
chmod 0600 "$deployment"
aws ec2 describe-volumes --volume-ids "$old_volume_id" \
  --query 'Volumes[0].[State,Size,length(Attachments)]' --output text
```

The old volume must be `available` at its old size with `0` attachments. The new output must name a new `host_id`, a `data_volume_id` other than `$old_volume_id`, and the new UUID. The apply leaves the new host running at a new address, so with a public hostname point the record at it now:

`cloudflare_env` is the private secret-reference file from [host commands](host-commands.md#cloudflare-token).

```sh
op run --env-file="$cloudflare_env" -- \
  python3 scripts/minecraft_host.py --deployment "$deployment" dns
```

**8. Blank-disk setup.** This is first setup: this apply created both the volume and the host. Follow [disk setup and readiness](infrastructure.md#disk-setup-and-readiness-handoff-to-5-and-7) as written, with `data_volume_id`, `host_id`, `backup_bucket`, and `data_filesystem_uuid` from the new output: the provenance lookups after the 15-minute wait, the marker-source check, the host-side check, zero-fill, initialize, then the storage restart and Docker start. Never zero-fill the old volume. Through SSM, record the new filesystem:

```sh
sudo df --block-size=1 --output=source,size,used,avail,pcent,target /srv/minecraft
sudo tune2fs -l "$(findmnt --noheadings --output SOURCE --mountpoint /srv/minecraft)" \
  | grep -E '^(Block count|Reserved block count|Block size):'
```

**9. Runtime and private configuration,** in this order, because the installer reads the on-host deployment file during preflight:

1. Transfer the new output to `/etc/minecraft/deployment.json` and `deployment-before-shrink.json` to `/etc/minecraft/source-deployment.json`. Both must be root-owned regular files with mode 0600 inside root-owned mode-0700 `/etc/minecraft`. Compare each on-host SHA-256 with its private copy.
2. Put the reviewed checkout on the new root disk. Run `sudo bash scripts/install_aws_cli.sh` and confirm that `aws --version` reports `aws-cli/2`.
3. Run `sudo bash scripts/install_runtime.sh`. The instance must be pristine.
4. Install `operator.json`, with its recorded EULA acceptance, and the RCON secret, as in [runtime installation](runtime-installation.md). If step 4 staged recorded inputs, point `definitions.survival` at the staged definition.

**10. Restore.** Through SSM, with the recovery point selected in step 3:

```sh
recovery_point="REPLACE_WITH_64_CHARACTER_COMPLETED_ID"
printf '%s\n' "$recovery_point" | grep -Eq '^[0-9a-f]{64}$' || exit 1
sudo /usr/bin/time -p -o /root/restore-time.txt /usr/local/sbin/minecraft-restore \
  --config /etc/minecraft/operator.json \
  --source-deployment /etc/minecraft/source-deployment.json survival "$recovery_point"
```

A failure before publication leaves the target absent: fix the cause and rerun this step before considering rollback. Then start from the operator computer, with the token when the deployment has a hostname:

```sh
op run --env-file="$cloudflare_env" -- \
  python3 scripts/minecraft_host.py --deployment "$deployment" start --instance survival
```

**11. Verify.** The start reports `ready: true`. Through SSM, with `rcon` and `instance_dir` defined as in step 1, `rcon "data get storage operations:checkpoint value"` returns the recorded value, and the three hashes and the manifest digest match step 1. The host `status` command shows `storage` near the expected percentage. Record `df` and `du` again, as in step 2.

**12. Commit forward.** Optionally hold one family session first. Stop the host with the `stop` command, again with the token, and require `backup: complete`. The newest completed recovery point must record the **new** volume as its source:

```sh
bucket="$(jq -er .backup_bucket_name "$deployment")" || exit 1
newest="$(aws s3api list-objects-v2 --bucket "$bucket" --prefix instances/survival/completed/ \
  --query 'sort_by(Contents, &LastModified)[-1].Key' --output text)" || exit 1
aws s3api get-object --bucket "$bucket" --key "$newest" \
  "$private_dir/first-new-disk-marker.json" >/dev/null || exit 1
jq -e --arg volume "$(jq -er .data_volume_id "$deployment")" \
  '.status == "complete" and .source.volume_id == $volume' "$private_dir/first-new-disk-marker.json"
```

**13. Evidence.** Record the measured times and sizes under [Evidence](#evidence) and on [#8](https://github.com/CraigDevJohnson/minecraft/issues/8), with no IDs.

### Retire the old volume

The old volume is deleted only through the [explicit destructive workflow](infrastructure.md#normal-replacement-and-explicit-destructive-workflow): after the checkpoint verification in step 11 and new-volume backup proof in step 12, and with Craig's approval to delete that specific volume. The earlier requirement for a family session was superseded by the [2026-10-03 owner acceptance](family-device-validation.md); the optional session in step 12 is not a retirement gate. Set `old_volume_id` from `deployment-before-shrink.json`. The state must name the new volume, and the old one must still be `available` at its old size, unattached, with its recorded tags:

```sh
old_volume_id="$(jq -er .data_volume_id "$private_dir/deployment-before-shrink.json")" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" state show 'aws_ebs_volume.data[0]' \
  | grep -E '^[[:space:]]+(id|size)[[:space:]]'
aws ec2 describe-volumes --volume-ids "$old_volume_id" \
  --query 'Volumes[0].[VolumeId,State,Size,length(Attachments),Tags]'
```

Then delete it and confirm that the lookup fails with `InvalidVolume.NotFound`:

```sh
aws ec2 delete-volume --volume-id "$old_volume_id"
aws ec2 describe-volumes --volume-ids "$old_volume_id"
```

Keep `deployment-before-shrink.json` until no retained recovery point names the old volume: 14 current points, plus 14 days of noncurrent versions. Restore needs the old volume's recorded ID and UUID, not the volume.

### Roll back

- **The plan is rejected; nothing was applied.** The old host and volume are untouched and still attached. Restore the old UUID and size in the private tfvars, then re-import the old volume:

  ```sh
  "$repo_dir/.tools/bin/tofu" -chdir="$tofu_dir" import \
    -var-file="$private_dir/infrastructure.tfvars" 'aws_ebs_volume.data[0]' "$old_volume_id"
  ```

  A new plan must show no change to the volume. It can still replace the host, because the revision changed the bootstrap. Do not apply that without separate approval; the rollback does not need it.
- **A failure after the apply, before new gameplay on the new disk.**
  1. Stop the host if it runs. If a session started, drain it with the `stop` command first. A host without an installed runtime cannot give drain evidence, so stop it directly with `aws ec2 stop-instances --instance-ids "$(jq -er .host_id "$deployment")"` and wait for `stopped`. Then run the `dns` command with the token, which parks the record.
  2. Run `tofu state rm 'aws_ebs_volume.data[0]'`, which drops the new volume from state, then the import above.
  3. Restore the old UUID and size in the private tfvars.
  4. Plan. Only the host and attachment replacements may appear, and the imported volume must show no change. Apply the saved plan that Craig approved.
  5. Cloud-init mounts the old filesystem without intervention. Never zero-fill it. Reinstall the runtime, restage the private configuration with the refreshed output, start with the token so the record follows the host, and verify the checkpoint.
  6. The abandoned new volume is unmanaged and holds only a copy of S3 data. Delete it with approval.
- **After new gameplay on the new disk,** going back to the old volume loses those changes. After the old volume is retired, recovery is the normal S3 restore onto a new disk sized from the marker.

## Evidence

### Replacement on 2026-10-03

The family deployment migrated from 40 GiB to 5 GiB using PR #25 at `c19afcc`, merged as `62da016`. The [migration record](https://github.com/CraigDevJohnson/minecraft/issues/22#issuecomment-5967187882) and [restore evidence](https://github.com/CraigDevJohnson/minecraft/issues/8#issuecomment-5967188290) record these results. Private inventory and evidence files hold the resource identities, filesystem UUIDs, recovery-point selection, state copies, and checkpoint values.

| Phase | Recorded result |
| --- | --- |
| Final backup and drain | 39 seconds; completed backup; host stopped and hostname parked |
| Saved-plan apply | 70 seconds; new encrypted 5 GiB disk and replacement host; old disk detached and unmodified |
| Provenance wait | 15 minutes; source, attachment, CloudTrail, and first-setup checks passed |
| Zero-fill | 40 seconds |
| Initialization | 42 seconds; new filesystem identity |
| S3 restore | 7 seconds; 233 MB archive and about 264 MB extracted |
| Start to ready | 54 seconds; data disk 9 percent used |
| Total family downtime | About 25 minutes |

World command-storage checkpoints, allowlist and operator file hashes, the Floodgate key hash, and the content revision matched after restore. Both game protocols answered through the hostname. The first completed backup from the new disk recorded its new source identity. A subsequent stopped-host plan reported no changes, and the next start updated DNS to the new address.

Craig separately approved retiring the old disk. The [retirement record](https://github.com/CraigDevJohnson/minecraft/issues/22#issuecomment-5967740514) confirms it was detached, matched the recorded tags, and was absent after deletion. At that check, the stopped deployment retained only its 20 GiB root disk, 5 GiB data disk, and S3 storage. Preserve the old source deployment record while any retained recovery point still names the old filesystem.

The linked migration summaries do not report separate zero-check or runtime-install durations, or a measured pre-migration disk-use percentage. Those timings are not inferred from total downtime. This migration supplied partial #8 evidence at the time; the subsequent [isolated recovery results](live-recovery-rehearsal.md#completed-recovery-proof-2026-10-03) completed restore/update recovery, failed-backup visibility and temporary cleanup on October 3. #23's on-demand mode is now activated with [separate live stop/cold-start proof](on-demand-host.md#recorded-live-status-2026-10-04); DNS repair and the timing decision keep #23 open. #24's sole-S3 mode remains unactivated. Craig resolved console and family testing requirements through the [owner acceptance record](family-device-validation.md); unperformed demonstrations are not marked passed.

No live grow has been recorded. Future replacements and grows should add their revision, phase times, total downtime, archive and extracted bytes, and disk use before and after here, without private identifiers.
