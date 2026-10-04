# On-demand host sessions

`retained-host` remains the default. The opt-in `on-demand-host` mode implements #23's host/root removal while preserving the encrypted, protected data disk. It requires one designated operator workstation, the pinned repository tools, an initialized default-workspace S3 backend, AWS CLI v2, Session Manager plugin, reviewed operator permissions and a Cloudflare token for DNS. It is incompatible with `idle_stop`.

## Prepare the migration and recurring policy

Keep the current runtime/recovery points available. Complete and record #8's independent restore/failure/update proof before migrating the live family world. Review the source and exact runtime release; keep private files outside Git with mode `0600`, and the policy/journal directories with mode `0700`. Private operator configuration still explicitly names `/etc/minecraft/deployment.json` and `/etc/minecraft/rcon.secret`.

Build the deterministic public runtime archive from the reviewed checkout:

```sh
python3 scripts/minecraft_bootstrap_release.py package --output operator-config/family/runtime.tar.gz
```

This prints only the SHA-256 and immutable S3 key. Independently review the fixed file inventory and publish the archive with conditional `IfNoneMatch="*"`; never replace an existing key. The archive contains reviewed scripts, installers and public runtime definitions/content, including the operator example. It has no private operator configuration or secrets. The host role gains only `s3:GetObject` for `bootstrap/releases/<digest>/*`, subject to its existing permissions boundary. Review effective boundary and operator upload permissions before using it.

Prepare private Terraform inputs with `lifecycle_mode="on-demand-host"`, `bootstrap_sha256=<archive digest>`, `session_present=true`, and `idle_stop=null`. Existing host/attachment addresses move to `aws_instance.host[0]` and `aws_volume_attachment.data[0]` without replacing resources solely for the address change. Existing default-mode bootstrap bytes remain identical. Enabling the opt-in bootstrap itself changes user data and can replace the host; inspect the actual migration plan. The retained data address/protection never changes in phase one. Review and approve that exact migration before apply; use the controller start to install the reviewed runtime after any replacement and verify original volume/UUID/world/player checkpoints.

Prepare an owner-only JSON controller policy with exactly these fields:

```json
{
  "schema_version": 4,
  "workstation": "device-sha256:the-reviewed-64-lowercase-hex-digest",
  "aws_profile": "reviewed-local-profile",
  "aws_source_identity": {
    "account_id": "reviewed-source-account-id",
    "principal_arn": "reviewed-stable-source-arn",
    "principal_id": "reviewed-stable-source-principal-id"
  },
  "aws_cli_path": "/absolute/reviewed/aws/installation/bin/aws",
  "aws_cli_sha256": "sha256-of-the-reviewed-versioned-launcher",
  "aws_cli_file_identity": {"device": 0, "inode": 0},
  "session_plugin_path": "/absolute/reviewed/project/.tools/session-manager-plugin/version/bin/session-manager-plugin",
  "session_plugin_sha256": "sha256-of-the-reviewed-canonical-plugin",
  "session_plugin_file_identity": {"device": 0, "inode": 0},
  "infra_root": "/absolute/reviewed/checkout/infra",
  "variables_file": "/absolute/private/deployment.tfvars",
  "variables_sha256": "sha256-of-the-exact-private-tfvars-bytes",
  "source_sha256": "computed-authored-source-digest",
  "backend_sha256": "computed-initialized-backend-security-digest",
  "tofu_sha256": "sha256-of-reviewed-project-local-tofu",
  "tofu_file_identity": {"device": 0, "inode": 0},
  "private_inputs_sha256": "sha256-of-operator-bytes-nul-secret-bytes",
  "backend": {
    "bucket": "private-state-bucket",
    "key": "infrastructure/terraform.tfstate",
    "region": "us-west-2"
  },
  "bootstrap_sha256": "the-reviewed-runtime-archive-sha256",
  "operator_file": "/absolute/private/operator.json",
  "secret_file": "/absolute/private/rcon.secret",
  "journal_dir": "/absolute/private/on-demand-journal"
}
```

Generate the stable workstation value on the designated operator computer from the reviewed checkout:

