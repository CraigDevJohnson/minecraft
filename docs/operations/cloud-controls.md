# Cloud controls: stop, start, allowlist and deploy from anywhere

Nothing about operating the family server runs on a workstation ([ADR 0006](../adr/0006-cloud-controls.md)). The [launcher](launcher.md) function in AWS starts, stops and allowlists the server; everything below only asks it, and the repository deploys itself from GitHub Actions. This runbook covers the three entry points, the Deploy workflow, and the one-time setup an administrator does once.

| Entry point | What it does | Who |
| --- | --- | --- |
| The `/minecraft` page in The Lobby | status, Start server, Stop server | Members with the `minecraft` grant |
| The **Server control** workflow (GitHub Actions) | status, start, stop (forced if asked), allowlist add or remove | Whoever may run workflows in this repository, through the `server` environment |
| `task server:status`, `server:start`, `server:stop`, `server:allowlist` | The same requests from any terminal | Anyone with an AWS profile that may invoke the launcher (`workloads-admin` may) |

Every one of them sends one SigV4-signed request to the launcher's Function URL. The launcher does the work in AWS with the same host adapter actions the workstation runbook used. It reads who signed each request: a site's role may only read the status, start and stop without force, and the allowlist and a forced stop answer it `403` with `forbidden`.

## The Server control workflow

Open the repository's **Actions** tab (or the GitHub mobile app), choose **Server control**, press **Run workflow**, pick an action and run it from `main`.

| Input | Values |
| --- | --- |
| `action` | `status`, `start`, `stop`, `allowlist-add`, `allowlist-remove` |
| `nobody_playing` | For `stop`. Unticked, the stop is refused while anyone is online or while the game does not answer and the count is unknown. Ticked, the stop goes ahead (the launcher's `force`). |
| `edition`, `player` | For the allowlist actions: `java` with the account name, or `bedrock` with the gamertag |

The run prints the launcher's answer in its summary. For `start` and `stop` it then polls the launcher every ten seconds and ends green when the server reads `ready` or `stopped`, red when it settles anywhere else (`failed`, `maintenance`, or a stop refused with `players_online`, `players_unknown` or `starting`). When the launcher changed nothing because someone else's start or stop is already under way, the run follows that one to the same end; when it changed nothing because the server is already there, the run is green at once; any other unchanged answer is red. A start takes about a minute; a stop takes the drain's own time, about ten minutes for the usual archive, and the run allows the launcher's 75-minute deadline. A second run waits for the first to finish rather than running beside it.

The run assumes `minecraft-server-control`, a role that can do nothing but invoke the launcher. Its trust policy accepts only a job of this repository (by repository and owner id) in the `server` environment, and the environment allows only `main`.

## The Task targets

From any checkout, with the launcher's URL and an AWS profile in the shell:

```sh
export LAUNCHER_URL=https://<id>.lambda-url.us-west-2.on.aws/   # tofu output launchers, or the MINECRAFT_LAUNCHER_URL repository variable
export AWS_PROFILE=workloads-admin                                # after aws sso login
task server:status
task server:start
task server:stop                      # FORCE=1 task server:stop stops over players or an unanswering game
task server:allowlist OP=add EDITION=java PLAYER=Steve
task server:allowlist OP=remove EDITION=bedrock PLAYER="Sample Player"
```

They run [`scripts/launcher_call.sh`](../../scripts/launcher_call.sh), which the workflow runs too. The credentials reach curl through a private file, never its arguments. `server:status` prints the launcher's answer; `server:start` and `server:stop` follow the server the way the workflow does and exit non-zero when it settles elsewhere; `server:allowlist` prints the adapter's result, `updated` or `unchanged`.

## Deploys

The **Deploy** workflow runs only when Craig starts it by hand, with a `mode` of `plan` (look only, the default) or `apply`. No push starts it, because every run holds the operator's write credentials, and its job runs only when Craig's own account started the run and any re-run of it (`github.actor_id` and `github.triggering_actor`), so another collaborator or an app with write access cannot start one; the Server control job carries the same check, because the launcher treats its role as the operator. The two roles trust a job by its environment, so no other workflow may name the `deploy` or `server` environment, which `tests/test_server_control_role.py` enforces in `task check`. A plan run builds both function packages with the pinned Go toolchain through `scripts/package_idle_stop.py`, assumes `minecraft-operator` through GitHub OIDC, writes the private inputs from the `deploy` environment's secrets to files it never prints and masks their quoted values in the log (`scripts/mask_private_inputs.py`, because OpenTofu's errors reach the log unfiltered), runs `tofu init` and `plan`, and posts the plan to the run summary with its digest, a short hash of the changes it would make (`scripts/plan_digest.py`). Nothing applies until Craig reads that plan and starts the workflow again with `mode` set to `apply` and `plan` set to that digest; that run plans again and applies its saved plan only when the digest matches, so it applies exactly the plan he read or nothing. If `main`, the private inputs or the state changed in between, it stops before applying and posts the new plan and its digest. Starting the apply run with the digest is his approval of the change (aws-setup `aws-design.md` G11). The `deploy` environment admits only `main` and carries no required reviewer, because GitHub offers that rule on a private repository only with GitHub Enterprise. The job summary shows the plan's resource headers and the apply's progress lines and totals, nothing indented deeper and no `[id=…]` suffixes, because attribute values and physical ids carry the private inputs.

