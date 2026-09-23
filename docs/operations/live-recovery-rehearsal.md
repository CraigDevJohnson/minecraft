# Prove live persistence and recovery

Use this procedure only after issue #7 records the approved AWS account, deployment, recovery resources, and cleanup list. The commands mutate live AWS resources. Save raw output outside Git in a mode-0700 directory. Do not put account IDs, resource IDs, player data, private configuration, state, or SSM output in an issue or pull request.

Issue #8 remains incomplete until a deployed `survival` instance passes every check in this guide. Archive extraction alone does not pass.

## Prepare the evidence directory

Set these paths on the operator computer. Use the repository revision that issue #7 deployed.

```sh
umask 077
repo_dir="$PWD"
evidence_dir="/absolute/private/minecraft/issue-8-$(date -u +%Y%m%dT%H%M%SZ)"
source_dir="/absolute/private/minecraft/family"
recovery_dir="$evidence_dir/infra/recovery-rehearsal"
mkdir -p "$evidence_dir" "$recovery_dir"
git rev-parse HEAD >"$evidence_dir/git-revision.txt"
git status --short --branch >"$evidence_dir/git-status.txt"
python3 scripts/bootstrap_tools.py
.tools/bin/task check 2>&1 | tee "$evidence_dir/repository-check.txt"
```

Copy `infra/recovery-rehearsal`, its sibling `scripts` directory, and private backend configuration into the private work area. Preserve the `infra/recovery-rehearsal` directory depth under `$evidence_dir`; the root's `../../scripts` template references must resolve to `$evidence_dir/scripts`. Fill `recovery.tfvars` from `deployment.tfvars.example`. Use the source deployment's account, region, AZ, AMI, VPC, subnet, and backup bucket as inputs. The recovery root must create its own temporary security group with no ingress; do not reuse the source game security group. Generate a new filesystem UUID. It must differ from the source UUID.

Record the approved scope in `authorization.txt`. Include the issue comment or approval reference, approved plan hashes, temporary resource classes, and exact cleanup boundary. Do not continue if issue #7 is open or the approved scope omits host replacement, the recovery host and disk, the recovery EIP, or cleanup.

## Record the source baseline

Capture the source deployment and recovery point before a mutation. The examples assume that `$source_dir/deployment.json` is the private `deployment` output and `$source_dir/operator.json` is the installed operator configuration source.

```sh
python3 scripts/minecraft_host.py \
  --deployment "$source_dir/deployment.json" status \
  | tee "$evidence_dir/source-status-before.json"

aws ec2 describe-instances \
  --instance-ids "$(jq -r .host_id "$source_dir/deployment.json")" \
  >"$evidence_dir/source-instance-before.json"
aws ec2 describe-volumes \
  --volume-ids "$(jq -r .data_volume_id "$source_dir/deployment.json")" \
  >"$evidence_dir/source-volume-before.json"
aws ec2 describe-addresses \
  --allocation-ids "$(jq -r .elastic_ip_id "$source_dir/deployment.json")" \
  >"$evidence_dir/source-address-before.json"
```

Through SSM, run these commands on the source host and retain the command IDs and private output:

```sh
sudo /usr/local/sbin/minecraft-storage verify \
  --volume-id "$data_volume_id" --filesystem-uuid "$data_filesystem_uuid"
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
/usr/bin/time -p -o /root/source-backup-time.txt \
  sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json backup survival
```

Copy `source-backup-time.txt` through SSM. Record the completed archive size from `minecraft-backup status` and `s3api head-object`. Then time the host stop and start. `minecraft_host.py` reports the host, application, and backup separately.

```sh
/usr/bin/time -p -o "$evidence_dir/clean-stop-time.txt" \
  python3 scripts/minecraft_host.py \
  --deployment "$source_dir/deployment.json" stop \
  | tee "$evidence_dir/clean-stop.json"

python3 scripts/minecraft_host.py \
  --deployment "$source_dir/deployment.json" status \
  | tee "$evidence_dir/stopped-status.json"

/usr/bin/time -p -o "$evidence_dir/clean-start-time.txt" \
  python3 scripts/minecraft_host.py \
  --deployment "$source_dir/deployment.json" start --instance survival \
  | tee "$evidence_dir/clean-start.json"
```

Require the stop result to show `host: stopped`, `application: stopped`, and `backup: complete`. The stopped status must report the application and backup as unavailable or unverified instead of claiming fresh runtime evidence. Require the start result to show `host: running`, `application: running`, and `ready: true`.

