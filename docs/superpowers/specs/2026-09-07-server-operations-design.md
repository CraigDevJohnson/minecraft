# Family server operations design

Approved direction: 2026-09-07. The user chose to build the first survival instance and evaluate specific mods/add-ons later. GitHub issues hold implementation acceptance criteria; this document defines the shared design.

Owner acceptance update, 2026-10-03: Craig accepted four simultaneous joins (Java on Mac, Bedrock on iPhone and two iPads) and resolved requirements for console or family testing. The [current acceptance record](../../operations/family-device-validation.md) supersedes the earlier mandatory device matrix and representative family play conditions. PlayStation 5 and Switch remain untested, and BedrockConnect is unset. Recovery, deployment and protocol validation remain separate.

Rebuild update, 2026-10-10: [ADR 0004](../../adr/0004-crafty-multiverse-rebuild.md) replaced the Docker Compose runtime and the independent `survival` and `creative` instances below with one Crafty-run `family` instance holding both worlds; the [Crafty host runbook](../../operations/crafty.md) describes the live server, and #70 retired the Compose-era code and runbooks. The shared requirements here otherwise stand.

## Delivery sequence

| Order | Issue | Dependency |
| --- | --- | --- |
| 1 | [Tooling foundation #3](https://github.com/CraigDevJohnson/minecraft/issues/3) | None |
| 2 | [Infrastructure #4](https://github.com/CraigDevJohnson/minecraft/issues/4) and [runtime #5](https://github.com/CraigDevJohnson/minecraft/issues/5) | #3; runtime tests also need EULA acceptance |
| 3 | [Backup and restore #6](https://github.com/CraigDevJohnson/minecraft/issues/6) | #4, #5 |
| 4 | [AWS bootstrap and deployment #7](https://github.com/CraigDevJohnson/minecraft/issues/7) | #4, #5, #6; human authorization |
| 5 | [Live recovery #8](https://github.com/CraigDevJohnson/minecraft/issues/8) and [family devices #9](https://github.com/CraigDevJohnson/minecraft/issues/9) | #7 |

The #8 technical recovery criteria and #9 family acceptance remain separate delivery records; #9's requirement for family testing is resolved by the owner update above. Future content selection does not block the initial vanilla-style survival instance.

## Deliverable

One fresh private survival instance, `survival`, for up to six simultaneous family players. Intended clients include Java on macOS/Windows and Bedrock on iPad/tablets, PlayStation, Switch and Windows; the owner update above resolves the mandatory family testing requirement without certifying every platform. Reproduce the entire machine and infrastructure, preserve world/player state, and demonstrate backup recovery.

Long-term independent creative, experimental, and guided instances follow [ADR 0001](../../adr/0001-independent-server-instances.md). The first delivery does not deploy those instances, a proxy, or a management UI.

## Architecture

- OpenTofu provisions AWS infrastructure. Initial region `us-west-2` and `m7g.large` (2 vCPU, 8 GiB ARM64) are deployment defaults to confirm in the live plan.
- One public subnet, Internet gateway, a stable DNS hostname over a public IPv4 address that changes on each host start ([ADR 0002](../../adr/0002-dynamic-public-ipv4-with-cloudflare-hostname.md)), Java TCP 25565, RakNet UDP 19132, built-in NetherNet signaling TCP 19132 and WebRTC UDP 19133. The approved October 3 dual-transport revision uses first-use trust and same-port public IPv4 advertisement; [ADR 0008](../../adr/0008-the-adapter-applies-the-pinned-server-content.md) records the pinned content and its transports. Administration uses Systems Manager; RCON stays local to the host.
- A 20 GiB root disk is replaceable. A separate encrypted gp3 disk sized by `data_volume_size_gib` (5 GiB start, grown online; [#22](https://github.com/CraigDevJohnson/minecraft/issues/22)) retains instance data across compute replacement. Verify its filesystem identity before starting Minecraft; a missing mount must never silently become a new world on the root disk.
- Docker Compose owns the Paper/Geyser/Floodgate runtime. Pin compatible AMI, image digest, Java, server/plugin builds and content when implementing that runtime.
- Private, encrypted, versioned S3 storage holds backups. A separate versioned state bucket uses locking. Host credentials are temporary and scoped; operator configuration and credentials stay outside Git.
- Start with a 5 GiB Java heap and measure actual memory/tick performance. Six players alone does not prove capacity, and 8 GiB is not a promise for several simultaneously running instances.

## Instance contract

The stable instance ID is `survival`; future IDs are lowercase letters/digits/hyphens, beginning with a letter and at most 32 characters. An ID names an instance, not an EC2 host or AWS environment.

The retained disk is mounted at `/srv/minecraft`. Runtime data lives at `/srv/minecraft/instances/<id>/data` and maps to container `/data`. Desired instance definitions and content manifests are separate from mutable data. Backups use `instances/<id>/` within the environment's backup bucket. Recovery metadata includes the instance ID and exact content revision. A restore uses a distinct target path/disk and never writes into an active instance.

Instance operations accept an explicit ID: start, stop, status, backup, restore, update. Host lifecycle is separate: stopping one instance does not authorize stopping other instances or the host. Host stop waits until every running instance completes its safe stop/backup sequence. Serialize conflicting operations and enforce one writer per data directory.

Infrastructure supplies resource IDs, region, the public hostname, data-volume/filesystem identity and bucket names through private deployment output. Instance configuration supplies game ports, memory, runtime/content manifest and data paths. Do not expose player identifiers, acceptance flags, secrets or deployment state in public logs.

## Lifecycle and backup

Start waits for EC2, SSM, the expected data mount and Minecraft readiness. Stop warns players, saves and cleanly exits Minecraft, verifies a completed S3 backup, requests EC2 stop when appropriate, and verifies machine state. Repeated commands handle existing state safely.

If save, application stop, or backup fails, return failure and do not claim the host stopped. Report separate host/application/backup states so the operator can retry. The normal path never force-kills a world process to claim success.

Create cold backups after each session, before upgrades and on demand. Keep the latest 14 successful backups; the latest successful recovery point must survive a long period without play. Prune only after verifying a new recovery point. Expire superseded/deleted S3 object versions after 14 days and incomplete multipart uploads after one day.

Back up all dimensions, player state, access/operator settings, mutable configuration, necessary plugin/Floodgate state, and the content manifest with checksums. Incomplete uploads are not recovery points. Restore rejects corrupt/incompatible input before modifying its isolated target. Validate recognizable world/player checkpoints after startup.

A complete disk loss during play can lose the current session. The recovery target is the last successful session-end backup. Record actual operator-initiated recovery duration and phase timings. Craig removed recovery time windows and overall time ceilings on October 3, 2026; elapsed time does not expire authorized work or determine acceptance. More frequent backups are a later content/operations decision.

## Clients and content

Tablets join the Bedrock endpoint directly. Console setup is optional; if used later, follow the documented BedrockConnect DNS route and preserve rollback instructions. Craig controls device settings, sign-ins and family permissions. Tablet success does not prove PlayStation/Switch success.

Keep family allowlisting and normal Java/Floodgate authentication; operator privileges belong to Craig. Verified access identities and saved-world integrity remain technical requirements; the owner update above resolves the human join/chat/gameplay acceptance. Select compatible content per instance; Docker does not convert arbitrary Java mods or native Bedrock add-ons.

## Validation and delivery boundaries

Foundation CI is credential-free: it may download public tools/providers but does not initialize a remote backend, plan against AWS, apply resources, or start Minecraft. Actual resources belong to the infrastructure issue; runtime pins and game tests belong to the runtime issue.

Live infrastructure/recovery acceptance proves stop/start persistence, host replacement using retained EBS and an isolated S3 restore. Family-play acceptance is resolved by the dated owner record above. Archive extraction and green CI alone do not establish those technical recovery outcomes.

Craig authorizes PR merges, account/state bootstrap and the concrete AWS apply/test plan. EULA acceptance is needed before any real Minecraft runtime test, including local tests. It has not been inferred from design approval.

The initial Oregon planning rates were $0.0816/hour compute, $0.08/GB-month gp3 and $0.005/hour retained IPv4. For 40–100 running hours, 60 GB disks and 10 GB average backups, the prior subtotal was about $12–$17/month before tax, charged traffic, monitoring and temporary recovery resources. The [cost review](../../operations/infrastructure.md#cost-review) has the current figures. Recheck rates and actual usage at deployment; the proposed $25 budget alert is a notification, not a spending cap.
