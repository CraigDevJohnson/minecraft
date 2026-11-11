# Host and retained infrastructure

Issue #4 implements the offline contract in the [approved Agent Brief](https://github.com/CraigDevJohnson/minecraft/issues/4#issuecomment-5579966412). It supplies no game runtime, live resources, or demonstrated recovery. Since 2026-11-10 the root declares one host per world ([ADR 0009](../adr/0009-one-host-per-world.md), [worlds](worlds.md)): `infra/worlds.tf` makes a world host for each world id in `runtime/crafty/server.json`. The family host of ADR 0004, with its retained data disk, its disk set-up and its zero-fill, is removed from the configuration and retired by the Deploy in [worlds](worlds.md#the-family-hosts-retirement); its archives stay in the backup bucket. World and instance IDs follow `^[a-z][a-z0-9-]{0,31}$`.

## Ownership and inventory

| Owner | Resources | Replacement and cost behavior |
| --- | --- | --- |
| `infra`: world hosts | One `m7g.large` per world (`aws_instance.world["<world>"]`), each with an encrypted 20 GiB gp3 root disk and no data disk; tagged `Name = <resource_prefix>-<world>`, `World = <world>`, `Purpose = replaceable-host`. They share one AMI, the public subnet, the security group and the instance profile | Compute charges while a world runs. The world lives on the root disk, which persists while stopped and is deleted on termination; the archive in S3 from the world's last stop is the recovery point, and a replaced host restores it at the world's next start. A new host powers itself off after its first boot. |
| `infra`: retained backups | Private AES256 encrypted, versioned S3 bucket, ownership/public/TLS controls and lifecycle | Separate from host and state; `prevent_destroy`, no forced emptying; storage and requests continue while compute stops. |
| `infra`: network | VPC, public subnet, Internet gateway, route table/association, security group/rules; each host takes an auto-assigned public IPv4 address | The address is assigned at launch and on every start, billed only while the host runs, and released on stop. It changes on every start; the launcher points the world's existing A record, `<world>.<public_hostname>`, at it and parks the record while the host is stopped. No Elastic IP. Java TCP 25565, RakNet UDP 19132, built-in signaling TCP 19132 and WebRTC UDP 19133 for game ingress; no SSH or RCON ingress. No NAT gateway or paid VPC endpoints. |
| `infra`: budget | Monthly account cost budget, actual-cost email notice | Default $25; required private recipient from Craig. Account-wide, including unrelated account costs; a notification, never a spending cap or automatic stop. |
| `infra/state-bootstrap` | Separate state bucket with encryption, versioning, ownership/public/TLS/operator-only access | Independent configuration/state and locked backend; no age-based state expiration. No host access or customer-managed KMS key. See [state bootstrap](state-bootstrap.md). |

AWS-managed temporary instance-role credentials permit only basic Session Manager channels and current backup objects under each world's `instances/<world>/` prefix, with bucket listing limited to those prefixes. Every world host has the one role, so a world host can reach another world's archives ([ADR 0009](../adr/0009-one-host-per-world.md)). The retired family host's `instances/family/` is no longer granted. No state, IAM, EC2, bucket administration, or historical-version deletion permissions are granted to the host. Current backup object deletion is reserved for #6's verified-success pruning, which keeps the latest 14 successful recovery points. S3 lifecycle expires superseded/deleted versions after 14 days and incomplete multipart uploads after one day; it never expires current completed recovery points by age.

SSM uses outbound HTTPS and requires a functioning, preinstalled agent. The host uses IMDSv2 with hop limit 1, keeping container access to instance credentials restricted. #6 must run its authorized S3 operations on the host or explicitly review any change to that boundary. Internet ingress still requires application allowlisting/authentication in #5. RCON must bind locally in that runtime. Host egress permits TCP 80/443 setup traffic and UDP destination ports 1–65535 for native ICE checks to client-selected candidates, as [ADR 0008](../adr/0008-the-adapter-applies-the-pinned-server-content.md) and the [Crafty host runbook](crafty.md#how-players-join) explain; no SSH keys or passwords are provisioned.

