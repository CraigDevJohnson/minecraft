# Install the host runtime

Install a reviewed checkout on the Ubuntu 24.04 ARM64 host after the [host/storage prerequisites](infrastructure.md) are installed and the retained filesystem is verified and mounted. Before installation, supply `/etc/minecraft/deployment.json`, the direct JSON object from `tofu output -json deployment`, as a root-owned regular file with mode `0600`. Its volume/filesystem identifiers let the installer verify retained state even when the previous release has been moved aside. This step belongs to the concrete deployment in #7. It installs local files and reloads systemd; it does not create AWS resources, initialize storage, accept the Minecraft EULA, create instance data, download game artifacts, or start Minecraft.

The private directory `/etc/minecraft` must already be root-owned with exact mode `0700`. The host bootstrap establishes this for fresh installations. If an older host has different ownership or permissions, inspect its contents and origin, then explicitly prepare a trusted directory through the reviewed deployment procedure before copying private files. The installer rejects an untrusted existing directory and never silently repairs its ownership or permissions. Managed release, launcher, storage command, service files, and their installation directories must also be root-owned and unwritable by other accounts.

```sh
sudo bash scripts/install_runtime.sh
```

The installer requires root, Python 3.11+, Docker, systemd, and the installed `minecraft-storage` command. It holds the same bounded host lock as instance operations. Before writing installed files, its source-tree guard runs a fresh volume/filesystem verification, rejects nested mounts or redirected control/data paths, and inspects every configured or existing retained instance plus the Docker runtime inventory. Installation requires a cleanly stopped session with a completed backup and recorded recovery-point identity, or an entirely pristine instance without a journal, data, runtime files, or container. Pending/failed backups, partial starts/stops, untracked data, or active/ambiguous containers refuse installation. This guard remains effective when `/opt/minecraft` is absent and never changes journals or sets the host drain barrier.

Both the installer and current instance controller use `/run/minecraft/runtime.lock`. They first verify that `/run` is a real root-owned directory without group or other write access, then create or verify the root-owned mode-`0700` `/run/minecraft` directory. Existing untrusted directories are rejected without repair. This protected parent prevents another account from planting, replacing, or unlinking the lock pathname between validation and opening; the runtime additionally opens its lock with `O_NOFOLLOW` and validates the opened descriptor. The private directory is recreated after boot, and the shared lock remains held throughout installation and retained-state checks.

It copies the instance/content scripts to `/opt/minecraft/scripts`, the reviewed `survival` definition and exact content manifest to `/opt/minecraft/runtime`, and installs `/usr/local/sbin/minecraft-instance` plus `minecraft@.service`. It never enables the service. An identical rerun preserves installed files and all private configuration; a differing installed release, desired definition, launcher, or service is refused before replacements. The retained-state check also runs on identical reruns.

Prepare these root-owned private regular files with mode `0600` outside Git and transfer them through the reviewed deployment procedure. The deployment file is required before installation; the operator configuration and secret are required before starting Minecraft:

- `/etc/minecraft/deployment.json`: the direct JSON object from `tofu output -json deployment`, including retained-volume identity and the configured instance mapping.
- `/etc/minecraft/operator.json`: adapt `/opt/minecraft/runtime/operator.json.example`. Its supplied EULA acceptance defaults to `false`; Craig's acceptance recorded on #5 authorizes explicitly setting it to `true` for this server and its local runtime tests. There is no need to ask for acceptance again. Shared examples and routine CI keep acceptance disabled.
- `/etc/minecraft/rcon.secret`: a randomly generated private value of 16–128 letters, digits, underscores, or hyphens. Do not print it in public logs or command arguments.

The operator example defines only `survival`, paths to private deployment/secret files, and bounded readiness/shutdown/backup/warning timeouts. `backup_hook: null` deliberately supplies no completion evidence: an instance can start after its other prerequisites pass, but its normal stop/update and host stop cannot complete successfully until the required hook is implemented and configured in #6. Do not put a test-success hook in a real deployment. Actual family allowlist identifiers and Craig's operator identity are private launch inputs in #7.

The systemd start unit has `Requires` and `After` dependencies on storage and Docker. It invokes the guarded CLI on every activation; it has no cached `RemainAfterExit` state, automatic restart, boot enablement, or service-stop handler. The CLI verifies the actual retained mount immediately before starting. The start unit becomes inactive after the CLI reports readiness while Compose continues owning the container. Its systemd state is therefore not Minecraft status.

```sh
sudo systemctl start minecraft@survival.service
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json status survival
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json stop survival
```

Always stop through the instance CLI or the separate [host commands](host-commands.md); `systemctl stop minecraft@survival.service` cannot safely stop a Compose-owned game. Host stop drains all configured instances and their outstanding backup work. Do not reboot, stop Docker, or stop EC2 as a substitute for that operation.

## Replace a reviewed installed release

The installer intentionally has no overwrite or force option. Stage the candidate checkout separately and review its scripts, definition/content changes, service, and operator-config compatibility before changing the installed release. Complete the existing release's guarded instance stop and required backup first; confirm all instances are stopped with completed backups and no other lifecycle command is running. Retain the old exact runtime manifest and a recovery point before making any content/world-format change.

The lock location is part of the controller contract. Replace the installer and every instance controller together during this stopped maintenance window; do not run a controller using the older `/run/lock/minecraft-runtime.lock` alongside this release or assume that the two paths serialize each other.

During this explicit maintenance window, preserve `/opt/minecraft`, `/usr/local/sbin/minecraft-instance`, and `/etc/systemd/system/minecraft@.service` together in a root-owned release archive outside the managed paths. Move those three managed paths aside, then run the candidate installer as a fresh installation. Absence of the old release does not bypass the retained-session check: if a backup is incomplete, restore the archived controller and finish its safe stop/backup before retrying installation. Do not alter the journal or substitute a success hook to bypass this refusal. Preserve the existing private operator/deployment/secret files; apply only reviewed configuration changes. Run the new CLI's explicit `update survival` command when changing desired content, so it checks the completed backup and retains the prior active manifest. Never run installation or swap scripts while an instance operation is in progress.

If installation fails, repair it and rerun the identical candidate; existing identical paths are preserved. If the candidate has not launched, the archived code/configuration can be put back during the same stopped maintenance window. Once a new runtime may have changed world data, restoring old binaries alone is not a rollback: use the isolated verified recovery procedure from #6 and retain the source data and backup.

Installer tests use temporary paths and controlled operating-system commands. They verify file installation, repeat behavior, private-directory ownership/permission refusal, protected shared-lock placement, refusal of untrusted lock directories, staged-replacement backup refusal, and private configuration preservation. Runtime lock tests exercise directory checks, unchanged repeated setup, and lock contention at that same path. The source-tree guard tests cover pristine/completed states, incomplete sessions, unconfigured retained instances, untracked data/containers, and storage failure before journal inspection. These checks do not establish actual systemd behavior, a mounted EBS disk, Minecraft readiness, or deployed recovery.
