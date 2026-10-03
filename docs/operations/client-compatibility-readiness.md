# Client compatibility readiness

The initial research checked `main@62da016`. Upstream release gates were rechecked on October 3, 2026 at 18:21 UTC against the official sources below. This is a dated research snapshot. Recheck upstream metadata before changing the manifest.

Later on October 3, Craig reported four simultaneous joins using Java on a Mac, Bedrock on an iPhone, and two iPads. He accepted iPad play and resolved the outstanding console and family-member testing requirements by owner acceptance. PlayStation and Switch remain untested, with BedrockConnect not configured. The [device runbook](family-device-validation.md) records that acceptance separately from performed tests. No further household testing blocks the current milestone or these issues; upstream releases, server-side protocol checks, deployment and recovery retain their own criteria.

| Issue | Verified state | Next action and owner |
| --- | --- | --- |
| [#9](https://github.com/CraigDevJohnson/minecraft/issues/9) | Java Mac, Bedrock iPhone and two iPad joins reported. Craig accepts current play and resolves the remaining household demonstrations by waiver. Console joins, reconnect/denial and near-six-player demonstrations were not performed. | Closed by owner acceptance. Console instructions remain available for optional later use. |
| [#26](https://github.com/CraigDevJohnson/minecraft/issues/26) | Dual transport with built-in signaling and the first-use trust prompt is merged in #30. Activation and qualifying release/protocol evidence remain. The dated released support check stops at Bedrock 26.52. | `needs-info` for qualifying 26.60 release and server-side protocol evidence, followed by approved activation. Household old/new-client and console demonstrations are resolved by owner waiver. |
| [#27](https://github.com/CraigDevJohnson/minecraft/issues/27) | Paper's 26.3 API returned 60 builds, only ALPHA/BETA, latest build 145 BETA. No STABLE build exists in that response. Released Geyser remains 2.11.3-b1247, supporting Java 26.2; upstream Java 26.3 PR #6712 is open. | `needs-info`, upstream release gates. Recheck both before selecting pins or scheduling the one-way world upgrade. |

## Java 26.3 upgrade gates

The [Paper build API](https://fill.papermc.io/v3/projects/paper/versions/26.3/builds) had no `STABLE` entry when checked. [Paper's downloads guidance](https://docs.papermc.io/misc/downloads-service/) directs production users to stable builds. The latest 26.3 BETA build is not a qualifying pin.

The [Geyser versions API](https://download.geysermc.org/v2/projects/geyser) ends at 2.11.3. Its [latest 2.11.3 build metadata](https://download.geysermc.org/v2/projects/geyser/versions/2.11.3/builds/latest) selects build 1247, commit `63a4e2b79b12f0d138777d5fd80176a112b4bd72`. The [supported-versions page](https://geysermc.org/wiki/geyser/supported-versions/) lists Java 26.2 and Bedrock 26.30–26.52. [PR #6712](https://github.com/GeyserMC/Geyser/pull/6712) remains open, with experimental previews. Its inspected head was `bc81f9ed819e946a83121c9a8d26600407027cc4`.

Keep the existing Paper 26.2/ViaVersion selection until both gates pass. A later review must select exact official downloads and recompute hashes, decide whether ViaVersion stays, test a copy of real 26.2 world data locally, and verify a pre-update recovery point. Follow [runtime content](runtime-content.md) and [backup recovery](backup-recovery.md). Rollback restores that old point with its matching release; reinstalling 26.2 over upgraded world data is not rollback. Craig's waiver resolves the household Java/Bedrock demonstration requirement, without certifying future native protocol support.

## NetherNet transition

[GeyserConfig at the pinned commit](https://github.com/GeyserMC/Geyser/blob/63a4e2b79b12f0d138777d5fd80176a112b4bd72/core/src/main/java/org/geysermc/geyser/configuration/GeyserConfig.java) defines `raknet`, `both` and `nethernet`. The source identifies RakNet removal in Bedrock 26.60. With `both`, WebRTC needs a UDP port different from RakNet. Built-in signaling binds TCP, normally the Bedrock port. TLS takes a certificate/key or PKCS12 file; without it, clients get the first-use trust prompt. The external `nxs` option registers with another service and supports a private token.

The [pinned NetherNet implementation](https://github.com/GeyserMC/Geyser/blob/63a4e2b79b12f0d138777d5fd80176a112b4bd72/core/src/main/java/org/geysermc/geyser/network/bedrock/nethernet/NetherNetServer.java) binds built-in TCP signaling and pins WebRTC to the selected UDP port. It stores a server identity under Geyser's data directory and reports failures when native WebRTC support is unavailable. ARM64 startup and real connectivity must therefore be tested rather than inferred from configuration parsing. Retain the identity through restart, backup and restore as part of plugin state.

Craig approved `both` during transition, with self-contained `builtin` signaling, RakNet UDP `19132`, signaling TCP `19132`, and WebRTC UDP `19133`. It avoids adding an external signaling account/service. The first-use trust prompt is approved; no TLS certificate or external signaling service is selected. Activation uses the concrete reviewed plan. Keep the generated built-in server identity private and retain it through backup/restore. Document address/port semantics from the selected release; household demonstrations are waived.

Implement config policy, manifest checksum, Compose port publication, infrastructure/recovery ingress, tests and transport-aware runtime validation together. A TCP HTTP response proves signaling only; a RakNet discovery ping proves only RakNet. Technical validation must establish the selected transport, supported protocols and external WebRTC reachability. A local native-channel result alone does not prove that external path. Household join demonstrations are waived and must not be reported as performed. Check whether the selected native library requires additional egress on the actual deployment before proposing a rule. The implementation adds initiated UDP egress to client-selected ports 1–65535 beside TCP 80/443; review this in the activation plan.

The [official Bedrock 26.50 changelog](https://feedback.minecraft.net/hc/en-us/articles/48826825649933-Minecraft-Bedrock-Edition-26-50-Changelog-Wilderness-Bound) says dedicated servers now default to NetherNet. It does not establish that this family's Geyser or console route passes 26.60. No official 26.60 release date or qualifying compatibility declaration was found in this check. That remains unknown; no speculative date belongs in the rollout plan.

## Optional console compatibility follow-up

[BedrockConnect](https://github.com/Pugmatt/BedrockConnect) redirects a featured-server connection to its server list, then transfers the client to the entered target. The inspected [release 1.71.0](https://github.com/Pugmatt/BedrockConnect/releases/tag/1.71.0), published September 15, declares 26.50 support. That metadata does not declare 26.60 support. If console play is pursued later, recheck the maintainer's release and transport guidance. List entry, DNS redirection or a successful tablet join does not establish console transport compatibility. Craig waived household console demonstrations for current acceptance.

## Remaining acceptance evidence

- #9 is resolved by actual reported joins and owner acceptance of the outstanding household demonstrations. Separate #8 recovery criteria remain.
- #26 needs qualifying 26.60 support, synchronized implementation/test evidence, an approved rollout, server-side ARM64 transport/protocol results and external WebRTC reachability. Household old/new-client and console demonstrations are waived.
- #27 needs released stable Paper/Geyser pins with recomputed digests, world-copy upgrade validation, a verified old recovery point and written restore rollback, the ViaVersion decision and approved deployment evidence. Household native Java/Bedrock demonstrations are waived.

Owner acceptance closes #9's household validation scope. Leave distinct unresolved technical criteria in #8/#26/#27 open and attach measured results when available.
