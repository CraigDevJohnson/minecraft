# Crafty Controller host

This runbook builds the family server described in [ADR 0004](../adr/0004-crafty-multiverse-rebuild.md): one Paper server run by Crafty Controller 4.11.0, with Geyser and Floodgate for Bedrock players and Multiverse for the `survival` and `creative` worlds. Everyone joins one address: Java on TCP 25565, and Bedrock on port 19132, over RakNet (UDP 19132) or NetherNet (signaling on TCP 19132, WebRTC on UDP 19133). Every AWS change below needs Craig's approval of the reviewed plan first.

## What is pinned

[`runtime/crafty/server.json`](../../runtime/crafty/server.json) pins the Crafty image by digest and every server jar by version and SHA-256: Paper 26.2 build 129, Geyser 2.12.1 build 1273, Floodgate 2.2.5, ViaVersion 5.12.0, Multiverse-Core 5.8.1, Multiverse-Inventories 5.3.6, Multiverse-Portals 5.3.0 and Multiverse-NetherPortals 5.1.0. Paper, Floodgate and ViaVersion are the versions already proven on the earlier stack. Geyser 2.12.1 is the first release that accepts Bedrock 26.60 (it accepts 26.40 through 26.60 and drops 26.30); it replaced the proven 2.11.3 on 2026-10-27, and the first Bedrock join after that deploy is its test. The Multiverse versions are their latest stable releases on 2026-10-10 and have not yet been run on this server, so the first session below is their compatibility test.

It also pins Geyser's configuration, which selects the Bedrock transports. Since #26 it is `both`: RakNet on UDP 19132 alongside NetherNet, with Geyser's built-in signaling on TCP 19132 and WebRTC on UDP 19133. There is no TLS and no external signaling service, so a player's device asks once whether to trust the server. The launcher carries this file, and every start brings the stopped server to it: jars, Geyser's configuration, Crafty's published ports and the host's current public address in the launch command ([ADR 0008](../adr/0008-the-adapter-applies-the-pinned-server-content.md)). A change merged to `main` therefore reaches the server at its first start after the Deploy. A new Paper is applied only when the server folder is exactly as its last archive stored it, so stop the server once first if anything ran since.

## Infrastructure inputs

Use the existing OpenTofu root with the default `lifecycle_mode = "retained-host"` and `data_ownership = "retained"`. In the private `deployment.tfvars`:

- `instance_ids = ["family"]`, so the host role may store archives under `instances/family/`.
- `bedrock_transport = "both"`, the default, so the security group admits signaling TCP 19132 and WebRTC UDP 19133 and lets ICE checks out. Set `"raknet"` only with a manifest whose Geyser configuration selects RakNet alone.
- No `additional_game_ingress`: the creative ports 25566 and 19134 are gone.
- A new `data_filesystem_uuid` for the new data disk, initialized as in [infrastructure operations](infrastructure.md).

The first retained-host apply on 2026-10-10 should have removed the stalled S3-only restore host and its blank 4 GiB disk, whose world was test data Craig chose not to keep. Nothing records that they are gone; the read-only check is listed in #70.

## Install Crafty on the host

After the host bootstrap has mounted `/srv/minecraft`, put the reviewed commit's `scripts/install_aws_cli.sh`, `scripts/install_crafty.sh`, `scripts/minecraft_crafty.py`, `scripts/minecraft_content.py`, `scripts/minecraft_access.py` and `runtime/crafty/server.json` in one root-only folder on the host. Keep their `scripts/` and `runtime/crafty/` layout, because the installer reads them relative to itself, and check each file's SHA-256 there against the commit's. On 2026-10-11 this went as one hash-verified bundle through SSM Run Command, with no checkout on the host. Run Command runs its commands with `sh`, which is not bash on this Ubuntu host, so send these lines as they are, each script called through `bash`. Never paste a bash script's body as the command: the first attempt to do that failed at its first line and changed nothing. Then, from that folder:

```sh
sudo bash scripts/install_aws_cli.sh
sudo bash scripts/install_crafty.sh
```

The installer writes `/opt/minecraft-crafty/compose.yaml` from the pinned server content, stores Crafty's configuration, servers and backups under `/srv/minecraft/crafty/`, installs the lifecycle adapter as `/usr/local/sbin/minecraft-instance` (its files and `server.json` in `/opt/minecraft-crafty/scripts`, which the launcher keeps up to date from then on, [ADR 0007](../adr/0007-the-launcher-carries-the-host-adapter.md)), and enables `minecraft-crafty.service`. Crafty's web UI and API listen on `127.0.0.1:8443` only, and nothing forwards that port: everything below reaches Crafty through the adapter, on the host. Each start renders `compose.yaml` again, so only a change to `minecraft-crafty.service` or the `minecraft-instance` wrapper needs the installer run again this way.

