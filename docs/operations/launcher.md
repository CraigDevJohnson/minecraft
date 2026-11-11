# Launcher backend

The Lobby's Minecraft Launcher page (`/minecraft` in [the-lobby](https://github.com/CraigDevJohnson/the-lobby)) shows each world's state and lets a Member start or stop a world. Each world runs on a host of its own ([ADR 0009](../adr/0009-one-host-per-world.md), [worlds](worlds.md)), and the function runs one copy of the same state machine per world, each with its own host, hostname and record, so two worlds starting or stopping at once share nothing. The page calls this repository's launcher function, a Go Lambda in [`lambda/launcher`](../../lambda/launcher). The site checks the Member's `minecraft` grant first and signs each request with its own role. The function's Function URL accepts that role and, when `launcher.control_role_arn` is set, the operator's server control role, which the [Server control workflow and the `task server:*` targets](cloud-controls.md) use (the-lobby ADR 0006, this repository's [ADR 0006](../adr/0006-cloud-controls.md)). The contract is the-lobby's [`docs/minecraft-backend.md`](https://github.com/CraigDevJohnson/the-lobby/blob/main/docs/minecraft-backend.md). The function is off until `launcher` is set in the private deployment inputs, and with `launcher = null` the plan creates none of its resources.

The site has one environment per hostname (dev, then production), and each site may call only backends named after its environment. `launcher.sites` therefore names the Lobby environments to serve, each with its site's role, and the plan makes one function per environment, `<prefix>-<environment>-launcher`, that only that site's role may call. The functions are twins: they run the same package, start the same world hosts, and share each world's record, the mods switches, the allowlist and one token parameter, so a start from one site shows as `starting` on the other. There are no dev worlds: a start or a mod switch from the dev site is real. Each function has its own logs, alarm and alert topic.

## What it answers

`GET /server` answers every world: `{name, state, last_known?, connection, worlds}`, where `worlds` lists each world in display order as `{id, name, state, last_known?, connection}` and the top-level fields repeat the first world, so a page that reads no `worlds` keeps working. The worlds are read side by side, so the slowest one decides how long the answer takes. `GET /server?world=creative` answers that one world in the shape a start answers.

`POST /server/start`, `POST /server/stop` and `POST /server/restore` take the world in their body, `{"world": "creative"}`, beside their own fields. No body, or no `world`, means the first world. A world the pinned content does not have answers `409` with `unknown_world`, and a body that is not JSON answers `400` with `bad_request`.

- `POST /server/start` starts a `stopped` or `failed` world and answers that world's status, `202` when it started something and `200` when nothing changed.
- `POST /server/stop` stops a `ready` world the same way: `202` with `stopping` when the stop began, `200` unchanged when the host is already off or a stop is under way, and `409` with a reason when it is refused: `starting` while a start is under way, `players_online` with the count, or `players_unknown` when the game does not answer, or when a failed restore's record stands (below). A body with `"force": true` stops over the last two, and only an operator may send it: from a site's role it answers `403` with `forbidden`.
- `POST /server/restore` with `{"world": "creative", "archive": "20261016T031500Z.tar.gz"}` puts that archive back in place of a stopped world and starts its game ([below](#how-a-restore-runs)): `202` with `starting` when it began, `409` with `not_stopped` and the state when the world is not stopped (or a game started by hand answers after a failed restore), `400` for a name that is not an archive's, and `403` for a site's role.
- `POST /server/allowlist` with `{operation, edition, name}` adds or removes one player on the family's one allowlist ([below](#allowlist-changes)). It takes no world, and it too answers a site's role `403`.
- `GET /mods` and `POST /mods` list and switch each world's mods ([below](#mods)). A site's role may call both.

A world's connection is its own hostname, `<world>.<the public hostname>`, for Java and the same hostname with port 19132 for Bedrock. Several worlds may be `ready` at once. The states below are each world's own.

| State | When |
| --- | --- |
| `stopped` | EC2 reports the host stopped. |
| `starting` | EC2 is `pending`. Or a restore of this function is under way (at most 45 minutes), whether or not a game answers. Or the host runs without a game answering, and either a start of this function is under way (at most 15 minutes) or the host launched less than 10 minutes ago, which covers a start from the workstation. |
| `ready` | The game answers a Java status ping at the host's public address, with one retry. EC2 or Crafty being up is not enough, and neither is a game that answers while a restore of this function is under way or after one failed on the run of the host its follow-up reached: it was started by hand on the world the restore replaces. Bedrock players join through Geyser in the same server, so the one ping covers both editions. |
| `stopping` | EC2 is `stopping`, a stop of this function is under way (at most 75 minutes), or the [idle stop](idle-stop.md) has a drain or EC2 stop under way for this run. |
| `maintenance` | The host has run for more than 10 minutes without a game answering, and no start of this function is pending. Examples are a server stopped from Crafty's page, or a workstation `stop` archiving before it stops EC2. Members get no Start button. |
| `failed` | A start or restore of this function failed, or has not finished after 15 or 45 minutes. Start is offered again, and on a running host it retries only the game. A restore that failed once its follow-up saw this run of the host reads `failed` even while a game started by hand answers (a restore of a stopped server that failed before its follow-up saw the host running, as when AWS could not be read, follows the usual rules; a restore of a running host is tied to that run from its request): Start then reads that game `ready`, while an unforced stop answers `players_unknown` and a restore `not_stopped` until a start or a forced stop replaces the record. |
| `unknown` | AWS could not be read. `last_known` is the last state this warm function answered, when it has one. |

The Member named in `X-Lobby-Member` is logged with every start, stop, restore and allowlist request, beside the world; the workflow sends none. The mods routes read no such header: a switch is logged as its world, mod and new setting only.

## How a start runs

A start of a world runs these steps on that world's host:

1. The request records the start in the world's SSM parameter, `/<resource_prefix>/launcher/worlds/<world>/state`, shared by every environment's function, calls `StartInstances` on the world's host, and starts a second, asynchronous run of the same function, which is told the world. The page gets `202` with `starting` straight away.
2. The second run waits for EC2 `running` and a public address. It points the world's hostname at that address through Cloudflare (DNS only, TTL 60), then waits for SSM. The A record must already exist: the function updates a record and never creates one.
3. It reads the mods that are on for the world and the allowlist of record, at that moment, so a switch or a change made up to then reaches the game. It then sends one command through `AWS-RunShellScript`: the lines that install [the host adapter](#the-host-adapter) the launcher carries, lines that write the allowlist to a private temporary file, and `minecraft-instance … start <world> --mods=<ids> --allowlist-file <that file>`. `--mods` names the mods that are on, in order of their ids, and may be empty; only ids the pinned content lists for that world are ever sent. The adapter writes the allowlist over the stopped server's `whitelist.json` before the game starts. The run waits for the adapter to report `running` and `ready`.
4. It records `ready` or `failed`, tied to the host's EC2 launch time so a later run never inherits it.

A DNS failure never stops the game. The run records it, the page shows no connection details for that run instead of a wrong address, and the run fails so the alarm emails you. Repeated taps, refreshes and several Members starting at once are safe. EC2's start is idempotent, the adapter takes its own lifecycle lock, and the second run's steps all repeat harmlessly. The second run is never retried: if it is lost, the start shows `failed` after 15 minutes and a Member can start again. The alarm also emails you when a second run could not be handed off or aged out before it ran, and when a start request fails. The function counts the first and last as `StartFailures` in the `Minecraft/Launcher` metrics, since the site still gets an answer, and Lambda counts dropped runs as `AsyncEventsDropped`.

### A world's first start

On a new or replaced host the same `start` command first sets the host's server up, under the adapter's lifecycle lock ([crafty](crafty.md#how-a-world-host-sets-itself-up)). It waits up to ten minutes for the host's own first-boot set-up, brings Crafty up, makes its API key, creates the server from the pinned content, and restores the world's newest archive when S3 holds one. A marker written last ends the set-up. All of that counts against the start's 15 minutes, so a first start may read `failed`; each step checks before it acts, and a second Start carries on where the first stopped.

## How a stop runs

A stop of a world drains its host, then stops it ([ADR 0006](../adr/0006-cloud-controls.md)). The request checks the player count with the same status ping, records the stop in the world's state parameter (with whether it was forced) and starts a second, asynchronous run; the caller gets `202` with `stopping` straight away. The second run waits for SSM, asks the game once more unless the stop was forced, and stands down without draining if somebody joined in between or the game no longer answers (recorded `stop-failed`, not counted as a failure), then sends `minecraft-instance … host-drain` with a one-hour execution limit, behind the lines that install the host adapter, so a host that never got as far as a start can still be stopped, and records the command id, follows it, validates its output the way the idle stop does (every instance stopped with a complete or not-required backup, behind the drain barrier), confirms with a fresh `host-status`, calls `StopInstances` on that world's host, waits for EC2 `stopped`, and parks the world's hostname on `192.0.2.1` through the same Cloudflare token. A run that reaches its own end first hands the rest to a fresh run, which picks the drain up by its command id. The whole stop has one deadline, 75 minutes from the request. A host whose server was never set up has nothing to archive: its drain answers `not-required`, and the stop goes on.

Past the deadline, or on any refusal, the stop is recorded `stop-failed` with its reason, counted as `StopFailures` for the alarm, and the host is left as it is, so a later stop can drain it again; the status then follows what the host is doing (`ready` if the game answers, `maintenance` if not). Before `StopInstances` the record notes that the hostname still owes its parking. A DNS failure after the host stopped is reported the same way, with the host off; until the hostname is parked, and also when a run ended between the EC2 stop and the parking, the status offers no address, and the next stop request (the workflow's `stop` or `task server:stop`) parks it instead of answering that there is nothing to stop. A Member may start the host as soon as EC2 shows it stopped; once a park is done, a start recorded meanwhile whose host already runs gets the hostname pointed back at it, so a slow park never outlasts the next start. While a stop of this function is under way, the idle stop stands aside, so it does not begin a drain of its own (one rare overlap remains, [below](#rare-overlaps)).

## How a restore runs

A restore puts one archive back in place of a world, then starts its game ([crafty](crafty.md#recover-from-an-archive), [ADR 0006](../adr/0006-cloud-controls.md)). Only an operator may ask for it, and only for a stopped server, or for a running host whose last start or restore of this function failed, so the world it replaces is never one in play. The request names the archive by the time in its name; the function builds its key, `instances/<world>/crafty/<name>`, itself, so a restore of a world only ever takes one of that world's own archives.

1. The request records the restore in the state parameter, phase `restoring` with the archive's key, calls `StartInstances` on a stopped host, and starts a second run. The caller gets `202` with `starting`; the page shows a restore as `starting` throughout, because it knows only the states above.
2. The second run waits for EC2 `running`, points the hostname at the host, and waits for SSM, as a start does.
3. It sends `minecraft-instance … restore <world> --key <key>` after the lines that install [the host adapter](#the-host-adapter), records the command id, and follows it. The adapter waits for Crafty, refuses while the host drains or the game runs, streams the archive from S3, checks its SHA-256 and only then swaps it in.
4. Once the adapter confirms that archive in place, the run records a fresh start on the same run of the host and hands it to a new run, which starts the game as any start does and records `ready` or `failed`.

The restore has one deadline, 45 minutes from the request; the adapter's own command may run 30 of them. A run that reaches its own end first hands the rest to a fresh run, which follows the same command by its id. A refusal, a failed or vanished command, or the deadline records `failed` with the adapter's reason, and the run fails, so the alarm emails you; a restore request that fails counts as `StartFailures`. While a restore of a world is under way, its Start answers `starting`, its Stop answers `409` with `starting`, an allowlist change leaves that world alone (its next start carries the list), and the [idle stop](idle-stop.md) stands aside. The other worlds are not affected.

## Rare overlaps

The launcher and the idle stop share their records through SSM parameters, which have no conditional write, so each change of owner is a read and then a write. Each world has its own records, so the windows below are per world. Four remain open, each needing a press to land within seconds of another request for the same world. Craig chose to leave them documented rather than move the records to a store with conditional writes (#64).

- **An idle stop and a Launcher stop draining together.** A Member presses Stop on an empty server in the second or two between the idle stop's read of the launcher's record and its save of its own drain. The host adapter runs one drain and fails the other at once. The server still stops and parks, and a spurious alarm emails you.
- **A stop's last write over a new start.** A Member presses Start in the tens of milliseconds between a stop's last read and its last write. The stop's record replaces the start's, the start's follow-up finds no start of its own, and the alarm fires. The host then runs without the game and with the hostname parked. Once the status reads `maintenance`, 10 minutes after the host started (before that a stop is refused with `starting`), stop it with the Server control workflow's `stop` with `nobody_playing` ticked, then start it again.
- **A start and a restore at once.** A Member presses Start in the same instant as an operator's restore of a stopped or failed server. Both requests' follow-ups then run whichever record was written last. The host adapter's lock lets one through and fails the other at once ("another lifecycle command is running"), so the server reads `failed` for a while, the alarm emails you, and the restore's workflow run ends red, though the server still comes up: with the archive if the restore's record was written last, without it if the start's was. When the start's record was written last and the two follow-ups do not meet at the lock, nothing fails and the run ends green without the archive. Either way, wait for the status to settle on `ready`, check the world, and restore again if it is not the archive's.
- **A Start just after a failed restore.** A Member presses Start on the `failed` server in the ten seconds between two of the restore run's status reads. The start replaces the failed restore's record, so the run sees `starting` and then `ready` on the world from before, and ends green; the alarm still emails you about the restore. Check the world after a restore whose alarm fired.

The mods switches and the allowlist of record are each one parameter shared by every world and both sites' functions, written with a read and then a write. Two switches, or two allowlist changes, in the same instant can lose one; ask again and check the answer.

## Allowlist changes

The family has one allowlist of record, the SSM parameter `/<resource_prefix>/launcher/allowlist`, a JSON list of `{edition, uuid, name}`. `POST /server/allowlist` with `{"operation": "add", "edition": "java", "name": "Steve"}` (or `remove`, or `bedrock` with a gamertag) changes that record and then each world whose game is running. It takes no world and works with every world stopped. Only an operator may call it.

The function first looks the player up, a Java name through Mojang's profile API and a Bedrock gamertag through GeyserMC's XUID lookup, from which it derives the Floodgate UUID the server holds; an unknown name answers `404` with `player_not_found` and nothing is written. GeyserMC knows only gamertags in its cache, which fills as Bedrock players join Geyser servers, and answers any other with `503` and "Unable to find user in our cache"; that counts as an unknown name, while any other `503` is an outage. A Bedrock player who has never joined a Geyser server may have to try joining once, and be turned away, before the change can go through. A gamertag linked to a Java account through GeyserMC's global linking, which Floodgate uses by default, joins as that Java account, so its own entry would neither admit nor remove the player: the function asks GeyserMC's link lookup, the one Floodgate makes, and answers `409` with `linked_account`; allowlist the linked Java account's name instead.

It then writes the record: an add appends the looked-up account unless its UUID is already listed, and a remove deletes the entry with that UUID. After that it runs the adapter's `allowlist` action described in [allowlist](allowlist.md) on every world that reads `ready`, side by side: one SSM command per world that writes the verified identity to a private temporary file on the host, runs `minecraft-instance … allowlist <world> add --edition java --player-file <that file>` and removes the file. A world in any other state is left alone, because its next start carries the whole list.

The answer is `200` with `{edition, operation, allowlist, worlds}`: `allowlist` is `updated` when the record or any running world changed and `unchanged` otherwise, and `worlds` lists the running worlds the change also reached. When a running world's adapter refuses or cannot be followed, the answer is `502` with `allowlist_failed`, the error the function logs names the world and carries the adapter's fixed diagnostic, and the record keeps the change, so asking again finishes it there. A malformed request answers `400`.

One exception to the lookup: a Java removal whose name Mojang does not know, as after a rename, goes ahead with the name alone. The record drops the Java entry it holds under that name, and each running world's adapter removes the entry it holds under it. If neither held one, the function answers `404` with `player_not_found`.

Names are validated with the adapter's own rules before anything is looked up. Neither a name nor the list is ever logged, and a refused write of the parameter is logged by its error code alone.

Each start writes the record over the world's `whitelist.json` in that file's own format, `[{uuid, name}]`. A Bedrock entry's name is written the way Floodgate writes it: a dot, then the gamertag with its spaces as underscores. A restored archive therefore never brings a removed player back. The record is one standard String parameter of 4 KB, room for roughly 40 players.

## Mods

`GET /mods` answers `{worlds}`: every world in display order as `{id, name, mods}`, and each world's mods in order of their ids as `{id, name, version, summary, bedrock, default, enabled}`. `default` is the pinned content's setting for that world and `enabled` is what its next start will use.

`POST /mods` with `{"world": "creative", "mod": "fawe", "enabled": false}` switches one mod for a world's next start and answers `200` with the whole list as it then stands. A world the pinned content does not have answers `409` with `unknown_world`, a mod that world does not list answers `409` with `unknown_mod`, a body that is not JSON or has no `enabled` answers `400` with `bad_request`, and a parameter that cannot be read or written answers `502` with `mods_unconfirmed`.

The switches live in `/<resource_prefix>/launcher/mods`, which holds only the settings that differ from the defaults, such as `{"creative": {"fawe": false}}`. A switch back to the default removes its entry, so a later change of that default in the pinned content reaches the world. A running world is not touched: the switch applies at its next start, when the start command names the mods that are on. Whatever the parameter holds, an id the pinned content does not list for that world is never sent, so a switch can only choose among pinned mods ([worlds](worlds.md)).

A site's role may call both routes; The Lobby shows its mods page only to a Member with the `minecraft-admin` grant. The operator's `mods`, `mod-on` and `mod-off` ([cloud controls](cloud-controls.md)) call the same routes.

### The host adapter

The function carries the Crafty adapter it calls, `minecraft_crafty.py` with the two modules it imports and the pinned server content `server.json` it applies, in its package's `adapter` folder (`scripts/package_idle_stop.py --function launcher`), and reads them when it starts. Every game start, drain, restore and allowlist command the function sends begins with lines that compare the host's `/opt/minecraft-crafty/scripts` against those files' SHA-256 and, when any differs or is not root's own regular file that nobody else may write, unpack the carried files beside it, check them again, rename each into place, replacing whatever held its name, and check them once more where they landed. A host whose adapter matches changes nothing. A host without `/opt/minecraft-crafty/scripts` (a world host's first-boot script creates it before anything else), or whose folder is not root's or may be written by anyone else, or where a folder above it would let anyone but root rename it, fails the command before it runs, because root runs what that folder holds. So a deploy of the launcher is the only step an adapter change needs, and the host has the new adapter from its next start or allowlist change; nothing copies the adapter to the host by hand ([ADR 0007](../adr/0007-the-launcher-carries-the-host-adapter.md)). The same holds for a change to `runtime/crafty/server.json`: the adapter's `start` applies it to the stopped server before Crafty starts it ([ADR 0008](../adr/0008-the-adapter-applies-the-pinned-server-content.md)). A stop's drain installs too, so a host that never got as far as a start can be stopped. The idle stop's own drain does not: it relies on the adapter an earlier Launcher command left on the host. The files travel compressed in the command, under 48 KiB in all, which a Go test keeps in check.

The Cloudflare token is read at runtime from one SSM SecureString, `/<resource_prefix>/launcher/cloudflare-token` by default, which you create (G6 in aws-setup's `aws-design.md`). [ADR 0005](../adr/0005-lobby-launcher-starts-from-aws.md) records why the token moves into AWS for these starts. OpenTofu, the function's environment, state and logs hold only its name. The token is sent only to the fixed Cloudflare API, redirects are refused, and errors never include it.

## Deploy

The [Deploy workflow](cloud-controls.md#deploys) builds the package and runs the root from GitHub Actions: Craig starts a run with `mode` set to `plan`, approves it as the `deploy` environment's reviewer and reads it, then starts and approves one with `mode` set to `apply` and that plan's digest; nothing below runs a deploy from a workstation. Steps 1 and 2 are the administrator's one-time setup of the launcher's IAM pieces and token, done from any terminal with the profiles named after `aws sso login`. Steps 3 to 5 are the deployment inputs and the checks. Check each output against what's shown before going on.

1. **Create the launcher's boundary and widen the operator role (administrator, once).** The operator role can create roles only inside the host boundary, which doesn't allow starting or stopping EC2. Set your prefix and account, then create the boundary policy and add the launcher statements to the operator role (an existing boundary from before the stop existed gets a new version instead; see [cloud controls](cloud-controls.md#one-time-setup)):

   ```sh
   prefix=minecraft-family          # resource_prefix from your private deployment.tfvars
   account=793680745829             # the workloads account
   sed -e "s/123456789012/$account/g" -e "s/example-prefix/$prefix/g" \
     docs/operations/operator-role/launcher-boundary.json.example > /tmp/launcher-boundary.json
   aws iam create-policy --profile workloads-admin --policy-name minecraft-launcher-boundary \
     --policy-document file:///tmp/launcher-boundary.json --tags Key=project,Value=minecraft
   ```

   This prints the new policy with `"Arn": "arn:aws:iam::793680745829:policy/minecraft-launcher-boundary"`. Then:

   ```sh
   sed -e "s/123456789012/$account/g" -e "s/example-launcher-boundary/minecraft-launcher-boundary/g" \
     -e "s/example-prefix/$prefix/g" \
     docs/operations/operator-role/launcher-operator.json.example > /tmp/launcher-operator.json
   aws iam put-role-policy --profile workloads-admin --role-name minecraft-operator \
     --policy-name launcher --policy-document file:///tmp/launcher-operator.json
   ```

   `put-role-policy` prints nothing when it succeeds.

2. **Save the Cloudflare token as a SecureString.** Use the same 1Password item the workstation start uses (the `op://…` reference in your Cloudflare env file). 1Password asks for approval, then:

   ```sh
   op read --no-newline "op://<vault>/<item>/credential" | aws ssm put-parameter --profile workloads-admin \
     --region us-west-2 --name "/$prefix/launcher/cloudflare-token" --type SecureString \
     --tags Key=project,Value=minecraft --value file:///dev/stdin
   ```

   This prints `{"Version": 1, "Tier": "Standard"}`. To rotate the token later, run the same command again with `--overwrite` added and without `--tags`.

3. **Add the launcher to the private deployment inputs.** The Deploy workflow builds the package itself (`scripts/package_idle_stop.py --function launcher`, reproducible: the same source and Go toolchain always give the same bytes) and names it through `package_dir`, so the inputs carry no `package_path`. Find the sites' roles with the read-only profile:

   ```sh
   aws iam list-roles --profile workloads-readonly --query "Roles[?starts_with(RoleName,'site-')].Arn"
   ```

   This prints each site environment's roles. Use `site-<environment>-server`, the site's own Lambda role, for each environment The Lobby has deployed (the-lobby's `infra/environments/`). Then add this to the private `deployment.tfvars` the Deploy workflow reads from its `DEPLOYMENT_TFVARS` secret:

   ```hcl
   launcher = {
     sites = {
       dev  = "arn:aws:iam::793680745829:role/site-dev-server"
       prod = "arn:aws:iam::793680745829:role/site-prod-server"
     }
     permissions_boundary_arn = "arn:aws:iam::793680745829:policy/minecraft-launcher-boundary"
     control_role_arn         = "arn:aws:iam::793680745829:role/minecraft-server-control"
   }
   ```

   `control_role_arn` admits the server control role beside the sites; leave it out until that role exists.

   Each key puts that site's environment in its function's name (`<prefix>-dev-launcher`, `<prefix>-prod-launcher`). A site's permissions boundary lets it call only backends named `*-<its environment>-*`, so a site that has no function here cannot reach the others'. Leave out an environment the site has not deployed yet and add it later: the plan then creates only that environment's function.

   A deployment made before October 2026 had `site_role_arn` and `site_environment` instead; replace them with `sites = { dev = <that role> }`. The configuration moves the existing resources to their `dev` key, so the plan shows them as moved, not replaced.

4. **Plan and apply from GitHub.** Merge the change, run the Deploy workflow by hand with `mode` set to `plan`, approve it under Review deployments, and read the run's summary. Expect creates only (and, for a deployment made before October 2026, the moves above). Those are, once, each world's state parameter and the mods and allowlist parameters, and then per environment the function with its role and inline policy, the log group, the async invoke setting, the Function URL, its permissions (two for the site, two more for the control role), the SNS topic and email subscription, and the alarm. Any change to the family host, its data disk, the buckets or the network, and any replacement, is a stop condition; a plan that adds or replaces a world host is read as [worlds](worlds.md) describes. Then start the workflow by hand with `mode` set to `apply` and `plan` set to the digest at the end of the plan run's summary, approve it, and read that run's summary. Then confirm each new SNS subscription email (one per environment). The Function URLs and ARNs are the `launchers` output; the plan summary does not print them, so read them once with the read-only profile, which needs no checkout or state copy, or from the-lobby's environment files after step 6:

   ```sh
   aws lambda get-function-url-config --profile workloads-readonly --region us-west-2 \
     --function-name "$prefix-dev-launcher" --query '{url: FunctionUrl, function_arn: FunctionArn}'
   ```

   This prints that environment's `url` (`https://<id>.lambda-url.us-west-2.on.aws/`) and `function_arn`; repeat it with `$prefix-prod-launcher`.

5. **Check it.** From any terminal with your administrator profile, which may call the Function URL too:

   ```sh
   export LAUNCHER_URL=<url>
   AWS_PROFILE=workloads-admin task server:status
   ```

   With every world off, this prints `{"name":"Survival","state":"stopped","connection":{…},"worlds":[…]}`, the first world at the top and every world in `worlds`. `WORLD=creative task server:start` starts that world and follows its status through `starting` to `ready`, with `creative.<the public hostname>` pointing at the new address; `WORLD=creative task server:stop` drains it and follows it to `stopped`. Without `WORLD` both act on the first world. The [Server control workflow](cloud-controls.md#the-server-control-workflow) does the same from the Actions tab.

6. **Connect each site** in the-lobby: that environment's `minecraft_backend_url` and `minecraft_backend_function_arn` (`infra/environments/<environment>/<environment>.tfvars`) take the `url` and `function_arn` printed for the same environment. The site's deploy then lets its role call `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` on the function. The Launcher is marked ready in `web/src/tools.ts` for every environment at once.

## Cost

Each status request makes, for each world, one EC2 describe, one or two SSM parameter reads and a status ping. The page polls every 30 seconds while it is visible, or every 5 seconds during a start or stop, which stays well inside Lambda's free tier; a stop adds a few runs of the function while it follows the drain. A standard SecureString costs nothing. Each environment's alarm watches four metrics, about $0.40 a month per environment. Each world's host costs its compute only while it runs ([infrastructure](infrastructure.md#cost-review)).

## Evidence limits

`task check` runs the Go unit tests (state rules, start, stop, restore and retry, the follow-up runs' steps against fake AWS, Cloudflare and SSM answers, the worlds' configuration and its match with the pinned content, the mods switches, the allowlist's lookups, record and command, token-safe errors and the HTTP answers) and the mocked plan assertions. None of this calls AWS. Before relying on it, a live check must show a start from the site reaching `ready` with DNS updated, a stop from the site or the workflow reaching `stopped` with the archive in S3 and the hostname parked, the page following an idle stop through `stopping` to `stopped`, and a restore from the workflow reaching `ready` with the archive's world. For the world hosts, none of that has been shown yet: the [proof run](worlds.md#the-proof-run-for-a-world) is that check, for each world.
