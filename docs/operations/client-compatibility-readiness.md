# Client compatibility readiness

Checked on October 3, 2026 against `main@62da016` and the official sources below. This is a dated research snapshot, not a runtime release or device-acceptance result. Recheck upstream metadata before changing the manifest. The [device runbook](family-device-validation.md) and [private evidence form](family-device-evidence.md.example) prepare the remaining household tests.

| Issue | Verified state | Next action and owner |
| --- | --- | --- |
| [#9](https://github.com/CraigDevJohnson/minecraft/issues/9) | Deployment prerequisite #7 is closed. The issue records Java 26.3 and Bedrock 26.50+ proof of concept joins, corrected allowlist identities, and stutter. There is no recorded complete tablet/PlayStation/Switch matrix, reconnect/denial proof or session approaching six players. | `ready-for-human`, Craig. Run the required clients and representative play, with agent help for observations and failures. |
| [#26](https://github.com/CraigDevJohnson/minecraft/issues/26) | The pinned Geyser source implements NetherNet and flags RakNet removal in 26.60. The October 3 approved local implementation selects both transports with built-in signaling and a first-use trust prompt; deployment and real-client gates remain. Released support/docs currently stop at Bedrock 26.52. | `needs-info` for qualifying 26.60 release/client evidence after the approved implementation and rollout. Final acceptance also needs the 26.60-compatible release and real old/new clients, including consoles. |
| [#27](https://github.com/CraigDevJohnson/minecraft/issues/27) | Paper's 26.3 API returned 58 builds, only ALPHA/BETA, latest build 143 BETA. No STABLE build exists. Released Geyser remains 2.11.3-b1247, supporting Java 26.2; upstream Java 26.3 PR #6712 is open. | `needs-info`, upstream release gates. Recheck both before selecting pins or scheduling the one-way world upgrade. |

## Java 26.3 upgrade gates

The [Paper build API](https://fill.papermc.io/v3/projects/paper/versions/26.3/builds) had no `STABLE` entry when checked. [Paper's downloads guidance](https://docs.papermc.io/misc/downloads-service/) directs production users to stable builds. The latest 26.3 BETA build is not a qualifying pin.

The [Geyser versions API](https://download.geysermc.org/v2/projects/geyser) ends at 2.11.3. Its [latest 2.11.3 build metadata](https://download.geysermc.org/v2/projects/geyser/versions/2.11.3/builds/latest) selects build 1247, commit `63a4e2b79b12f0d138777d5fd80176a112b4bd72`. The [supported-versions page](https://geysermc.org/wiki/geyser/supported-versions/) lists Java 26.2 and Bedrock 26.30–26.52. [PR #6712](https://github.com/GeyserMC/Geyser/pull/6712) remains open, with experimental previews. Its inspected head was `bc81f9ed819e946a83121c9a8d26600407027cc4`.

Keep the existing Paper 26.2/ViaVersion selection until both gates pass. A later review must select exact official downloads and recompute hashes, decide whether ViaVersion stays, test a copy of real 26.2 world data locally, and verify a pre-update recovery point. Follow [runtime content](runtime-content.md) and [backup recovery](backup-recovery.md). Rollback restores that old point with its matching release; reinstalling 26.2 over upgraded world data is not rollback. Real native Java 26.3 and Bedrock joins remain required after the approved rollout.

## NetherNet transition

[GeyserConfig at the pinned commit](https://github.com/GeyserMC/Geyser/blob/63a4e2b79b12f0d138777d5fd80176a112b4bd72/core/src/main/java/org/geysermc/geyser/configuration/GeyserConfig.java) defines `raknet`, `both` and `nethernet`. The source identifies RakNet removal in Bedrock 26.60. With `both`, WebRTC needs a UDP port different from RakNet. Built-in signaling binds TCP, normally the Bedrock port. TLS takes a certificate/key or PKCS12 file; without it, clients get the first-use trust prompt. The external `nxs` option registers with another service and supports a private token.

The [pinned NetherNet implementation](https://github.com/GeyserMC/Geyser/blob/63a4e2b79b12f0d138777d5fd80176a112b4bd72/core/src/main/java/org/geysermc/geyser/network/bedrock/nethernet/NetherNetServer.java) binds built-in TCP signaling and pins WebRTC to the selected UDP port. It stores a server identity under Geyser's data directory and reports failures when native WebRTC support is unavailable. ARM64 startup and real connectivity must therefore be tested rather than inferred from configuration parsing. Retain the identity through restart, backup and restore as part of plugin state.

Craig approved `both` during transition, with self-contained `builtin` signaling, RakNet UDP `19132`, signaling TCP `19132`, and WebRTC UDP `19133`. It avoids adding an external signaling account/service. The first-use trust prompt is approved; no TLS certificate or external signaling service is selected. This approval covers local implementation; activation uses a concrete reviewed plan. Keep certificate keys/passwords private, include renewal and restored server identity in the operations plan, and verify exact client address/port behavior with the selected release.

Implement config policy, manifest checksum, Compose port publication, infrastructure/recovery ingress, tests and transport-aware runtime validation together. A TCP HTTP response proves signaling only; a RakNet discovery ping proves only RakNet. The test must establish WebRTC reachability and real Bedrock joins while preserving Java and old Bedrock connectivity. Check whether the selected native library requires additional egress on the actual deployment before proposing a rule. The implementation adds initiated UDP egress to client-selected ports 1–65535 beside TCP 80/443; review this in the activation plan.

The [official Bedrock 26.50 changelog](https://feedback.minecraft.net/hc/en-us/articles/48826825649933-Minecraft-Bedrock-Edition-26-50-Changelog-Wilderness-Bound) says dedicated servers now default to NetherNet. It does not establish that this family's Geyser or console route passes 26.60. No official 26.60 release date or qualifying compatibility declaration was found in this check. That remains unknown; no speculative date belongs in the rollout plan.

## Console compatibility gate

[BedrockConnect](https://github.com/Pugmatt/BedrockConnect) redirects a featured-server connection to its server list, then transfers the client to the entered target. Its latest [release 1.71.0](https://github.com/Pugmatt/BedrockConnect/releases/tag/1.71.0), published September 15, declares 26.50 support. The inspected release metadata does not declare 26.60 support. Recheck the maintainer's release and transport guidance, then test each household console on both sides of the migration. List entry, DNS redirection or a successful tablet join does not establish console transport compatibility.

## Remaining acceptance evidence

- #9 needs each real device's version/network/route, common-world chat and interaction, stable identity/inventory after reconnect, approved-account joins and an unapproved-account denial, near-six-player metrics/user experience, exact console rollback, and documented operator/recovery use.
- #26 needs final synchronized implementation/test evidence and an approved rollout, an ARM64 NetherNet runtime result, a compatible 26.60 client join, successful pre-26.60 Bedrock and Java transition joins, and tested tablet/console instructions.
- #27 needs released stable Paper/Geyser pins with recomputed digests, world-copy upgrade validation, verified old recovery point and written restore rollback, the ViaVersion decision, approved deployment evidence and real native Java/Bedrock joins.

No issue is complete from this document or from local configuration tests. Leave unresolved criteria open and attach the measured results when available.
