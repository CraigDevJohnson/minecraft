# Finalized build plan (2026-10-10)

Craig asked to revisit and finalize the build plan, using an earlier outside recommendation (Paper + Geyser + Floodgate + Multiverse under Crafty Controller on one Linux host) together with what this repository has already built and tested. This plan records where that recommendation agrees with the built system, where it conflicts, and the ordered remaining work.

## What is built and accepted

- **Runtime:** Docker Compose with `itzg/minecraft-server` running Paper 26.2, Geyser 2.11.3, Floodgate and ViaVersion ([ADR 0001](../../adr/0001-independent-server-instances.md), [runtime content](../../operations/runtime-content.md)).
- **Worlds:** two independent instances on one host, `survival` (Java 25565, Bedrock 19132, 5G heap) and `creative` (Java 25566, Bedrock 19134, 1G heap) ([creative world](../../operations/creative-world.md)).
- **Host:** EC2 `m7g.large` (8 GiB) in `us-west-2`, started and stopped on demand from the operator workstation, with a Cloudflare hostname that follows the dynamic IPv4 ([ADR 0002](../../adr/0002-dynamic-public-ipv4-with-cloudflare-hostname.md), [ADR 0003](../../adr/0003-on-demand-host-sessions.md)).
- **Backups:** cold backups to versioned S3 on every stop, with verified restore tooling ([backup recovery](../../operations/backup-recovery.md)).
- **Play acceptance:** on 2026-10-03 Craig accepted four simultaneous joins (Java on Mac, Bedrock on iPhone and two iPads). Console play was waived, not tested ([family device validation](../../operations/family-device-validation.md)).
- **On-demand cold start:** accepted on 2026-10-05 at 3 minutes 53 seconds for both worlds ([on-demand host](../../operations/on-demand-host.md#recorded-live-status-2026-10-05)).

## Pasted recommendation against the built system

| Topic | Pasted recommendation | Built system | Plan |
| --- | --- | --- | --- |
| Server software | Paper + Geyser + Floodgate | Same | Keep. |
| Cross-play content limits | Server-side plugins only; client mods and Bedrock add-ons excluded | Same rule in ADR 0001 | Keep. Evaluate each named plugin before adding it. |
| Multiple worlds | One Paper process with Multiverse, portals and a lobby | Independent instances with separate ports, data and backups | **Decision 1.** Recommended: keep independent instances. |
| Admin UI | Crafty Controller | Workstation controller (`scripts/minecraft_host.py`) plus the planned craigdevjohnson.com dashboard (portfolio PR #126) | **Decision 1.** ADR 0001 already rules out Crafty controlling the same processes as Compose. |
| World selection for players | In-game selector first, website second | Players add one server entry per world | Keep for now. Revisit only if consoles are added, where one entry is much easier than two. |
| Consoles (PS, Switch) | First acceptance test, through BedrockConnect | Waived on 2026-10-03; BedrockConnect not configured | Optional follow-up, not a blocker. |
| Hosting | Home, VPS, Lightsail or on-demand EC2 | On-demand EC2 | Keep. |
| Sizing for six players | 8 GB host, about 4 GB heap | 8 GiB host, 5G + 1G heap | Keep. Measure before adding a third concurrent world. |
| Maintenance | Pinned versions, off-host backups, restore tests | Pinned, digest-checked content; S3 backups; restore tooling | Keep. #26 and #27 below are the upcoming version moves. |

## Current state and a blocker found today

Both worlds are offline. Their last completed backups (2026-10-05) are the authoritative S3 recovery receipts from the #24 S3-only off trial. A cold restore stalled with a new host and a blank, unformatted 4 GiB disk, and on 2026-10-09 that host was stopped with a plain EC2 stop at Craig's request.

The S3-only first-setup provenance check requires the host to be on its first boot (`assert len(rows) == 1` over `journalctl --list-boots` in `scripts/minecraft_provenance.py`). Restarting the stopped host gives it a second boot, so the stalled restore will very likely refuse on that host for good. This is a safety rule, not a defect: it is how the controller proves a fresh disk was never used. The blank disk holds no world data, so nothing is lost, but the stalled host and disk need a reviewed repair that removes them so a fresh cold start can create new ones.

## Decisions for Craig

1. **Stack and admin UI.** Decided 2026-10-10: Craig chose to rebuild on Crafty Controller and Multiverse, with fresh worlds, recorded in [ADR 0004](../../adr/0004-crafty-multiverse-rebuild.md). The options were: keep the built Compose stack with independent worlds and the planned website dashboard (recommended), or rebuild around Crafty Controller and Multiverse. Rebuilding would discard the accepted lifecycle, backup and recovery tooling.
2. **How the server sleeps when off.** Decided 2026-10-10: Craig chose the retained disk. After the world is restored, return to the retained-disk on-demand mode (3 minute 53 second cold starts, accepted on 2026-10-05, for roughly $0.40 a month of disk) rather than staying S3-only (no disk while off, but every start adds a mandatory 15-minute fresh-disk proof plus a full restore). The next restore still runs through the S3-only path once, because the world currently lives only in S3.

## Remaining work, in order

Craig said on 2026-10-10 that the existing worlds were test worlds and need not be kept, so no restore is needed. The work below replaces the earlier restore-first order.

1. **Build the Crafty host in the repo** (no AWS needed): pinned Crafty image and plugin manifest, host bootstrap that installs Docker and Crafty on the retained data disk, a script that creates the Paper server and Multiverse worlds through the Crafty API, S3 backup sync, single-address ingress, and workstation `start`, `stop`, `status` and `admin` (SSM port-forward to Crafty) commands. Offline tests for each.
2. **Tear down and deploy** (needs Craig's workstation, AWS SSO and the Cloudflare token, and his approval of the reviewed plan): remove the stalled S3-only host and blank disk, apply `retained-host` with the new data disk and Crafty bootstrap, set the Crafty admin password, and confirm Java and Bedrock joins and portal travel between worlds.
3. **Retire the old controllers**: remove the S3-only, on-demand and Compose instance code once the Crafty host is proven, and close #24 as superseded.
4. **#26, NetherNet before Bedrock 26.60 drops RakNet (high)** and **#27, Java 26.3 (medium)**: unchanged gates, now applied to the Crafty-managed server's pinned jars.
5. **Website dashboard** from portfolio PR #126, talking to Crafty's API once its exposure is decided.
6. **Optional:** console joins through BedrockConnect, an adventure or lobby world, and pointing the idle-stop Lambda at Crafty.

Upstream release checks for #26 and #27 could not be refreshed from this session on 2026-10-10 because the release sites were unreachable; the last recorded check is 2026-10-05 in [client compatibility](../../operations/client-compatibility-readiness.md).