Compare the source instance, volume, address, filesystem, manifest, and checkpoint evidence. The EC2 instance ID, EBS volume ID, filesystem UUID, EIP allocation ID, public address, manifest digest, and checkpoints must match the baseline.

## Replace the host and retain the data disk

Drain and stop the source host again. Create a saved replacement plan from the same private infrastructure directory and state used by issue #7.

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" plan \
  -var-file="$source_dir/deployment.tfvars" \
  -replace=aws_instance.host \
  -out="$evidence_dir/host-replacement.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" show -no-color \
  "$evidence_dir/host-replacement.tfplan" \
  >"$evidence_dir/host-replacement.txt"
shasum -a 256 "$evidence_dir/host-replacement.tfplan" \
  >"$evidence_dir/host-replacement.tfplan.sha256"
```

Stop if the plan replaces or deletes the data volume, filesystem identity, EIP allocation, backup bucket, state bucket, network, or IAM role. The plan may replace the host, its root disk, the volume attachment, and the EIP association. Apply only the exact saved plan that Craig approved.

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" apply \
  "$evidence_dir/host-replacement.tfplan" \
  2>&1 | tee "$evidence_dir/host-replacement-apply.txt"
"$repo_dir/.tools/bin/tofu" -chdir="$source_dir/infra" output -json deployment \
  >"$source_dir/deployment-after-replacement.json"
```

Wait for SSM. Verify the retained mount before you install or start the runtime. Install the same reviewed revision on the new root disk, restore the private operator configuration, and run the normal host start command. Record the startup time and checkpoints.

Require a new EC2 instance ID. Require the original EBS volume ID, filesystem UUID, EIP allocation ID, public address, backup bucket, manifest digest, and checkpoint values.

Before creating the isolated recovery host, perform the final source backup that will supply the restore. Wait for its completed marker, then select that completed 64-character recovery point. Record its marker and archive object keys, sizes, ETags, and current version IDs with `aws s3api head-object` and `aws s3api list-object-versions`; hash each JSON evidence file after capture. Selecting the point only after the final source backup prevents a later stop, start, or replacement backup from pruning the selected point before restore.

## Create the isolated recovery host

Start the 60-minute operator recovery timer before the first recovery infrastructure command. Initialize the recovery root with its separate backend key. Create and inspect a saved plan.

```sh
date -u +%Y-%m-%dT%H:%M:%SZ >"$evidence_dir/recovery-started.txt"
cp -R "$repo_dir/infra/recovery-rehearsal/." "$recovery_dir/"
cp -R "$repo_dir/scripts" "$evidence_dir/scripts"
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

The plan may create only the temporary host, root disk, data disk, attachment, temporary no-ingress security group and its reviewed outbound web rules, EIP and association, instance role and profile, inline policies, and temporary backup bucket with its protection settings. The source VPC, subnet, bucket, data disk, host, EIP, and state are inputs; the source game security group must not be attached to the recovery host. The recovery security group must have no ingress and only the approved TCP 80/443 egress needed for package, AWS API, and SSM access. Stop if the plan manages any source resource. Source S3 access must remain read-only. Write, delete, and multipart access must name only the temporary recovery bucket.

Apply only the exact saved plan that Craig approved. Save both sensitive outputs with mode 0600.

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" apply \
  "$evidence_dir/recovery-create.tfplan" \
  2>&1 | tee "$evidence_dir/recovery-create-apply.txt"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" output -json deployment \
  >"$evidence_dir/recovery-deployment.json"
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" output -json cleanup_inventory \
  >"$evidence_dir/recovery-cleanup-inventory.json"
chmod 0600 "$evidence_dir/recovery-deployment.json" \
  "$evidence_dir/recovery-cleanup-inventory.json"
```

The first cloud-init run stops at the blank-disk storage gate. Through SSM, compare the target volume ID and fresh UUID with the approved output. Initialize that exact zero-filled disk once.

```sh
sudo /usr/local/sbin/minecraft-storage initialize \
  --volume-id "$target_data_volume_id" \
  --confirm-volume-id "$target_data_volume_id" \
  --filesystem-uuid "$target_data_filesystem_uuid"
sudo systemctl restart minecraft-storage.service
sudo systemctl start docker.service
```

