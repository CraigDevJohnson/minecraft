# Crafty Controller host

This runbook builds the family server described in [ADR 0004](../adr/0004-crafty-multiverse-rebuild.md): one Paper server run by Crafty Controller 4.11.0, with Geyser and Floodgate for Bedrock players and Multiverse for the `survival` and `creative` worlds. Everyone joins one address: Java on TCP 25565, and Bedrock on port 19132, over RakNet (UDP 19132) or NetherNet (signaling on TCP 19132, WebRTC on UDP 19133). Every AWS change below needs Craig's approval of the reviewed plan first.

## What is pinned

[`runtime/crafty/server.json`](../../runtime/crafty/server.json) pins the Crafty image by digest and every server jar by version and SHA-256: Paper 26.2 build 129, Geyser 2.11.3, Floodgate 2.2.5, ViaVersion 5.12.0, Multiverse-Core 5.8.1, Multiverse-Inventories 5.3.6, Multiverse-Portals 5.3.0 and Multiverse-NetherPortals 5.1.0. Paper, Geyser, Floodgate and ViaVersion are the versions already proven on the earlier stack. The Multiverse versions are their latest stable releases on 2026-10-10 and have not yet been run on this server, so the first session below is their compatibility test.

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

The installer writes `/opt/minecraft-crafty/compose.yaml` from the pinned server content, stores Crafty's configuration, servers and backups under `/srv/minecraft/crafty/`, installs the lifecycle adapter as `/usr/local/sbin/minecraft-instance` (its files and `server.json` in `/opt/minecraft-crafty/scripts`, which the launcher keeps up to date from then on, [ADR 0007](../adr/0007-the-launcher-carries-the-host-adapter.md)), and enables `minecraft-crafty.service`. Crafty's web UI listens on `127.0.0.1:8443` only. Each start renders `compose.yaml` again, so only a change to `minecraft-crafty.service` or the `minecraft-instance` wrapper needs the installer run again this way.

## Open the Crafty admin page

The tunnel needs the Session Manager plugin on the workstation (`brew install --cask session-manager-plugin`). Forward the loopback port through SSM and leave the command running. A plain `start-session` shell does not forward the port:

```sh
aws ssm start-session --target "$HOST_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=8443,localPortNumber=8443
```

Then open `https://localhost:8443` and accept Crafty's self-signed certificate. On first start Crafty writes its initial admin login to `/srv/minecraft/crafty/config/default-creds.txt`; sign in, change the password at once, and delete that file.

## Create the API key and operator configuration

In Crafty, open `https://localhost:8443/panel/edit_user_apikeys?id=1`, create a key named `lifecycle` with **Full Access**, and use its token button to show the token. In an SSM shell on the host, store it without echoing it. The command prompts for the paste:

```sh
sudo bash -c 'read -rsp "Paste the Crafty token, then press Enter: " t; umask 077; printf %s "$t" > /etc/minecraft/crafty-api.token; echo'
```

Write `/etc/minecraft/operator.json` (root-owned, mode 0600):

```json
{
  "schema_version": 1,
  "runtime": "crafty",
  "crafty_url": "https://127.0.0.1:8443",
  "token_file": "/etc/minecraft/crafty-api.token",
  "data_root": "/srv/minecraft",
  "instances": {"family": {"server_name": "family", "java_port": 25565}},
  "backup": {"bucket": "the-private-backup-bucket", "region": "us-west-2"}
}
```

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

## Set up the worlds

Start the server once: in the same SSM shell, run `sudo /usr/local/sbin/minecraft-instance start family`. Then run these in the Crafty console:

```text
mv create creative normal
mv modify creative set gamemode creative
mvinv list
```

Multiverse-Inventories keeps a world that is not in any group on its own inventory, so `creative` must not appear in the `default` group in `mvinv list`. If it does, run `mvinv remove-worlds default creative`.

Portals are made in game by an operator (`op <JavaName>` from the console first). Build a frame at the `survival` spawn and run `/mvp wand`, which gives a wooden pickaxe. Left-click one corner block of the frame's opening and right-click the opposite corner, then run `/mvp create to-creative w:creative`. Make the return portal in `creative` the same way with `/mvp create to-survival w:survival`.

Multiverse-Portals lets only players with `multiverse.portal.access.<portal>` use a portal, and this server installs no permissions plugin, so by default only operators get through. With the server stopped, set `enforce-portal-access: false` under `portal-usage` in `plugins/Multiverse-Portals/config.yml` in the server folder (`/srv/minecraft/crafty/servers/<server id>/`). Multiverse-Core's `enforce-access` is already `false`, so entering `creative` needs no further permission.

