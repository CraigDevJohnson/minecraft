# Worlds and mods

Each world runs on an EC2 host of its own ([ADR 0009](../adr/0009-one-host-per-world.md)). [`runtime/crafty/server.json`](../../runtime/crafty/server.json) names the worlds and pins everything they run, and OpenTofu makes one host per world from that list. This runbook covers what the file holds, how to add a mod or a world, the first Deploy, the proof run for a world, and how the old family host is retired later. Day-to-day starts and stops are in [cloud controls](cloud-controls.md).

Status: built and checked offline on 2026-11-10. Nothing below has run against AWS, Cloudflare or a real host yet. Record each world's proof run on its issue when it is done.

## What the pinned content holds

`server.json` is schema 2 and has three parts.

- **`base`** is what every world's server holds: Paper, Geyser and Floodgate, Geyser's approved configuration, and the server properties the worlds share (the port, the allowlist switches, `max-players`, and so on).
- **`mods`** is the catalog. A mod is a Paper plugin with an id, a `name`, a `version`, a jar `path` under `plugins/`, a `url`, a `sha256`, a `summary` and a `bedrock` note for the mods page, and optionally `managed_files`: configuration the adapter writes at each start while the mod is on. A managed file a mod wrote stays in place when the mod is off.
- **`worlds`** is an ordered list. Its order is the display order, and the first world is the one a request that names no world means. Each world has an `id`, a `name`, `server_properties` of its own (`level-name`, `gamemode`, `motd`), and `mods`: each catalog id the world may run, with `true` or `false` for its default.

| World | Game mode | Mods on by default |
| --- | --- | --- |
| `survival` | survival | GriefPrevention, GSit |
| `creative` | creative, forced | GriefPrevention, FastAsyncWorldEdit |
| `adventure` | survival | GriefPrevention, AuraSkills, DecentHolograms |

World and mod ids match `^[a-z][a-z0-9-]{0,31}$`, because they travel unquoted in the commands the Launcher sends. A world may not set `server-port`, `online-mode`, `white-list`, `enforce-whitelist`, `enable-rcon` or `enable-query`. The adapter refuses a manifest that breaks these rules, and the Launcher refuses to start up unless the manifest's world ids are exactly the worlds OpenTofu gave it.

Only a mod a world lists can be switched on for it. A Member with the site's `minecraft-admin` grant switches mods on the site; an operator can use the [Server control workflow](cloud-controls.md#the-server-control-workflow) or `task server:mod-on` and `task server:mod-off`. The Launcher keeps only the switches that differ from the defaults, in `/<resource_prefix>/launcher/mods`, and a switch applies at that world's next start. At that start the adapter downloads and checks any jar it lacks, writes the managed files of the mods that are on, and removes every other jar directly in `plugins/`. A mod's own data folder, `plugins/<Name>/`, stays, so switching a mod back on brings its data back.

## Add a mod

A mod is added by pull request. Nothing reaches a world that is not pinned here.