## Private deployment inputs and output

Since October 2026 the [Deploy workflow](cloud-controls.md#deploys) plans and applies this root from GitHub Actions, as the operator role, with the private inputs below held as environment secrets; the workstation commands in this section remain for the first state bootstrap and for an operator working without GitHub.

Copy `infra/deployment.tfvars.example` outside Git for #7. Review account/operator role, the administrator-created host permissions boundary ARN (`host_permissions_boundary_arn`; see [state bootstrap](state-bootstrap.md)), environment and stable resource prefix, region/AZ, exact AMI and publisher, backup name, the public hostname and its Cloudflare zone ID (`public_hostname`, `cloudflare_zone_id`; see [launcher](launcher.md)), and budget inputs. A private copy from before the family host's retirement may still set `data_filesystem_uuid`, `data_volume_size_gib`, `instance_ids` and `launcher.server_name`: OpenTofu warns about the three undeclared variables and ignores them and the extra `launcher` attribute, so remove them when the copy is next edited. The Cloudflare API token is never an OpenTofu input. The required AMI is Ubuntu 24.04 LTS ARM64, HVM/EBS, with cloud-init, Python 3, util-linux, e2fsprogs, and SSM Agent preinstalled. Validate the image's provenance and actual packages in #7. No moving AMI lookup or default live identifier is committed. No input names a world: the worlds come from `runtime/crafty/server.json`, each world's hostname is `<world>.<public_hostname>`, and the same AMI serves every host. Each host's bootstrap installs Ubuntu's `docker.io` and `docker-compose-v2`; the server content is pinned in `runtime/crafty/server.json` ([Crafty host](crafty.md#what-is-pinned)).

An AZ change replaces the subnet and every world host; stop every world first, so each world's archive is in S3. Keep both the sensitive main `deployment` output and bootstrap outputs in the private inventory; the main configuration cannot infer its backend's bucket settings.

After state creation/migration is approved and verified, use an isolated private copy of `infra` **and its sibling `scripts` directory** so template references resolve. Initialize with the private infrastructure backend configuration. Example #7 plan commands, not run by checks:

```sh
mkdir -p "$private_dir/infrastructure"
cp -R "$repo_dir/infra" "$repo_dir/scripts" "$private_dir/infrastructure/"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" init \
  -input=false -lockfile=readonly -backend-config="$private_dir/infrastructure.backend.hcl"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" plan \
  -var-file="$private_dir/infrastructure.tfvars" -out="$private_dir/infrastructure.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" show -no-color \
  "$private_dir/infrastructure.tfplan" > "$private_dir/infrastructure-plan.txt"
```

Craig reviews the saved account-specific plan before authorizing its apply. Run that separately authorized apply so the private record holds its UTC window and caller:

```sh
caller_arn="$(aws sts get-caller-identity --query Arn --output text)" || exit 1
printf 'apply-start %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$caller_arn" \
  >>"$private_dir/infrastructure-apply-window.txt" || exit 1
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/infrastructure/infra" apply \
  "$private_dir/infrastructure.tfplan" || exit 1
printf 'apply-end %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >>"$private_dir/infrastructure-apply-window.txt" || exit 1
```

Then redirect `tofu output -json deployment` to a mode-600 private file. Do not publish outputs or plans as PR/CI artifacts. A new world host runs its first-boot script and powers itself off, and its hostname follows it at its world's next start through the launcher.

## Normal replacement and explicit destructive workflow

A world host is replaced without ceremony, because nothing on it is the only copy once its world has been stopped: stop the world, which stores its archive, then apply. The new host sets itself up and the world's next start restores the newest archive ([Crafty host](crafty.md#how-a-world-host-sets-itself-up)). Three things decide when a world host is replaced:

- **Any edit to `scripts/world-bootstrap.sh.tftpl` or `scripts/install_aws_cli.sh` replaces every world host**, because both are rendered into `user_data` with `user_data_replace_on_change = true`. The first-boot script takes only the world's id, the backup bucket and the region from OpenTofu, so a change to `runtime/crafty/server.json` replaces nothing.
- **A stopped world host must not plan as a replacement.** EC2 releases the auto-assigned address on stop, and the provider then reads `associate_public_ip_address` as `false`. `ignore_changes` on that attribute keeps a plan taken while the host is stopped at no changes. A replacement still takes `true` from the configuration.
- **A world added to or removed from `server.json`** adds or destroys that world's host, parameters and idle-stop target ([worlds](worlds.md#add-a-world)).

Every Deploy runs with all worlds stopped, so a plan that replaces a world host never ends a game and never loses play. A world host that was running when it was replaced loses whatever was played since its last archive.

`prevent_destroy` and nonempty-bucket refusal block ordinary destroy/replacement plans for retained storage. They are not an IAM denial against a trusted operator deliberately removing code or using AWS directly. There is no repository `destroy` task. For any intentional deletion, reformatting, AZ migration, protection removal, state overwrite, or permanent version deletion: stop all writers, inventory exact resources and dependencies, verify an independent recoverable copy, prepare a separate change that names each protection being removed, produce a concrete destructive plan and rollback/recovery steps, and obtain Craig's explicit approval for those exact resources/actions before execution. Preserve state long enough to finish and verify the teardown; never remove it as part of routine compute replacement.

## Cost review

The design's Oregon planning inputs remain estimates to revalidate in #7: `m7g.large` $0.0816/running hour, gp3 $0.08/GB-month, public IPv4 $0.005/hour, and an illustrative 10 GB average backup footprint. For a 730-hour month, each world host's 20 GiB root disk is about $1.60 whether or not the world is played, $4.80 for three worlds, and each running world adds its own compute and public IPv4 by the hour. The public IPv4 address bills only while a host runs: with compute, that adds $3.46–$8.66 for 40–100 running hours of one host. With the disks above, a month of 40–100 world-hours in all is approximately $8.26–$13.46, **before S3 storage/versions/requests, traffic, taxes, monitoring, and temporary recovery resources**. An Elastic IP would instead bill about $3.65 every month, running or not. The hostnames' Cloudflare DNS is on the free plan. Retained resources continue billing when EC2 is stopped. Long-lived current recovery points and noncurrent versions count toward S3 usage; actual storage can exceed the example.

Reprice the exact region, instance/AMI, storage, public IPv4, AWS Budgets notification configuration, account allowances, and recovery test resources at deployment using [EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS pricing](https://aws.amazon.com/ebs/pricing/), [VPC pricing](https://aws.amazon.com/vpc/pricing/), and [S3 pricing](https://aws.amazon.com/s3/pricing/). Rates here are review inputs, not a bill forecast. The budget recipient remains a required private input; budget notifications can be delayed and do not cap spending.

## Evidence limits

`check` validates formatting, shell lint/syntax, Python syntax and behavioral tests, both OpenTofu roots, and mocked plan assertions without a remote backend or AWS credentials. The first-boot script tests replace only the operating-system boundary. Source-policy checks cover lifecycle safeguards that mocked plans do not expose. No check applies infrastructure or starts Minecraft.

#7 must prove effective IAM, approved AMI prerequisites, backend locking/migration, actual plan/apply, attachment/device discovery, SSM, network reachability, AWS capacity and the budget recipient. #8 proved persistence, host replacement and isolated recovery on 2026-10-03 on the earlier Compose stack ([#8](https://github.com/CraigDevJohnson/minecraft/issues/8)); the family host's proof was its [acceptance](crafty.md#acceptance), and each world host's is its [proof run](worlds.md#the-proof-run-for-a-world), not yet run. The first real plan with the world hosts, the account's on-demand vCPU quota for three more `m7g.large` hosts, and a world host's first boot are unproven until the [first Deploy](worlds.md#the-first-deploy). Craig resolved #9's console and family testing requirements on 2026-10-03; see the [current acceptance record](family-device-validation.md). That decision is separate from these technical proofs; green offline tests or PR CI do not establish live infrastructure or recovery outcomes.