Add each family player to the allowlist with the [Server control workflow](cloud-controls.md#the-server-control-workflow)'s `allowlist-add`, which looks the account up and confirms the change in `whitelist.json` ([allowlist](allowlist.md)). In a hands-on emergency the console takes `whitelist add <JavaName>` for Java accounts and `fwhitelist add <Gamertag>` for Bedrock accounts.

## Everyday start and stop

The launcher starts and stops the server; the `/minecraft` page in The Lobby, the Server control workflow and `task server:*` only ask it ([cloud controls](cloud-controls.md)).

A start starts EC2, installs the adapter and `server.json` the launcher carries, updates DNS, and runs `minecraft-instance start family`. That brings the stopped server to the pinned content ([What is pinned](#what-is-pinned)) and asks Crafty to start it, waiting until the Java port answers. A stop runs `minecraft-instance host-drain`, which asks Crafty to stop the server and streams an archive of the stopped server folder to `s3://<bucket>/instances/family/crafty/<UTC time>.tar.gz` (no second copy on the data disk and no 5 GB limit). It verifies the stored size and writes the SHA-256 beside it as `<UTC time>.tar.gz.sha256`. It keeps the newest 14 archives that have their `.sha256` and removes any archive older than those. Then the launcher stops EC2 and parks DNS. If the archive fails, EC2 is not stopped. Once a stop has begun, `start` is refused until the host has been stopped and started again, so a stop can never race a new session. Stopping the host any other way still runs the same drain from `minecraft-crafty.service`; if that drain fails, the unit is marked failed in the journal.

## How players join

- **Java** (Mac or PC): Multiplayer, Add Server, `mc.craigdevjohnson.com`. The default port 25565 needs no entry.
- **Bedrock on a phone, tablet or PC** (iPhone, iPad, Windows): Play, Servers, Add Server, `mc.craigdevjohnson.com` with port `19132`, the same entry as before NetherNet. A client that still speaks RakNet joins over UDP 19132. A client that speaks NetherNet asks Geyser's built-in signaling on TCP 19132 and is then asked, once, whether to trust this server; the player accepts. Bedrock 26.60 clients join only this way. The prompt has not yet been seen on the family's devices.
- **Consoles** (PlayStation, Switch, Xbox) cannot add a server. BedrockConnect is the optional route ([family device validation](family-device-validation.md)); it is untested, and #9 waived those tests.

Geyser keeps the server's NetherNet identity in `plugins/Geyser-Spigot/nethernet/` in the server folder, so every archive holds it and a restore brings it back. Deleting that folder makes every player confirm the server again.

## Recover from an archive

Every archive is the whole server folder: worlds, player data, plugin settings and the allowlist. To put one back, stop the server, pick a key from `aws s3 ls s3://<bucket>/instances/family/crafty/`, and on the host run:

```sh
sudo /usr/local/sbin/minecraft-instance restore family --key instances/family/crafty/<UTC time>.tar.gz
```

The command streams the archive from S3, checks it against its recorded SHA-256, and only then swaps it in. It unpacks beside the current folder, so the data disk needs free space for one more copy of the server; [grow the disk](data-disk-capacity.md#grow-the-disk) first if `status` shows less than that. The folder it replaces is kept as `/srv/minecraft/crafty/staging/replaced-family-<UTC time>`; delete it once the restored server has been checked. If the command is cut off partway, such as by a power loss, and the server folder is missing afterwards, move that kept folder back to `/srv/minecraft/crafty/servers/<server id>` and run `restore` again. Then start the server as usual.

The two archives from 2026-10-11 at 05:59Z have no `.sha256` beside them. The adapter that stored them (#53) put the SHA-256 in the object's `sha256` metadata instead, and `restore` falls back to that metadata; neither object has been read to confirm it. Retention does not count them, so they go once 14 newer archives with a `.sha256` exist.

After losing the data disk, first rebuild the host through this runbook up to and including [Create the server](#create-the-server), which gives Crafty an empty `family` server, then run the same `restore`. Crafty's own login, API key and settings live on the data disk and are not in the archive, so they are set up again.

## Acceptance

1. A Java client and a Bedrock client join the hostname, and an unlisted account is refused.
2. Both players, including one who is not an operator, walk through the portal to `creative`, are in creative mode there, and keep separate inventories from `survival`.
3. `stop` stores an archive, and the next `start` comes back with the same worlds and inventories.
4. After the first start with both transports, the [Minecraft health](unattended-health.md) workflow reports `nethernet_signaling_ok`, and a Bedrock client that speaks NetherNet joins after accepting the trust prompt.
