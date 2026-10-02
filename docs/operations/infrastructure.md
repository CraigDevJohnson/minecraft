# Host and retained infrastructure

Issue #4 implements the offline contract in the [approved Agent Brief](https://github.com/CraigDevJohnson/minecraft/issues/4#issuecomment-5579966412). It supplies no game runtime, live resources, or demonstrated recovery. The initial deployment is one `survival` instance; future IDs follow `^[a-z][a-z0-9-]{0,31}$` without sharing one global world directory.

## Ownership and inventory

| Owner | Resources | Replacement and cost behavior |
| --- | --- | --- |
| `infra`: host | One `m7g.large`, instance profile/role and policies, encrypted 20 GiB gp3 root disk | Compute charges while running; root disk persists while stopped but is deleted on termination. Root contents are replaceable. |
| `infra`: retained data | Separate encrypted 40 GiB gp3 EBS volume and orderly attachment | Explicit stable AZ, independent of host lifecycle, `prevent_destroy`; charged while stopped or detached. No inline data block or forced detach. |
| `infra`: retained backups | Private AES256 encrypted, versioned S3 bucket, ownership/public/TLS controls and lifecycle | Separate from host and state; `prevent_destroy`, no forced emptying; storage and requests continue while compute stops. |
| `infra`: network | VPC, public subnet, Internet gateway, route table/association, security group/rules, one Elastic IP and association | EIP allocation survives host replacement and incurs a retained IPv4 charge. TCP 25565 and UDP 19132 only for game ingress; no SSH or RCON ingress. No NAT gateway or paid VPC endpoints. |
| `infra`: budget | Monthly account cost budget, actual-cost email notice | Default $25; required private recipient from Craig. Account-wide, including unrelated account costs; a notification, never a spending cap or automatic stop. |
| `infra/state-bootstrap` | Separate state bucket with encryption, versioning, ownership/public/TLS/operator-only access | Independent configuration/state and locked backend; no age-based state expiration. No host access or customer-managed KMS key. See [state bootstrap](state-bootstrap.md). |

AWS-managed temporary instance-role credentials permit only basic Session Manager channels and current backup objects under the configured `instances/<id>/` prefixes, with bucket listing limited to those prefixes. No state, IAM, EC2, bucket administration, or historical-version deletion permissions are granted to the host. Current backup object deletion is reserved for #6's verified-success pruning, which keeps the latest 14 successful recovery points. S3 lifecycle expires superseded/deleted versions after 14 days and incomplete multipart uploads after one day; it never expires current completed recovery points by age.

SSM uses outbound HTTPS and requires a functioning, preinstalled agent. The host uses IMDSv2 with hop limit 1, keeping container access to instance credentials restricted. #6 must run its authorized S3 operations on the host or explicitly review any change to that boundary. Internet ingress still requires application allowlisting/authentication in #5. RCON must bind locally in that runtime. Host egress permits the HTTP/HTTPS package repositories and services needed during setup; no SSH keys or passwords are provisioned.

## Private deployment inputs and output

Copy `infra/deployment.tfvars.example` outside Git for #7. Review account/operator role, the administrator-created host permissions boundary ARN (`host_permissions_boundary_arn`; see [state bootstrap](state-bootstrap.md)), environment and stable resource prefix, region/AZ, exact AMI and publisher, backup name, filesystem UUID, and budget inputs. The required AMI is Ubuntu 24.04 LTS ARM64, HVM/EBS, with cloud-init, Python 3, util-linux, e2fsprogs, and SSM Agent preinstalled. Validate the image's provenance and actual packages in #7. No moving AMI lookup or default live identifier is committed. Bootstrap installs Ubuntu's `docker.io` and `docker-compose-v2`; the runtime/content pins remain #5's responsibility.

Generate the filesystem UUID privately once, before first deployment, and preserve it through host replacement. It identifies the expected ext4 filesystem, not a request to format on boot. AZ changes require a separate data migration/recovery plan because EBS volumes are zonal. Keep both the sensitive main `deployment` output and bootstrap outputs in the private inventory; the main configuration cannot infer its backend's bucket settings.

