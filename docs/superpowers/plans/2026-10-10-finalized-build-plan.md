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

1. **Stack and admin UI.** Keep the built Compose stack with independent worlds and the planned website dashboard (recommended), or rebuild around Crafty Controller and Multiverse. Rebuilding would discard the accepted lifecycle, backup and recovery tooling.
2. **How the server sleeps when off.** Decided 2026-10-10: Craig chose the retained disk. After the world is restored, return to the retained-disk on-demand mode (3 minute 53 second cold starts, accepted on 2026-10-05, for roughly $0.40 a month of disk) rather than staying S3-only (no disk while off, but every start adds a mandatory 15-minute fresh-disk proof plus a full restore). The next restore still runs through the S3-only path once, because the world currently lives only in S3.

## Remaining work, in order

1. **Bring the worlds back** (needs Craig's workstation: AWS SSO and the Cloudflare token from 1Password; every AWS change needs his approval).
   1. Review and apply a repair that removes the stalled host and blank 4 GiB disk while keeping both S3 receipts and the journal history.
   2. Run the normal S3-only `start` for a fresh cold restore, then confirm both worlds with Java and Bedrock joins.
   3. Run `dns` to repair the parked record that the 2026-10-05 stop left pending.
   4. Record the round trip in `docs/operations/on-demand-host.md` and re-scope #24, since S3-only stays available but is no longer the operating mode.
2. **Apply Decision 2.** Prepare and review the ownership handoff back to `retained-host`/`on-demand-host` with protection restored.
3. **#26, NetherNet before Bedrock 26.60 drops RakNet (high).** The dual-transport configuration is merged (#30) with Craig's approved ports (signaling TCP 19132, WebRTC UDP 19133). Waiting on an upstream Geyser release that supports 26.60, then a reviewed activation plan and server-side transport checks.
4. **#27, Java 26.3 (medium).** Waiting on a STABLE Paper 26.3 build and a Geyser release supporting Java 26.3; then world-copy upgrade test, verified pre-upgrade recovery point and reviewed rollout.
5. **Website dashboard.** Build the `/minecraft` page from portfolio PR #126 once its access model and world display names are settled.
6. **Optional:** console joins through BedrockConnect; a lobby or single entry point if consoles are added; idle auto-stop (PR #18) is incompatible with on-demand mode and stays disabled.

Upstream release checks for #26 and #27 could not be refreshed from this session on 2026-10-10 because the release sites were unreachable; the last recorded check is 2026-10-05 in [client compatibility](../../operations/client-compatibility-readiness.md).
