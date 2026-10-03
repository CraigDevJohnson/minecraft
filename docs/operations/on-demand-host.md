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
  "schema_version": 2,
  "workstation": "the-output-of-socket.gethostname",
  "aws_profile": "reviewed-local-profile",
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

Compute `source_sha256` with `minecraft_on_demand.source_digest(Path(checkout_root))`; it covers the Terraform source/lock and authored bootstrap/controller files. Compute the variables digest with SHA-256 over the exact private file. Use `minecraft_on_demand.backend_digest(Path(infra_root), policy)` for the initialized backend digest and `private_inputs_digest(policy)` for the exact operator/RCON bytes. Record SHA-256 plus the actual `st_dev`/`st_ino` of the reviewed regular `.tools/bin/tofu`; the example zeroes are placeholders to replace with those values. The controller rejects symlinked tool paths and rechecks its identity/hash before each invocation. The version-2 policy fixes the designated hostname, explicit AWS profile, canonical infra directory, backend security settings, executable identity, exact private input bytes and runtime release. Source-profile AWS and OpenTofu commands strip ambient credential-file and endpoint overrides, and ignore configured endpoint overrides; custom backend endpoints, credential paths and insecure validation/TLS settings are rejected. Private input or executable replacement between completed operations needs a newly reviewed policy. Automatic unreviewed `.auto.tfvars`, overrides and a non-default workspace refuse operation. The initialized backend must match bucket/key/region with encryption, native locking, the same allowed account and the recorded operator role. The configured profile supplies credentials only to assume that reviewed operator role temporarily. Operational AWS calls and the SSM tunnel use in-memory short-lived role credentials; no machine-wide profile is created. Review this concrete policy, initial migration and rollback together, including recurring authority to create/delete only the host/attachment and root-on-termination. Record the SHA-256 of the exact policy bytes. A changed policy/input/source/release needs a new review; an incomplete operation cannot silently adopt it.

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

Start plans and applies only missing session resources, verifies the handoff against actual infrastructure state, waits for SSM/cloud-init, retries the existing storage-gated bootstrap if attachment visibility delayed it, fetches and verifies the pinned public release, stages private configuration over the short-lived loopback receiver and installs the reviewed runtime. Only then does the normal instance start run. A retry after interrupted installation repeats identical setup. A retry after runtime installation resumes normal idempotent instance start without reinstalling. An already-present host with an installed runtime uses the existing normal game start path. A migration-created host without a runtime automatically receives the same reviewed setup before game start. Failed transfer, installation or readiness never reports a ready game.

Stop first drains all configured instances and requires stopped application plus completed required backup evidence. It gracefully stops EC2 and confirms stopped state before Terraform can detach the filesystem, parks DNS promptly when EC2 releases the address, verifies the exact saved Terraform plan, applies session removal, independently checks the host is terminated, its recorded root volume no longer exists, and the original encrypted data volume is available with no attachments. Only then does it publish the absent-host handoff and park DNS. Failed drain performs no plan/apply. Failure to prove deletion/detachment retains the private journal for repair. A DNS failure reports failure while preserving confirmed lifecycle results; repair it with `dns`.

Before a resumed removal, a still-running source host must freshly complete its guarded drain and gracefully stop; a stopped host requires the same persisted stop request and independent stopped-state observation. The only accepted refresh drift is that host’s explicit stop-related state/public-address change; protected drift still refuses. A recorded partial create must independently show the exact retained volume encrypted, available and unattached before planning a missing attachment; uncertain prior attachment outcomes are preserved for explicit repair. An interrupted create with no recorded host identity refuses automatic recreation, because empty eventually-consistent EC2 results cannot prove the earlier request failed. Preserve the journal and independently inventory/import any owned resources before preparing an explicit repair. A recorded partial host is checked against cloud ownership and its recorded root before planning the missing attachment. The workstation lock covers the entire operation. Run the same command with the same files to resume; changing any reviewed policy field, action/instance/profile/backend/release/inputs, changed protected handoff or resources or mismatched backend lineage/serial refuses. Completed operations are retained as private per-operation journal files. Saved plans and journals are private and must survive host/root deletion. A backend lock alone does not authorize another workstation to operate concurrently. Do not remove the journal/lock or recreate a clean directory to bypass an incomplete operation.

No force-detach, force-format, automatic data deletion or root recovery shortcut exists. Keep unrelated resources and the original disk intact during repair. If the opt-in mode fails before the game starts, repair/retry its operation. For rollback after a successful stopped phase, prepare and review a plan that restores `retained-host`, creates the host/attachment against the same disk/UUID and preserves the protected resources; reinstall the compatible reviewed runtime and verify checkpoints before play. If content/world formats changed, use the independent verified recovery workflow rather than old binaries alone.

## Live acceptance

The controller records `start_to_ready_seconds` through its application/DNS-API milestone and `controller_ready_within_target` against 180 seconds. `startup_target_met` stays null within that bound until independent live endpoint verification; it is false when the controller itself misses the target or DNS fails. `host_recreated` distinguishes a full cold start from an already-present host start. The timestamp starts at command entry before policy validation, role assumption or initial state access, with a monotonic lower bound. Backward/future/discontinuous clocks and resumed cross-process timing produce an unknown timing/target result rather than zero seconds or a passing target. Timer evidence covers the saved operation, including interruptions. Confirm endpoint availability through actual network/client checks and record the elapsed time against the target. A slower cold start fails that target and calls for a measured optimization proposal; do not count an already-running start or Paper-only boot time as cold-start proof.

Demonstrate a completed stop with no session host/root volume, preserved source disk/UUID/checkpoints, then a start with no manual host setup and intact world/player state. Exercise failed drain/restore/readiness, interruption and DNS repair on the reviewed live boundary. Offline tests prove controller/refusal behavior and mocked resource boundaries, without AWS mutation, real SSM transfer, host/root deletion, family-client readiness or timing. Keep #23 open until that live evidence meets its criteria. The separate S3-only phase requires #24's independent exact-version receipt and ownership/protection migration; this mode deliberately keeps the data disk.