Pull request CI is unchanged: `task check` validates, formats and runs the mocked plan tests without AWS access. A merge to `main` produces the plan; the apply is one Run workflow in the Actions tab after reading it. The checkout on a workstation can stay behind `main` indefinitely; it is no longer part of a deploy.

Two inputs make the root deployable there and change nothing on a workstation: `package_dir` names the directory holding `launcher.zip` and `idle-stop.zip` and overrides every `package_path`, and `operator_role_arn` may be left unset, which leaves the provider's `assume_role` out, because the runner already is the operator role.

## One-time setup

These are the IAM pieces aws-setup's `aws-design.md` keeps with an administrator (G7) and the GitHub settings only a repository owner can set. Each runs once, from any terminal with the `workloads-admin` profile after `aws sso login`, and the `gh` CLI signed in to GitHub. Check each output against what is shown before going on. Do them before merging the change that brings the workflows, because the first Deploy run needs steps 2, 4 and 5, and the first stop needs step 1.

1. **Publish the launcher boundary's new version.** It adds `ec2:StopInstances` on the project's hosts beside `StartInstances`:

   ```sh
   prefix=minecraft-family
   account=793680745829
   sed -e "s/123456789012/$account/g" -e "s/example-prefix/$prefix/g" \
     docs/operations/operator-role/launcher-boundary.json.example > /tmp/launcher-boundary.json
   aws iam create-policy-version --profile workloads-admin \
     --policy-arn "arn:aws:iam::$account:policy/minecraft-launcher-boundary" \
     --policy-document file:///tmp/launcher-boundary.json --set-as-default
   ```

   This prints the new version, `"VersionId": "v2"` with `"IsDefaultVersion": true`. (A managed policy keeps five versions; delete the oldest with `aws iam delete-policy-version` if a later change hits that limit.)

2. **Let the Deploy workflow assume the operator role.** Add the GitHub statement to the role's existing trust policy without touching the SSO statement:

   ```sh
   aws iam get-role --profile workloads-admin --role-name minecraft-operator \
     --query Role.AssumeRolePolicyDocument > /tmp/operator-trust-current.json
   python3 - <<'PY'
   import json
   current = json.load(open('/tmp/operator-trust-current.json'))
   example = json.load(open('docs/operations/operator-role/trust.json.example'))
   github = next(s for s in example['Statement'] if s['Sid'] == 'DeployWorkflowOnly')
   github['Principal']['Federated'] = github['Principal']['Federated'].replace('123456789012', '793680745829')
   current['Statement'] = [s for s in current['Statement'] if s.get('Sid') != 'DeployWorkflowOnly'] + [github]
   json.dump(current, open('/tmp/operator-trust.json', 'w'), indent=2)
   print(len(current['Statement']), 'statements')
   PY
   aws iam update-assume-role-policy --profile workloads-admin --role-name minecraft-operator \
     --policy-document file:///tmp/operator-trust.json
   ```

   The Python step prints `2 statements`; `update-assume-role-policy` prints nothing when it succeeds. The statement trusts only a job of this repository (repository id 1359070973, owner id 42454849) in the `deploy` environment, through the account's existing GitHub OIDC provider.

3. **Create the server control role** from the committed JSON, with a session long enough to follow a stop:

   ```sh
   aws iam create-role --profile workloads-admin --role-name minecraft-server-control \
     --assume-role-policy-document file://docs/operations/server-control-role/trust.json \
     --max-session-duration 7200 --tags Key=project,Value=minecraft \
     --description "GitHub Actions Server control workflow: may only invoke the Minecraft launcher"
   aws iam put-role-policy --profile workloads-admin --role-name minecraft-server-control \
     --policy-name invoke-launcher --policy-document file://docs/operations/server-control-role/permissions.json
   ```

   `create-role` prints the role with `"Arn": "arn:aws:iam::793680745829:role/minecraft-server-control"`; `put-role-policy` prints nothing.