After state creation/migration is approved and verified, use an isolated private copy of `infra` **and its sibling `scripts` directory** so template references resolve. Initialize with the private infrastructure backend configuration. Example #7 plan commands, not run by checks:

```sh
mkdir -p "$private_dir/infrastructure"
cp -R "$repo_dir/infra" "$repo_dir/scripts" "$private_dir/infrastructure/"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" init \
  -input=false -lockfile=readonly -backend-config="$private_dir/infrastructure.backend.hcl"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" plan \
  -var-file="$private_dir/infrastructure.tfvars" -out="$private_dir/infrastructure.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" show -no-color \
  "$private_dir/infrastructure.tfplan" > "$private_dir/infrastructure-plan.txt"
```

Craig reviews the saved account-specific plan before authorizing its apply. Run that separately authorized apply so the private record holds its UTC window and caller, which a first-setup [zero-fill](#disk-setup-and-readiness-handoff-to-5-and-7) requires:

```sh
caller_arn="$(aws sts get-caller-identity --query Arn --output text)" || exit 1
printf 'apply-start %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$caller_arn" \
  >>"$private_dir/infrastructure-apply-window.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" apply \
  "$private_dir/infrastructure.tfplan" || exit 1
printf 'apply-end %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >>"$private_dir/infrastructure-apply-window.txt" || exit 1
```

Then redirect `tofu output -json deployment` to a mode-600 private file. Do not publish outputs or plans as PR/CI artifacts. New EC2 creation initially runs the host; #7 must account for this while arranging the later on-demand start/stop workflow.

## Disk setup and readiness: handoff to #5 and #7

Cloud-init installs `/usr/local/sbin/minecraft-storage`, private `/etc/minecraft/storage.conf`, and `minecraft-storage.service`. Every boot verifies the attached NVMe device's EBS serial, whole-disk identity, ext4 UUID, and actual writable mount at `/srv/minecraft`. It refuses missing/ambiguous devices, partitions, mismatched filesystems, incorrect mounts, or root-disk data occupying the mountpoint. Commands have timeouts and storage operations serialize through a bounded lock.

A fresh blank disk intentionally leaves storage/Docker unavailable. After attachment and SSM access are verified in #7, the operator reviews the exact volume ID and UUID against private outputs.

A newly created encrypted EBS volume does not read back as zeros. During the 2026-10-01 deployment, the new 40 GiB data volume and a separate 1 GiB probe volume both returned a zero first 4 KiB block followed by high-entropy bytes throughout, with no snapshot and no prior writer. `initialize` therefore refuses a fresh volume until it is explicitly zero-filled. Zero-fill erases anything it cannot recognize. It is permitted **only during first setup**: after the approved apply that created both this volume and this host, and before this host has mounted or initialized the volume or installed the runtime. AWS history can prove that no other host had the volume, not that this host never wrote it. Stopping and starting a host does not detach its volume, so a long-used volume with a damaged filesystem, still on its original host, would pass the AWS checks. Never zero-fill a retained, restored, snapshot-derived, or copied volume, a volume and host from different applies, or a disk that once mounted; follow the [recovery workflow](backup-recovery.md) instead.

Set `data_volume_id`, `host_id`, and `backup_bucket` from the private `deployment` output (`data_volume_id`, `host_id`, `backup_bucket_name`). From the operator workstation, with the operator role, which needs `cloudtrail:LookupEvents`, query the deployment Region:

```sh
aws ec2 describe-volumes --volume-ids "$data_volume_id" \
  --query 'Volumes[0].{Snapshot:SnapshotId,Source:SourceVolumeId,Created:CreateTime,Attachments:Attachments[].[InstanceId,AttachTime]}'
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue="$data_volume_id" \
  --query 'Events[].[EventTime,EventName,ReadOnly,Username,EventId,Resources[].ResourceName]' --output text
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue="$host_id" \
  --query 'Events[?ReadOnly==`false`].[EventTime,EventName,Username,EventId]' --output text
```

Each event prints its time, name, read-only flag, user, and event ID, then the resource IDs it references. Current attachments are not history: a volume can be attached elsewhere, written, detached, and reattached.

- Wait at least 15 minutes after the current `AttachTime` before trusting an empty or partial history. CloudTrail typically delivers an event within about 5 minutes but does not guarantee it.
- Stop if any lookup fails, is denied, or returns unreadable output.
- If an expected event is missing, wait and re-query. A history that still lacks `CreateVolume`, `AttachVolume`, or the host's `RunInstances` is incomplete: stop.
- Event history keeps only the last 90 days of management events, per Region; stop if the apply is older.

Require all of:

- an empty `Snapshot` and `Source` (a volume copy carries data without a snapshot), a `Created` time inside the recorded apply window, and exactly one current attachment, to the new host ID in the private outputs;
- exactly one `CreateVolume`, inside the recorded apply window, whose `Username` equals that of the host's single `RunInstances` event;
- among the host's write events, only that apply's `RunInstances`, `AssociateAddress`, and `AttachVolume`; any other, such as `CreateReplaceRootVolumeTask`, `StopInstances`, or `ModifyInstanceAttribute`, means the host's logs or root disk may not cover first setup;
- at least one `AttachVolume`, each to that host ID, and the current `AttachTime` inside the recorded apply window and equal, to the second, to the `EventTime` of the apply's own `AttachVolume` (same `Username`). A gp3 volume attaches to one instance at a time, so any detach and use elsewhere changes `AttachTime`, whatever CloudTrail indexed;
- no other event whose `ReadOnly` is `false`, such as `DetachVolume`, `CreateSnapshot`, `CopyVolumes`, `ModifyVolume`, or `CreateTags`.

The window is the newest `apply-start` and `apply-end` pair, recorded around that one apply; the host's `RunInstances` must fall inside the same pair; a failed or split apply has none. An apply without a recorded window needs a private bound that Craig accepts, such as the `LastModified` times of the state object versions written just before and by that apply. If an `AttachVolume` lists no instance, read only its target from that event. Keep the raw event private; it contains source IP addresses and ARNs:

```sh
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventId,AttributeValue="$event_id" \
  --query 'Events[0].CloudTrailEvent' --output text | jq -er .requestParameters.instanceId || exit 1
```

A 2026-10-02 lookup of the first deployment's data volume showed a clean apply: exactly `CreateVolume` then `AttachVolume`, which listed both the volume and the instance. No `CreateTags` or KMS events were indexed under the volume. Both events and the host's `RunInstances` had the same `Username`, OpenTofu's generated `aws-go-sdk-<digits>` role session name, and all were visible within minutes. An attach and detach of a different volume to the host was indexed under the instance ID only. Attach and detach events are therefore not reliably indexed under a volume ID: the identical-`AttachTime` rule, not an absence of events, proves the volume was never reattached.

No completed recovery point may name this volume as its source. This records each completed point's source volume privately and stops if any equals the data volume or a lookup fails:

```sh
aws s3api list-objects-v2 --bucket "$backup_bucket" --prefix instances/ \
  --query 'Contents[].Key' --output json >"$private_dir/backup-keys.json" || exit 1
jq -r '.[]? | select(test("/completed/[0-9a-f]{64}[.]json$"))' \
  "$private_dir/backup-keys.json" >"$private_dir/backup-markers.txt" || exit 1
while IFS= read -r key; do
  aws s3 cp "s3://$backup_bucket/$key" - </dev/null | jq -er .source.volume_id || exit 1
done <"$private_dir/backup-markers.txt" >"$private_dir/backup-sources.txt" || exit 1
if grep -Fxq "$data_volume_id" "$private_dir/backup-sources.txt"; then
  echo 'a recovery point records this volume as its source: stop' >&2; exit 1
fi
```

Through SSM, check that the host has not already used storage or the runtime. The data disk is not mounted, so instance data is checked through the backup bucket above. The first boot listed must begin within minutes of the host's `RunInstances` time; otherwise the logs do not cover first setup. Any printed evidence of use, missing log, or nonzero exit is a stop: follow the recovery workflow, never zero-fill.

```sh
sudo bash -c 'found=0; used="storage (mount|verify) succeeded|filesystem initialized"
journalctl --list-boots --no-pager
test -r /var/log/cloud-init-output.log || found=1
grep -E "$used" /var/log/cloud-init-output.log && found=1
journalctl -o cat --no-pager -u minecraft-storage.service -u "minecraft-storage-*" \
  -u docker.service -u cloud-final.service | grep -E "$used" && found=1
for path in /opt/minecraft /etc/minecraft/deployment.json /etc/minecraft/operator.json; do
  if test -e "$path"; then printf "present: %s\n" "$path"; found=1; fi
done
findmnt --mountpoint /srv/minecraft && found=1
ls -A /srv/minecraft 2>/dev/null | grep . && found=1
test "$found" = 0'
```

Install the runtime and transfer deployment handoffs only after initialization; their presence fails this check. Immediately before zero-fill, rerun the provenance lookups and the host-side check together and require the same single attachment with an identical `AttachTime`; if more than 10 minutes pass before zero-fill starts, rerun both again. Then, only with Craig's approval, zero-fill and initialize that exact disk, with `data_volume_id` and `data_filesystem_uuid` set in the SSM session to the reviewed outputs.

Session Manager ends an interactive session after 20 idle minutes by default, but each step can run up to its size-derived bound below. Run each as a transient systemd unit, which keeps running if the session is lost. The fixed unit name refuses a second concurrent run:

```sh
sudo systemd-run --unit=minecraft-storage-zero-fill --collect --wait -p Type=oneshot \
  /usr/local/sbin/minecraft-storage zero-fill \
  --volume-id "$data_volume_id" --confirm-volume-id "$data_volume_id" \
  --filesystem-uuid "$data_filesystem_uuid"
```

`--wait` returns the command's exit status. After a lost session, reconnect and read the outcome. With `--collect`, the unit disappears once it exits, so the journal is the record:

```sh
systemctl status minecraft-storage-zero-fill.service --no-pager
sudo journalctl -u minecraft-storage-zero-fill.service -o cat --no-pager
```

While the unit is active, wait. Success is `disk zero-filled; run initialize separately`; anything else is a failure. Only then initialize the same way, and check `minecraft-storage-initialize.service` likewise for `filesystem initialized or already matches; run mount separately`:

```sh
sudo systemd-run --unit=minecraft-storage-initialize --collect --wait -p Type=oneshot \
  /usr/local/sbin/minecraft-storage initialize \
  --volume-id "$data_volume_id" --confirm-volume-id "$data_volume_id" \
  --filesystem-uuid "$data_filesystem_uuid"
sudo systemctl restart minecraft-storage.service
sudo systemctl start docker.service
```

Alternatively, send each command through SSM Run Command with `executionTimeout` above the combined bounds of the commands in that invocation; `AWS-RunShellScript` defaults to 3600 seconds and allows at most 172800.

`zero-fill` refuses any disk with a filesystem, signature, partition, holder, or mount. It cannot recognize every kind of unlabelled data, so the provenance and host checks above are the authority for "new". Initialization requires an unmounted, unpartitioned, signature-free, completely zero-filled disk before creating ext4. An existing matching filesystem is left intact. Zero-fill and the whole-device zero check are each bounded at 300 seconds plus one second per 50 MiB of device size, rounded up: a 50 MiB/s floor plus five minutes, about 19 minutes for 40 GiB and 80 minutes for 220 GiB. The 40 GiB gp3 volume zero-filled in about 5.5 minutes. Formatting is bounded at two minutes. A timeout is a failure, never a reason to force-format. A timed-out zero-fill can return well after its bound and can leave the disk partly zeroed: wait for its unit to exit, then rerun zero-fill to completion. Initialize's whole-device check remains the gate. `mount` mounts and verifies; `verify` checks an already mounted filesystem without formatting or repairing it. Never use `mkfs -F`, erase signatures, or change the UUID to bypass a mismatch. Investigate or follow the separate recovery workflow.

Every bootstrap run directly checks/mounts storage even if systemd units are already active, then verifies again before reporting success. Docker startup itself requires storage and a fresh verification, preventing container restart policies from starting against an absent mount after reboot. #5 must also require/order its runtime units after `minecraft-storage.service` and run a fresh `minecraft-storage verify --volume-id … --filesystem-uuid …` immediately before any start. Service-active status alone is insufficient. A failed bootstrap, mount, verification, or Docker dependency must keep the runtime stopped. Unexpected mount loss while already running is a live operational failure; these startup checks do not replace recovery procedures.

After verification, #5 creates `/srv/minecraft/instances/<id>/data` on the retained disk with its explicitly selected runtime ownership and maps it to `/data`. Backup prefixes are `instances/<id>/`. Desired definitions/content manifests stay outside mutable data. No instance data directories, EULA files, or Minecraft containers are created by this issue.

Package setup is bounded and repeatable on a fresh/idle host. Retry a failed setup using the reviewed installed cloud-init script through SSM after fixing its cause; never rerun package bootstrap during family play. Attachment/EIP association can finish after cloud-init starts; an initial missing-disk/network failure is safe and requires an explicit retry. This issue does not automatically stop or replace an active game host.

## Normal replacement and explicit destructive workflow

The #6/#7 operator workflow must first stop every runtime cleanly and verify its successful backup, then stop the host. Plan replacement of `aws_instance.host` with the same retained volume, UUID, AZ, backup bucket, and backend keys. Inspect the saved plan: only host/root/attachment/association changes are expected; any data/backups/state replacement is a stop condition. Orderly attachment removal waits for the old host to release storage, with no force-detach or skip-destroy shortcut. Follow the [live persistence and recovery rehearsal](live-recovery-rehearsal.md) to replace the host and verify the mount, endpoint, and world/player state.

`prevent_destroy` and nonempty-bucket refusal block ordinary destroy/replacement plans for retained storage. They are not an IAM denial against a trusted operator deliberately removing code or using AWS directly. There is no repository `destroy` task. For any intentional deletion, reformatting, AZ migration, protection removal, state overwrite, or permanent version deletion: stop all writers, inventory exact resources and dependencies, verify an independent recoverable copy, prepare a separate change that names each protection being removed, produce a concrete destructive plan and rollback/recovery steps, and obtain Craig's explicit approval for those exact resources/actions before execution. Preserve state long enough to finish and verify the teardown; never remove it as part of routine compute replacement.

## Cost review

The design's Oregon planning inputs remain estimates to revalidate in #7: `m7g.large` $0.0816/running hour, gp3 $0.08/GB-month, retained public IPv4 $0.005/hour, and an illustrative 10 GB average backup footprint. For a 730-hour month, 60 GB of disks is about $4.80 and one IPv4 address about $3.65 before compute. Compute adds $3.26–$8.16 for 40–100 running hours: approximately $11.71–$16.61 **before S3 storage/versions/requests, traffic, taxes, monitoring, and temporary recovery resources**. Retained resources continue billing when EC2 is stopped. Long-lived current recovery points and noncurrent versions count toward S3 usage; actual storage can exceed the example.

Reprice the exact region, instance/AMI, storage, public IPv4, AWS Budgets notification configuration, account allowances, and recovery test resources at deployment using [EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS pricing](https://aws.amazon.com/ebs/pricing/), [VPC pricing](https://aws.amazon.com/vpc/pricing/), and [S3 pricing](https://aws.amazon.com/s3/pricing/). Rates here are review inputs, not a bill forecast. The budget recipient remains a required private input; budget notifications can be delayed and do not cap spending.

## Evidence limits

`check` validates formatting, shell lint/syntax, Python syntax and behavioral tests, all three OpenTofu roots, and mocked plan assertions without a remote backend or AWS credentials. The bootstrap tests replace only the operating-system boundary; they do not format a real disk. Source-policy checks cover lifecycle safeguards that mocked plans do not expose. No check applies infrastructure or starts Minecraft.

#7 must prove effective IAM, approved AMI prerequisites, backend locking/migration, actual plan/apply, attachment/device discovery, SSM, network reachability, AWS capacity and the budget recipient. #8 must follow the [live persistence and recovery rehearsal](live-recovery-rehearsal.md) to prove persistence, host replacement, and isolated recovery. #9 must prove family device compatibility. Green offline tests or PR CI prove none of these live outcomes.