```sh
python3 -c 'import sys; sys.path.insert(0, "scripts"); from minecraft_on_demand import workstation_identity; print(workstation_identity())'
```

This prints only `device-sha256:<digest>`. It hashes the Mac platform UUID read through the fixed system `ioreg` command, or a root-owned Linux `/etc/machine-id`; raw hardware identifiers never enter policy or output. Hostname/network changes do not change the device value. Missing, malformed, untrusted or unsupported platform identity refuses operation without falling back to a hostname. Existing policies containing an exact hostname retain their earlier comparison. Switching identity forms requires reviewing the updated policy and source digest; preserve any incomplete operation journal for repair rather than replacing its binding.

Compute `source_sha256` with `minecraft_on_demand.source_digest(Path(checkout_root))`; it covers the Terraform source/lock and authored bootstrap/controller files. Compute the variables digest with SHA-256 over the exact private file. Use `minecraft_on_demand.backend_digest(Path(infra_root), policy)` for the initialized backend digest and `private_inputs_digest(policy)` for the exact operator/RCON bytes. Record SHA-256 plus the actual `st_dev`/`st_ino` of the reviewed regular `.tools/bin/tofu`; the example zeroes are placeholders to replace with those values. The controller rejects symlinked tool paths and rechecks identity/hash before each invocation. Resolve the installed AWS launcher and Session Manager plugin to their canonical regular paths, record the same SHA-256/device/inode fields, and review that concrete installation. The AWS pin covers its versioned launcher under the trusted workstation platform runtime, with inherited Python import overrides removed; it does not claim recursive library attestation. Source role assumption, refreshed evidence and workstation S3 calls use that rechecked exact path. The tunnel constrains plugin lookup to the pinned canonical plugin directory and rechecks both tools after credential refresh. Each OpenTofu invocation uses a fresh private empty CLI configuration and isolated XDG directories, excluding user provider overrides while preserving the reviewed AWS profile home. The version-4 policy fixes the designated workstation identity, explicit AWS profile and stable source identity, canonical infra directory, backend security settings, executable identity, exact private input bytes and runtime release. Source-profile AWS and OpenTofu commands strip ambient direct, web-identity and credential-file overrides, endpoint overrides and CA/proxy transport overrides, and ignore configured endpoint overrides; custom backend endpoints, credential paths and insecure validation/TLS settings are rejected. Private input or executable replacement between completed operations needs a newly reviewed policy. Private operator JSON/encoding and RCON bundle format are preflighted before any operation journal or resource creation. Automatic unreviewed `.auto.tfvars`, overrides and a non-default workspace refuse operation. The initialized backend must match bucket/key/region with encryption, native locking, the same allowed account and the recorded operator role. The configured profile supplies credentials only through an in-memory snapshot from the pinned AWS CLI `configure export-credentials --format process` command. `GetCallerIdentity` verifies that exact snapshot before role assumption or any OpenTofu backend/provider invocation. Record `aws_source_identity` from `minecraft_host.source_principal(caller_response)`: account ID plus ARN/UserId, with only the final assumed-role session name and UserId session suffix removed. IAM user/root identities are retained exactly; assumed-role name and unique principal ID remain pinned, so normal session refresh works while a different trusted principal or recreated identity is refused. Credential values never enter policy, journal, argv or output. OpenTofu receives those admitted direct credentials and private empty AWS config/credential files, with profile variables removed; initialized backend `profile` must be unset. The authored provider has no explicit profile, and any explicit profile cannot re-resolve the workstation files. A profile change after snapshot admission cannot change the credentials used for that call. The same snapshot assumes the reviewed operator role temporarily. Operational AWS calls and the SSM tunnel use in-memory short-lived role credentials; no machine-wide profile is created. Review this concrete policy, initial migration and rollback together, including recurring authority to create/delete only the host/attachment and root-on-termination. Record the SHA-256 of the exact policy bytes. A changed policy/input/source/release needs a new review; an incomplete operation cannot silently adopt it.

Before adopting an existing host, prepare the reviewed pinned verifier and private files through the secure receiver while all games are stopped, and verify the resulting certificate; do not synthesize a certificate from unreviewed installed bytes. This is part of the exact migration preparation, not an automatic private-file update on an already-running host.

