# Data disk capacity

The retained data disk, `aws_ebs_volume.data[0]` mounted at `/srv/minecraft`, takes its size from the required private input `data_volume_size_gib`. EBS grows a volume online and never shrinks one, so the disk starts small and grows when the [usage warning](#usage-report-and-warning) says so. gp3 gives the same 3,000 IOPS and 125 MiB/s at any size: a smaller disk is not a slower one. Keep volume IDs, UUIDs, and command output in the private operator directory, never in an issue or pull request.

## What the disk holds

| Path under `/srv/minecraft` | Size, October 2026 | Grows with |
| --- | --- | --- |
| `crafty/servers/<server id>/`: the server folder: both worlds with their dimensions, player data, allowlist and operators, plugin data, the Floodgate key, logs, and Paper's re-downloadable `libraries/`, `cache/`, and `versions/` | about 0.2–0.3 GB | Exploration of new chunks, logs |
| `crafty/staging/`: the folder a restore replaced, kept until the next restore or until you delete it ([recover from an archive](crafty.md#recover-from-an-archive)) | one more copy of the server folder | Restores |
| `crafty/config/`, `crafty/state/`, `crafty/logs/`, `crafty/import/`: Crafty's configuration and logs, the adapter's state records, and a first import's staging | kilobytes, logs | — |
| ext4 journal, inode tables, and the 5 percent of blocks reserved for root | about 8 percent of the disk | Disk size |

Archives stream from the server folder to S3 without a workspace on this disk. The 20 GiB root disk holds Docker images and logs, `/opt/minecraft-crafty`, and `/etc/minecraft`.

## Why 5 GiB

- After `mkfs.ext4` with its defaults, Minecraft (UID 1000) can use about 4.6 GiB, roughly nine times the current 0.5 GB. Root-run tools can also use the reserved blocks, so Minecraft meets a full disk first and maintenance and restore keep room. `tune2fs -m` can change the reserve later, while mounted.
- Growth is an estimate. At `view_distance` 8, newly explored terrain adds tens of MB per hour of exploration, and building near existing terrain adds little. Six family players plausibly add well under a few GB a year. The usage warning, not this estimate, triggers growth.
- A restore unpacks beside the current server folder, so it needs room for one more copy of the server ([recover from an archive](crafty.md#recover-from-an-archive)). On a new 5 GiB disk that works until the server folder reaches about 2 GiB. A larger world restores onto a larger new disk, as in the [replacement](#replace-the-disk-with-a-smaller-one).
- 3 GiB would save $0.16 a month more and leave little restore headroom. 8 GiB costs $0.24 a month more for headroom not needed yet. At $0.08/GB-month, each 5 GiB costs $0.40 a month.

`data_volume_size_gib` accepts a whole number from 2 through 128. Below 2 GiB a restore has no headroom. Archives stream to S3 with no size limit of their own.

No code depends on the size. `minecraft-storage mount` and `verify` check the EBS serial, the ext4 UUID, and the mount. Only the zero-fill and zero-check bounds scale with it: 403 seconds for 5 GiB.

## A smaller size is always a stop

The variable has no default, so a deployment that has not set it fails to plan instead of planning a shrink. An existing deployment sets its current size, 40 for the first deployment, before any plan with this revision.

`prevent_destroy` does not catch a shrink. The pinned AWS provider plans a smaller `size` as an in-place update. The apply then calls `ModifyVolume`, AWS rejects it, and other changes in that apply may already have happened. Treat **any plan in which `aws_ebs_volume.data[0]` `size` decreases** as a stop, whether it shows as an update or as a replacement. A smaller disk comes only from the [replacement](#replace-the-disk-with-a-smaller-one) below.

## Usage report and warning

The emergency [host commands](host-commands.md#data-disk-usage) `minecraft_host.py start` and `status` print the host's `storage` report and add `storage_warning` once `used_percent` reaches 80; the Launcher's everyday starts do not report it. The warning never fails a command or blocks a start. At 5 GiB it leaves about 0.9 GiB, and growing takes minutes. A full disk can lose chunk saves, so grow the disk when the warning first appears. Each content update also adds about 0.1 GB that is never pruned.

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

EBS cannot shrink a volume or create a smaller one from a snapshot. A replacement is a new blank disk, created by OpenTofu together with a new host, then a restore of the newest verified archive onto it: first setup of the new disk ([disk setup and readiness](infrastructure.md#disk-setup-and-readiness-handoff-to-5-and-7)), then [Install Crafty on the host](crafty.md#install-crafty-on-the-host) through [Create the server](crafty.md#create-the-server), then [Recover from an archive](crafty.md#recover-from-an-archive). Any plan that shrinks `aws_ebs_volume.data[0]` in place is a stop, and Craig approves the exact replacement plan and, separately, retiring the old volume.

The 2026-10-03 replacement ([Evidence](#evidence)) followed the Compose-era step-by-step procedure, which #70 retired with its restore tooling; it stays in git history. A Crafty-era replacement needs its own reviewed step-by-step procedure before it runs.

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

The linked migration summaries do not report separate zero-check or runtime-install durations, or a measured pre-migration disk-use percentage. Those timings are not inferred from total downtime. This migration supplied partial #8 evidence at the time; the subsequent isolated recovery results ([#8](https://github.com/CraigDevJohnson/minecraft/issues/8)) completed restore/update recovery, failed-backup visibility and temporary cleanup on October 3. #23's on-demand mode was accepted on October 5 and #24's sole-S3 mode never activated; the Crafty rebuild superseded both ([ADR 0004](../adr/0004-crafty-multiverse-rebuild.md)). Craig resolved console and family testing requirements through the [owner acceptance record](family-device-validation.md); unperformed demonstrations are not marked passed.

No live grow has been recorded. Future replacements and grows should add their revision, phase times, total downtime, archive and extracted bytes, and disk use before and after here, without private identifiers.
