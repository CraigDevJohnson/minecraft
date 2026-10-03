# Unattended read-only Minecraft health

Status: reviewed and approved for publication and exact IAM activation. Deployed OIDC and GitHub-to-AWS validation remain separate evidence; passing local tests do not establish them.

## First scope

The proposed [workflow](../../.github/workflows/minecraft-health.yml) supports only manual dispatch from `main`. After activation, an agent can dispatch one check through the existing authorized GitHub connection without interactive AWS SSO. It has no recurring schedule, push trigger or PR trigger. The job has a five-minute timeout, uses the existing GitHub-hosted Ubuntu runner pattern, and requests 900-second AWS credentials through the existing GitHub OIDC provider. It provisions no worker or infrastructure. Private-repository runs consume GitHub Actions minutes; charges depend on the account's existing plan and usage.

Standard GitHub-hosted runners are free for public repositories. For charged standard Linux x64 minutes, GitHub's current rate is $0.006 per billed minute, with each job rounded up to a whole minute. One charged minute is $0.006; five charged minutes are approximately $0.03 before included allowances. This is a conditional illustration, not a confirmed cost, measured runtime or guaranteed billing cap. Craig's plan and remaining allowance were not inspected. Publishing/merging also runs the repository's existing CI under its existing billing settings. See [official pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing) and [billing rules](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

[The reporter](../../scripts/minecraft_health.py) verifies the AWS account, locates the family host by the existing `Project`, `Environment` and `Name` tags, reads EC2 status, and checks DNS plus public Java/Bedrock discovery. It follows host replacements without changing a deployment handoff. No matching host or a stopped host is reported as idle, with game readiness unverified. Multiple matching hosts, an AWS/authentication error, wrong account, failed EC2 checks, stale DNS or failed game discovery fails the run. A successful discovery probe does not establish an authenticated family join, inventory persistence, backup recovery, TPS or terrain performance.

The JSON and job summary exclude credentials, raw AWS errors, host IDs/IPs, player names/UUIDs, MOTD, RCON data and private configuration. They contain fixed status fields and bounded numeric protocol/player-count observations. No logs, SSM commands/sessions, Secrets Manager, S3 backups, Terraform state, IAM administration, EC2 start/stop or paid compute provisioning are granted. The audit found no `/aws/lambda/minecraft-family-*` log group; collecting runtime logs would require a separate reviewed design and exact permission scope.

## Public output and metadata masking

The reporter publishes exactly `state`, `status`, `identity_verified`, `ec2_checks_passed`, `dns_matches_host`, `java_status_ok`, `bedrock_discovery_ok`, and `game_ready`; a failed run adds one authored `failure` code. Successful public probes can add `java`/`bedrock` objects containing bounded numeric `protocol`, `online`, `limit` and an optional numeric-dot `version` string. The only emitted EC2 value is the allowlisted host-state label. Account comparison and EC2 checks are emitted as booleans, never raw identity or status payloads. No raw DescribeInstances/DescribeInstanceStatus response, IP, instance ID, user data, arbitrary tag, unrelated resource, player identity, secret or private host inventory is published.

The JSON is printed to the job log and job summary. It remains a runner-temporary file; this workflow uploads no artifact. Fixed target account/region/health-role ARN, public hostname, family tag filters and repository IDs appear in the proposed source/policies. The private local audit/inventory documents outside this Git worktree are not part of the PR patch.

The pinned official AWS action normally logs `AssumedRoleId`, even with account masking enabled. Before that action, require repository secret `MINECRAFT_HEALTH_ROLE_ID` containing **only the new role's noncredential RoleId** from administrator creation/readback, validate its format, and register masks for that ID, the exact run session name and account ID. This preserves the conventional action without a custom authentication implementation or added IAM permission. Missing/malformed metadata fails before requesting AWS credentials. Secrets/tokens are masked by the official action and credential step outputs are disabled.

The metadata secret must match the current role before activation. Shape validation cannot detect a stale ID; any approved deletion/recreation of this role requires secret replacement and administrator readback before dispatch. The RoleId is a masking value, not a key or credential and grants no AWS access.

## Exact IAM proposal

Create **one** administrator-owned role, `minecraft-github-health-readonly`, in account `793680745829`, with [trust.json](health-role/trust.json) and inline policy `minecraft-health` from [permissions.json](health-role/permissions.json). Keep the new role's maximum session duration at 3600 seconds, AWS's minimum role setting; this workflow requests only 900 seconds. No existing SSO session duration changes are proposed. Do not attach managed `ReadOnlyAccess` or the existing host/operator boundary or administrator policies.

The trust allows only `sts:AssumeRoleWithWebIdentity` from the already existing provider `arn:aws:iam::793680745829:oidc-provider/token.actions.githubusercontent.com`. Both conditions use exact `StringEquals`:

- `aud = sts.amazonaws.com`
- `sub = repo:CraigDevJohnson@42454849/minecraft@1359070973:ref:refs/heads/main`

The repository's OIDC settings reported `use_default=true`, `use_immutable_subject=true`, and this exact immutable prefix on 2026-10-03. These settings plus the documented default branch context establish the configured subject shape; an issued token and STS exchange have not been observed. Do not log, save or paste a token to validate it. Successful STS assumption after activation is the end-to-end test.

