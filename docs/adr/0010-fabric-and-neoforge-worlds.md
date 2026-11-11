# ADR 0010: Fabric and NeoForge worlds

Status: Accepted, 2026-11-11. Changes what [ADR 0009](0009-one-host-per-world.md) and [ADR 0008](0008-the-adapter-applies-the-pinned-server-content.md) say of Paper and its plugins; the hosts, the lifecycle, the allowlist and the mod switches stay as they are. Built and checked offline, and every pin downloaded and checked. Nothing is proven live until each world's first start on its loader ([worlds runbook](../operations/worlds.md#the-move-from-paper)).

Every world ran Paper, so a mod was a Paper plugin. On November 11, 2026, Craig asked for `survival` and `creative` on Fabric and `adventure` on NeoForge, so the worlds can run the mods written for those loaders.

## Decision

- **A world names its loader.** [`runtime/crafty/server.json`](../../runtime/crafty/server.json) is schema 3. `loaders` holds what each loader's server needs: the jar Crafty launches, the other jars it starts from, Geyser and Floodgate for that loader, and Geyser's configuration under that loader's folder. Each world has a `loader`, and each mod lists the `loaders` it runs on. The adapter refuses a world that lists a mod for another loader. `base` keeps only the server properties every world shares.
- **Fabric starts from its server launcher.** `fabric-server-launch.jar` is Fabric's own launcher for one Minecraft, loader and installer version, pinned by SHA-256 like every jar (Fabric's service answers the same bytes each time). At its first launch it downloads Minecraft's server and Fabric's libraries, which the manifest does not pin: Fabric's launcher checks them itself. Fabric API is pinned beside it because Geyser, Floodgate and Flan need it.
- **NeoForge starts from its starter jar and installer.** Crafty launches one jar, and NeoForge ships an installer and a run script instead. NeoForge's [ServerStarterJar](https://github.com/neoforged/ServerStarterJar) is pinned as `server.jar`: it runs the pinned `neoforge-installer.jar` beside it when the folder has no run script, then launches NeoForge in the same Java process, so Crafty's memory settings and the NetherNet address flag still apply. The installer downloads Minecraft's server and NeoForge's libraries, which the manifest does not pin. When the adapter writes a new installer it removes `run.sh`, so the next launch installs that version.
- **Mods live in `mods/`.** The adapter writes pinned jars there and removes every other jar directly in it, as it did for `plugins/`. A managed file may be Java properties (`properties-v1`) as well as canonical JSON, because WorldEdit reads properties.
- **The worlds keep their data.** The adapter moves a server Crafty imported as Paper to its loader in place: it adds the loader's jars and mods, and changes Crafty's launch command to the loader's jar. The world folder, player data and the allowlist are not touched, and nothing of Paper's is deleted (`paper.jar`, `plugins/` and Paper's configuration files stay, unused). A new loader is applied only to a server folder that is exactly as its last archive stored it, so the archive from the last stop on Paper is the way back.
- **The mods are the closest that exist for each loader, and work without a client mod**, because Bedrock players cannot install one: Flan for land claims on every world, WorldEdit on `creative`, Sit on `survival`. AuraSkills and DecentHolograms have no equivalent of that kind on NeoForge and left the catalog.

## Consequences

- A mod that adds blocks, items or creatures needs the same mod in each player's own Minecraft, which Bedrock players cannot have. Such a mod can be pinned, but a world that runs it is for Java players with that mod installed.
- Paper's plugin data does not carry over: GriefPrevention's claims, and AuraSkills' levels on `adventure`. Flan starts with no claims.
- WorldEdit is no longer held to a player's own claims, as FastAsyncWorldEdit was through GriefPrevention. On `creative` every player may use it anywhere outside another player's claim that Flan protects.
- Geyser keeps its NetherNet identity under its loader's folder now, so each Bedrock device confirms each world once more.
- A first launch on a loader downloads from Mojang, Fabric or NeoForge. If one of them is down, that start fails and the next one tries again.
- Going back to Paper is a revert of this change, a Deploy, and a restore of the world's last archive from before the move.
- Rejected: one loader for every world, because Craig asked for both; running the NeoForge installer from the adapter, because the host has no Java outside Crafty's container.
