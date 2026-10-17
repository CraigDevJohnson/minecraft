# Host and session commands

Everyday operation no longer uses these commands: the launcher starts, stops, restores and allowlists the server from AWS, asked from the `/minecraft` page, the Server control workflow or `task server:*` ([cloud controls](cloud-controls.md), [ADR 0006](../adr/0006-cloud-controls.md)). The Crafty console runs in a Session Manager shell opened from the AWS console ([crafty](crafty.md#send-a-console-command)). The commands below remain only for a hands-on emergency the launcher cannot reach.

Run `scripts/minecraft_host.py` on the operator computer with Python 3.11+ and AWS CLI v2 installed. AWS CLI was available during implementation; the repository does not install it. Use the existing operator AWS profile/session. The game host's IAM role receives no EC2 start/stop permissions.

The private deployment handoff is the direct JSON object produced by `tofu output -json deployment`, described in [infrastructure operations](infrastructure.md). The command consumes `account_id`, `host_id`, `region`, and the `instances` mapping, plus `public_hostname` and `cloudflare_zone_id` when the deployment has them. `account_id` must be a 12-digit ASCII string, matching the reviewed infrastructure account. Keep this file private and outside tracked files; it contains deployment identifiers. For example, after an authorized deployment:

```sh
umask 077
mkdir -p operator-config/family
.tools/bin/tofu -chdir=infra output -json deployment > operator-config/family/deployment.json
```

Install and configure the instance runtime on the host before using these commands. SSM invokes `/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json` as the fixed host-side interface. That private configuration supplies explicit EULA acceptance, retained-volume identity, and the required backup hook. Host startup does not supply or infer those values.

Every invocation, including `status`, first calls `sts get-caller-identity` with the operator's active credentials and requires its account to match the deployment handoff before any EC2 or SSM call. A missing or malformed deployment account, failed identity lookup, or mismatch stops the command. Correct the AWS profile/session or handoff and retry; the diagnostic does not print either account ID or the caller identity. The STS request shares the 30-second AWS process bound. [GetCallerIdentity](https://docs.aws.amazon.com/cli/latest/reference/sts/get-caller-identity.html) identifies the account owning the caller and does not require a new IAM permission grant.

```sh
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json start --instance survival
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json status
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json stop
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json dns
```

When the deployment has a public hostname, `start`, `stop`, and `dns` also keep its DNS record on the host and need the Cloudflare token for that one command; see [Public hostname](#public-hostname).

`start --instance survival` starts EC2 only when stopped, waits for EC2 running and SSM online, then starts that configured instance and requires actual application readiness. The instance ID is required and must match the deployment. Repeating start on a running, ready instance is safe. An already-pending EC2 startup is observed to completion. Other transitional states fail with a retry instruction.

`stop` is a separate host operation. It asks the host to drain every configured instance, including already-stopped instances with pending or failed backup work. The host holds lifecycle exclusion during shutdown and cold backup, and writes a drain barrier tied to the current Linux boot. Another session start cannot cancel that barrier. EC2 stop is requested only after the response covers exactly the configured instances, confirms the barrier, and reports every application stopped with backup complete. `not-required` is accepted only through the host runtime's pristine, never-started-instance rule.

The drain barrier survives failed backups and failed EC2 calls. Repair the failure and repeat host stop. After a successful EC2 stop followed by a new boot, the runtime recognizes the old boot identity and permits a new session. Do not remove the barrier to work around an incomplete stop. Instance-only stop leaves EC2 running and does not initiate a host drain.

`status` reports host, application, and backup separately. The `host` field is the last observed EC2 state. Application statuses are `running`, `stopped`, or `unknown`; backup statuses from the runtime are `pending`, `complete`, `not-required`, or `failed`. Stopped/unreachable hosts cannot supply fresh runtime evidence: output uses `application: unavailable` and `backup: unverified` for a host that is not running, or returns a structured failure when a running host cannot be reached through SSM. A repeated host stop on an already-stopped EC2 instance returns success for that existing host state, with backup `unverified`; it does not assert that a backup was checked.

Failed operations exit nonzero. If EC2 stop fails after a successful drain, output retains the confirmed application/backup outcomes. Failures before runtime evidence is available report application `unknown` and backup `unverified`. JSON output includes only configured instance names, controlled status values, data disk usage numbers, and bounded error descriptions. Raw AWS stderr, SSM output, Cloudflare responses, deployment identifiers, credentials, RCON secrets, and player identities are not printed. The one exception is `public_ip` after a failed DNS update on a running host. Diagnose detailed errors in private AWS/SSM records; do not paste those records into public logs.

## Data disk usage

`start` and `status` add the host's `storage` report for the retained data disk: `size_bytes`, `available_bytes`, which is what Minecraft's unprivileged account can still write, and `used_percent`. The percentage is computed like `df`'s `Use%`, so the blocks ext4 reserves for root count as neither used nor available:

```json
{"host": "running", "draining": false, "instances": {"survival": {"instance": "survival", "application": "running", "backup": "pending"}}, "storage": {"size_bytes": 5196382208, "available_bytes": 4100000000, "used_percent": 17}}
```

At 80 percent or more the output also has `storage_warning`, `data disk N% used; grow it (docs/operations/data-disk-capacity.md)`. The warning never changes the exit status and never blocks a start. [Grow the disk](data-disk-capacity.md#grow-the-disk) when it first appears; that takes minutes and no downtime.

A host whose runtime predates this report sends none, and the output then has no `storage` field. A malformed report fails the command and keeps the verified application and backup states; a game that `start` already started keeps running. `stop` and its drain evidence carry no storage report.

## Public hostname

The host takes a new public IPv4 address on every start and releases it on stop. A deployment whose handoff has `public_hostname` and `cloudflare_zone_id` keeps one Cloudflare A record on that address. The recovery rehearsal handoff has neither, so these commands make no DNS call for a rehearsal host and behave and print exactly as before.

Craig creates the record once: type A, **DNS only** (not proxied), TTL 60. The commands only update that record. They never create or delete one, and they write only two values: the running host's address, or `192.0.2.1`, an [RFC 5737](https://www.rfc-editor.org/rfc/rfc5737) documentation address that is never routed. Each update forces DNS only and TTL 60. An update is refused unless the exact name has exactly one record and that record is type A: a stray AAAA or CNAME would send some clients elsewhere.

- `start` updates the record once EC2 reports running and before it waits for SSM, so DNS propagates while the game boots. The address comes from the `describe-instances` responses the command already reads. Output adds `dns: updated` or `dns: current`.
- `stop` parks the record on `192.0.2.1`, only after EC2 reports `stopped`. A repeat stop of an already-stopped host parks it too. Output adds `dns: parked` or `dns: current`. AWS can assign a released address to another customer; parking keeps the name from pointing at it.
- `dns` catches the record up after a start or stop made elsewhere, such as the console: it updates the record for a running host and parks it for a stopped one. In any other state it makes no Cloudflare call and exits nonzero with a retry instruction.
- `status` makes no Cloudflare call.

A DNS failure never blocks or undoes the game start, the drain, or the EC2 stop. The lifecycle step finishes and keeps its evidence; the command then exits 1 with `dns: failed` and an `error` that says to rerun `dns`. For a running host whose address is known, the output also has `public_ip`, so Craig can hand out the address or correct the record in the dashboard. Treat that address as private. If the game start also fails, `error` reports the game failure and the `dns` and `public_ip` fields remain. Failures include a missing or malformed token, an HTTP or API error, a timeout, an unconfirmed update, a record that breaks the rules above, and a running host that reports no global IPv4 address.

### Cloudflare token

Create a custom API token with **Zone → DNS → Edit**, scoped to **Include → Specific zone →** the hostname's zone, with an expiry of about 12 months. A Cloudflare token cannot be scoped to one record: it can edit every record in the zone. It therefore stays on the operator computer and never goes in tfvars, OpenTofu state, the repository, CI, or the host.

Store it in a password manager, not in a file. The examples use the 1Password CLI: save the token as an API Credential item, and keep one private file outside Git, `cloudflare_env` below, that holds only the secret reference, never the value:

```sh
CLOUDFLARE_API_TOKEN="op://<vault>/<item>/credential"
```

`op run` resolves the reference for one command, after the usual 1Password approval, and masks the value if anything prints it:

```sh
cloudflare_env=operator-config/family/cloudflare.env
op run --env-file="$cloudflare_env" -- \
  python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json start --instance survival
```

Any other source works if it sets the variable for that one command rather than exporting it into a long-lived shell.

The command reads the token only from `CLOUDFLARE_API_TOKEN`. It sends it only in the `Authorization` header to the fixed `https://api.cloudflare.com/client/v4` endpoint, refuses redirects, never prints it, and removes it from the environment of every `aws` process it runs. To rotate, use **Roll** on the token in the Cloudflare dashboard, then replace the value in the password manager item; the reference file does not change.

Requests use Python's standard library with default TLS verification. A python.org macOS build has no CA bundle until its `Install Certificates.command` has run; until then every request fails certificate verification and is reported as `dns: failed`.

### Clients

- Java: add the server by hostname. Port 25565 is the default, so no SRV record is needed.
- Bedrock: the hostname and port 19132. Bedrock does not use SRV records.
- After a start, the new address is visible within about a minute. Some resolvers hold a record longer than its TTL, and Java caches a lookup for 30 seconds. If a join fails right after a start, restart the client.

The family allowlist and online authentication remain the access control. The hostname is not a secret; it is a deployment input, and the health check and runbooks name it.

## Timeouts and retries

- Each AWS CLI process is bounded to 30 seconds, or the remaining phase deadline when shorter.
- EC2 transitions and SSM online readiness each have a 300-second deadline.
- With a public hostname, a running host that has not yet reported a global IPv4 address is polled for at most 300 seconds. Each Cloudflare request applies a 30-second timeout to each network operation, not to the whole request, and one DNS update makes at most two requests.
- After an acknowledged EC2 start or stop request, polling also accepts the previous `stopped` or `running` state within that same 300-second deadline. [EC2 eventual consistency](https://docs.aws.amazon.com/ec2/latest/devguide/eventual-consistency.html) can delay visibility of the request. The command sends the mutation once; a persistently old state times out, and unrelated or terminal states still fail immediately. Observing an already-pending start without submitting a mutation does not enable this previous-state retry.
- SSM delivery has a 60-second timeout and the remote command has a 3,600-second execution timeout. Local invocation polling is bounded to 3,660 seconds and handles initial `InvocationDoesNotExist` propagation.
- Missing, failed, incomplete, or timed-out drain evidence forbids EC2 stop. No timeout path force-stops EC2.
- An AWS transport timeout can leave the requested action in progress. Local timeout does not cancel an already-submitted SSM operation. Inspect status and retry the same lifecycle command; host-side locking, backup state, and the boot-bound drain barrier govern the retry. A host already stopping returns a retry instruction until its transition finishes.

The initial single `survival` instance fits this remote execution envelope with the documented per-instance shutdown and backup bounds. Before deploying additional instances, reassess the total sequential drain duration against the one-hour SSM limit; a larger fleet must not rely on a timeout to finish its backups.

The operator needs existing access to `ec2:DescribeInstances`, `ec2:StartInstances`, `ec2:StopInstances`, `ssm:DescribeInstanceInformation`, `ssm:SendCommand`, and `ssm:GetCommandInvocation`. The [example operator role](operator-role/permissions.json.example) allows `ssm:SendCommand` only on instances tagged `Project=minecraft`, which the OpenTofu provider's default tags apply to the host, and only with the `AWS-RunShellScript` document. Operators also need `cloudtrail:LookupEvents`, but only for the disk provenance check before a first-setup zero-fill in [infrastructure operations](infrastructure.md#disk-setup-and-readiness-handoff-to-5-and-7); these commands do not use it. DNS updates need no additional AWS permission: the address comes from the same `ec2:DescribeInstances` call. These commands do not create or modify IAM policies. Actual AWS authorization and live start/stop validation remain the deployment work in issue #7.

## Validation boundary

`tests/test_minecraft_host.py` exercises the public command interface against a controlled AWS CLI boundary. It covers deployment/caller account matching before EC2/SSM access, malformed account inputs, failed STS lookup, duplicate operations, all-instance drain evidence, stopped instances with pending backups and retry, EC2/SSM failures, startup/readiness timeouts, remote invocation propagation, malformed responses, ID validation, and private-output filtering. Process-level fixtures verify the real CLI adapter's timeout and diagnostic handling.

For the public hostname, the same tests use a controlled Cloudflare boundary. They cover the update after start and its order before the game start, no update when the record is current, forced DNS only and TTL 60, the bounded wait for a global address, every failure type leaving the game started with `dns: failed` and `public_ip`, parking only after EC2 reports `stopped` and on a repeat stop, a failed park keeping the drain evidence, `dns` on running, stopped, and transitional hosts, no DNS call without a hostname or for `status`, and hostname and zone validation before any AWS or Cloudflare call. A loopback HTTP server and a stand-in `aws` executable exercise the real adapters: the token appears only in the `Authorization` header, is never printed, is never sent to a redirect target, and is absent from the `aws` process environment.

For data disk usage, they cover the three validated numbers passing through `start` and `status`, the warning at 80 percent and not at 79, a malformed report failing with the verified states kept, a host that sends no report, and unchanged `stop` and drain output.

These tests contact neither AWS nor Cloudflare and do not establish live backup recovery, live DNS behavior, or a real disk's usage.

AWS interface references: [SendCommand delivery timeout and parameters](https://docs.aws.amazon.com/cli/latest/reference/ssm/send-command.html), [GetCommandInvocation eventual consistency](https://docs.aws.amazon.com/cli/latest/reference/ssm/get-command-invocation.html), and [Run Shell Script execution timeout](https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-command-ssm-plugin-reference.html#aws-runShellScript).

## Opt-in absent-host lifecycle

The version-2 `on-demand-host` handoff can name an absent host. That mode requires the separately reviewed workstation policy (`--lifecycle` and `--policy-sha256`) and replaces EC2 stop/start with guarded OpenTofu session removal/recreation. The protected data disk remains retained. Follow [on-demand host sessions](on-demand-host.md) for migration, private transfer, interruption repair, recurring authority and actual cold-start timing. Existing handoffs retain the commands above.

The separate opt-in `s3-only` mode also removes the session data disk, with exact independently verified recovery selections in its absent-data handoff. Follow [S3-only world operations](s3-only-world.md) for the explicit ownership/protection migration, provenance floor, receipt transfer, restore retries and measured timing. Existing retained-host and on-demand-host modes remain compatible.
