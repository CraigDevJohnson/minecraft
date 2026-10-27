# ADR 0001: Independently configured server instances

Status: Accepted, 2026-09-07. Superseded by [ADR 0004](0004-crafty-multiverse-rebuild.md) on 2026-10-10 for the Compose runtime and the independent-instance layout.

The family plans survival, creative, rotating experimental content, and possibly guided adventures. The first delivery remains a single survival instance. Craig approved proceeding with this structure and evaluating specific content when needed.

Use Docker Compose with `itzg/minecraft-server` as the runtime owner. Begin with Paper, Geyser, and Floodgate for `survival`. Each future instance owns its content configuration, writable data, ports, and backups. Instances can share a host within measured CPU/memory limits; stored instances need not all run simultaneously.

Multiverse remains an optional plugin within an instance when maps share one compatible server installation. It does not isolate arbitrary plugins, loaders, or content versions. Crafty would be a different runtime-management choice; it must not independently control the same processes as Compose.

Shared family experiences must work with their intended Java and Bedrock clients. Client-required Java modpacks and native Bedrock behavior packs are not automatically compatible with Paper/Geyser. Choose a supported equivalent or explicitly revisit the edition requirement if a future package needs it.

Rotate experiments by archiving the previous instance with its content manifest and creating a fresh revision. Do not swap arbitrary mods/loaders inside the persistent survival world. Keep host provisioning separate from instance configuration so a future runtime or hardware change is possible.

Sources: [itzg configuration](https://docker-minecraft-server.readthedocs.io/en/latest/), [Geyser compatibility](https://geysermc.org/wiki/geyser/faq/), [Multiverse limitations](https://mvplugins.org/core/reference/faq/), [Crafty runtime model](https://docs.craftycontrol.com/pages/getting-started/installation/docker/).
