# Crafty Controller host

This runbook builds the family server described in [ADR 0004](../adr/0004-crafty-multiverse-rebuild.md): one Paper server run by Crafty Controller 4.11.0, with Geyser and Floodgate for Bedrock players and Multiverse for the `survival` and `creative` worlds. Everyone joins one address: Java on TCP 25565, Bedrock on UDP 19132. Every AWS change below needs Craig's approval of the reviewed plan first.

## What is pinned

[`runtime/crafty/server.json`](../../runtime/crafty/server.json) pins the Crafty image by digest and every server jar by version and SHA-256: Paper 26.2 build 129, Geyser 2.11.3, Floodgate 2.2.5, ViaVersion 5.12.0, Multiverse-Core 5.8.1, Multiverse-Inventories 5.3.6, Multiverse-Portals 5.3.0 and Multiverse-NetherPortals 5.1.0. Paper, Geyser, Floodgate and ViaVersion are the versions already proven on the earlier stack. The Multiverse versions are their latest stable releases on 2026-10-10 and have not yet been run on this server, so the first session below is their compatibility test.

## Infrastructure inputs

Use the existing OpenTofu root with the default `lifecycle_mode = "retained-host"` and `data_ownership = "retained"`. In the private `deployment.tfvars`:

- `instance_ids = ["family"]`, so the host role may store archives under `instances/family/`.
- `bedrock_transport = "raknet"` until #26 activates NetherNet.
- No `additional_game_ingress`: the creative ports 25566 and 19134 are gone.
- A new `data_filesystem_uuid` for the new data disk, initialized as in [infrastructure operations](infrastructure.md).

Before the first apply, the stalled S3-only restore host and its blank 4 GiB disk must be removed. The world data they were restoring was test data that Craig chose not to keep. Review that plan with Craig, since it deletes resources.

## Install Crafty on the host

After the host bootstrap has mounted `/srv/minecraft`, open an SSM shell on the host and, from a reviewed checkout:

```sh
sudo bash scripts/install_aws_cli.sh
sudo bash scripts/install_crafty.sh
```

The installer writes `/opt/minecraft-crafty/compose.yaml`, stores Crafty's configuration, servers and backups under `/srv/minecraft/crafty/`, installs the lifecycle adapter as `/usr/local/sbin/minecraft-instance`, and enables `minecraft-crafty.service`. Crafty's web UI listens on `127.0.0.1:8443` only.

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
  --manifest /opt/minecraft-crafty/runtime/server.json \
  --destination /srv/minecraft/crafty/import/family \
  --archive /srv/minecraft/crafty/import/upload/family.zip
sudo /usr/local/sbin/minecraft-instance import-server family --archive-name family.zip --max-memory-gb 5
sudo rm -rf /srv/minecraft/crafty/import/family /srv/minecraft/crafty/import/upload/family.zip
```

The imported server runs `paper.jar` on the container's default Java 25 with auto-start off, because the workstation `start` command starts the server. The staged `eula.txt` has no trailing newline on purpose: Crafty 4.11 compares its first line exactly and otherwise refuses every start without logging an error.

## Set up the worlds

Start the server once from the workstation (`start --instance family` below), then run these in the Crafty console:

```text
mv create creative normal
mv modify creative set gamemode creative
mvinv list
```

Multiverse-Inventories keeps a world that is not in any group on its own inventory, so `creative` must not appear in the `default` group in `mvinv list`. If it does, run `mvinv remove-worlds default creative`.

Portals are made in game by an operator (`op <JavaName>` from the console first). Build a frame at the `survival` spawn, run `/mvp wand`, select two opposite corners of the frame with the wand, then run `/mvp create to-creative w:creative`. Make the return portal in `creative` the same way with `/mvp create to-survival w:survival`.

Add each family player to the allowlist from the console: `whitelist add <JavaName>` for Java accounts and `fwhitelist add <Gamertag>` for Bedrock accounts, as in [allowlist](allowlist.md).

## Everyday start and stop

The existing workstation command drives the adapter unchanged:

```sh
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json start --instance family
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json status
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json stop
```

`start` starts EC2, updates DNS, and asks Crafty to start the server, waiting until the Java port answers. `stop` asks Crafty to stop the server, archives the stopped server folder to `s3://<bucket>/instances/family/crafty/<UTC time>.tar.gz`, verifies the stored size and SHA-256, keeps the newest 14 archives, then stops EC2 and parks DNS. If the archive fails, EC2 is not stopped. Stopping the host any other way still runs the same drain from `minecraft-crafty.service`.

## Acceptance

1. A Java client and a Bedrock client join the hostname, and an unlisted account is refused.
2. Both players walk through the portal to `creative`, are in creative mode there, and keep separate inventories from `survival`.
3. `stop` stores an archive, and the next `start` comes back with the same worlds and inventories.