## Open a shell on the host

Every hands-on step on the host runs in a Session Manager shell opened in the browser, so nothing is installed or forwarded on a workstation ([ADR 0006](../adr/0006-cloud-controls.md)). The host must be running.

1. Sign in to the AWS console with the `WorkloadsAdmin` role and switch to US West (Oregon).
2. Open EC2, Instances, and select `minecraft-family-host`.
3. Choose **Connect**, the **Session Manager** tab, then **Connect**.

The shell opens as `ssm-user` in `sh`; the commands below start with `sudo`, which that user may use. Session Manager closes a shell left idle for 20 minutes.

## Create the API key and operator configuration

On a freshly installed Crafty, write `/etc/minecraft/operator.json` (root-owned, mode 0600), with the real backup bucket in place of `the-private-backup-bucket`:

```sh
sudo sh -c 'umask 077; cat > /etc/minecraft/operator.json' <<'JSON'
{
  "schema_version": 1,
  "runtime": "crafty",
  "crafty_url": "https://127.0.0.1:8443",
  "token_file": "/etc/minecraft/crafty-api.token",
  "data_root": "/srv/minecraft",
  "instances": {"family": {"server_name": "family", "java_port": 25565}},
  "backup": {"bucket": "the-private-backup-bucket", "region": "us-west-2"}
}
JSON
```

Then let the adapter create its API key:

```sh
sudo /usr/local/sbin/minecraft-instance setup-api-key
```

On its first start Crafty writes its initial admin login to `/srv/minecraft/crafty/config/default-creds.txt`. `setup-api-key` signs in with that login through Crafty's API, creates a key named `lifecycle` with full access, checks that Crafty accepts it, and stores its token in `/etc/minecraft/crafty-api.token`. It then moves the login to `/etc/minecraft/crafty-admin.json`, outside the folder Crafty's container shares, and deletes `default-creds.txt`. Both files are root's alone (mode 0600), and neither secret is printed. It prints:

```json
{"admin_file": "/etc/minecraft/crafty-admin.json", "key": "lifecycle", "token_file": "/etc/minecraft/crafty-api.token"}
```

If it is cut off after storing the token, such as by a power loss, run it again once Crafty has started: when Crafty accepts the stored key, it finishes moving the login and prints only the two files. If Crafty still refuses that key, it leaves the login in place; remove the token file and run it again. If the admin user then has two `lifecycle` keys in Crafty's panel, delete the older one, which the cut-off run had created. If it says `crafty-admin.json` is not a plain copy of the login in `default-creds.txt`, or holds an earlier Crafty login, that file is incomplete from a cut-off run, a link, or another Crafty's login: move it away and run it again.

It refuses to run again once the token file exists and `default-creds.txt` is gone, and it refuses while Crafty is still starting; run it again a minute later.

## Create the server

Crafty 4.11 has no import-from-folder form: it imports only a `.zip` from its `import/upload` folder. Stage the pinned files and that archive on the host, then create the server through Crafty's API:

```sh
sudo /usr/local/sbin/minecraft-instance stage-import \
  --manifest /opt/minecraft-crafty/scripts/server.json \
  --destination /srv/minecraft/crafty/import/family \
  --archive /srv/minecraft/crafty/import/upload/family.zip
sudo /usr/local/sbin/minecraft-instance import-server family --archive-name family.zip --max-memory-gb 5
sudo rm -rf /srv/minecraft/crafty/import/family /srv/minecraft/crafty/import/upload/family.zip
```

`import-server` returns only after Crafty has finished unpacking the archive in the background, so the cleanup on the last line cannot remove it too early. The imported server runs `paper.jar` on the container's default Java 25 with auto-start off, because the launcher's start starts the server. The staged `eula.txt` has no trailing newline on purpose: Crafty 4.11 compares its first line exactly and otherwise refuses every start without logging an error.

## Send a console command

The adapter's `console` action sends one line to the running server's console through Crafty's loopback API and prints what the server logged in the next 3 seconds (at most 200 lines):

```sh
sudo /usr/local/sbin/minecraft-instance console family 'list'
```

