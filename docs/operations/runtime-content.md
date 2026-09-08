# Runtime content and local validation

`runtime/survival.json` defines the first instance's desired ports, six-player limit, 5 GiB heap, and view/simulation distances. `runtime/content.json` pins the Linux ARM64 image and every externally selected server/plugin JAR. The Compose document is generated deterministically by `minecraft_content.render_compose`; keep the generated JSON private because it contains the operator's RCON secret and explicit EULA acceptance.

## Verified selection

The initial selection was resolved from official upstream metadata on 2026-09-08:

| Component | Exact selection | Integrity / authoritative metadata |
| --- | --- | --- |
| itzg Minecraft image | ARM64 `sha256:fa70ecc878ed641a89bc7dceaf43d8160faa351d9da4b48a2d9973f6ac9cb594` | Pulled the platform manifest, inspected image revision `da99021d880273b4fd5c1bf86a9a8d2489e0be43`, and ran `java -version` |
| Java | Eclipse Temurin `25.0.4+7-LTS` | Included in the immutable image; [Paper requires Java 25 for 26.1+](https://docs.papermc.io/paper/getting-started/) |
| Paper | Minecraft `26.2`, build `121`, stable | [Official build metadata](https://fill.papermc.io/v3/projects/paper/versions/26.2/builds/121); SHA-256 recorded in manifest |
| Geyser | `2.11.2`, build `1234`, commit `9b65a39` | [Official build metadata](https://download.geysermc.org/v2/projects/geyser/versions/2.11.2/builds/1234); SHA-256 recorded in manifest |
| Floodgate | `2.2.5`, build `140`, commit `8780fa4` | [Official build metadata](https://download.geysermc.org/v2/projects/floodgate/versions/2.2.5/builds/140); SHA-256 recorded in manifest |

Geyser's [compatibility documentation](https://geysermc.org/wiki/geyser/supported-versions/) and the pinned runtime's `geyser version` output identify Bedrock 26.0–26.45 support and Java 26.2 compatibility. Bedrock clients can update automatically; confirm compatibility again before a launch or controlled content update. Family-device acceptance remains issue #9.

The itzg image uses `TYPE=CUSTOM` and `FAMILY=SPIGOT` with a local, read-only Paper JAR. This avoids itzg's runtime Paper build lookup and moving default-configuration downloads. The [custom-server startup implementation](https://github.com/itzg/docker-minecraft-server/blob/da99021d880273b4fd5c1bf86a9a8d2489e0be43/scripts/start-deployCustom) supports a local `CUSTOM_SERVER` path. Paper's own bundled bootstrap dependencies are versioned by the pinned Paper JAR; Geyser's bundled mappings/resources are selected by its pinned build. No artifact selector in the desired manifest uses `latest`.

## Access and persistent state

Startup downloads are bounded, SHA-256 checked before publication, and checked again whenever an existing artifact cache is used. Corruption fails the operation; remove the corrupt cache entry and retry only after investigating it. The read-only plugin source is synced into `/data/plugins` on each start, replacing the two managed JARs and managed Geyser configuration even if destination timestamps are newer. Floodgate's generated key and other mutable plugin files remain in retained data. Do not install extra JARs manually into this instance; make content changes through its reviewed manifest and update operation.

The generated Geyser configuration uses Floodgate authentication. Java stays in online mode. Both whitelist settings are enabled, and an empty new whitelist denies unlisted players. No public roster or operators are included. Use the instance allowlist command for Java accounts and Floodgate's [`fwhitelist` command](https://geysermc.org/wiki/floodgate/features/) for Bedrock accounts; a Bedrock UUID may be used without publishing a family gamertag. Craig's operator identity is a private launch input.

`enforce-secure-profile=false` follows [Geyser's signed-chat compatibility requirement](https://geysermc.org/wiki/geyser/supported-versions/). It does not disable Java online authentication. Floodgate and Geyser run together, so Geyser obtains the Floodgate key from the installed plugin as described in the [official setup](https://geysermc.org/wiki/floodgate/setup/).

RCON has no published host port. Paper's [`rcon.ip` setting](https://github.com/PaperMC/Paper/blob/a2a42c5b12249aaba42a347327fd930a1f94af06/paper-server/patches/sources/net/minecraft/server/rcon/thread/RconThread.java.patch) additionally binds it to container loopback. Operations invoke `docker compose exec -T minecraft rcon-cli`. Compose has `restart: "no"`; every new start must pass the host's fresh storage gate. Bind mounts disable automatic creation of source paths.

For Paper 26.2, the observed world layout is `data/world/level.dat` plus `data/world/dimensions/minecraft/{overworld,the_nether,the_end}/`. Player data, stats and advancements use `world/players/{data,stats,advancements}/`; [Minecraft's 26.1 release notes](https://www.minecraft.net/en-us/article/minecraft-java-edition-26-1) describe the storage migration. Do not assume older Paper's `world_nether` / `world_the_end` or root `playerdata` layout. Back up the entire instance data directory, including player records, allowlist/operators, plugin state, and Floodgate key, together with the exact runtime manifest and desired configuration.

## Explicit local runtime validation

Ordinary `scripts/check.sh` does not accept the EULA or launch Minecraft. On a Linux ARM64 Docker engine, create a private operator JSON file with `"eula_accepted": true` only after acceptance has been supplied by the operator, set mode `0600`, then run:

```sh
python3 scripts/validate_runtime.py \
  --operator-config /private/path/operator.json \
  --output-dir /private/path/new-runtime-evidence
```

The output directory must be new. It contains the secret-bearing Compose JSON, retained test data, exact downloaded artifacts, container logs, and `report.json`; do not commit or publish the directory. The command uses a unique Compose project and temporary loopback game ports, retains its data, and removes its own stopped containers/network. It does not operate any other containers or AWS resources. If clean shutdown cannot be confirmed, it retains the runtime for inspection instead of force-killing it to report success.

The test checks real Minecraft readiness, loaded crossplay plugins, a Bedrock UDP discovery response, runtime access settings, and RCON's loopback listener. It creates a diamond-block fixture in each dimension, saves and exits cleanly, adds synthetic player NBT/stats/advancement records and mutable plugin markers while stopped, recreates the container using the same retained data, then verifies every dimension fixture and preserved player/plugin/Floodgate bytes after another clean shutdown. Synthetic player persistence is distinct from authenticated family login. Local bind persistence is distinct from EBS mount validation, S3 recovery, live EC2 operations, and family console testing.

## Recorded runtime result

The explicit command passed on 2026-09-08 using Docker Engine 29.7.2 on Linux ARM64, the full configured 5 GiB heap, and a two-CPU container limit. Manifest SHA-256 was `9562abd45e889c598a5f8883d1b37e9be0c5d39a3fb10ad8be1194977625b9bf`. Paper reported `26.2-121-main@a2a42c5`, Geyser reported `2.11.2-b1234`, and Floodgate loaded build `140`. Bedrock UDP discovery returned a valid `MCPE` response advertising protocol `2169`, version `26.45`, and six player slots.

The run completed two healthy starts and two clean saves/shutdowns with exit code zero, verified a different container ID after recreation, and checked retained diamond blocks in all three dimensions. Eight fixture/state files were unchanged: synthetic player NBT/stats/advancements, two mutable plugin markers, both plugin configurations, and the real generated Floodgate key. The runtime's actual RCON socket was limited to loopback; only the temporary game ports were published, both on host loopback. Its stopped test containers and network were removed, and private retained test data/report/logs were kept for review. No authenticated player or family device connected, and no AWS or S3 operation was performed.