The operator needs existing infrastructure administration plus scoped `ssm:StartSession` access to the intended host and `AWS-StartPortForwardingSession`, and permission to terminate its own sessions. The host role never gets infrastructure ownership, operator credentials, or secret storage access. Install the Session Manager plugin locally through the reviewed official workflow; the controller does not change global workstation tooling.

## Run and resume

After that initial approved migration and recurring policy review, save the direct private `tofu output -json deployment` object. The output has `schema_version=2`, explicit mode/presence and a null host ID while off. Pass the approved policy digest to every command:

```sh
python3 scripts/minecraft_host.py --deployment /absolute/private/deployment.json \
  --lifecycle /absolute/private/lifecycle.json --policy-sha256 "$approved_policy_sha256" \
  start --instance survival

python3 scripts/minecraft_host.py --deployment /absolute/private/deployment.json \
  --lifecycle /absolute/private/lifecycle.json --policy-sha256 "$approved_policy_sha256" stop
```

Use the existing one-command Cloudflare token injection from [host commands](host-commands.md). `status` and `dns` accept the same policy options. Both `status` and `dns` compare the full handoff with authoritative OpenTofu output and actual session inventory before using it. Off-state `status` reports an absent host and retained disk, with application unavailable and backup unverified. `dns` parks the existing A record when absent.

Start plans and applies only missing session resources, verifies the handoff against actual infrastructure state, waits for SSM/cloud-init, retries the existing storage-gated bootstrap if attachment visibility delayed it, fetches and verifies the pinned public release, stages private configuration over the short-lived loopback receiver and installs the reviewed runtime. Only then does the normal instance start run. A retry after interrupted installation repeats identical setup. A retry after runtime installation resumes normal idempotent instance start without reinstalling. The pinned verifier checks complete installed public scripts, launchers, definitions/content and service, and verifies its own bootstrap-loader bytes before accepting reuse or drain. An already-present host with an installed runtime uses the normal game start path only after the pinned verifier proves the actual operator/RCON bytes and their root-owned mode-0600 `/etc/minecraft/private-inputs.sha256` certificate match the reviewed private-input digest. The receiver creates that certificate with the files; a missing certificate or changed installed/local inputs refuses reuse and requires explicit reviewed reconfiguration or recreation. A newly reviewed policy alone does not reconfigure an existing host. A migration-created host without a runtime automatically receives the same reviewed setup before game start. Failed transfer, installation or readiness never reports a ready game.

Stop first verifies the same installed private inputs before draining all configured instances and requires stopped application plus completed required backup evidence. It gracefully stops EC2 and confirms stopped state before Terraform can detach the filesystem, parks DNS promptly when EC2 releases the address, verifies the exact saved Terraform plan, applies session removal, independently checks the host is terminated, its recorded root volume no longer exists, and the original encrypted data volume is available with no attachments. Only then does it publish the absent-host handoff and park DNS. Failed drain performs no plan/apply. Failure to prove deletion/detachment retains the private journal for repair. A DNS failure reports failure while preserving confirmed lifecycle results; repair it with `dns`.

Before a resumed removal, a still-running source host must freshly complete its guarded drain and gracefully stop; a stopped host requires the same persisted stop request and independent stopped-state observation. The only accepted refresh drift is that host’s explicit stop-related state/public-address change; protected drift still refuses. After a persisted removal apply intent, a retry can reconcile only exact source host/attachment deletion drift with stopped evidence; terminated or forgotten instances still require independent root deletion and retained-data detachment proof. A recorded partial create must independently show the exact retained volume encrypted, available and unattached before planning a missing attachment; uncertain prior attachment outcomes are preserved for explicit repair. An interrupted create with no recorded host identity refuses automatic recreation, because empty eventually-consistent EC2 results cannot prove the earlier request failed. Preserve the journal and independently inventory/import any owned resources before preparing an explicit repair. A recorded partial host is checked against cloud ownership and its recorded root before planning the missing attachment. The workstation lock covers the entire operation. Run the same command with the same files to resume; changing any reviewed policy field, action/instance/profile/backend/release/inputs, changed protected handoff or resources or mismatched backend lineage/serial refuses. Completed operations are retained as private per-operation journal files. Saved plans and journals are private and must survive host/root deletion. A backend lock alone does not authorize another workstation to operate concurrently. Do not remove the journal/lock or recreate a clean directory to bypass an incomplete operation.