Install the reviewed runtime revision. Supply a fresh RCON secret and the recorded EULA acceptance in target-private configuration. Keep the source deployment as `/etc/minecraft/source-deployment.json`. Configure the target operator file from the recovery deployment output and stage the selected point's original definition and manifest as described in [step 3 of the isolated restore procedure](backup-recovery.md#restore-onto-an-isolated-host). Its backup destination is the temporary recovery bucket. The restore command reads the source bucket only through `--source-deployment`.

## Restore and verify the selected recovery point

On the recovery host, run:

```sh
/usr/bin/time -p -o /root/restore-time.txt \
  sudo /usr/local/sbin/minecraft-restore \
  --config /etc/minecraft/operator.json \
  --source-deployment /etc/minecraft/source-deployment.json \
  survival "$recovery_point"

/usr/bin/time -p -o /root/restored-start-time.txt \
  sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json start survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json status survival
sudo df --block-size=1 --output=source,size,used,avail,target /srv/minecraft
sudo du --bytes --summarize /srv/minecraft/instances/survival
```

Copy the timing and storage output into the private evidence directory through SSM. Verify the active manifest digest and every recorded world, player, access, Floodgate, and plugin checkpoint after Minecraft reports ready. Record the UTC time of the last confirmed checkpoint. Compute total operator recovery time from the initial timestamp to that confirmation. If the total exceeds 60 minutes, report the measured time and the slowest phase.

Repeat `head-object` and `list-object-versions` for the selected source marker and archive. Their version IDs, sizes, and ETags must match the baseline.

## Rehearse update recovery on the isolated host

Use the current pinned manifest for an idempotent update rehearsal unless Craig separately approves a compatible update candidate. Run the supported update command, verify readiness, and compare the checkpoints.

```sh
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json update survival
sudo /usr/local/sbin/minecraft-instance \
  --config /etc/minecraft/operator.json status survival
```

Do not infer rollback from reinstalling old binaries. To test recovery from an update, remove and recreate only the approved temporary recovery resources. Capture the new private recovery deployment output with mode 0600. After the new root host is ready, initialize and mount the new blank target disk using the same guarded sequence above. Then repeat the reviewed AWS CLI and runtime installation, fresh RCON secret and recorded EULA staging, `/etc/minecraft/source-deployment.json` copy, and target `/etc/minecraft/operator.json` configuration from the new recovery deployment output. Restage the selected point's original definition and manifest using the isolated restore procedure linked above. Restore the selected pre-update recovery point through `minecraft-restore`, then verify its recorded manifest and checkpoints again. Never run this rehearsal against the family data directory.

## Prove failed backups remain visible

After the update rehearsal and checkpoint verification, remove only the temporary backup write policy. Save and inspect the plan before applying it. The source read policy, host, disk, endpoint, and both buckets must remain unchanged.

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
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" apply \
  "$evidence_dir/recovery-disable-backups.tfplan" \
  2>&1 | tee "$evidence_dir/recovery-disable-backups-apply.txt"
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

Stop the source host through `minecraft_host.py stop`. Inventory the resources that continue to incur charges while compute is stopped:

- the source root and retained data EBS volumes;
- the allocated source EIP;
- current and noncurrent S3 backup objects;
- the versioned state bucket and lock objects;
- the account budget resource.

Record actual sizes and current region pricing. Do not copy the estimates from the design as measured cost evidence.

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
"$repo_dir/.tools/bin/tofu" -chdir="$recovery_dir" apply \
  "$evidence_dir/recovery-destroy.tfplan" \
  2>&1 | tee "$evidence_dir/recovery-destroy-apply.txt"
```

Use `describe-instances`, `describe-volumes`, and `describe-addresses` to prove that the temporary IDs are absent or terminal. Then verify that the source host, source retained volume, source EIP, state, bucket, and selected recovery point remain intact.

## Publish the redacted result

Add a dated result below after the live run. Keep identifiers and player data in the private evidence directory. Include hashes of the private evidence index so the result remains tied to the tested revision.

Record these fields:

- Git revision and UTC test interval;
- clean stop, backup, clean start, replacement start, restore, and total recovery times;
- compressed backup bytes, restored bytes, and source and target disk use;
- old and new host distinction plus unchanged EBS, filesystem, EIP, manifest, and checkpoint results;
- failed-backup result and blocked restart/drain results;
- source backup integrity before and after the rehearsal;
- stopped-state billable resource classes;
- temporary cleanup result;
- pass or measured exception for the 60-minute target;
- private evidence index filename and SHA-256.

### Live results

No deployed run has been completed. Issue #8 remains blocked by issue #7 as of 2026-09-18.
