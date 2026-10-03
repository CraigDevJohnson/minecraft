# Idle stop

A forgotten running host keeps billing until it is stopped. The optional idle-stop function drains and stops the host after nobody has played for a set time. It is off until `idle_stop` is set in the private deployment inputs. With `idle_stop = null` the plan creates none of its resources.

## How it decides

An EventBridge rule invokes the Go function in [`lambda/idlestop`](../../lambda/idlestop) every five minutes. It supports one configured instance on Java port 25565. Enabling it with multiple instances is rejected because one status ping cannot prove that every instance is idle. Reserved concurrency is one so invocations cannot overwrite each other's progress. Each invocation does one step:

1. Describe the host. If it is not `running`, finish any pending DNS parking and clear the saved state.
2. Ask the host's current EC2 public IPv4 address for its player count with the Java Server List Ping. Bedrock players join through Geyser and Floodgate as Java players, so this count covers both editions. A failed ping or missing address clears the idle countdown, leaves the host running, and fails the invocation so repeated failures raise the alarm.
3. Record when the server first looked idle. Any player online clears that time.
4. Once it has been idle for `idle_minutes` (default 30), send the same host drain that `minecraft_host.py stop` sends: `minecraft-instance … host-drain` through SSM `AWS-RunShellScript`.
5. On later invocations, follow that SSM command. Call `StopInstances` only when the drain output covers exactly the configured instances, confirms the drain barrier, and reports every application `stopped` with backup `complete` or `not-required`. These are the same rules the operator command applies. Before every stop attempt, send a fresh read-only `host-status` and poll for at most 20 seconds within the same invocation. Validate its current barrier, exact inventory, application state and backup evidence. A reboot clears the boot-bound barrier, so old drain output cannot stop a rebooted host. Recheck the host's launch time immediately before stopping it so an old drain cannot stop a new session.
6. Persist the DNS retry obligation before issuing the EC2 stop. If this checkpoint fails, leave the host running. After EC2 accepts the stop, park the existing Cloudflare A record on `192.0.2.1`, DNS only with TTL 60. A DNS failure reports an error and retains a retry, including after EC2 is stopped. It never repeats the drain or undoes the stop. A new launch discards the old parking retry.

The drain can run for up to an hour, longer than one Lambda invocation can last, so the function stores its progress in the SSM parameter `/<resource_prefix>/idle-stop/state`. That state is tied to the host's EC2 launch time. A stop and start between two invocations discards any old idle timer or drain, so a new session is never stopped by an earlier one.

The function never force-stops EC2 and never stops a host whose drain lacks backup evidence.

## When it is blocked

If the drain fails, reports anything other than every instance stopped with a completed backup, never becomes visible, or runs past about 65 minutes, the function records `blocked` and leaves the host **running**. From then on, every invocation fails without sending another drain. After two failing invocations in a row, the `<prefix>-idle-stop-failing` alarm emails the budget recipient, and the alarm stays on until the block clears.

Repair the cause using the normal procedure in [host commands](host-commands.md), then stop the host with `python3 scripts/minecraft_host.py --deployment … stop`. The next launch clears the block automatically. To clear it without a restart, overwrite the parameter with `{}`.

DNS parking retrieves the token at runtime from one administrator-owned Secrets Manager secret. Terraform, Lambda environment variables, state, and logs contain only its ARN. The token is sent only to the fixed HTTPS Cloudflare API, redirects are refused, and response bodies and secret errors are excluded from diagnostics. The token needs DNS edit for the existing Cloudflare zone. The adapter requires exactly one matching A record and never creates or deletes DNS records.

A transient `StopInstances` failure after a verified drain is retried on the next invocation without draining again. A single transient AWS error does not trigger the alarm.

## Pausing it

Disable the rule before maintenance that keeps the host running with Minecraft stopped. An unreachable server produces errors and an alarm. Maintenance with an answering but empty Minecraft server can still trigger the idle stop. Re-enable the rule afterwards.

```sh
aws events disable-rule --name "<resource_prefix>-idle-stop"
aws events enable-rule --name "<resource_prefix>-idle-stop"
```

An apply restores the rule's configured `ENABLED` state.

## Build and enable

Go is required (see [development](../development.md)). From the repository root:

```sh
.tools/bin/task lambda:package
```

This writes `.build/idle-stop.zip`, a reproducible ARM64 `bootstrap` for the `provided.al2023` runtime, and prints its SHA-256. Rebuilding the same source with the same Go toolchain produces identical bytes. A different toolchain can change the code hash. Add to the private deployment tfvars:

```hcl
idle_stop = {
  package_path = "/absolute/path/to/minecraft/.build/idle-stop.zip"
  idle_minutes = 30
  cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef"
}
```

Have an administrator create the token secret in the deployment account and region before enabling the function. Store a plain SecretString API token using the default AWS-managed encryption key; a custom key needs separately reviewed decrypt permissions that this role does not grant. Never put the token in deployment inputs, environment variables, source, or user data.

Plan as described in [infrastructure](infrastructure.md). Expect creates only: the function and its role and inline policy, the state parameter, the log group, the schedule rule, its target and permission, the SNS topic and email subscription, and the alarm. Any change to the host, data volume, buckets, or network is a stop condition. After the approved apply, confirm the SNS subscription email so the alarm can reach you.

## Permissions

The function role allows:

- `ec2:DescribeInstances`, which has no resource-level scoping.
- `ec2:StopInstances` on this host only.
- `ssm:SendCommand` on this host with the `AWS-RunShellScript` document only.
- `ssm:GetCommandInvocation`.
- `ssm:GetParameter` and `ssm:PutParameter` on its own state parameter.
- Writing to its own log group.
- `secretsmanager:GetSecretValue` on exactly the configured token secret ARN.

It cannot start instances, run commands on other hosts, or read backups. The host role is unchanged.

The operator role needs permissions it does not have for the host-only deployment: Lambda, EventBridge rules, CloudWatch Logs and alarms, SNS, the state parameter, `iam:PassRole` to `lambda.amazonaws.com`, and creating the `<resource_prefix>-idle-stop` role. If the operator role caps the roles it creates with a host permissions boundary, that boundary does not allow EC2 stop or SSM commands. An administrator then provides a separate boundary for this role, and the operator passes it as `idle_stop.permissions_boundary_arn`. Have an administrator review and grant these before the plan. The function boundary must also allow reading this exact token secret. No secret, function, or permission is created by local verification.

## Cost

The function adds Lambda invocations, scheduling, logs, an alarm, an SNS email subscription, and a Secrets Manager secret with runtime retrieval. Review [Lambda pricing](https://aws.amazon.com/lambda/pricing/) and [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/) with the administrator before enabling it. About 8,900 scheduled invocations run each month, plus retries. Token retrieval occurs when parking is required, rather than on every player-count check.

## Evidence limits

`task check` runs the Go unit tests and mocked plan assertions. The Go tests cover state transitions, drain evidence, blocking, launch-time resets, the stop retry, failed activity checks, current-address discovery, stale evidence, DNS parking and retries, token-safe diagnostics, and the ping protocol against a local fake server. None of this calls AWS. Before relying on it, a live check must show that a real idle period drains with a verified backup and stops EC2, that a blocked drain leaves the host running and raises the alarm, and that the existing hostname is parked after stop or after a DNS retry.

The opt-in `on-demand-host` lifecycle rejects `idle_stop` because this function’s permissions and environment bind to one host ID. Keep the retained-host mode for this integration; supporting recreated session hosts is separate work. See [on-demand host sessions](on-demand-host.md).
