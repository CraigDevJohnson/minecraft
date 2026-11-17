# Cloud controls: stop, start, restore, allowlist and deploy from anywhere

Nothing about operating the family's worlds runs on a workstation ([ADR 0006](../adr/0006-cloud-controls.md)). The [launcher](launcher.md) function in AWS starts, stops and restores each world, switches its mods and keeps the allowlist; everything below only asks it, and the repository deploys itself from GitHub Actions. Each world has a host of its own ([ADR 0009](../adr/0009-one-host-per-world.md), [worlds](worlds.md)), so start, stop and restore name a world, and with none they act on the first world, `survival`. This runbook covers the three entry points, the Deploy workflow, and the one-time setup an administrator does once.

| Entry point | What it does | Who |
| --- | --- | --- |
| The `/minecraft` page in The Lobby | each world's status, Start and Stop; on Minecraft Admin, the mods switches, the allowlist, each world's archives with a restore, and each world's operators | Members with the `minecraft` grant; the switches, the allowlist, the archives and the operators on Minecraft Admin need `minecraft-admin` |
| The **Server control** workflow (GitHub Actions) | status, start, stop (forced if asked), restore an archive, list and switch mods, allowlist add or remove | Craig: the job runs only for runs his own account started, through the `server` environment |
| `task server:status`, `server:start`, `server:stop`, `server:restore`, `server:mods`, `server:mod-on`, `server:mod-off`, `server:allowlist` | The same requests from any terminal | Anyone with an AWS profile that may invoke the launcher (`workloads-admin` may) |

Every one of them sends one SigV4-signed request to the launcher's Function URL. The launcher does the work in AWS with the same host adapter actions the workstation runbook used. It reads who signed each request: a site's role may do everything but force a stop, which answers it `403` with `forbidden`. The Lobby's Minecraft Admin page, shown only to Members with `minecraft-admin`, lists and changes the allowlist, lists each world's archives and restores one, and makes players operators of a world ([ADR 0011](../adr/0011-minecraft-admin-keeps-players-and-archives.md)).

## From a cloud thread