No force-detach, force-format, automatic data deletion or root recovery shortcut exists. Keep unrelated resources and the original disk intact during repair. If the opt-in mode fails before the game starts, repair/retry its operation. For rollback after a successful stopped phase, prepare and review a plan that restores `retained-host`, creates the host/attachment against the same disk/UUID and preserves the protected resources; reinstall the compatible reviewed runtime and verify checkpoints before play. If content/world formats changed, use the independent verified recovery workflow rather than old binaries alone.

## Live acceptance

The controller records `start_to_ready_seconds` through its application/DNS-API milestone and `controller_ready_within_target` against 180 seconds. `startup_target_met` stays null within that bound until independent live endpoint verification; it is false when the controller itself misses the target or DNS fails. `host_recreated` distinguishes a full cold start from an already-present host start. The timestamp starts at command entry before policy validation, role assumption or initial state access, with a monotonic lower bound. Backward/future/discontinuous clocks and resumed cross-process timing produce an unknown timing/target result rather than zero seconds or a passing target. Timer evidence covers the saved operation, including interruptions. Confirm endpoint availability through actual network/client checks and record the elapsed time against the target. A slower cold start fails that target and calls for a measured optimization proposal; do not count an already-running start or Paper-only boot time as cold-start proof.

Demonstrate a completed stop with no session host/root volume, preserved source disk/UUID/checkpoints, then a start with no manual host setup and intact world/player state. Exercise failed drain/restore/readiness, interruption and DNS repair on the reviewed live boundary. Offline tests prove controller/refusal behavior and mocked resource boundaries, without AWS mutation, real SSM transfer, host/root deletion, family-client readiness or timing. Keep #23 open until that live evidence meets its criteria. The separate S3-only phase requires #24's independent exact-version receipt and ownership/protection migration; this mode deliberately keeps the data disk.

Provider refresh may populate the host role’s computed inline-policy list from its separately managed policies and the host’s computed EBS block from the recorded data volume and attachment. The controller accepts these metadata changes only after verifying the complete actual policy set and exact disk/device/encryption/size/I/O/KMS/effective-tag and delete-on-termination values. Cached policy bodies may predate an owned policy update; refreshed bodies must match the separately managed policies. Plan, post-apply and resume checks preserve every other resource attribute and ownership identity. Unknown policies, volumes or changed configuration still refuse operation.

The final before/after-plan race check compares the complete state, preserving lineage, serial, resource attributes, outputs, check statuses and failure details. It canonicalizes only check-result list order by unique `config_addr` and nested object order by unique `object_addr`; malformed or duplicate identities refuse apply. Missing, null and empty fields remain distinct. Receipt and recovery-selection comparisons stay exact.

## Recorded live status (2026-10-04)

#23's `on-demand-host` mode is activated. A supported stop with both worlds at zero players completed both backups, removed the host and root volume, and left the original encrypted 5 GiB data disk available without attachments. The subsequent genuinely absent-host cold start used normal unattended setup and preserved the disk/UUID, all 17 Survival and 1 Creative world/player checkpoint files, access controls and Floodgate key. Private-input certificate verification and pinned runtime attestation passed. Both worlds subsequently answered independent direct Java and Bedrock protocol probes.

Survival reached controller readiness in 157.656 seconds; Creative took a further 69.776 seconds, with both ready 227.438 seconds after command entry. This clean measurement misses the provisional 180-second target. DNS remains parked pending existing 1Password CLI authentication; DNS repair and the owner's timing preference remain pending, so #23 stays open.

#24 preparation has an exact saved Stage A plan with three policy changes and a prepared same-ID data ownership migration. Neither is applied, `s3-only` is not activated, and no phase-two data deletion has occurred. Its separate protection and live recovery criteria remain required.
