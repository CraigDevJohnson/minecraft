# Idle stop

A forgotten running host is the largest avoidable cost: about $60 a month for `m7g.large` compute alone. The optional idle-stop function drains and stops the host after nobody has played for a set time. It is off until `idle_stop` is set in the private deployment inputs. With `idle_stop = null` the plan creates none of its resources.

## How it decides

An EventBridge rule invokes the Go function in [`lambda/idlestop`](../../lambda/idlestop) every five minutes. Each invocation does one step:

1. Describe the host. If it is not `running`, clear the saved state and stop.
2. Ask the game endpoint for its player count with the Java Server List Ping. Bedrock players join through Geyser and Floodgate as Java players, so this count covers both editions. A failed ping counts as idle.
3. Record when the server first looked idle. Any player online clears that time.
4. Once it has been idle for `idle_minutes` (default 30), send the same host drain that `minecraft_host.py stop` sends: `minecraft-instance … host-drain` through SSM `AWS-RunShellScript`.
5. On later invocations, follow that SSM command. Call `StopInstances` only when the drain output covers exactly the configured instances, confirms the drain barrier, and reports every application `stopped` with backup `complete` or `not-required`. These are the same rules the operator command applies.

The drain can run for up to an hour, longer than one Lambda invocation can last, so the function stores its progress in the SSM parameter `/<resource_prefix>/idle-stop/state`. That state is tied to the host's EC2 launch time. A stop and start between two invocations discards any old idle timer or drain, so a new session is never stopped by an earlier one.

The function never force-stops EC2 and never stops a host whose drain lacks backup evidence.

## When it is blocked

If the drain fails, reports anything other than every instance stopped with a completed backup, never becomes visible, or runs past about 65 minutes, the function records `blocked` and leaves the host **running**. From then on, every invocation fails without sending another drain. After two failing invocations in a row, the `<prefix>-idle-stop-failing` alarm emails the budget recipient, and the alarm stays on until the block clears.

Repair the cause using the normal procedure in [host commands](host-commands.md), then stop the host with `python3 scripts/minecraft_host.py --deployment … stop`. The next launch clears the block automatically. To clear it without a restart, overwrite the parameter with `{}`.

A transient `StopInstances` failure after a verified drain is retried on the next invocation without draining again. A single transient AWS error does not trigger the alarm.

## Pausing it

Disable the rule before maintenance that keeps the host running with Minecraft stopped. An unreachable server counts as idle, and the drain would stop the host. Re-enable the rule afterwards.

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

This writes `.build/idle-stop.zip`, a reproducible ARM64 `bootstrap` for the `provided.al2023` runtime, and prints its SHA-256. Rebuilding the same source produces identical bytes, so a plan shows a code change only when the source changed. Add to the private deployment tfvars:

```hcl
idle_stop = {
  package_path = "/absolute/path/to/minecraft/.build/idle-stop.zip"
  idle_minutes = 30
}
```

Plan as described in [infrastructure](infrastructure.md). Expect creates only: the function and its role and inline policy, the state parameter, the log group, the schedule rule, its target and permission, the SNS topic and email subscription, and the alarm. Any change to the host, data volume, buckets, or network is a stop condition. After the approved apply, confirm the SNS subscription email so the alarm can reach you.

## Permissions

The function role allows:

- `ec2:DescribeInstances`, which has no resource-level scoping.
- `ec2:StopInstances` on this host only.
- `ssm:SendCommand` on this host with the `AWS-RunShellScript` document only.
- `ssm:GetCommandInvocation`.
- `ssm:GetParameter` and `ssm:PutParameter` on its own state parameter.
- Writing to its own log group.

It cannot start instances, run commands on other hosts, or read backups. The host role is unchanged.

The operator role needs permissions it does not have for the host-only deployment: Lambda, EventBridge rules, CloudWatch Logs and alarms, SNS, the state parameter, `iam:PassRole` to `lambda.amazonaws.com`, and creating the `<resource_prefix>-idle-stop` role. If the operator role caps the roles it creates with a host permissions boundary, that boundary does not allow EC2 stop or SSM commands. An administrator then provides a separate boundary for this role, and the operator passes it as `idle_stop.permissions_boundary_arn`. Have an administrator review and grant these before the plan.

## Cost

Within normal free-tier allowances for Lambda, EventBridge, CloudWatch alarms and SNS email, the function costs a few cents a month or less. About 8,900 invocations a month at 128 MB, each taking a second or two, is well inside the Lambda free tier. Recheck the rates when you deploy.

## Evidence limits

`task check` runs the Go unit tests and mocked plan assertions. The Go tests cover state transitions, drain evidence, blocking, launch-time resets, the stop retry, and the ping protocol against a local fake server. None of this calls AWS. Before relying on it, a live check must show that a real idle period drains with a verified backup and stops EC2, and that a blocked drain leaves the host running and raises the alarm.
