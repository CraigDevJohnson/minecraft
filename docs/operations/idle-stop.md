# Idle stop

A forgotten running host keeps billing until it is stopped. The optional idle-stop function drains and stops a world's host after nobody has played on that world for a set time. Each world has its own host ([ADR 0009](../adr/0009-one-host-per-world.md)), and each is checked on its own: one empty world stops while another is being played. It is off until `idle_stop` is set in the private deployment inputs. With `idle_stop = null` the plan creates none of its resources.

## How it decides

One EventBridge rule invokes the Go function in [`lambda/idlestop`](../../lambda/idlestop) every five minutes, once for each world: the rule has one target per world, whose input is `{"world": "<id>"}`, and an invocation checks only the world its event names. An event that names no configured world fails. Each world host runs one instance, named by the world's id, on Java port 25565, so one status ping proves that world idle. The function gets each world's host, hostname and state parameters from the same `WORLDS` setting the launcher gets. Each invocation does one step and ends within its 60-second limit, well inside the five-minute schedule, so invocations do not overlap; the function reserves no concurrency, because a reservation must leave 100 unreserved executions in the account. The steps below are one world's. One rare overlap is left open: Lambda can deliver the same scheduled event twice, and if both copies land as the idle countdown ends they each send a drain. The host adapter refuses the second, and if the refused one's record wins, the idle stop blocks with the drained host still running, as [below](#when-it-is-blocked). The alarm emails you, and a Stop from the launcher finishes the job. Each step:

1. Describe the world's host. If it is not `running`, finish any pending DNS parking and clear the saved state.
2. When the [launcher](launcher.md) is deployed, read its record for this world. A stop of the launcher's for this launch of the host, inside its 75-minute deadline, means the host is already being drained: record `launcher-stopping`, clear the idle countdown, and do nothing else this time. A restore of the launcher's for this launch, or one recorded before the host started, inside its 45-minute deadline, keeps the game down while an archive goes in place: record `launcher-restoring` the same way. An unreadable launcher state fails the invocation, like a failed ping. A Member's Stop in the second or two between this read and the save of the idle stop's own drain still lets both begin one ([launcher](launcher.md#rare-overlaps)).
3. Ask the host's current EC2 public IPv4 address for its player count with the Java Server List Ping. Bedrock players join through Geyser and Floodgate as Java players, so this count covers both editions. A failed ping or missing address clears the idle countdown, leaves the host running, and fails the invocation so repeated failures raise the alarm.
4. Record when the server first looked idle. Any player online clears that time.
5. Once it has been idle for `idle_minutes` (default 30), send the host drain: `minecraft-instance … host-drain` through SSM `AWS-RunShellScript`. Unlike the launcher's stop, this command does not install the host adapter first; it runs the adapter an earlier launcher command left on the host.
6. On later invocations, follow that SSM command. Call `StopInstances` only when the drain output covers exactly the configured instances, confirms the drain barrier, and reports every application `stopped` with backup `complete` or `not-required`. These are the same rules the operator command applies. Before every stop attempt, send a fresh read-only `host-status` and poll for at most 20 seconds within the same invocation. Validate its current barrier, exact inventory, application state and backup evidence. A reboot clears the boot-bound barrier, so old drain output cannot stop a rebooted host. Recheck the host's launch time immediately before stopping it so an old drain cannot stop a new session.
7. Persist the DNS retry obligation before issuing the EC2 stop. If this checkpoint fails, leave the host running. After EC2 accepts the stop, park the existing Cloudflare A record on `192.0.2.1`, DNS only with TTL 60. A DNS failure reports an error and retains a retry, including after EC2 is stopped. It never repeats the drain or undoes the stop. A new launch discards the old parking retry.

The drain can run for up to an hour, longer than one Lambda invocation can last, so the function stores each world's progress in that world's SSM parameter, `/<resource_prefix>/idle-stop/worlds/<world>/state`. That state is tied to the host's EC2 launch time. A stop and start between two invocations discards any old idle timer or drain, so a new session is never stopped by an earlier one.

The function never force-stops EC2 and never stops a host whose drain lacks backup evidence.

## When it is blocked

If the drain fails, reports anything other than every instance stopped with a completed backup, never becomes visible, or runs past about 65 minutes, the function records `blocked` for that world and leaves its host **running**. From then on, every invocation for that world fails without sending another drain; the other worlds are still checked. After two failing invocations in a row, the `<prefix>-idle-stop-failing` alarm emails the budget recipient, and the alarm stays on until the block clears. There is one alarm for the function, so the log line, which carries the world's id, says which world it is.

Repair the cause in a [shell on the host](crafty.md#open-a-shell-on-the-host), then stop the world with the launcher ([cloud controls](cloud-controls.md): the Server control workflow's `stop` with the world, or `WORLD=<world> task server:stop`). The next launch clears the block automatically. To clear it without a restart, overwrite that world's parameter with `{}`.

One case is never drained by the idle stop. A world host that is running but never received a launcher command, such as a first start that failed before its command was sent, has no adapter for the idle stop's drain to run. The game does not answer on such a host, so the check fails and the alarm emails you rather than the host being drained. Stop it with the launcher's forced stop, which installs the adapter first.

DNS parking reads the Cloudflare token at runtime from the [launcher](launcher.md)'s SecureString, `/<resource_prefix>/launcher/cloudflare-token` or the launcher's `cloudflare_token_parameter`, so there is one copy of the token. OpenTofu, Lambda environment variables, state, and logs contain only its name. The token is sent only to the fixed HTTPS Cloudflare API, redirects are refused, and response bodies and secret errors are excluded from diagnostics. The token needs DNS edit for the existing Cloudflare zone. The adapter requires exactly one matching A record and never creates or deletes DNS records.

A transient `StopInstances` failure after a verified drain is retried on the next invocation without draining again. A single transient AWS error does not trigger the alarm.

## Pausing it

Disable the rule before maintenance that keeps a world's host running with Minecraft stopped. The one rule covers every world, so no world idles out while it is disabled. An unreachable server produces errors and an alarm. Maintenance with an answering but empty Minecraft server can still trigger the idle stop. Re-enable the rule afterwards.

```sh
aws events disable-rule --name "<resource_prefix>-idle-stop"
aws events enable-rule --name "<resource_prefix>-idle-stop"
```

An apply restores the rule's configured `ENABLED` state.

## Enable

The Deploy workflow builds `idle-stop.zip` itself (`scripts/package_idle_stop.py`, reproducible with the pinned Go toolchain) and names it through `package_dir`, so the private inputs carry no `package_path`. Add to the private `deployment.tfvars` the Deploy workflow reads from its `DEPLOYMENT_TFVARS` secret:

```hcl
idle_stop = {}
```

That takes the defaults for every world: 30 idle minutes, the launcher's token SecureString and the launcher's permissions boundary. Set `idle_minutes` (10 to 240) or `permissions_boundary_arn` inside the braces to change them.

The token SecureString already exists for the launcher; never put the token in deployment inputs, environment variables, source, or user data. Before the first plan, an administrator puts the operator role's updated `launcher` inline policy from [`launcher-operator.json.example`](operator-role/launcher-operator.json.example), the same `put-role-policy` as in the [launcher runbook](launcher.md): it adds the EventBridge schedule rule and its target to what the operator may manage.

Plan and apply through the [Deploy workflow](cloud-controls.md#deploys). Expect creates for the function and its role and inline policy, each world's state parameter, the log group, the schedule rule, one target per world and the permission, the SNS topic and email subscription, and the alarm. Each launcher also changes in place: its inline policy gains `ReadIdleStopProgress` and its `WORLDS` setting names each world's idle-stop state parameter. Any other change, and any change to a host, the family host's data volume, buckets, or network, is a stop condition. A deployment from before the worlds keeps its old `/<resource_prefix>/idle-stop/state` parameter, which the function no longer reads, until the family host is retired. After the approved apply, confirm the SNS subscription email so the alarm can reach you.

## Permissions

The function role allows:

- `ec2:DescribeInstances`, which has no resource-level scoping.
- `ec2:StopInstances` on the world hosts only.
- `ssm:SendCommand` on the world hosts with the `AWS-RunShellScript` document only.
- `ssm:GetCommandInvocation`.
- `ssm:GetParameter` and `ssm:PutParameter` on its own state parameter for each world, and `ssm:GetParameter` on the launcher's for each world.
- Writing to its own log group.
- `ssm:GetParameter` on the launcher's Cloudflare token SecureString, and `kms:Decrypt` on the AWS managed `aws/ssm` key through SSM only.

It cannot start instances, run commands on other hosts, including the family host, or read backups.

The operator creates the `<resource_prefix>-idle-stop` role under the launcher's permissions boundary, which already allows everything above for the project's hosts, parameters and logs, so no boundary of its own is needed. The operator role's `launcher` policy covers the function, its role, logs, alarm, SNS topic and state parameter; the version in [`launcher-operator.json.example`](operator-role/launcher-operator.json.example) adds the schedule rule and its target. Nothing is created by local verification.

## Cost

The function adds Lambda invocations, scheduling, logs, an alarm and an SNS email subscription: about 8,900 scheduled invocations a month for each world, plus retries, a few cents at most ([Lambda pricing](https://aws.amazon.com/lambda/pricing/)). The token is read only when parking is required, rather than on every player-count check. A world's host left running by mistake costs about 8 cents an hour.

## Evidence limits

`task check` runs the Go unit tests and mocked plan assertions. The Go tests cover state transitions, drain evidence, blocking, launch-time resets, the stop retry, failed activity checks, current-address discovery, stale evidence, DNS parking and retries, token-safe diagnostics, the ping protocol against a local fake server, and the event's choice of world. None of this calls AWS. Before relying on it, a live check must show that a real idle period drains with a verified backup and stops EC2, that a blocked drain leaves the host running and raises the alarm, and that the world's hostname is parked after stop or after a DNS retry. That was shown on the family host; each world shows it in its [proof run](worlds.md#the-proof-run-for-a-world).
