# State bootstrap and recovery

`infra/state-bootstrap` owns only the state bucket and its protection settings. `infra` owns the replaceable host, data disk, backup bucket, network, and budget. The two roots use separate state object keys. State storage has no dependency on compute. All commands below are **reviewable operator procedures for issue #7**, requiring Craig's concrete account/bootstrap/plan/apply approval. They are not part of `check`, and have not been executed against AWS.

## Private configuration

Use an existing authorized operator role and temporary AWS credentials. All providers restrict the target account and assume the supplied role. Backend authentication must independently assume the same role; provider authentication does not configure the backend. Never place credentials in backend files or tfvars.

If no suitable operator role exists, create one before the bootstrap plan, with Craig's approval for that IAM change. The [trust](operator-role/trust.json.example), [permission](operator-role/permissions.json.example), and [host boundary](operator-role/host-boundary.json.example) examples describe the role. Only the administrator SSO role can assume it. Before the operator role is used, the administrator creates the host boundary managed policy, named outside the resource prefix; it allows only the actions the host and recovery roles' inline policies use today (SSM agent registration and channels, and backup object access), limited to prefixed buckets. Both roots require its ARN as `host_permissions_boundary_arn` and attach it to the role they create. The operator can create a prefixed role, change its policies, or set its boundary only when that boundary is the host boundary; it can never remove a role's boundary or change or delete the boundary policy, so any prefixed role it passes to EC2 stays within the boundary. Otherwise the role is limited to EC2 in the deployment region, prefixed instance profiles and S3 buckets, the prefixed budget, public AMI parameters, and read-only CloudTrail event lookup to prove a volume's attachment history. SSM commands and sessions require a target instance tagged `Project=minecraft` and the reviewed shell documents, and sessions can be resumed or ended only by the user who started them. Name the role outside the resource prefix so it cannot rewrite its own policy. Region-wide `ec2:*` is the remaining exposure: the operator can still stop, modify, or snapshot other EC2 workloads in the same account and region, and could run code under their instance roles, for example by tagging one into SSM scope or replacing its user data. A dedicated account or tag-scoped EC2 permissions are the remaining hardening. The role limits the blast radius of operator tooling and mistakes; it is not a boundary against the administrator who assumes it. The first deployment's operator and host roles predate the boundary: create the boundary, then update the operator policy, then apply a reviewed plan that only adds the existing host role's boundary. Validate the documents with IAM Access Analyzer `validate-policy`, then create the role with a one-hour maximum session (role chaining from SSO cannot exceed one hour). Tooling that starts from an SSO profile can use a private AWS config profile with `source_profile` set to that SSO profile and `role_arn` set to the operator role.

Copy `infra/state-bootstrap/deployment.tfvars.example` and `infra/backend.hcl.example` into a private directory outside Git. Supply the real account, operator role, environment, region, unique state bucket, and three distinct keys. The keys hold bootstrap, primary infrastructure, and temporary recovery-rehearsal state. Use the default OpenTofu workspace; environment isolation comes from explicit private backend coordinates. The state bucket must differ from the backup bucket. Keep inputs, initialized working copies, plan text/binaries, outputs, and state private, with directory mode 700 and file mode 600.

State encryption uses S3-managed AES256 keys (SSE-S3); there is no customer-managed KMS key or associated key-lifecycle dependency. Record this choice in the private inventory. Versioning has no age expiration. Public access is blocked, ACLs are disabled, TLS is required, and a bucket policy denies every principal except the operator role. Current and historical state objects cannot be deleted through the ordinary policy. State locking uses OpenTofu's native S3 lock objects, one beside each state key.

`operator_backend_policy` is a reviewable, sensitive policy output for the existing operator role: list this bucket, read/write exactly the three state objects, and read/write/delete their `.tflock` objects. It is not attached automatically. #7 must review the role's effective permissions, including the separate permissions needed to provision resources and to assume the role. Host credentials have no state access. Recovery-specific `GetObjectVersion` and `ListBucketVersions` permissions require a separate operator grant when needed.

## Create, protect, and migrate

Run repository checks first. In #7, verify the selected identity and role, region, unique names, and the concrete saved creation plan before apply. Use a private bootstrap working copy, leaving the repository configuration unchanged. In this example, `private_dir` is an operator-selected absolute directory on encrypted storage and `repo_dir` is the checked-out repository root:

```sh
umask 077
repo_dir="$PWD"
private_dir="/absolute/private/minecraft/example"
mkdir -p "$private_dir"
cp -R infra/state-bootstrap "$private_dir/bootstrap"
# Fill $private_dir/bootstrap.tfvars from the safe example before planning.
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" init -input=false -lockfile=readonly
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" plan \
  -var-file="$private_dir/bootstrap.tfvars" -out="$private_dir/state-create.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" show -no-color \
  "$private_dir/state-create.tfplan" > "$private_dir/state-create.txt"
```

