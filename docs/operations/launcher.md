# Launcher backend

The Lobby's Minecraft Launcher page (`/minecraft` in [the-lobby](https://github.com/CraigDevJohnson/the-lobby)) shows whether the `family` server is up and lets a Member start it. The page calls this repository's launcher function, a Go Lambda in [`lambda/launcher`](../../lambda/launcher). The site checks the Member's `minecraft` grant first and signs each request with its own role. The function's Function URL accepts only that role (the-lobby ADR 0006). The contract is the-lobby's [`docs/minecraft-backend.md`](https://github.com/CraigDevJohnson/the-lobby/blob/main/docs/minecraft-backend.md). The function is off until `launcher` is set in the private deployment inputs, and with `launcher = null` the plan creates none of its resources.

The site has one environment per hostname (dev, then production), and each site may call only backends named after its environment. `launcher.sites` therefore names the Lobby environments to serve, each with its site's role, and the plan makes one function per environment, `<prefix>-<environment>-launcher`, that only that site's role may call. The functions are twins: they run the same package, start the same host, and share one start record and one token parameter, so a start from one site shows as `starting` on the other. Each has its own logs, alarm and alert topic.

## What it answers

`GET /server` answers `{name, state, last_known?, connection}`. `POST /server/start` starts a `stopped` or `failed` server and answers the same shape, `202` when it started something and `200` when nothing changed. The connection is the players' hostname for Java and the hostname with port 19132 for Bedrock.

| State | When |
| --- | --- |
| `stopped` | EC2 reports the host stopped. |
| `starting` | EC2 is `pending`. Or the host runs without a game answering, and either a start of this function is under way (at most 15 minutes) or the host launched less than 10 minutes ago, which covers a start from the workstation. |
| `ready` | The game answers a Java status ping at the host's public address, with one retry. EC2 or Crafty being up is not enough. Bedrock players join through Geyser in the same server, so the one ping covers both editions. |
| `stopping` | EC2 is `stopping`, or the [idle stop](idle-stop.md) has a drain or EC2 stop under way for this run. |
| `maintenance` | The host has run for more than 10 minutes without a game answering, and no start of this function is pending. Examples are a server stopped from Crafty's page, or a workstation `stop` archiving before it stops EC2. Members get no Start button. |
| `failed` | A start of this function failed, or has not finished after 15 minutes. Start is offered again, and on a running host it retries only the game. |
| `unknown` | AWS could not be read. `last_known` is the last state this warm function answered, when it has one. |

The Member named in `X-Lobby-Member` is logged with every start request.

## How a start runs

A start runs the same steps as `python3 scripts/minecraft_host.py start --instance family`, without the workstation:

1. The request records the start in the SSM parameter `/<resource_prefix>/launcher/state`, shared by every environment's function, calls `StartInstances`, and starts a second, asynchronous run of the same function. The page gets `202` with `starting` straight away.
2. The second run waits for EC2 `running` and a public address. It points the players' hostname at that address through Cloudflare (DNS only, TTL 60), then waits for SSM.
3. It sends `minecraft-instance … start family` through `AWS-RunShellScript` and waits for the adapter to report `running` and `ready`. That is the same command and the same completion check the workstation start uses.
4. It records `ready` or `failed`, tied to the host's EC2 launch time so a later run never inherits it.

A DNS failure never stops the game. The run records it, the page shows no connection details for that run instead of a wrong address, and the run fails so the alarm emails you. Repeated taps, refreshes and several Members starting at once are safe. EC2's start is idempotent, the adapter takes its own lifecycle lock, and the second run's steps all repeat harmlessly. The second run is never retried: if it is lost, the start shows `failed` after 15 minutes and a Member can start again. The alarm also emails you when a second run could not be handed off or aged out before it ran, and when a start request fails. The function counts the first and last as `StartFailures` in the `Minecraft/Launcher` metrics, since the site still gets an answer, and Lambda counts dropped runs as `AsyncEventsDropped`.

The function can start the host but never stop it. Stopping stays with the workstation `stop`, the idle stop, and EC2's own shutdown drain.

