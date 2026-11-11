# Crafty Controller host

Each world runs on a host of its own ([ADR 0009](../adr/0009-one-host-per-world.md)): one Paper server run by Crafty Controller 4.11.0, with Geyser and Floodgate for Bedrock players and the mods that world has on. Each world has its own address, `<world>.<the public hostname>`: Java on TCP 25565, and Bedrock on port 19132, over RakNet (UDP 19132) or NetherNet (signaling on TCP 19132, WebRTC on UDP 19133). A world host sets Crafty up itself at its world's first start; nothing is installed by hand. The by-hand build this runbook once described was the retired family host's, and is kept short [at the end](#the-retired-family-hosts-build-by-hand). Every AWS change below needs Craig's approval of the reviewed plan first.

## What is pinned

[`runtime/crafty/server.json`](../../runtime/crafty/server.json) pins the Crafty image by digest and every jar by version and SHA-256. Its base, which every world's server holds, is Paper 26.3 build 242, Geyser 2.12.1 build 1288 and Floodgate 2.2.5 build 141. Its catalog of mods is GriefPrevention 16.18.8, FastAsyncWorldEdit 2.16.2, AuraSkills 2.4.0, DecentHolograms 2.10.2 and GSit 3.8.0, and each world lists the mods it may run ([worlds](worlds.md#what-the-pinned-content-holds)). The Multiverse plugins left on 2026-11-10 with the family server's worlds. Paper 26.3 build 242 and Geyser build 1288 are the newest builds on 2026-11-05 ([readiness](client-compatibility-readiness.md#november-5-2026-geyser-build-1288-and-paper-build-242)). Paper 26.3 replaced Paper 26.2 build 129 for [#27](https://github.com/CraigDevJohnson/minecraft/issues/27), first as build 227 on 2026-10-31, and ViaVersion 5.12.0 left with it, because Geyser 2.12.1 speaks Java 26.3 to the server and Floodgate 2.2.5 build 141 is its 26.3 update. Geyser 2.12.1 is the first release that accepts Bedrock 26.60 (it accepts 26.40 through 26.60 and drops 26.30); it replaced the proven 2.11.3 on 2026-10-27, and an iPhone on Bedrock 26.60 joined it over NetherNet on 2026-10-29 ([#26](https://github.com/CraigDevJohnson/minecraft/issues/26)). The paragraph below is the family server's record; the worlds start fresh on Paper 26.3.

The first start on Paper 26.3, on 2026-10-31 ([#27](https://github.com/CraigDevJohnson/minecraft/issues/27#issuecomment-6541645064)), upgraded the world's data in place, one way: Paper 26.2 will not load it afterwards. A later 26.3 build changes no world format. The adapter applies a changed `paper.jar` only to a server folder that is exactly as its last archive stored it, and otherwise fails the start and asks for one stop ([ADR 0008](../adr/0008-the-adapter-applies-the-pinned-server-content.md)), so the archive from the stop before that start is always the recovery point. For that upgrade it was the archive stored by the stop that ended the session of 2026-10-29, which the [Server control](cloud-controls.md#the-server-control-workflow) `stop` [run 40831523919](https://github.com/CraigDevJohnson/minecraft/actions/runs/40831523919) followed to `stopped` at 06:50Z; the launcher reports `stopped` only after the host's drain stored a complete archive and EC2 stopped the host. To roll back to 26.2: stop the server, revert the pin (Paper 26.2 build 129 and ViaVersion 5.12.0, revision `family-2026-10-27-geyser-2121-bedrock-2660` in git history) and deploy it, then [restore that archive](#recover-from-an-archive), which starts the game on Paper 26.2. Reinstalling 26.2 over the upgraded world is not a rollback. The adapter removed `plugins/ViaVersion.jar` at that first start, from the record of applied content it kept then (since 2026-11-10 a start removes every jar in `plugins/` it does not want, with no record); the plugin's own `plugins/ViaVersion/` folder stays, unused, until someone deletes it in a [shell on the host](#open-a-shell-on-the-host).

It also pins Geyser's configuration, which selects the Bedrock transports. Since #26 it is `both`: RakNet on UDP 19132 alongside NetherNet, with Geyser's built-in signaling on TCP 19132 and WebRTC on UDP 19133. There is no TLS and no external signaling service, so a player's device asks once whether to trust the server. From configuration version 12 (Geyser build 1280 and later) it also names the server `Family Minecraft` with 10 slots, which Bedrock lists whenever Geyser cannot read them from Paper; Geyser's own fallback is `Geyser` with 100 slots. The launcher carries this file, and every start brings the stopped server to its world's part of it: the base jars and the jars of the mods that are on, Geyser's configuration and those mods' managed files, Crafty's published ports and the host's current public address in the launch command ([ADR 0008](../adr/0008-the-adapter-applies-the-pinned-server-content.md)). A change merged to `main` therefore reaches each world at its first start after the Deploy. A new Paper is applied only when the server folder is exactly as its last archive stored it, so stop the server once first if anything ran since.

## Infrastructure inputs

Use the existing OpenTofu root. The worlds need nothing new in the private `deployment.tfvars`: OpenTofu reads the world ids from `server.json` and makes each world's host, parameters and hostname (`<world>.<public_hostname>`) from the inputs already there.

- No input names a world: the host role may store archives under each world's `instances/<world>/` without the worlds being named.
- `bedrock_transport = "both"`, the default, so the security group the hosts share admits signaling TCP 19132 and WebRTC UDP 19133 and lets ICE checks out. Set `"raknet"` only with a manifest whose Geyser configuration selects RakNet alone.
- No `additional_game_ingress`: every world uses the same ports on its own host.
- A world host has no data disk, and no input sizes its root disk. The retired family host's `data_filesystem_uuid`, `data_volume_size_gib`, `instance_ids` and `launcher.server_name` are no longer read ([worlds](worlds.md#the-family-hosts-retirement)).

## How a world host sets itself up

OpenTofu launches each world host with `scripts/world-bootstrap.sh.tftpl` as its user data. On Ubuntu 24.04 arm64 it:

1. creates `/opt/minecraft-crafty/scripts` (root's, closed to everyone else, as the launcher's adapter install requires), `/etc/minecraft`, and `/srv/minecraft/crafty/` with its `backups`, `logs`, `servers`, `config`, `import`, `state` and `staging` folders, on the root disk;
2. writes `/etc/minecraft/operator.json` (mode 0600) with the world's id as its one instance, the backup bucket and the region;
3. installs the `/usr/local/sbin/minecraft-instance` wrapper and `minecraft-crafty.service`, whose stop runs the adapter's drain before the containers stop;
4. installs Docker with Compose, and the AWS CLI through the reviewed `scripts/install_aws_cli.sh` it carries;
5. enables the service without starting it (the unit is skipped until `compose.yaml` exists), writes `/var/lib/minecraft/bootstrapped` last, and powers the host off.

A new host therefore costs nothing but its disk until its world is started. A run that fails powers the host off too, and the script runs again at every boot until the marker is there; a later boot that finishes the set-up stays up, because a start is waiting for it. The script holds nothing from `server.json`, so a content change never replaces a host. A change to the script, or to `scripts/install_aws_cli.sh`, replaces every world host at the next apply.

The rest happens inside the world's first `start`, under the adapter's lifecycle lock, until a marker written last (`/srv/minecraft/crafty/state/<world>-provisioned`) says it is done:

1. wait up to ten minutes for `/var/lib/minecraft/bootstrapped`, and fail with "the host's setup did not finish" without it;
2. render `/opt/minecraft-crafty/compose.yaml` and start `minecraft-crafty.service`;
3. make the adapter's API key, as `setup-api-key` does ([below](#create-the-api-key-and-operator-configuration)), trying again while Crafty is still starting;
4. create the server, unless Crafty already has one of the world's name: stage the pinned files for the world with the mods the start names, and import them ([below](#create-the-server));
5. look under `instances/<world>/crafty/` in the backup bucket. With an archive that has its `.sha256`, restore the newest. With objects there but no such archive, fail: a fresh world is never started over them. With nothing there, keep the fresh server;
6. write the marker.

Each step checks whether an earlier start did it, so a start cut off by the launcher's deadline is carried on by the next. Until the marker is there no game starts on the host, and a drain answers `not-required` without archiving, so a half-set-up host can be stopped and a server that was created but not yet restored never becomes the newest archive. The start then goes on as every start does: it applies the mods and the allowlist the launcher sent and starts the game.

## Open a shell on the host

Every hands-on step on a world's host runs in a Session Manager shell opened in the browser, so nothing is installed or forwarded on a workstation ([ADR 0006](../adr/0006-cloud-controls.md)). The host must be running.

1. Sign in to the AWS console with the `WorkloadsAdmin` role and switch to US West (Oregon).
2. Open EC2, Instances, and select the world's host, named `<resource_prefix>-<world>`, such as `minecraft-family-survival`.
3. Choose **Connect**, the **Session Manager** tab, then **Connect**.

The shell opens as `ssm-user` in `sh`; the commands below start with `sudo`, which that user may use. Session Manager closes a shell left idle for 20 minutes.

## Send a console command

The adapter's `console` action, run in a shell on a world's host with the world's id as the instance, sends one line to the running server's console through Crafty's loopback API and prints what the server logged in the next 3 seconds (at most 200 lines):

```sh
sudo /usr/local/sbin/minecraft-instance console survival 'list'
```

It prints one line of JSON, such as `{"instance": "survival", "log": ["[12:00:00] [Server thread/INFO]: There are 0 of a max of 20 players online: "], "sent": "list"}`. Put the command in single quotes, without the leading `/`. Nobody is an operator in game, so this is the way to run an operator's command. The server must be running: start the world with the [Server control workflow](cloud-controls.md#the-server-control-workflow) first. The action refuses while the host drains for a stop.

## Everyday start and stop

The launcher starts and stops each world; the `/minecraft` page in The Lobby, the Server control workflow and `task server:*` only ask it ([cloud controls](cloud-controls.md)).

A start of a world starts its host, installs the adapter and `server.json` the launcher carries, updates the world's DNS record, and runs `minecraft-instance start <world>` with the mods that are on and the family's allowlist. That brings the stopped server to the pinned content ([What is pinned](#what-is-pinned)): it downloads and checks any jar it lacks, removes every other jar directly in `plugins/`, writes the managed files, and writes the allowlist over `whitelist.json`. Then it asks Crafty to start the server and waits until the Java port answers. On a new or replaced host the same start first [sets the host up](#how-a-world-host-sets-itself-up).

A stop runs `minecraft-instance host-drain`, which asks Crafty to stop the server and streams an archive of the stopped server folder to `s3://<bucket>/instances/<world>/crafty/<UTC time>.tar.gz` (no second copy on the host and no 5 GB limit). It verifies the stored size and writes the SHA-256 beside it as `<UTC time>.tar.gz.sha256`. It keeps the newest 14 archives that have their `.sha256` and removes any archive older than those. Then the launcher stops EC2 and parks DNS. If the archive fails, EC2 is not stopped. Once a stop has begun, `start` is refused until the host has been stopped and started again, so a stop can never race a new session. Stopping the host any other way still runs the same drain from `minecraft-crafty.service`; if that drain fails, the unit is marked failed in the journal.

The archive matters: the world lives on its host's root disk, which goes when the host is replaced, so the newest archive is the only copy that outlives the host.

## How players join

Each world has its own address: `survival.<the public hostname>`, `creative.<the public hostname>` and `adventure.<the public hostname>`. A player adds each world they play as its own server, and changes world by joining another one. The `/minecraft` page shows each world's address while it is ready.

- **Java** (Mac or PC): Multiplayer, Add Server, the world's address. The default port 25565 needs no entry.
- **Bedrock on a phone, tablet or PC** (iPhone, iPad, Windows): Play, Servers, Add Server, the world's address with port `19132`. A client that still speaks RakNet joins over UDP 19132. A client that speaks NetherNet asks Geyser's built-in signaling on TCP 19132 and is then asked, once for each world, whether to trust the server; the player accepts. Bedrock 26.60 clients join only this way; a family iPhone on 26.60 first joined the family server so on 2026-10-29 ([#26](https://github.com/CraigDevJohnson/minecraft/issues/26)).
- **Consoles** (PlayStation, Switch, Xbox) cannot add a server. BedrockConnect is the optional route ([family device validation](family-device-validation.md)); it is untested, and #9 waived those tests.

Geyser keeps a server's NetherNet identity in `plugins/Geyser-Spigot/nethernet/` in the server folder. It creates it at the world's first start, so every archive of the world holds it and a restore, or a replaced host, brings it back. Deleting that folder makes every NetherNet player confirm that world again.

## Recover from an archive

Every archive is one world's whole server folder: the world, player data, plugin settings and the allowlist as it stood. (The next start writes the allowlist of record over the restored one.) The launcher puts one back from the cloud ([ADR 0006](../adr/0006-cloud-controls.md)):

1. **Find the archive's name.** In the AWS console, open S3, the backup bucket, then `instances/<world>/crafty/`. Each archive is named for the UTC time it was made, such as `20261016T031500Z.tar.gz`, with its `.sha256` beside it.
2. **Stop the world** with the [Server control workflow](cloud-controls.md#the-server-control-workflow)'s `stop`, if it is running. A restore runs only on a stopped server, or on a running host whose last start or restore failed.
3. **Restore it.** Run the Server control workflow with `action` set to `restore`, `world` set to the world and `archive` set to that name, or from a terminal run `WORLD=<world> task server:restore ARCHIVE=20261016T031500Z.tar.gz`. A world takes only its own archives. The S3 URI or key the console copies works too.

The launcher starts the host, points the hostname at it, and has the adapter stream the archive from S3, check it against its recorded SHA-256, and only then swap it in. Then it starts the game the way every start does. The run follows the server through `starting` to `ready`, for up to an hour; the `/minecraft` page shows `starting` the whole time, and the [idle stop](idle-stop.md) leaves the host alone.

A restore that fails reads `failed`, the alarm emails you, and the launcher's log holds the adapter's reason. Until its swap, the world in place is the one from before; a Member's **Start** then starts whatever world is in place, and a new `restore` tries again on the same run of the host, unless someone started the game by hand meanwhile: stop it first. Its reasons include a full disk and an archive that does not match its SHA-256. A restore unpacks beside the current folder, so the host's 20 GiB root disk needs free space for one more copy of the server; one that runs out fails with "the data disk filled up while unpacking the archive" (the adapter's wording, from the family host) and leaves the old world in place. Delete what earlier restores left in `/srv/minecraft/crafty/staging/` in a [shell on the host](#open-a-shell-on-the-host) and restore again.

The folder a restore replaces stays in `/srv/minecraft/crafty/staging/`. When its world is in S3 (the last stop archived it, or it was itself restored from an archive, and nobody played since), it is named `archived-<world>-<UTC time>`, and the next restore removes it, with any folder a restore cut off partway left behind. Otherwise, such as after the host went down without its drain, or on a host the adapter holds no backup record for yet, it is named `unarchived-<world>-<UTC time>` and kept until you delete it in a [shell on the host](#open-a-shell-on-the-host), because it may hold the only copy of that play. A restore refuses a server started from Crafty's page while it ran ("the server was started during the restore"); it asks the host whether a process in Crafty's container works in the server folder as well as asking Crafty, which reports a start at least half a second later. A shell of yours in that folder does not count. A start in the instant of the swap makes it fail after the swap instead, naming the folder it moved aside, which is then kept as `unarchived-<world>-<UTC time>` too, because the game may work in that folder and hold what is played there. To keep that play, stop the server from Crafty's page, which leaves the host running, and move that folder back in place of the server folder in a shell on the host. Otherwise stop it with a forced stop (Server control's `stop` with `nobody_playing` ticked) and restore again. Folders set aside by a restore live on the host's root disk, so they go when the host is replaced. If a restore is cut off partway, such as by a power loss, run it again once the server reads `failed` or `stopped`: it puts the archive in place whether or not the old folder is still there.

The family server's own archives are under `instances/family/crafty/`. No world restores them: they hold the Multiverse worlds, and a restore of a world takes only keys under that world's prefix.

After losing a world's host, or replacing it on purpose, nothing is restored by hand. The new host sets itself up, and the world's next start restores the newest archive that has its `.sha256` ([above](#how-a-world-host-sets-itself-up)). To bring back an older archive instead, start the world once so the host is set up, stop it, and restore the archive you want. Crafty's own login, API key and settings are not in the archive, and the new host makes them again. A jar from before a pin changed is not a concern: each start removes every jar in `plugins/` it does not want.

## Acceptance

Each world is accepted by its own [proof run](worlds.md#the-proof-run-for-a-world): a Java and a Bedrock join at the world's address with an unlisted account refused, the world's game mode and mods, a stop that stores an archive, a start that comes back with the same world, the health workflow's `nethernet_signaling_ok`, and, the first time its host is replaced, the same world restored from its archive. None has been run yet.

The family server passed its own acceptance in October 2026 (#53, #26, #27): joins by both editions, the portal to `creative` with separate inventories, a stop and a start with the same worlds, and a NetherNet join.

## The retired family host's build by hand

The family host of [ADR 0004](../adr/0004-crafty-multiverse-rebuild.md) was built by hand in October 2026, and these were its steps. That host was retired in November 2026 ([worlds](worlds.md#the-family-hosts-retirement)), and `scripts/install_crafty.sh` went with it; the steps are kept as a record of what a world's first start now does itself. A world host needs none of this; its first start does the same work itself ([above](#how-a-world-host-sets-itself-up)).

### Install Crafty on the host

After the family host's bootstrap had mounted its data disk at `/srv/minecraft`, the reviewed commit's `scripts/install_aws_cli.sh`, `scripts/install_crafty.sh`, the three adapter files and `runtime/crafty/server.json` went to the host as one hash-verified bundle through SSM Run Command, and ran as `sudo bash scripts/install_aws_cli.sh` and `sudo bash scripts/install_crafty.sh`. Run Command runs its commands with `sh`, so each script was called through `bash`. The installer wrote `compose.yaml`, the folders under `/srv/minecraft/crafty/`, the `minecraft-instance` wrapper and `minecraft-crafty.service`, the things a world host's user data writes now.

### Create the API key and operator configuration

`/etc/minecraft/operator.json` was written by hand with `"instances": {"family": {"server_name": "family", "java_port": 25565}}`; a world host's user data writes the same file with the world's id. Then `sudo /usr/local/sbin/minecraft-instance setup-api-key` made the adapter's key. A world's first start runs the same action, so this is still how the key is made:

On its first start Crafty writes its initial admin login to `/srv/minecraft/crafty/config/default-creds.txt`. `setup-api-key` signs in with that login through Crafty's API, creates a key named `lifecycle` with full access, checks that Crafty accepts it, and stores its token in `/etc/minecraft/crafty-api.token`. It then moves the login to `/etc/minecraft/crafty-admin.json`, outside the folder Crafty's container shares, and deletes `default-creds.txt`. Both files are root's alone (mode 0600), and neither secret is printed. It refuses to run again once the token file exists and `default-creds.txt` is gone, and it refuses while Crafty is still starting. If it is cut off after storing the token and Crafty then refuses that key, remove the token file in a [shell on the host](#open-a-shell-on-the-host) and start the world again. If it says `crafty-admin.json` is not a plain copy of the login in `default-creds.txt`, that file is incomplete from a cut-off run: move it away and start the world again.

### Create the server

Crafty 4.11 has no import-from-folder form: it imports only a `.zip` from its `import/upload` folder. On the family host `stage-import` staged the pinned files and that archive, `import-server family --archive-name family.zip --max-memory-gb 5` created the server through Crafty's API, and the staged files were removed. A world's first start does the same with the world's id as the server's name. `import-server` returns only after Crafty has finished unpacking the archive. The imported server runs `paper.jar` on the container's default Java 25 with at most 5 GB of memory and auto-start off, because the launcher's start starts the server. The staged `eula.txt` has no trailing newline on purpose: Crafty 4.11 compares its first line exactly and otherwise refuses every start without logging an error.

The family server's two Multiverse worlds, their portals and their inventory groups were then set up from the console by an operator account. None of that applies to a world host: each world is its server's only world, and nobody is made an operator.
