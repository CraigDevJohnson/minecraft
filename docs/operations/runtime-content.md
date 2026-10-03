# Runtime content and local validation

`runtime/survival.json` defines the first instance's desired ports, six-player limit, 5 GiB heap, and view/simulation distances. `runtime/content.json` pins the Linux ARM64 image, every externally selected server/plugin JAR, and the complete payload and integrity metadata for managed plugin configuration. The Compose document is generated deterministically by `minecraft_content.render_compose`; keep the generated JSON private because it contains the operator's RCON secret and explicit EULA acceptance.

## Verified selection

Revision `survival-2026-10-03-dual-transport` adds the dual-transport configuration approved on October 3. It keeps the stable artifacts resolved from official metadata on October 2. Local staging rechecks each downloaded JAR against its unchanged SHA-256:

| Component | Exact selection | Integrity / authoritative metadata |
| --- | --- | --- |
| itzg Minecraft image | ARM64 `sha256:fa70ecc878ed641a89bc7dceaf43d8160faa351d9da4b48a2d9973f6ac9cb594` (unchanged since 2026-09-08) | Pulled the platform manifest, inspected image revision `da99021d880273b4fd5c1bf86a9a8d2489e0be43`, and ran `java -version` |
| Java | Eclipse Temurin `25.0.4+7-LTS` | Included in the immutable image; [Paper requires Java 25 for 26.1+](https://docs.papermc.io/paper/getting-started/) |
| Paper | Minecraft `26.2`, build `129`, `STABLE` channel, commit `9240f58` | [Official build metadata](https://fill.papermc.io/v3/projects/paper/versions/26.2/builds/129); SHA-256 `b1d8f6bf…eb083` |
| Geyser | `2.11.3`, build `1247`, commit `63a4e2b` | [Official build metadata](https://download.geysermc.org/v2/projects/geyser/versions/2.11.3/builds/1247); SHA-256 `6fed2d97…fd7a6` |
| Floodgate | `2.2.5`, build `141`, commit `81b65cc` | [Official build metadata](https://download.geysermc.org/v2/projects/floodgate/versions/2.2.5/builds/141); SHA-256 `21570aff…6fd7e` |
| ViaVersion | `5.12.0` GitHub release (2026-09-18, not a prerelease) | [Official release](https://github.com/ViaVersion/ViaVersion/releases/tag/5.12.0) asset `ViaVersion-5.12.0.jar`; SHA-256 `72c40a6a…750b0` matches GitHub's asset digest |

The manifest records the full checksums. Paper, Geyser and Floodgate come from CI build services and carry their exact `build` number. ViaVersion is a GitHub release asset with no CI build number, so its manifest entry deliberately has no `build` field. Its exact release tag is its `version`, and the loader accepts only that tag's official asset URL, `https://github.com/ViaVersion/ViaVersion/releases/download/<version>/ViaVersion-<version>.jar`. Each artifact may come only from its own official host. The loader accepts exactly Paper, Geyser, Floodgate and ViaVersion at their fixed paths (`paper.jar`, `plugins/Geyser-Spigot.jar`, `plugins/floodgate-spigot.jar`, `plugins/ViaVersion.jar`). It rejects unknown, missing or duplicate artifacts.

### Supported clients

- **Java Edition:** 26.2 (protocol 776) connects natively. Java 26.3 (protocol 777) connects through ViaVersion 5.12.0, which added 26.3 client support. Geyser's [supported-versions documentation](https://geysermc.org/wiki/geyser/supported-versions/) names ViaVersion as the plugin that lets newer Java clients join a server running an older version. Geyser itself still connects to the server as a 26.2 client.
- **Bedrock Edition:** Geyser 2.11.3 accepts 26.30–26.52 using protocols 1001, 2168, 2169 and 2193. Current 26.50–26.52 clients use protocol 2193. Geyser's version list stops at the label 26.51, and its discovery answer advertises protocol `2193`, version `26.51`. The previous pin, Geyser 2.11.2-1234, stopped at Bedrock 26.45 (protocol 2169), so it rejected every current Bedrock client.

Bedrock clients can update automatically; confirm compatibility again before a launch or controlled content update. Geyser 2.11.3 marks RakNet as deprecated and slated for removal in Bedrock 26.60. This revision selects `both` with built-in signaling and preserves RakNet during the transition. It does not add 26.60 protocol support; a qualifying release, approved deployment and real old/new-client joins remain #26 acceptance gates. Family-device acceptance remains issue #9.

### Why the server stays on Minecraft 26.2

On 2026-10-02, Paper 26.3 had only `ALPHA` and `BETA` builds and no `STABLE` build. No released Geyser build supported Java 26.3: [Geyser PR #6712](https://github.com/GeyserMC/Geyser/pull/6712) ("Update to Java 26.3") was still open. Geyser targets exactly one Java server version, so the server stays on stable Paper 26.2 and adds ViaVersion for 26.3 Java clients.

A later move to 26.3 is a one-way world upgrade. When Paper 26.3 opens the world, it converts the data, and Paper 26.2 does not support loading data saved by a newer version. Before that update:

- Retain the 26.2 manifest and a verified recovery point.
- Treat rollback as an [isolated restore](backup-recovery.md) of that point, never as reinstalling 26.2 binaries over upgraded data.
- Re-evaluate ViaVersion then. It is no longer needed for 26.3 clients once the server itself runs 26.3.

The itzg image uses `TYPE=CUSTOM` and `FAMILY=SPIGOT` with a local, read-only Paper JAR. This avoids itzg's runtime Paper build lookup and moving default-configuration downloads. The [custom-server startup implementation](https://github.com/itzg/docker-minecraft-server/blob/da99021d880273b4fd5c1bf86a9a8d2489e0be43/scripts/start-deployCustom) supports a local `CUSTOM_SERVER` path. Paper's own bundled bootstrap dependencies are versioned by the pinned Paper JAR; Geyser's bundled mappings/resources are selected by its pinned build. No artifact selector in the desired manifest uses `latest`.

## Access and persistent state

Startup downloads are bounded, SHA-256 checked before publication, and checked again whenever an existing artifact cache is used. Corruption fails the operation; remove the corrupt cache entry and retry only after investigating it. The read-only plugin source is synced into `/data/plugins` on each start, replacing the three managed plugin JARs (Geyser, Floodgate and ViaVersion) and managed Geyser configuration even if destination timestamps are newer. Floodgate's generated key, ViaVersion's generated `plugins/ViaVersion/` configuration, and other mutable plugin files remain in retained data. Do not install extra JARs manually into this instance; make content changes through its reviewed manifest and update operation.

`managed_files` records each managed file's destination, structured JSON payload, `canonical-json-v1` encoding, and SHA-256. The encoding is UTF-8 JSON with sorted keys, compact separators (`,` and `:`), literal Unicode rather than ASCII escapes, and exactly one final LF. The resulting JSON document is written as Geyser's `plugins/Geyser-Spigot/config.yml`; the manifest contains everything needed to reproduce its exact bytes. The loader checks the checksum and the approved typed Geyser v8 authentication, UDP and privacy schema before staging. A reviewed schema change must update the payload and its checksum in the manifest.

The approved Geyser v8 payload selects `bedrock.transport: both`, `bedrock.webrtc-port: 19133`, and `bedrock.signaling.mode: builtin`. It retains Bedrock address `0.0.0.0`, port `19132`, `clone-remote-port: false`, Floodgate authentication, disabled player-IP logging and empty saved logins. Its canonical file SHA-256 is `961fc91c79c38f8e09fd14d1e959b06522ec72b595b933e78fef83563e4b8e42`; manifest SHA-256 is `760f0497dabd4cea0713e2b0d73c584c887cb91519bfb732f58e723cb400affd`. Unapproved transport, signaling services/tokens, TLS keys, missing fields or scalar-type changes are refused. No additional secret or certificate is supplied by this revision.

The listeners and Docker/AWS ingress are Java TCP `25565`, RakNet UDP `19132`, built-in signaling TCP `19132`, and WebRTC UDP `19133`. The [pinned implementation](https://github.com/GeyserMC/Geyser/blob/63a4e2b79b12f0d138777d5fd80176a112b4bd72/core/src/main/java/org/geysermc/geyser/network/bedrock/nethernet/NetherNetServer.java) advertises additional IPs through `geyserAdvertiseAddresses`, retaining the gathered UDP port. The native host controller reads its current global IPv4 from the fixed IMDSv2 endpoint with proxies and redirects disabled, then supplies that one JVM property each time it prepares a start. A failed or invalid lookup refuses launch. WebRTC therefore publishes `19133:19133/udp`; translating it to another host port is unsupported by this pin. Local harnesses substitute loopback only and preflight that UDP port without stopping other containers.

Built-in signaling selects no external STUN/TURN service. The [pinned native ICE agent](https://github.com/opencollab-incubator/libjuice/blob/2a00d1cedaf78b52496b0a6233fc092609df616d/src/agent.c#L1780) initiates checks to client/NAT-selected UDP candidate ports. The family security group adds UDP egress destination ports `1–65535` to `0.0.0.0/0`, beside existing TCP `80/443`. Stateful reply allowance alone does not cover the first initiated check. This outbound broadening belongs in the concrete activation plan review. The isolated recovery root keeps its existing no-ingress/TCP-web-only rules; it does not accept external Bedrock peers.

Clients add the private family hostname with port `19132`, the signaling address as well as the retained RakNet address. They do not enter the WebRTC port separately. Built-in signaling uses HTTP and presents a first-use trust prompt instead of a hostname certificate. Confirm the intended hostname before accepting, preserve `plugins/Geyser-Spigot/nethernet/identity.pem` through backup/restore, and investigate an unexpected identity change. The exact client's transport-selection UI and PlayStation/Switch BedrockConnect transfer still require real-device evidence; see [device validation](family-device-validation.md).

Completed recovery points still recognize the exact earlier RakNet v8 manifest `72f0640583a5fec446ccd299cbbf35c5aff6815a358eb251cece7975bd148c82` and v7 manifest `ebe2fd621e9328dd708aa77918dbd96f8039670cddc3364c4f45e582b931c29e`. Catalog/status/prune accept those recorded policies only for those exact digests; tampered earlier manifests are invalid. This release stages only the new payload. Restoring an earlier point still requires its matching definition, manifest and installed release. Stop and back up with the old release before staged installation and `update survival`. Preserve the old release archive and recovery point; a code merge does not activate this transport.


The manifest digest includes these configuration fields, so changed managed bytes produce a different content revision and enter the controlled update/backup path. Existing cached configuration is verified and never silently replaced after an integrity mismatch. File publication is atomic. Code releases no longer generate unrecorded plugin configuration values during staging. Other plugin defaults come from the pinned JARs; mutable configuration and authentication keys are retained and backed up as instance data.

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

The output directory must be new. It contains the secret-bearing Compose JSON, retained test data, exact downloaded artifacts, container logs, and `report.json`; do not commit or publish the directory. The command uses a unique Compose project, temporary loopback Java/signaling/RakNet ports and fixed loopback WebRTC UDP `19133`, retains its data, and removes its own stopped containers/network. It does not operate any other containers or AWS resources. If clean shutdown cannot be confirmed, it retains the runtime for inspection instead of force-killing it to report success.

The test checks real Minecraft readiness and requires Geyser, Floodgate and ViaVersion to be loaded. Geyser's Bedrock UDP discovery response must advertise the pinned default, protocol `2193` and version `26.51`. On the loopback Java port, two status pings must succeed: a handshake with protocol `776` (Java 26.2) must be answered with `776`, and one with protocol `777` (Java 26.3) must be answered with `777`, which ViaVersion provides. The test calls the pinned signaling `GET /v1/join` endpoint and requires its typed protocol/version/capacity, NetherNet transport and authenticated-admission metadata. An unsigned SDP `POST` must return `401`; it creates no ICE peer. A separate no-network helper compiles with the official ARM64 Temurin JDK image `sha256:12f85590d237a882284d98f76886e6395a50764e91038d13a8a87b4f03b8b64b` and runs in the pinned server JRE with the checksum-matching Geyser JAR. Two synthetic native peers exchange reliable and unreliable nonce echoes over ICE/DTLS/SCTP using UDP `19133`. The report distinguishes that bundled-library capability from configured Geyser authenticated admission and external Docker/AWS UDP reachability, which remain explicitly unverified. No token is fabricated and production authentication is unchanged.

The test also checks runtime access settings, RCON's loopback listener, and that Geyser left the managed `config.yml` bytes unchanged. It preserves the generated first-use trust identity alongside plugin/player fixtures. It creates a diamond-block fixture in each dimension, saves and exits cleanly, adds synthetic player NBT/stats/advancement records and mutable plugin markers while stopped, recreates the container using the same retained data, then verifies every dimension fixture and preserved player/plugin/Floodgate bytes after another clean shutdown. Synthetic player persistence is distinct from authenticated family login. Local bind persistence is distinct from EBS mount validation, S3 recovery, live EC2 operations, and family console testing.

## Recorded runtime result

Current dual-transport validation evidence is recorded after the final explicit run below. Authenticated Geyser WebRTC admission, actual household joins and 26.60 compatibility remain unverified.

### Previous revision `survival-2026-10-02-bedrock-2650-viaversion`

The explicit command passed on 2026-10-02 for revision `survival-2026-10-02-bedrock-2650-viaversion` (manifest SHA-256 `72f0640583a5fec446ccd299cbbf35c5aff6815a358eb251cece7975bd148c82`) using Docker Desktop Engine 29.8.1 on Linux ARM64, the full 5 GiB heap, and a two-CPU container limit. Paper reported `26.2-129` (`9240f58`), Geyser `2.11.3-b1247` (`63a4e2b`), Floodgate `2.2.5` build `141` (`81b65cc`), and ViaVersion `5.12.0`, all loaded without errors. Bedrock UDP discovery advertised `MCPE` protocol `2193`, version `26.51`, six slots. Java status handshakes with protocol `776` and `777` were answered with `776` and `777` respectively, both as `Paper 26.2`. Geyser left the managed `config.yml` unchanged (SHA-256 `a98d3442120aa0cfe4eb63e28eebb935b01e7c52b0aa1d8e55bb6d618d1c442f`). The run completed two healthy starts (Paper ready in 129.6 s with world creation, then 101.7 s) and two clean shutdowns, recreated the container, and preserved every dimension fixture and player/plugin/Floodgate state file. The lifecycle and recovery harnesses have not been rerun for this revision.

### Previous revision `survival-2026-09-08-managed-config`

The explicit command passed on 2026-09-08 using Docker Engine 29.7.2 on Linux ARM64, the full configured 5 GiB heap, and a two-CPU container limit. Manifest SHA-256 was `ebe2fd621e9328dd708aa77918dbd96f8039670cddc3364c4f45e582b931c29e`. Paper reported `26.2-121-main@a2a42c5`, Geyser reported `2.11.2-b1234`, and Floodgate loaded build `140`. Bedrock UDP discovery returned a valid `MCPE` response advertising protocol `2169`, version `26.45`, and six player slots.

Geyser accepted the manifest-backed canonical JSON `config.yml`; the staged and final retained bytes matched its SHA-256 `ccaa9a5d704431af409ea334068555419571ac56a9861fb3d1c6ff2881d4b38d` exactly. The run completed two healthy starts and two clean saves/shutdowns with exit code zero, verified a different container ID after recreation, and checked retained diamond blocks in all three dimensions. Eight fixture/state files were unchanged: synthetic player NBT/stats/advancements, two mutable plugin markers, both plugin configurations, and the real generated Floodgate key. The runtime's actual RCON socket was limited to loopback; only the temporary game ports were published, both on host loopback. Its stopped test containers and network were removed, and private retained test data/report/logs were kept for review. No authenticated player or family device connected, and no AWS or S3 operation was performed.

## Controller integration with real Docker

The separate lifecycle harness exercises the actual `minecraft_instance.Manager` and `LinuxMachine` together:

```sh
python3 scripts/validate_lifecycle.py \
  --operator-config /private/path/operator.json \
  --output-dir /private/path/new-lifecycle-evidence
```

It uses a unique configured instance and Compose project, the canonical content renderer, real Docker preparation/start/readiness, real RCON warning/save/shutdown, durable controller journals, a real bounded file lock, and the backup-hook subprocess/request/receipt protocol. The generated game ports bind only to loopback. Its test subclass substitutes the Linux root/service/EBS/ownership prerequisites, boot ID, inventory of unrelated local containers, and root-owned backup-executable ancestry. That last substitution permits only the exact fixture interpreter/script/receipt paths and checks the script's bytes; no production CLI option can bypass these checks. The backup subprocess returns explicitly marked fixture completion evidence and records its input privately. It does not copy a recovery point or contact S3.

The sequence first injects an interruption after real preparation creates data but before a container executes, then requires a newly constructed controller to reconcile and retry successfully. It confirms duplicate start, a real clean stop followed by persisted failure because the backup hook is absent, restart refusal while that backup is unresolved, successful retry with the same session ID, and duplicate stop without another receipt. It then checks the same-boot drain barrier, simulates a new boot, deliberately submits an invalid Compose launch flag while the previous container remains stopped, and verifies that a normal retry preserves the completed backup and starts a new session. A running host drain saves, stops and records the second session's fixture receipt. A real world block and Floodgate key must remain intact across the controller-managed restart. The simulated boot and inventory boundaries do not establish actual EC2 stop/start or whole-host inventory behavior; those remain separately tested at their controlled boundaries.

This controller integration passed on 2026-09-08 against the previous ARM64 manifest `ebe2fd621e9328dd708aa77918dbd96f8039670cddc3364c4f45e582b931c29e`; it is pending re-validation for the current revision. That run had two healthy starts, two clean stops and two distinct fixture backup receipts, and every rejection/retry check above passed. The intentional real Compose prelaunch failure left the stopped container ID and `StartedAt` unchanged; status recovered the completed session's backup evidence, and the following real start succeeded. The test container/network were removed after clean shutdown. Private operation journals, fixture hook requests, container logs and the report were retained for review.

Credential-free tests cover:

- reconstruction from a saved manifest and configuration revision changes;
- missing or incorrect integrity metadata;
- the exact four-artifact set and ViaVersion's release-asset identity;
- the required typed Geyser v8 access settings;
- catalog retention for recovery points recorded under the earlier reviewed manifest;
- the runtime validator's Bedrock discovery, plugin and Java status-handshake expectations;
- rejection of cache corruption without silently rewriting its bytes.
