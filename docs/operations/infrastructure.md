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

Copy `infra/deployment.tfvars.example` outside Git for #7. Review account/operator role, environment and stable resource prefix, region/AZ, exact AMI and publisher, backup name, filesystem UUID, and budget inputs. The required AMI is Ubuntu 24.04 LTS ARM64, HVM/EBS, with cloud-init, Python 3, util-linux, e2fsprogs, and SSM Agent preinstalled. Validate the image's provenance and actual packages in #7. No moving AMI lookup or default live identifier is committed. Bootstrap installs Ubuntu's `docker.io` and `docker-compose-v2`; the runtime/content pins remain #5's responsibility.

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

Craig reviews the saved account-specific plan before authorizing its apply. After that separately authorized apply, redirect `tofu output -json deployment` to a mode-600 private file. Do not publish outputs or plans as PR/CI artifacts. New EC2 creation initially runs the host; #7 must account for this while arranging the later on-demand start/stop workflow.

## Disk setup and readiness: handoff to #5 and #7

Cloud-init installs `/usr/local/sbin/minecraft-storage`, private `/etc/minecraft/storage.conf`, and `minecraft-storage.service`. Every boot verifies the attached NVMe device's EBS serial, whole-disk identity, ext4 UUID, and actual writable mount at `/srv/minecraft`. It refuses missing/ambiguous devices, partitions, mismatched filesystems, incorrect mounts, or root-disk data occupying the mountpoint. Commands have timeouts and storage operations serialize through a bounded lock.

A fresh blank disk intentionally leaves storage/Docker unavailable. After attachment and SSM access are verified in #7, the operator reviews the exact volume ID and UUID against private outputs.

A newly created encrypted EBS volume does not read back as zeros. During the 2026-10-01 deployment, the new 40 GiB data volume and a separate 1 GiB probe volume both returned a zero first 4 KiB block followed by high-entropy bytes throughout, with no snapshot and no prior writer. `initialize` therefore refuses a fresh volume until it is explicitly zero-filled. Zero-fill erases anything it cannot recognize, so first prove from the operator workstation that the volume is new and the new host is its only writer. Current attachments are not history: a volume can be attached elsewhere, written, detached, and reattached. With the operator role, which needs `cloudtrail:LookupEvents`, query the deployment Region at least 15 minutes after the current `AttachTime`:

```sh
aws ec2 describe-volumes --volume-ids "$data_volume_id" \
  --query 'Volumes[0].{Snapshot:SnapshotId,Created:CreateTime,Attachments:Attachments[].[InstanceId,AttachTime]}'
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue="$data_volume_id" \
  --query 'Events[].[EventTime,EventName,Username,Resources[].ResourceName]' --output text
```

Each event prints its time, name, and user, then the resource IDs it references. Require all of:

- an empty `Snapshot`, a `Created` time from the approved apply, and exactly one current attachment, to the new host ID in the private outputs;
- exactly one `CreateVolume`, by the operator session that ran the approved apply, at the apply time;
- `AttachVolume` events only for that host ID; if one lists no instance, read `requestParameters.instanceId` from its `CloudTrailEvent`;
- no `DetachVolume`, `CreateSnapshot`, `ModifyVolume`, or other event that is not read-only.

Event history keeps only the last 90 days of management events, per Region. CloudTrail typically delivers an event within about 5 minutes but does not guarantee it; if `CreateVolume` or `AttachVolume` is missing, wait and re-query. Stop without zero-filling if a lookup fails or is denied, or the history is incomplete, ambiguous, or older than the 90-day window. Event history cannot show what an attached host wrote, so any other attachment is a stop condition. Then, only with Craig's approval, zero-fill and initialize that exact disk:

```sh
sudo /usr/local/sbin/minecraft-storage zero-fill \
  --volume-id "$data_volume_id" --confirm-volume-id "$data_volume_id" \
  --filesystem-uuid "$data_filesystem_uuid"
sudo /usr/local/sbin/minecraft-storage initialize \
  --volume-id "$data_volume_id" --confirm-volume-id "$data_volume_id" \
  --filesystem-uuid "$data_filesystem_uuid"
sudo systemctl restart minecraft-storage.service
sudo systemctl start docker.service
```

`zero-fill` refuses any disk with a filesystem, signature, partition, holder, or mount. It cannot recognize every kind of unlabelled data, so the AWS provenance check is the authority for "new". Never use it on a retained, restored, or snapshot-derived volume. Initialization requires an unmounted, unpartitioned, signature-free, completely zero-filled disk before creating ext4. An existing matching filesystem is left intact. Zero-fill and the whole-device zero check are each bounded at 300 seconds plus one second per 50 MiB of device size, rounded up: a 50 MiB/s floor plus five minutes, about 19 minutes for 40 GiB and 80 minutes for 220 GiB. The 40 GiB gp3 volume zero-filled in about 5.5 minutes. Formatting is bounded at two minutes. Timeout is a failure, never a reason to force-format. Through SSM Run Command, set `executionTimeout` above the combined bounds of the commands in that invocation; `AWS-RunShellScript` defaults to 3600 seconds and allows at most 172800. `mount` mounts and verifies; `verify` checks an already mounted filesystem without formatting or repairing it. Never use `mkfs -F`, erase signatures, or change the UUID to bypass a mismatch. Investigate or follow the separate recovery workflow.

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
