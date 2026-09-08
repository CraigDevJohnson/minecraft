# Host and session commands

Run `scripts/minecraft_host.py` on the operator computer with Python 3.11+ and AWS CLI v2 installed. AWS CLI was available during implementation; the repository does not install it. Use the existing operator AWS profile/session. The game host's IAM role receives no EC2 start/stop permissions.

The private deployment handoff is the direct JSON object produced by `tofu output -json deployment`, described in [infrastructure operations](infrastructure.md). The command consumes `host_id`, `region`, and the `instances` mapping. Keep this file private and outside tracked files; it contains deployment identifiers. For example, after an authorized deployment:

```sh
umask 077
mkdir -p operator-config/family
.tools/bin/tofu -chdir=infra output -json deployment > operator-config/family/deployment.json
```

Install and configure the instance runtime on the host before using these commands. SSM invokes `/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json` as the fixed host-side interface. That private configuration supplies explicit EULA acceptance, retained-volume identity, and the required backup hook. Host startup does not supply or infer those values.

```sh
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json start --instance survival
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json status
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json stop
```

`start --instance survival` starts EC2 only when stopped, waits for EC2 running and SSM online, then starts that configured instance and requires actual application readiness. The instance ID is required and must match the deployment. Repeating start on a running, ready instance is safe. An already-pending EC2 startup is observed to completion. Other transitional states fail with a retry instruction.

`stop` is a separate host operation. It asks the host to drain every configured instance, including already-stopped instances with pending or failed backup work. The host holds lifecycle exclusion during shutdown and cold backup, and writes a drain barrier tied to the current Linux boot. Another session start cannot cancel that barrier. EC2 stop is requested only after the response covers exactly the configured instances, confirms the barrier, and reports every application stopped with backup complete. `not-required` is accepted only through the host runtime's pristine, never-started-instance rule.

The drain barrier survives failed backups and failed EC2 calls. Repair the failure and repeat host stop. After a successful EC2 stop followed by a new boot, the runtime recognizes the old boot identity and permits a new session. Do not remove the barrier to work around an incomplete stop. Instance-only stop leaves EC2 running and does not initiate a host drain.

`status` reports host, application, and backup separately. The `host` field is the last observed EC2 state. Application statuses are `running`, `stopped`, or `unknown`; backup statuses from the runtime are `pending`, `complete`, `not-required`, or `failed`. Stopped/unreachable hosts cannot supply fresh runtime evidence: output uses `application: unavailable` and `backup: unverified` for a host that is not running, or returns a structured failure when a running host cannot be reached through SSM. A repeated host stop on an already-stopped EC2 instance returns success for that existing host state, with backup `unverified`; it does not assert that a backup was checked.

Failed operations exit nonzero. If EC2 stop fails after a successful drain, output retains the confirmed application/backup outcomes. Failures before runtime evidence is available report application `unknown` and backup `unverified`. JSON output includes only configured instance names, controlled status values, and bounded error descriptions. Raw AWS stderr, SSM output, deployment identifiers, credentials, RCON secrets, and player identities are not printed. Diagnose detailed errors in private AWS/SSM records; do not paste those records into public logs.

## Timeouts and retries

- Each AWS CLI process is bounded to 30 seconds, or the remaining phase deadline when shorter.
- EC2 transitions and SSM online readiness each have a 300-second deadline.
- SSM delivery has a 60-second timeout and the remote command has a 3,600-second execution timeout. Local invocation polling is bounded to 3,660 seconds and handles initial `InvocationDoesNotExist` propagation.
- Missing, failed, incomplete, or timed-out drain evidence forbids EC2 stop. No timeout path force-stops EC2.
- An AWS transport timeout can leave the requested action in progress. Local timeout does not cancel an already-submitted SSM operation. Inspect status and retry the same lifecycle command; host-side locking, backup state, and the boot-bound drain barrier govern the retry. A host already stopping returns a retry instruction until its transition finishes.

The initial single `survival` instance fits this remote execution envelope with the documented per-instance shutdown and backup bounds. Before deploying additional instances, reassess the total sequential drain duration against the one-hour SSM limit; a larger fleet must not rely on a timeout to finish its backups.

The operator needs existing access to `ec2:DescribeInstances`, `ec2:StartInstances`, `ec2:StopInstances`, `ssm:DescribeInstanceInformation`, `ssm:SendCommand` for `AWS-RunShellScript` on the intended host, and `ssm:GetCommandInvocation`. These commands do not create or modify IAM policies. Actual AWS authorization and live start/stop validation remain the deployment work in issue #7.

## Validation boundary

`tests/test_minecraft_host.py` exercises the public command interface against a controlled AWS CLI boundary. It covers duplicate operations, all-instance drain evidence, stopped instances with pending backups and retry, EC2/SSM failures, startup/readiness timeouts, remote invocation propagation, malformed responses, ID validation, and private-output filtering. Process-level fixtures verify the real CLI adapter's timeout and diagnostic handling. These tests neither contact AWS nor establish live backup recovery.

AWS interface references: [SendCommand delivery timeout and parameters](https://docs.aws.amazon.com/cli/latest/reference/ssm/send-command.html), [GetCommandInvocation eventual consistency](https://docs.aws.amazon.com/cli/latest/reference/ssm/get-command-invocation.html), and [Run Shell Script execution timeout](https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-command-ssm-plugin-reference.html#aws-runShellScript).