1. **Check that it fits.** It must be a Paper plugin for the pinned Minecraft version, and it must not need a client-side mod or a Bedrock add-on. Read what its project says about Geyser and Floodgate.
2. **Find the pin.** Use the exact release file's HTTPS URL, with no query string and no `latest` in the path, on one of the download hosts the adapter allows: `github.com`, `cdn.modrinth.com`, `download.geysermc.org` or `fill-data.papermc.io`. For a mod Modrinth hosts, run the **Pins** workflow by hand with `modrinth_version` set to the version's eight-character id. It prints the file's URL, its name and the SHA-256 of the file as downloaded, after checking the download against the SHA-512 Modrinth publishes. For a GitHub release, download the jar once and take its SHA-256 (`sha256sum <file>`).
3. **Add it to `mods`** in `server.json` under a new id, with its `name`, `version`, `path` (`plugins/<file>.jar`), `url`, `sha256`, a one-sentence `summary` and a `bedrock` note. Write "Not yet tried on Bedrock." until someone has tried it.
4. **List it under the worlds that may run it**, in each world's `mods`, with `true` to have it on by default or `false` to leave it for a Member to switch on.
5. **Give it managed files only if it needs them.** A managed file is canonical JSON (`"format": "canonical-json-v1"`), which is also valid YAML, with the SHA-256 of its rendered bytes. The adapter refuses a manifest whose checksum does not match.
6. **Change the `revision`** string, run `.tools/bin/task check`, and open the pull request. `task check` is offline and can only tell that a pin is well formed. The Pins workflow runs on the pull request, downloads every pinned file and fails on a SHA-256 that does not match. Do not merge with it red.
7. **Deploy** after the merge, with every world stopped ([cloud controls](cloud-controls.md#deploys)). The plan shows the Launcher functions' new package and nothing about a host. The mod reaches each world that lists it at that world's next start.

Updating a mod is the same edit to its `version`, `path`, `url` and `sha256`. The old jar goes at the next start because it is no longer wanted. Removing a mod from the catalog, or from a world's `mods`, removes its jar the same way and leaves its data folder until someone deletes it in a [shell on the host](crafty.md#open-a-shell-on-the-host).

## Add a world

1. **Add it to `worlds`** in `server.json`: an `id`, a `name`, its `server_properties` and its `mods`. Put it where it should appear on the page. The first entry is the default world, so add a new one after it unless the default should change.
2. **Tell GriefPrevention about it**, if the world lists that mod: add the world's three level names (`<id>`, `<id>_nether`, `<id>_the_end`) to the claims modes in the mod's managed `config.yml`, and update that file's `sha256`.
3. **Add the id to the two workflow choices**, which GitHub needs written out: the `world` input of `.github/workflows/server-control.yml` and of `.github/workflows/minecraft-health.yml`.
4. **Run `.tools/bin/task check`.** Several tests name the worlds on purpose (the mocked plan in `infra/tests/infrastructure.tftest.hcl`, `tests/test_minecraft_crafty.py`, `lambda/launcher/worlds_test.go`); update them to the new list.
5. **Create the world's DNS record** before its first start: an A record named `<id>.<the public hostname>`, DNS only, TTL 60, on `192.0.2.1` ([below](#the-first-deploy)).
6. **Deploy** with every world stopped. Expect creates only for the new world: its host, its two state parameters and its idle-stop target. The Launcher and idle-stop functions and their policies change in place, and so does the host role's backup policy, which gains the world's prefix. Any replacement or destroy is a stop condition.
7. **Run the [proof run](#the-proof-run-for-a-world)** for the new world.

The private deployment inputs do not change: nothing names a world there. The allowlist of record already covers the new world.

Removing a world is the reverse edit. The next plan destroys that world's host, root disk and parameters, so it needs Craig's explicit approval of those destroys. The world's archives stay in S3 under `instances/<id>/crafty/`.

## The first Deploy

This is the Deploy that brings the world hosts. It is done once.

**Before the plan**

1. **Stop the family server** with the current Launcher (the Server control workflow's `stop`), and wait for `stopped`. After this Deploy the Launcher no longer reaches the family host, so this is the last stop that archives it from the cloud. The family host then stays stopped.
2. **Create three DNS records** in the Cloudflare zone, one per world: type A, named `survival.<the public hostname>`, `creative.<the public hostname>` and `adventure.<the public hostname>`, **DNS only** (not proxied), TTL 60, pointing at `192.0.2.1`. The Launcher only ever updates an existing record and never creates one. A world started without its record still starts its game, but the run fails, the alarm emails you, and the page shows no address. The Launcher's existing Cloudflare token must be able to edit records in that zone; that has not been checked for the new names.
3. **Change nothing in the private inputs.** Every variable keeps the value it has. `instance_ids = ["family"]` stays, and `launcher.server_name` is still accepted and no longer used. No IAM step is needed by hand: the committed role and boundary examples already scope hosts by the `Project` tag and parameters by the resource prefix, and the live policies were applied from them.
4. **Check the account's on-demand vCPU quota** covers three more `m7g.large` hosts launching at once (six vCPUs). They power themselves off within minutes, but they all launch in the one apply.

**Read the plan**

Run the Deploy workflow with `mode` set to `plan` and read its summary. With the launcher on two sites and the idle stop on, expect:

- **Added:** three `aws_instance.world` hosts, three `world_launcher_state` and three `world_idle_stop_state` parameters, `launcher_mods`, `launcher_allowlist`, and the idle-stop targets for `creative` and `adventure`. That is 13 to add.
- **Changed in place:** each Launcher function and its role policy, the idle-stop function, its role policy, its schedule rule and alarm (descriptions), the host role's backup policy (it gains the worlds' prefixes), and the idle-stop target that moves to `survival` and gains its input.
- **Destroyed: nothing.** `0 to destroy` is the rule.

Stop and report if the plan has any line for the family host (`aws_instance.host`), its data disk or attachment, a bucket or the network, or any replacement. The move of the one schedule target has only been planned against mocks; if the real plan shows that target replaced instead of updated, stop and read it before going on.

**Apply and check**

1. Start the workflow again with `mode` set to `apply` and that plan's digest, and approve it.
2. Each new host boots once, sets itself up and powers off. A few minutes after the apply, the Server control workflow's `status` with `world` left at `none` should list all three worlds as `stopped`. A first boot whose set-up failed powers off as well, so `stopped` does not prove the set-up. The next boot, which is the world's first start, tries it again while the adapter waits up to ten minutes for it. If it fails again the host powers itself off and the start reads `failed`; if it is only slow, the start fails with "the host's setup did not finish" and the next Start carries on. The script's output is in `/var/log/cloud-init-output.log` on the host, and a host that powered off can be read from the EC2 console's system log for the instance.
3. **Add the players again.** The worlds are fresh and the allowlist of record starts empty, so run the Server control workflow's `allowlist-add` once for each family player ([allowlist](allowlist.md)). It works with every world stopped, and one change covers all three worlds.
4. Run the proof run for each world, the same day.

The existing `/minecraft` page keeps working before the-lobby shows a card per world: a request that names no world means the first world, so its Start starts the new `survival` and shows `survival.<the public hostname>`.

## The proof run for a world

Run it once for each world after the first Deploy, and again for a new world. Use the Server control workflow with `world` set to the world.

1. **Start it.** A first start boots the host, pulls Crafty's image, downloads the jars, creates the server and, when S3 holds an archive for the world, restores the newest. That can take longer than the Launcher's 15-minute start deadline, and the run then ends red on `failed`. Run `start` again: it carries on where the first stopped. Expect `ready`.
2. **Check the address.** `status` shows the world `ready` with `<world>.<the public hostname>` for Java and for Bedrock on port 19132.
3. **Run the [Minecraft health](unattended-health.md) workflow** with the same world. Expect `healthy`, with `dns_matches_host`, `java_status_ok`, `bedrock_discovery_ok` and `nethernet_signaling_ok` all true.
4. **Join.** A Java client and a Bedrock client join the address, and an account that is not on the allowlist is refused. The Bedrock player should be one added while the world was stopped: that proves Floodgate admits a player from the entry the start wrote, which no test can show. A Bedrock client accepts the trust prompt once for each world.
5. **Check the world is the right one.** Players are in the world's game mode. In a [shell on the host](crafty.md#open-a-shell-on-the-host), `sudo /usr/local/sbin/minecraft-instance console <world> 'plugins'` lists Geyser, Floodgate and the world's default mods, and no other.
6. **Stop it** with nobody playing. Expect `stopped`, an archive and its `.sha256` under `instances/<world>/crafty/` in the backup bucket, and the hostname back on `192.0.2.1`.
7. **Start it again.** What was built in step 4 is still there.
8. **Switch a mod** off with `mod-off`, stop and start the world, and check with `plugins` that it is gone; then switch it back on the same way.
9. **Leave it empty** for the idle time once, and see the idle stop take it to `stopped` with a new archive.

Restoring an older archive by hand needs a world host that has finished its first start; on a rebuilt host, start the world once (it restores the newest archive), stop it, then restore. The restore on a rebuilt host is proven the first time a Deploy replaces the world hosts, which any change to the first-boot script causes: after that apply, start the world and check that it is the world from its last stop, not a fresh one.

## Retire the family host

The family host of [ADR 0004](../adr/0004-crafty-multiverse-rebuild.md) stays declared, stopped and billing for its disks until a separate pull request removes it. That pull request is not written yet. When the worlds have been proven and nothing more is wanted from the old server:

1. Decide what to keep from `instances/family/crafty/`. Those archives hold the Multiverse worlds and stay in the bucket; no world host restores them.
2. The pull request removes `aws_instance.host`, `aws_ebs_volume.data` and its attachment, the two old state parameters (`/<prefix>/launcher/state` and `/<prefix>/idle-stop/state`), the family bootstrap (`scripts/host-bootstrap.sh.tftpl`, `scripts/minecraft_storage.py`), the by-hand tooling that only served it (`scripts/install_crafty.sh`, `scripts/minecraft_host.py`), the inputs that only it used, and its runbooks ([host commands](host-commands.md), [data disk capacity](data-disk-capacity.md), the disk set-up in [infrastructure](infrastructure.md)).
3. The data disk has `prevent_destroy`, so this is the [explicit destructive workflow](infrastructure.md#normal-replacement-and-explicit-destructive-workflow): the pull request names the protection it removes, the plan lists exactly those destroys, and Craig approves those exact resources.
4. After that Deploy, Craig deletes the old A record, the public hostname's own. The three world records stay.