After Craig approves that exact plan, the authorized operator can apply it:

```sh
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" apply "$private_dir/state-create.tfplan"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" output -json backend_configs \
  > "$private_dir/backend-configs.json"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" output -raw operator_backend_policy \
  > "$private_dir/operator-backend-policy.json"
```

Before migration, create a protected recovery copy of the local `bootstrap/terraform.tfstate`. Use an independently retained encryption identity, for example the operator's reviewed `age` installation and recovery recipient:

```sh
age -r "$recovery_recipient" -o "$private_dir/bootstrap-recovery.tfstate.age" \
  "$private_dir/bootstrap/terraform.tfstate"
```

Store the encrypted copy in a protected recovery location independent of this workstation and state bucket, with restricted access and the decryption identity held separately. Verify that a designated operator can decrypt it into a separate protected scratch directory and compare its bytes to the source before relying on it. Keep original plaintext state and migration backups confined to encrypted private storage. Do not print state, copy it into an issue, or upload it as a CI artifact.

Prepare private `bootstrap.backend.hcl` and `infrastructure.backend.hcl` from the backend example and the corresponding `backend_configs` values. They must use the same reviewed bucket/role/account/region, distinct keys, `encrypt = true`, and `use_lockfile = true`. After verifying the intended operator role's backend permissions:

```sh
cp "$repo_dir/infra/state-bootstrap/remote-backend.tf.example" "$private_dir/bootstrap/backend.tf"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" init -migrate-state \
  -backend-config="$private_dir/bootstrap.backend.hcl"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" state pull \
  > "$private_dir/bootstrap-remote.tfstate"
"$repo_dir/.tools/bin/tofu" -chdir="$private_dir/bootstrap" plan \
  -var-file="$private_dir/bootstrap.tfvars" -out="$private_dir/bootstrap-verify.tfplan"
```

Inspect the remote state lineage, serial, and all resource addresses/IDs against the protected pre-migration copy, and require a no-change verification plan. OpenTofu 1.12.6 assigned a new lineage when migrating the local bootstrap state into the empty S3 key during the first deployment. The serial and every resource address/ID matched, and the verification plan reported no changes. A changed lineage alone therefore does not indicate a wrong state; mismatched resources or a non-empty verification plan do. Confirm encryption, versioning, public blocking, bucket policy, and lock creation/release through authenticated AWS inspection. Test contention using overlapping read-only plans from two isolated initialized operator copies pointing to the same backend key. Start the first plan, then run the second with `-lock-timeout=0s` while the first holds its lock; retain the second plan's lock-acquisition failure and verify a later plan succeeds after the first finishes. If both plans finish without contention, the overlap was insufficient: repeat the check, including against the host configuration after deployment if necessary. Do not report contention as proven until it is observed. Do not apply merely to test locking. Never use `-lock=false` or force-unlock an active operation.

Retain and refresh the independent encrypted recovery copy after verified state changes. Creation may partially succeed before a later resource fails; preserve that local state, inspect the created resources, and re-plan. Do not blindly create another bucket or discard partial state.

## Recovery

Quiesce operators and confirm there is no active writer/lock. Record the actual bucket/key, last good object version, state lineage/serial, and incident before changing anything. A forgotten active lock is not permission to overwrite state.

In a new private directory, restore and decrypt the independent recovery copy or retrieve the selected known-good S3 object version using explicitly authorized recovery permissions. If the state bucket is available, preserve the current state first, compare the candidate state with actual resources, and review a refresh-only plan. Reconnect the isolated bootstrap copy to the same backend only after verifying its settings. Prefer a reviewed state push with normal lineage/serial safeguards; do not use `-force` to bypass an unresolved mismatch. Recovery makes a new current version while retaining the old history.

If state has been lost but resources remain, use local state in the isolated bootstrap copy and import the existing resources at their declared addresses. The bucket name is the import ID for `aws_s3_bucket.state`, `aws_s3_bucket_versioning.state`, `aws_s3_bucket_server_side_encryption_configuration.state`, `aws_s3_bucket_public_access_block.state`, `aws_s3_bucket_ownership_controls.state`, and `aws_s3_bucket_policy.state`. Review each import and a no-change plan before repeating the protected-copy/migration procedure. If the bucket itself is lost, restore its protected configuration and states from the independent recovery copies under a separately reviewed creation/recovery plan.

`prevent_destroy` protects the state bucket and each protection resource in normal plans; the bucket refuses forced emptying. These are trusted-operator guardrails, not protection against someone deliberately removing the configuration or policy. Deleting state, disabling protection, changing ownership, or emptying/removing either retained bucket requires the separate destructive workflow in [infrastructure operations](infrastructure.md). Offline checks do not prove deployed access, locking, migration, or recovery.

References: [OpenTofu S3 backend and locking](https://opentofu.org/docs/language/settings/backends/s3/), [AWS role restrictions using PrincipalArn](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_notprincipal.html).