It prints one line of JSON, such as `{"instance": "family", "log": ["[12:00:00] [Server thread/INFO]: There are 0 of a max of 20 players online: "], "sent": "list"}`. Put the command in single quotes, without the leading `/`. The server must be running: start it with the [Server control workflow](cloud-controls.md#the-server-control-workflow) first. The action refuses while the host drains for a stop.

## Set up the worlds

Start the server once: in the same shell, run `sudo /usr/local/sbin/minecraft-instance start family`. Then send these console commands:

```sh
sudo /usr/local/sbin/minecraft-instance console family 'mv create creative normal'
sudo /usr/local/sbin/minecraft-instance console family 'mv modify creative set gamemode creative'
sudo /usr/local/sbin/minecraft-instance console family 'mvinv list'
```

Multiverse-Inventories keeps a world that is not in any group on its own inventory, so `creative` must not appear in the `default` group in `mvinv list`. If it does, send `mvinv remove-worlds default creative`. Creating a world can take longer than the 3 seconds the action waits; send `mv list` to see it once it is done.

Portals are made in game by an operator: send `op Steve` first, with the Java account's name in place of Steve. Build a frame at the `survival` spawn and run `/mvp wand`, which gives a wooden pickaxe. Left-click one corner block of the frame's opening and right-click the opposite corner, then run `/mvp create to-creative w:creative`. Make the return portal in `creative` the same way with `/mvp create to-survival w:survival`.

Multiverse-Portals lets only players with `multiverse.portal.access.<portal>` use a portal, and this server installs no permissions plugin, so by default only operators get through. With the server stopped, set `enforce-portal-access: false` under `portal-usage` in `plugins/Multiverse-Portals/config.yml` in the server folder (`/srv/minecraft/crafty/servers/<server id>/`). Multiverse-Core's `enforce-access` is already `false`, so entering `creative` needs no further permission.

Add each family player to the allowlist with the [Server control workflow](cloud-controls.md#the-server-control-workflow)'s `allowlist-add`, which looks the account up and confirms the change in `whitelist.json` ([allowlist](allowlist.md)). In a hands-on emergency, send `whitelist add` with a Java account's name, or `fwhitelist add` with a Bedrock gamertag, through the `console` action.

## Everyday start and stop

The launcher starts and stops the server; the `/minecraft` page in The Lobby, the Server control workflow and `task server:*` only ask it ([cloud controls](cloud-controls.md)).

A start starts EC2, installs the adapter and `server.json` the launcher carries, updates DNS, and runs `minecraft-instance start family`. That brings the stopped server to the pinned content ([What is pinned](#what-is-pinned)) and asks Crafty to start it, waiting until the Java port answers. A stop runs `minecraft-instance host-drain`, which asks Crafty to stop the server and streams an archive of the stopped server folder to `s3://<bucket>/instances/family/crafty/<UTC time>.tar.gz` (no second copy on the data disk and no 5 GB limit). It verifies the stored size and writes the SHA-256 beside it as `<UTC time>.tar.gz.sha256`. It keeps the newest 14 archives that have their `.sha256` and removes any archive older than those. Then the launcher stops EC2 and parks DNS. If the archive fails, EC2 is not stopped. Once a stop has begun, `start` is refused until the host has been stopped and started again, so a stop can never race a new session. Stopping the host any other way still runs the same drain from `minecraft-crafty.service`; if that drain fails, the unit is marked failed in the journal.

## How players join

- **Java** (Mac or PC): Multiplayer, Add Server, `mc.craigdevjohnson.com`. The default port 25565 needs no entry.
- **Bedrock on a phone, tablet or PC** (iPhone, iPad, Windows): Play, Servers, Add Server, `mc.craigdevjohnson.com` with port `19132`, the same entry as before NetherNet. A client that still speaks RakNet joins over UDP 19132. A client that speaks NetherNet asks Geyser's built-in signaling on TCP 19132 and is then asked, once, whether to trust this server; the player accepts. Bedrock 26.60 clients join only this way. The prompt has not yet been seen on the family's devices.
- **Consoles** (PlayStation, Switch, Xbox) cannot add a server. BedrockConnect is the optional route ([family device validation](family-device-validation.md)); it is untested, and #9 waived those tests.

Geyser keeps the server's NetherNet identity in `plugins/Geyser-Spigot/nethernet/` in the server folder. It creates it at the first start with both transports, so every archive stored after that start holds it and a restore brings it back. Restoring an archive from before that start, or deleting that folder, makes every NetherNet player confirm the server again.

## Recover from an archive

Every archive is the whole server folder: worlds, player data, plugin settings and the allowlist. The launcher puts one back from the cloud ([ADR 0006](../adr/0006-cloud-controls.md)):

1. **Find the archive's name.** In the AWS console, open S3, the backup bucket, then `instances/family/crafty/`. Each archive is named for the UTC time it was made, such as `20261016T031500Z.tar.gz`, with its `.sha256` beside it.
2. **Stop the server** with the [Server control workflow](cloud-controls.md#the-server-control-workflow)'s `stop`, if it is running. A restore runs only on a stopped server, or on a running host whose last start or restore failed.
3. **Restore it.** Run the Server control workflow with `action` set to `restore` and `archive` set to that name, or from a terminal run `task server:restore ARCHIVE=20261016T031500Z.tar.gz`. The S3 URI or key the console copies works too.

The launcher starts the host, points the hostname at it, and has the adapter stream the archive from S3, check it against its recorded SHA-256, and only then swap it in. Then it starts the game the way every start does. The run follows the server through `starting` to `ready`, for up to an hour; the `/minecraft` page shows `starting` the whole time, and the [idle stop](idle-stop.md) leaves the host alone.

A restore that fails reads `failed`, the alarm emails you, and the launcher's log holds the adapter's reason. Until its swap, the world in place is the one from before; a Member's **Start** then starts whatever world is in place, and a new `restore` tries again on the same run of the host, unless someone started the game by hand meanwhile: stop it first. Its reasons include a full data disk and an archive that does not match its SHA-256. A restore unpacks beside the current folder, so the data disk needs free space for one more copy of the server; one that runs out fails with "the data disk filled up while unpacking the archive" and leaves the old world in place, so [grow the disk](data-disk-capacity.md#grow-the-disk) and restore again.

The folder a restore replaces stays in `/srv/minecraft/crafty/staging/`. When its world is in S3 (the last stop archived it, or it was itself restored from an archive, and nobody played since), it is named `archived-family-<UTC time>`, and the next restore removes it, with any folder a restore cut off partway left behind. Otherwise, such as after the host went down without its drain, or on a disk the adapter holds no backup record for yet, it is named `unarchived-family-<UTC time>` and kept until you delete it in a [shell on the host](#open-a-shell-on-the-host), because it may hold the only copy of that play. A restore refuses a server started from Crafty's page while it ran ("the server was started during the restore"); it asks the host whether a process in Crafty's container works in the server folder as well as asking Crafty, which reports a start at least half a second later. A shell of yours in that folder does not count. A start in the instant of the swap makes it fail after the swap instead, naming the folder it moved aside, which is then kept as `unarchived-family-<UTC time>` too, because the game may work in that folder and hold what is played there. To keep that play, stop the server from Crafty's page, which leaves the host running, and move that folder back in place of the server folder in a shell on the host. Otherwise stop it with a forced stop (Server control's `stop` with `nobody_playing` ticked) and restore again. A `replaced-family-<UTC time>` folder is from a restore by an earlier adapter, which never checked its world; it too stays until you delete it. If a restore is cut off partway, such as by a power loss, run it again once the server reads `failed` or `stopped`: it puts the archive in place whether or not the old folder is still there.

The two archives from 2026-10-11 at 05:59Z have no `.sha256` beside them. The adapter that stored them (#53) put the SHA-256 in the object's `sha256` metadata instead, and `restore` falls back to that metadata; neither object has been read to confirm it. Retention does not count them, so they go once 14 newer archives with a `.sha256` exist.

After losing the data disk, first rebuild the host through this runbook up to and including [Create the server](#create-the-server), which gives Crafty an empty `family` server. The host is running then, so restore by hand in the same shell, with the archive's key:

```sh
sudo /usr/local/sbin/minecraft-instance restore family --key instances/family/crafty/20261016T031500Z.tar.gz
```

The empty server it replaces is kept as `unarchived-family-<UTC time>`, since no backup record exists yet; it holds no play, so delete it. Crafty's own login, API key and settings live on the data disk and are not in the archive, so they are set up again. So does the record of which jars earlier manifests pinned: if the archive is older than a change that renamed or dropped a jar in `server.json`, delete that old jar from `plugins/` in `/srv/minecraft/crafty/servers/<server id>/` now, in the same shell, because the next start would keep it. Then stop the host with the Server control workflow's `stop` with `nobody_playing` ticked, which archives the restored server again, and start it as usual.

## Acceptance

1. A Java client and a Bedrock client join the hostname, and an unlisted account is refused.
2. Both players, including one who is not an operator, walk through the portal to `creative`, are in creative mode there, and keep separate inventories from `survival`.
3. `stop` stores an archive, and the next `start` comes back with the same worlds and inventories.
4. After the first start with both transports, the [Minecraft health](unattended-health.md) workflow reports `nethernet_signaling_ok`, and a Bedrock client that speaks NetherNet joins after accepting the trust prompt.