4. **Create the two GitHub environments.** Both allow only `main`. Neither has a required reviewer: GitHub offers that rule on a private repository only with GitHub Enterprise, so the apply gate is the manual `apply` run instead:

   ```sh
   repo=CraigDevJohnson/minecraft
   gh api -X PUT "repos/$repo/environments/deploy" --input - <<'JSON'
   {"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   JSON
   gh api -X POST "repos/$repo/environments/deploy/deployment-branch-policies" -f name=main -f type=branch
   gh api -X PUT "repos/$repo/environments/server" --input - <<'JSON'
   {"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   JSON
   gh api -X POST "repos/$repo/environments/server/deployment-branch-policies" -f name=main -f type=branch
   ```

   Each `PUT` prints the environment as JSON (`"name": "deploy"`, then `"name": "server"`); each `POST` prints the branch policy with `"name": "main"`. The same settings are under the repository's Settings, Environments, in the web UI.

5. **Set the variables and the two secrets.** The variables are the role ARNs and the launcher's Function URL (either environment's function; both share one state and admit the control role). The secrets are the private inputs the workstation deploy used, with three edits to `deployment.tfvars`: remove `operator_role_arn`, remove each `package_path` line, and add `control_role_arn` to the `launcher` block. The backend file is `infrastructure.backend.hcl` without its `assume_role` block (and any `profile`), keeping `bucket`, `key`, `region`, `encrypt` and `use_lockfile`:

   ```sh
   gh variable set AWS_OPERATOR_ROLE_ARN --repo "$repo" --body arn:aws:iam::793680745829:role/minecraft-operator
   gh variable set AWS_SERVER_CONTROL_ROLE_ARN --repo "$repo" --body arn:aws:iam::793680745829:role/minecraft-server-control
   gh variable set MINECRAFT_LAUNCHER_URL --repo "$repo" --body https://doedzzndd54qlqchcrywrz24jm0uhahj.lambda-url.us-west-2.on.aws/
   gh secret set BACKEND_CONFIG --repo "$repo" --env deploy < /path/to/private/ci.backend.hcl
   gh secret set DEPLOYMENT_TFVARS --repo "$repo" --env deploy < /path/to/private/ci.deployment.tfvars
   ```

   Each `variable set` prints `✓ Set Actions variable NAME for CraigDevJohnson/minecraft`; each `secret set` prints `✓ Set Actions secret NAME for CraigDevJohnson/minecraft` (`gh` does not name the environment). The `launcher` block then reads, with your real values:

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

After these, merge the change, then start the Deploy workflow by hand (Actions, Deploy, Run workflow, `mode` left at `plan`). Read its summary. The first plan should show the launcher roles' new statements and environment, the two new Function URL permissions per site, the alarm's new metric, and the idle stop's new environment variable and statement; any change to the host, data disk, buckets or network is a stop condition, as in [infrastructure](infrastructure.md). Then start it again with `mode` set to `apply` and `plan` set to the digest at the end of that summary: it plans again, checks the digest, applies, and its summary ends with the apply's resource lines. Then prove the controls from the Server control workflow: `status`, `start`, and `stop` with nobody playing. The stop archive lands in S3 beside the earlier ones.

Later changes to these roles are the same commands again: a new boundary version, `update-assume-role-policy`, or `put-role-policy` with the updated committed JSON.

## What still runs on a workstation

Two rare procedures keep their runbooks and `scripts/minecraft_host.py`: restoring a world from a stop archive (`minecraft-instance restore`, [backup and recovery](backup-recovery.md)) and the Crafty console over an SSM port-forward ([crafty](crafty.md)). Neither is part of starting, stopping, allowlisting or deploying. The [host commands](host-commands.md) remain for a hands-on emergency.

## Evidence limits

`task check` covers the launcher's stop and allowlist with unit tests against fake AWS and identity services, the idle stop's stand-aside, the policy documents, the workflow guards and the mocked plan's new statements. None of it calls AWS or GitHub. Before relying on it, a live check must show one Deploy run approved and applied from GitHub, and a start and a stop from the Server control workflow reaching `ready` and `stopped`, with the stop's archive in S3 and the hostname parked.
