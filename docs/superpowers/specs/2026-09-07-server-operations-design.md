# Family server operations design

Approved direction: 2026-09-07. The user chose to build the first survival instance and evaluate specific mods/add-ons later. GitHub issues hold implementation acceptance criteria; this document defines the shared design.

## Delivery sequence

| Order | Issue | Dependency |
| --- | --- | --- |
| 1 | [Tooling foundation #3](https://github.com/CraigDevJohnson/minecraft/issues/3) | None |
| 2 | [Infrastructure #4](https://github.com/CraigDevJohnson/minecraft/issues/4) and [runtime #5](https://github.com/CraigDevJohnson/minecraft/issues/5) | #3; runtime tests also need EULA acceptance |
| 3 | [Backup and restore #6](https://github.com/CraigDevJohnson/minecraft/issues/6) | #4, #5 |
| 4 | [AWS bootstrap and deployment #7](https://github.com/CraigDevJohnson/minecraft/issues/7) | #4, #5, #6; human authorization |
| 5 | [Live recovery #8](https://github.com/CraigDevJohnson/minecraft/issues/8) and [family devices #9](https://github.com/CraigDevJohnson/minecraft/issues/9) | #7 |

Both #8 and #9 must pass for the milestone to be complete. Future content selection does not block the initial vanilla-style survival instance.

## Deliverable

One fresh private survival instance, `survival`, for up to six simultaneous family players. Java clients on macOS/Windows and Bedrock clients on iPad/tablets, PlayStation, Switch, and Windows must be tested as applicable. Reproduce the entire machine and infrastructure, preserve world/player state, and demonstrate backup recovery.

Long-term independent creative, experimental, and guided instances follow [ADR 0001](../../adr/0001-independent-server-instances.md). The first delivery does not deploy those instances, a proxy, or a management UI.

## Architecture

- OpenTofu provisions AWS infrastructure. Initial region `us-west-2` and `m7g.large` (2 vCPU, 8 GiB ARM64) are deployment defaults to confirm in the live plan.
- One public subnet, Internet gateway, Elastic IP, Java TCP 25565, Bedrock UDP 19132. Administration uses Systems Manager; RCON stays local to the host.
- A 20 GiB root disk is replaceable. A separate encrypted 40 GiB gp3 disk retains instance data across compute replacement. Verify its filesystem identity before starting Minecraft; a missing mount must never silently become a new world on the root disk.
- Docker Compose owns the Paper/Geyser/Floodgate runtime. Pin compatible AMI, image digest, Java, server/plugin builds and content when implementing that runtime.
- Private, encrypted, versioned S3 storage holds backups. A separate versioned state bucket uses locking. Host credentials are temporary and scoped; operator configuration and credentials stay outside Git.
- Start with a 5 GiB Java heap and measure actual memory/tick performance. Six players alone does not prove capacity, and 8 GiB is not a promise for several simultaneously running instances.

## Instance contract

The stable instance ID is `survival`; future IDs are lowercase letters/digits/hyphens, beginning with a letter and at most 32 characters. An ID names an instance, not an EC2 host or AWS environment.

The retained disk is mounted at `/srv/minecraft`. Runtime data lives at `/srv/minecraft/instances/<id>/data` and maps to container `/data`. Desired instance definitions and content manifests are separate from mutable data. Backups use `instances/<id>/` within the environment's backup bucket. Recovery metadata includes the instance ID and exact content revision. A restore uses a distinct target path/disk and never writes into an active instance.

Instance operations accept an explicit ID: start, stop, status, backup, restore, update. Host lifecycle is separate: stopping one instance does not authorize stopping other instances or the host. Host stop waits until every running instance completes its safe stop/backup sequence. Serialize conflicting operations and enforce one writer per data directory.

Infrastructure supplies resource IDs, region, EIP, data-volume/filesystem identity and bucket names through private deployment output. Instance configuration supplies game ports, memory, runtime/content manifest and data paths. Do not expose player identifiers, acceptance flags, secrets or deployment state in public logs.

## Lifecycle and backup

Start waits for EC2, SSM, the expected data mount and Minecraft readiness. Stop warns players, saves and cleanly exits Minecraft, verifies a completed S3 backup, requests EC2 stop when appropriate, and verifies machine state. Repeated commands handle existing state safely.

If save, application stop, or backup fails, return failure and do not claim the host stopped. Report separate host/application/backup states so the operator can retry. The normal path never force-kills a world process to claim success.

Create cold backups after each session, before upgrades and on demand. Keep the latest 14 successful backups; the latest successful recovery point must survive a long period without play. Prune only after verifying a new recovery point. Expire superseded/deleted S3 object versions after 14 days and incomplete multipart uploads after one day.

Back up all dimensions, player state, access/operator settings, mutable configuration, necessary plugin/Floodgate state, and the content manifest with checksums. Incomplete uploads are not recovery points. Restore rejects corrupt/incompatible input before modifying its isolated target. Validate recognizable world/player checkpoints after startup.

A complete disk loss during play can lose the current session. The recovery target is the last successful session-end backup. Measure a proposed 60-minute operator-initiated recovery target for the initial world and available AWS capacity. More frequent backups are a later content/operations decision.

## Clients and content

Tablets join the Bedrock endpoint directly. Start console testing with the documented BedrockConnect DNS route, preserving rollback instructions. Craig performs device settings, sign-ins and family permissions. Tablet success does not prove PlayStation/Switch success.

Keep family allowlisting and normal Java/Floodgate authentication; operator privileges belong to Craig. Check real identity, inventory, join/chat and gameplay. Select compatible content per instance; Docker does not convert arbitrary Java mods or native Bedrock add-ons.

## Validation and delivery boundaries

Foundation CI is credential-free: it may download public tools/providers but does not initialize a remote backend, plan against AWS, apply resources, or start Minecraft. Actual resources belong to the infrastructure issue; runtime pins and game tests belong to the runtime issue.

Live acceptance proves stop/start persistence, host replacement using retained EBS, an isolated S3 restore, and representative family play. Archive extraction and green CI alone do not establish those outcomes.

Craig authorizes PR merges, account/state bootstrap and the concrete AWS apply/test plan. EULA acceptance is needed before any real Minecraft runtime test, including local tests. It has not been inferred from design approval.

The initial Oregon planning rates were $0.0816/hour compute, $0.08/GB-month gp3 and $0.005/hour retained IPv4. For 40–100 running hours, 60 GB disks and 10 GB average backups, the prior subtotal was about $12–$17/month before tax, charged traffic, monitoring and temporary recovery resources. Recheck rates and actual usage at deployment; the proposed $25 budget alert is a notification, not a spending cap.