The Cloudflare token is read at runtime from one SSM SecureString, `/<resource_prefix>/launcher/cloudflare-token` by default, which you create (G6 in aws-setup's `aws-design.md`). [ADR 0005](../adr/0005-lobby-launcher-starts-from-aws.md) records why the token moves into AWS for these starts. OpenTofu, the function's environment, state and logs hold only its name. The token is sent only to the fixed Cloudflare API, redirects are refused, and errors never include it.

## Deploy

Everything below runs on your Mac in `/Users/craigjohnson/Projects/minecraft`, after `aws sso login` for the profiles named. Steps 1, 2 and 6 change AWS. Check each output against what's shown before going on.

1. **Create the launcher's boundary and widen the operator role (administrator, once).** The operator role can create roles only inside the host boundary, which doesn't allow starting EC2. Set your prefix and account, then create the boundary policy and add the launcher statements to the operator role:

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

3. **Build the function.**

   ```sh
   .tools/bin/task lambda:package:launcher
   ```

   This prints `/Users/craigjohnson/Projects/minecraft/.build/launcher.zip sha256=<64 hex characters>`. The same source and Go toolchain always give the same bytes.

4. **Add the launcher to the private deployment inputs.** Find the sites' roles with the read-only profile:

   ```sh
   aws iam list-roles --profile workloads-readonly --query "Roles[?starts_with(RoleName,'site-')].Arn"
   ```

   This prints each site environment's roles. Use `site-<environment>-server`, the site's own Lambda role, for each environment The Lobby has deployed (the-lobby's `infra/environments/`). Then add this to your private `deployment.tfvars`:

   ```hcl
   launcher = {
     package_path = "/Users/craigjohnson/Projects/minecraft/.build/launcher.zip"
     sites = {
       dev  = "arn:aws:iam::793680745829:role/site-dev-server"
       prod = "arn:aws:iam::793680745829:role/site-prod-server"
     }
     permissions_boundary_arn = "arn:aws:iam::793680745829:policy/minecraft-launcher-boundary"
   }
   ```

   Each key puts that site's environment in its function's name (`<prefix>-dev-launcher`, `<prefix>-prod-launcher`). A site's permissions boundary lets it call only backends named `*-<its environment>-*`, so a site that has no function here cannot reach the others'. Leave out an environment the site has not deployed yet and add it later: the plan then creates only that environment's function.

   A deployment made before October 2026 had `site_role_arn` and `site_environment` instead; replace them with `sites = { dev = <that role> }`. The configuration moves the existing resources to their `dev` key, so the plan shows them as moved, not replaced.

5. **Plan** as described in [infrastructure](infrastructure.md#private-deployment-inputs-and-output). Expect creates only (and, for a deployment made before October 2026, the moves above). Those are, once, the shared state parameter, and then per environment the function with its role and inline policy, the log group, the async invoke setting, the Function URL, its two permissions, the SNS topic and email subscription, and the alarm. Any change to the host, data disk, buckets or network, and any replacement, is a stop condition.

6. **Apply** the reviewed plan once you've approved it. Then confirm each new SNS subscription email (one per environment) and read the addresses:

   ```sh
   .tools/bin/tofu -chdir="$private_dir/infrastructure/infra" output -json launchers
   ```

   This prints, per environment, `url` (`https://<id>.lambda-url.us-west-2.on.aws/`) and `function_arn`. Keep the terminal open and set `url` to the one you want to check:

   ```sh
   url=$(.tools/bin/tofu -chdir="$private_dir/infrastructure/infra" output -json launchers | python3 -c 'import json,sys; print(json.load(sys.stdin)["dev"]["url"])')
   ```

7. **Check it from the Mac.** Your administrator role may call the Function URL too:

   ```sh
   eval "$(aws configure export-credentials --profile workloads-admin --format env)"
   curl -sS --aws-sigv4 "aws:amz:us-west-2:lambda" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
     -H "x-amz-security-token: $AWS_SESSION_TOKEN" "${url%/}/server"
   ```

   With the server off, this prints `{"name":"Family server","state":"stopped","connection":{…}}`. Replacing the path with `-X POST "${url%/}/server/start"` starts it. Following `GET` answers go `starting`, then `ready` about a minute later, and mc.craigdevjohnson.com points at the new address.

8. **Connect each site** in the-lobby: that environment's `minecraft_backend_url` and `minecraft_backend_function_arn` (`infra/environments/<environment>/<environment>.tfvars`) take the `url` and `function_arn` printed for the same environment. The site's deploy then lets its role call `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` on the function. The Launcher is marked ready in `web/src/tools.ts` for every environment at once.

## Cost

Each status request makes one EC2 describe, one or two SSM parameter reads and a status ping. The page polls every 30 seconds while it is visible, or every 5 seconds during a start, which stays well inside Lambda's free tier. A standard SecureString costs nothing. Each environment's alarm watches three metrics, about $0.30 a month per environment. The server itself costs what it does today while it runs.

## Evidence limits

`task check` runs the Go unit tests (state rules, start and retry, the second run's steps against fake AWS, Cloudflare and SSM answers, token-safe errors and the HTTP answers) and the mocked plan assertions. None of this calls AWS. Before relying on it, a live check must show a start from the site reaching `ready` with DNS updated. It must also show the page following a workstation stop and an idle stop through `maintenance` or `stopping` to `stopped`.