Main's active ruleset requires a PR, resolved review threads, linear history, and `Secret scan` / `Workflow security` checks. It currently requires zero approving reviews. The new workflow has no PR trigger, uses the triggering main revision, and rejects a manual dispatch on another ref. Its checkout and AWS action are pinned to immutable commits. No environment is configured or proposed. Recheck protections and OIDC settings immediately before applying; stop and revise the proposal if they differ.

**Consequence to approve:** main-ref jobs in this exact repository that can request an OIDC token can assume this role; the default AWS-supported `aud`/`sub` trust does not bind permission to one workflow filename. This workflow requests 15 minutes, but another qualifying main job could request up to the role's one-hour maximum. Protected-main contributors must keep AWS-enabled jobs away from untrusted PR code. Changing subject customization, repository/owner identity, or adding an environment changes the subject and must fail closed until a separately reviewed trust update.

The identity policy grants exactly `ec2:DescribeInstances` and `ec2:DescribeInstanceStatus`, restricted to `aws:RequestedRegion=us-west-2`. These APIs do not support instance resource scoping, so `Resource="*"` is required. **The principal can read those EC2 metadata APIs for other instances in the same account/region**, even though this reporter filters the family host. Tag filters control the reporter's selection; they are not an IAM boundary. `sts:GetCallerIdentity` needs no Allow grant.

The existing provider, portfolio OIDC roles, `minecraft-operator`, host roles/boundaries and local SSO profiles remain unchanged. The new role is outside the `minecraft-family-` host prefix and is not owned by the live host root. Repository documentation already uses administrator-owned JSON proposals for bootstrap roles; this follows that pattern without running or altering the active OpenTofu state. No AWS account infrastructure repository was inspected because the available Minecraft references and IAM metadata did not establish one.

## Approval bundle and activation order

These concrete actions require their corresponding approval; publication and exact-role activation can be approved first, with merge and dispatch approved separately:

1. Publish this isolated branch as a draft PR to `CraigDevJohnson/minecraft` and obtain the repository's required checks/review.
2. Using the existing administrator SSO path, create only `minecraft-github-health-readonly` with the exact trust above and attach only the `minecraft-health` inline policy. Reuse the existing provider without modification. Populate GitHub repository secret `MINECRAFT_HEALTH_ROLE_ID` with only that new role's RoleId and verify its matching administrator readback. No new login/credential grant or SSO/security-setting update is included.
3. Merge this exact reviewed implementation into protected main to make manual dispatch available, dispatch the health workflow on main once, and verify the completed run's identity/EC2/public-probe outcome. Read back the role's trust/inline/attached policies, confirm that no broader policies were attached, and retain the run URL plus sanitized observation.

Before creation, validate the documents with IAM Access Analyzer and simulate the two allowed reads and representative forbidden operations. Recheck current provider/role inventory and protection settings. If the proposed role already exists or is managed by another source, stop instead of overwriting it. Merge makes the dispatch endpoint available but does not itself launch a health run or AWS request; the existing Validate and Security push workflows do run on merge. Verify the new role before the first health dispatch. A failed OIDC exchange or public probe is failure evidence, not a reason to broaden permissions or relogin silently.

## Agent procedure after activation

Within separately authorized check work, dispatch exactly this workflow with `main`, then read its run conclusion and sanitized summary:

```sh
gh workflow run minecraft-health.yml --repo CraigDevJohnson/minecraft --ref main
gh run list --repo CraigDevJohnson/minecraft --workflow minecraft-health.yml --event workflow_dispatch --limit 5 --json databaseId,headSha,status,conclusion,url,createdAt
gh run view <exact-run-id> --repo CraigDevJohnson/minecraft --json databaseId,headSha,status,conclusion,url,jobs
```

Match the new run by workflow, main revision and dispatch time instead of attributing another operator's run to this request. A successful dispatch only queues work; require the completed health job's outcome before claiming AWS or game readiness. No AWS credential from the Mac is passed to GitHub. Existing GitHub authentication is still required to dispatch/read the private repository.

For rollback, disable this workflow's dispatch and remove only the new role's `minecraft-health` inline policy, new role and its `MINECRAFT_HEALTH_ROLE_ID` metadata secret. Disabling the workflow stops its future requests but does not by itself revoke an already-issued session or prevent another qualifying main workflow from assuming the role. This workflow's sessions last up to 15 minutes; the role allows other qualifying jobs to request up to one hour. Removing permissions cuts off future EC2 reads from those sessions. Never delete or modify the shared provider or existing roles. Rollback mutations require their corresponding authorization.

The local draft itself does not need a new AWS login. Keep human SSO for administrator and server operations. Start, backup, stop, log export, Roles Anywhere and persistent/local unattended credentials are separate future decisions.

## References

- [AWS IAM GitHub OIDC trust](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html)
- [GitHub OIDC in AWS, including immutable subjects](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [EC2 service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonec2.html)
- [STS GetCallerIdentity requires no permission](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)
- [STS web identity session duration](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html)
- [Pinned official credentials action v6.3.0](https://github.com/aws-actions/configure-aws-credentials/tree/e1253824e5c10ff9df46874f81ed3ec929e19cfd)
