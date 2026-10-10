# ADR 0004: Rebuild on Crafty Controller and Multiverse

Status: Accepted direction, 2026-10-10. Supersedes ADR 0001's Compose runtime and independent-instance layout. Implementation pending.

On October 10, 2026, Craig chose to rebuild around Crafty Controller and Multiverse instead of keeping the Compose stack with independent `survival` and `creative` instances. He also chose to keep the world on a disk while the server is off, rather than only in S3, and said the existing worlds were test worlds that do not need to be kept. See the [finalized build plan](../superpowers/plans/2026-10-10-finalized-build-plan.md).

## Decision

- **Runtime manager:** Crafty Controller 4 runs in Docker on the host, pinned by image digest, with its configuration, database, servers and backups on the retained data disk. Crafty is the only process manager for the Minecraft server; nothing else starts or stops the Java process.
- **Server:** one Paper server managed by Crafty, with Geyser and Floodgate for Bedrock players and the Multiverse plugins (Core, Inventories, Portals, NetherPortals) for worlds. Every plugin is pinned by version and SHA-256, as today.
- **Worlds:** fresh worlds, not migrated. `survival` is the default world with its Nether and End, and `creative` is a creative-mode world with its own inventory group. Players travel by portal from spawn. An adventure or lobby world can be added later through Multiverse without new ports or infrastructure.
- **Ports:** one address for everyone. Java on TCP 25565 and Bedrock on UDP 19132, plus #26's NetherNet ports when that activates. The `creative` ports 25566 and 19134 are removed.
- **Admin access:** the Crafty web UI (HTTPS 8443) is not exposed publicly. Craig reaches it through an SSM port-forwarding session from his workstation. Public access for the craigdevjohnson.com dashboard is a separate later decision.
- **Backups:** Crafty's scheduled backups write to the data disk, and the host copies completed backup archives to the existing versioned S3 bucket with its host role. The stop command takes a final backup before the server stops.
- **Retained infrastructure:** network, security group, host role, S3 bucket, budget, state backend and the Cloudflare hostname stay. By default the host and its data disk are kept and the host is simply stopped between sessions (`lifecycle_mode="retained-host"`, `data_ownership="retained"`); Craig was asked on 2026-10-10 to confirm this over recreating the host on every start.

## Consequences

- The S3-only and on-demand host-recreation controllers, recovery receipts, provenance checks and the Compose instance controller are retired once the Crafty host works. #24 closes as superseded, and ADR 0003's session lifecycle no longer applies.
- The stalled S3-only restore host and its blank 4 GiB disk are removed through a reviewed apply. The Oct 5 test-world backups are no longer needed.
- Multiverse worlds share one Paper process, so a restart or a bad plugin affects every world. That is acceptable for one family server and is the trade-off Craig chose.
- Client-required Java mods and native Bedrock add-ons remain incompatible, as before. Each new plugin is checked for Geyser compatibility before it is added.
- #26 (NetherNet) and #27 (Java 26.3) carry over unchanged: they change the pinned Paper and Geyser versions inside the Crafty server instead of the Compose manifest.
- The idle-stop Lambda (PR #18) becomes usable again with a retained host, after its drain call is pointed at Crafty instead of the retired instance controller.