Where these runbooks say a terminal, a cloud thread counts too: an agent session in a cloud container, not on Craig's computer. It holds no AWS credential until it signs in with `aws sso login --sso-session identity-center --use-device-code`. The thread posts the link and the code, which lasts 10 minutes, and Craig approves on his phone (aws-setup `aws-design.md` §5.2). One sign-in covers `workloads-admin` and `workloads-readonly` for that thread alone, so the same commands apply. Anything that can change AWS or Cloudflare still needs Craig's approval of that specific change first. The IAM writes in the [one-time setup](#one-time-setup) have not been tried from a cloud thread.

Approving a run in the `deploy` environment, changing a GitHub environment's settings and typing a secret's value stay Craig's, from a phone or any browser. The private files kept out of Git, such as the workstation's deployment inputs and anything under `operator-config/`, exist only on Craig's computer, so a step that reads one cannot run in a cloud thread. The four private input files of the two OpenTofu roots are the exception: they are in the state bucket, and a thread pulls them from there ([the private inputs](#the-private-inputs)). An interactive Session Manager shell does not work from a cloud thread ([crafty](crafty.md#open-a-shell-on-the-host)). The joins in a [proof run](worlds.md#the-proof-run-for-a-world) need a person with a real device.

### The private inputs

Each OpenTofu root reads two private files that Git ignores. So that planning a root does not need Craig's computer, the four are in the state bucket, under `inputs/` beside the state, and a checkout's copies go in `operator-config/inputs/`:

| Name | What it is |
| --- | --- |
| `deployment.tfvars` | The `infra` root's inputs as the Deploy workflow reads them: the same text as its `DEPLOYMENT_TFVARS` secret |
| `backend.hcl` | The `infra` root's backend settings: the same text as the `BACKEND_CONFIG` secret |
| `bootstrap.tfvars` | The inputs of `infra/state-bootstrap` ([state bootstrap](state-bootstrap.md)) |
| `bootstrap.backend.hcl` | That root's backend settings |

All four were uploaded on 2026-11-17 by a cloud thread that had copied them from Craig's computer. The same thread pulled them back and planned both roots from them: `infra/state-bootstrap` showed no changes, and `infra` showed only its three functions, whose code had changed on `main` since the last Deploy. `backend.hcl` is the workstation's backend file without its `assume_role` block, as step 5 of the [one-time setup](#one-time-setup) describes.

None of the four holds a credential. They do hold private names and one private email address, so the two commands print a file's name and its object version and never its contents, and a pulled file is readable by its owner only. A thread may read and edit a pulled file. It keeps what the file holds out of Git, pull requests, issues and logs, and shows a file's text only to Craig, in their conversation.

The bucket's policy denies every principal but `minecraft-operator`, the administrator SSO role included, so the commands run under a profile that assumes that role from `workloads-admin`. The bucket's name is private and is not in this repository. A thread finds it in a listing of the account's buckets with `workloads-readonly`, or in aws-setup's `aws-design.md` (§6.6). In a cloud thread, after the sign-in:

```sh
aws configure set profile.minecraft-operator.role_arn arn:aws:iam::793680745829:role/minecraft-operator
aws configure set profile.minecraft-operator.source_profile workloads-admin
aws configure set profile.minecraft-operator.region us-west-2
export AWS_PROFILE=minecraft-operator
export MINECRAFT_STATE_BUCKET=<the state bucket's name>
task inputs:pull                          # every one of the four the bucket holds, into operator-config/inputs/
FORCE=1 task inputs:pull                  # also replaces a local file that differs from the bucket's copy
task inputs:push                          # every one of the four that operator-config/inputs/ holds
task inputs:push NAME=deployment.tfvars   # one of them
```

They run [`scripts/private_inputs.sh`](../../scripts/private_inputs.sh). A pull is a read: it is part of a plan and needs no approval. It names and passes over each of the four the bucket does not hold, and fails when it finds none of them or when a call to AWS fails for any other reason. It leaves a local file that differs from the bucket's copy as it is, says so and ends with an error, unless `FORCE=1` is set. A push changes AWS, so it needs Craig's approval of that change first, like any other write. The role's [permissions](operator-role/permissions.json.example) let this profile read and write `inputs/`. Before it signs out, a thread deletes `operator-config/inputs/`, any plan it saved, and `infra/.terraform/`, where `tofu init` keeps the backend settings.

`FROM` uploads a file that sits elsewhere, or under another name, as one of the four:

```sh
task inputs:push NAME=deployment.tfvars FROM=/path/to/private/ci.deployment.tfvars
```

With `deployment.tfvars` and `backend.hcl` pulled, the `infra` root is planned the way the Deploy workflow plans it, under the same operator profile:

```sh
python3 scripts/package_idle_stop.py --output operator-config/inputs/packages/idle-stop.zip
python3 scripts/package_idle_stop.py --function launcher --output operator-config/inputs/packages/launcher.zip
DEPLOY_DIR="$PWD/operator-config/inputs" scripts/deploy_tofu.sh plan
```

The script prints the plan's resource headers and its digest and saves the plan in that folder. A function's package path is part of the plan, and here it lies under `operator-config/inputs/`, so a thread's plan can show each function as updated in place when nothing else differs, and its digest is not the Deploy workflow's. Only the Deploy workflow masks the inputs' values, so in a thread an OpenTofu error can quote one. `infra/state-bootstrap` is planned with the commands in [state bootstrap](state-bootstrap.md).

## The Server control workflow

Open the repository's **Actions** tab (or the GitHub mobile app), choose **Server control**, press **Run workflow**, pick an action and run it from `main`.

| Input | Values |
| --- | --- |
| `action` | `status`, `start`, `stop`, `restore`, `mods`, `mod-on`, `mod-off`, `allowlist-add`, `allowlist-remove` |
| `world` | `none`, `survival`, `creative` or `adventure`. With `none`, `status` lists every world and `start`, `stop` and `restore` act on the first world. `mod-on` and `mod-off` need a world. `mods` and the allowlist actions take none. |
| `nobody_playing` | For `stop`. Unticked, the stop is refused while anyone is online or while the game does not answer and the count is unknown. Ticked, the stop goes ahead (the launcher's `force`). |
| `mod` | For `mod-on` and `mod-off`: the mod's id as `mods` lists it, such as `worldedit`. The switch applies at that world's next start. |
| `archive` | For `restore`: the archive's name in S3, among that world's archives, such as `20261016T031500Z.tar.gz` ([crafty](crafty.md#recover-from-an-archive)). An S3 URI or key that ends in it works too. |
| `edition`, `player` | For the allowlist actions: `java` with the account name, or `bedrock` with the gamertag. One change covers every world, running or not. |

The run prints the launcher's answer in its summary. For `start` and `stop` it then polls that world's status every ten seconds and ends green when the world reads `ready` or `stopped`, red when it settles anywhere else (`failed`, `maintenance`, or a stop refused with `players_online`, `players_unknown` or `starting`). When the launcher changed nothing because someone else's start or stop is already under way, the run follows that one to the same end; when it changed nothing because the server is already there, the run is green at once; any other unchanged answer is red. A `restore` is followed the same way, through `starting` until the server reads `ready`, for up to an hour; it is red when the launcher refuses it (`not_stopped` with the server's state, unless the server is stopped, or failed with no game answering) or the restore fails. The launcher reads `ready` only once the archive is in place, so a game started by hand meanwhile keeps the run on `starting` and then ends it `failed`; in practice only a Member's Start landing within seconds of the restore's own failure, or of its request, can end it green on the old world ([rare overlaps](launcher.md#rare-overlaps)). A start of a world that has run before took about a minute on the family host; a world's first start on a new or replaced host sets the server up first and may end red on `failed` after the launcher's 15 minutes, and a second `start` carries on ([launcher](launcher.md#a-worlds-first-start)). A stop takes the drain's own time, and the run allows the launcher's 75-minute deadline. A second run for the same world waits for the first to finish rather than running beside it; runs for different worlds go side by side, because each world has its own host.

The run assumes `minecraft-server-control`, a role that can do nothing but invoke the launcher. Its trust policy accepts only a job of this repository (by repository and owner id) in the `server` environment, and the environment allows only `main`.

## The Task targets

From any checkout, with the launcher's URL and an AWS profile in the shell:

```sh
export LAUNCHER_URL=https://<id>.lambda-url.us-west-2.on.aws/   # tofu output launchers, or the MINECRAFT_LAUNCHER_URL repository variable
export AWS_PROFILE=workloads-admin                                # after aws sso login
task server:status                    # every world; WORLD=creative task server:status for one
WORLD=creative task server:start      # without WORLD, the first world
WORLD=creative task server:stop       # FORCE=1 stops over players or an unanswering game
WORLD=creative task server:restore ARCHIVE=20261016T031500Z.tar.gz
task server:mods                      # each world's mods and whether each is on
WORLD=creative MOD=worldedit task server:mod-off   # server:mod-on switches it back; applies at the next start
task server:allowlist OP=add EDITION=java PLAYER=Steve
task server:allowlist OP=remove EDITION=bedrock PLAYER="Sample Player"
```

They run [`scripts/launcher_call.sh`](../../scripts/launcher_call.sh), which the workflow runs too. The credentials reach curl through a private file, never its arguments. `server:status` prints the launcher's answer; `server:start`, `server:stop` and `server:restore` follow the server the way the workflow does and exit non-zero when it settles elsewhere; `server:mods`, `server:mod-on` and `server:mod-off` print the launcher's list of mods; `server:allowlist` prints the launcher's answer, `updated` or `unchanged` with the running worlds the change reached.

## Deploys

The **Deploy** workflow runs only when Craig starts it by hand, with a `mode` of `plan` (look only, the default) or `apply`. No push starts it, because every run holds the operator's write credentials, and its job runs only when Craig's own account started the run and any re-run of it (`github.actor_id` and `github.triggering_actor`), so another collaborator or an app with write access cannot start one; the Server control job carries the same check, because the launcher treats its role as the operator. The two roles trust a job by its environment, so no other workflow may name the `deploy` or `server` environment, which `tests/test_server_control_role.py` enforces in `task check`. A plan run builds both function packages with the pinned Go toolchain through `scripts/package_idle_stop.py`, assumes `minecraft-operator` through GitHub OIDC, writes the private inputs from the `deploy` environment's secrets to files it never prints and masks their quoted values in the log (`scripts/mask_private_inputs.py`, because OpenTofu's errors reach the log unfiltered), runs `tofu init` and `plan`, and posts the plan to the run summary with its digest, a short hash of the changes it would make (`scripts/plan_digest.py`). Nothing applies until Craig reads that plan and starts the workflow again with `mode` set to `apply` and `plan` set to that digest; that run plans again and applies its saved plan only when the digest matches, so it applies exactly the plan he read or nothing. If `main`, the private inputs or the state changed in between, it stops before applying and posts the new plan and its digest. The `deploy` environment admits only `main` and has Craig as its required reviewer (Prevent self-reviews off, since he is the only reviewer and starts every run), so each run waits under Review deployments until he approves it. Approving the apply run he started with the digest is his approval of the change (aws-setup `aws-design.md` G11). The job summary shows the plan's resource headers and the apply's progress lines and totals, nothing indented deeper and no `[id=…]` suffixes, because attribute values and physical ids carry the private inputs.

Every Deploy runs with all worlds stopped: an apply replaces the functions under any start or stop in flight, and a change to the world hosts' first-boot script (`scripts/world-bootstrap.sh.tftpl`, or the `scripts/install_aws_cli.sh` it carries) replaces every world host, whose world then comes back from its last archive at its next start ([ADR 0009](../adr/0009-one-host-per-world.md)). Stop each world first and read the plan for `aws_instance.world` lines before approving.

Pull request CI is unchanged: `task check` validates, formats and runs the mocked plan tests without AWS access. The Pins workflow also runs on a pull request that changes `runtime/crafty/server.json`, and downloads and checks every pinned file ([worlds](worlds.md#add-a-mod)). A merge to `main` deploys nothing: Craig starts a plan run, reads it, then starts and approves an apply run in the Actions tab. The checkout on a workstation can stay behind `main` indefinitely; it is no longer part of a deploy. The same holds for the host: the launcher package carries the Crafty adapter, and the launcher installs it on a world's host before its next command there ([launcher](launcher.md#the-host-adapter)), so an adapter change needs no step beyond the Deploy, and reaches each world host at its next start, stop, restore or allowlist change.

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

   This prints the new version with `"IsDefaultVersion": true`. (A managed policy keeps five versions; delete the oldest with `aws iam delete-policy-version` if a later change hits that limit.) The same command publishes each later version of the example, such as the one that adds `s3:ListBucket` on the backup bucket's `instances/` prefix for the archives list ([ADR 0011](../adr/0011-minecraft-admin-keeps-players-and-archives.md)).

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

4. **Create the two GitHub environments.** Both allow only `main`. `deploy` has Craig as its required reviewer; `server` has none, because its runs only ask the launcher and its job already runs only for runs Craig's own account started. GitHub offers required reviewers on GitHub Pro only in public repositories, which this one is:

   ```sh
   repo=CraigDevJohnson/minecraft
   gh api -X PUT "repos/$repo/environments/deploy" --input - <<'JSON'
   {"reviewers": [{"type": "User", "id": 42454849}], "prevent_self_review": false, "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   JSON
   gh api -X POST "repos/$repo/environments/deploy/deployment-branch-policies" -f name=main -f type=branch
   gh api -X PUT "repos/$repo/environments/server" --input - <<'JSON'
   {"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   JSON
   gh api -X POST "repos/$repo/environments/server/deployment-branch-policies" -f name=main -f type=branch
   ```

   Each `PUT` prints the environment as JSON (`"name": "deploy"`, then `"name": "server"`); each `POST` prints the branch policy with `"name": "main"`. The `deploy` one also lists a `required_reviewers` protection rule naming CraigDevJohnson. The same settings are under the repository's Settings, Environments, in the web UI; the `PUT` updates an existing environment in place.

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

   The two files behind the secrets are `deployment.tfvars` and `backend.hcl` of [the private inputs](#the-private-inputs). The state bucket's copy of each is the source, and each secret is a paste of it. The Deploy workflow reads only the secrets, so they stay what deploys. A later change to an input goes in this order:

   1. The file is edited in `operator-config/inputs/`, after a pull.
   2. The thread pushes it, with Craig's go-ahead: `task inputs:push NAME=deployment.tfvars`.
   3. Craig pastes the same text into the `DEPLOYMENT_TFVARS` secret (`BACKEND_CONFIG` for `backend.hcl`), under the repository's Settings, Environments, `deploy`, from any browser. A cloud thread cannot set an environment's secret, so it gives Craig the file's text in their conversation.
   4. A Deploy plan run follows. Its summary shows only the change the edit intends; anything else means the secret and the bucket's copy differ.

After these, merge the change, then start the Deploy workflow by hand (Actions, Deploy, Run workflow, `mode` left at `plan`) and approve it under Review deployments. Read its summary. That first plan, in October 2026, showed the launcher roles' new statements and environment, the two new Function URL permissions per site, the alarm's new metric, and the idle stop's new environment variable and statement; any change to the host, data disk, buckets or network was a stop condition, as in [infrastructure](infrastructure.md). The Deploy that brings the world hosts has its own list ([worlds](worlds.md#the-first-deploy)). Then start it again with `mode` set to `apply` and `plan` set to the digest at the end of that summary, and approve it: it plans again, checks the digest, applies, and its summary ends with the apply's resource lines. Then prove the controls from the Server control workflow: `status`, `start`, and `stop` with nobody playing. The stop archive lands in S3 beside the earlier ones.

Later changes to these roles are the same commands again: a new boundary version, `update-assume-role-policy`, or `put-role-policy` with the updated committed JSON.

## Hands-on work on the host

Nothing runs on a workstation, and nothing forwards a port to it. Restoring a world from an archive is the workflow's `restore` ([crafty](crafty.md#recover-from-an-archive)). A new or replaced world host sets Crafty up itself at its world's first start, so nothing is installed by hand. The Crafty console and a hands-on emergency use a Session Manager shell opened from the AWS console in a browser ([crafty](crafty.md#open-a-shell-on-the-host)): the adapter's `console` action sends a console command through Crafty's loopback API. The host commands and `scripts/minecraft_host.py`, which knew only the family host, were removed with it.

## Evidence limits

`task check` covers the launcher's worlds, stop, restore, mods and allowlist with unit tests against fake AWS and identity services, the adapter's first-start set-up, restore, console and key setup against a fake Crafty, the idle stop's stand-asides, the policy documents, the workflow guards and the mocked plan's new statements. None of it calls AWS or GitHub. Before relying on it, a live check must show one Deploy run approved and applied from GitHub, and a start and a stop from the Server control workflow reaching `ready` and `stopped`, with the stop's archive in S3 and the hostname parked, and a `restore` of the newest archive reaching `ready` with the same world. That was shown on the family host; for each world host it is the [proof run](worlds.md#the-proof-run-for-a-world).
