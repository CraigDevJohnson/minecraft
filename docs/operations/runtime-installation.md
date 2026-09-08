# Install the host runtime

Install a reviewed checkout on the Ubuntu 24.04 ARM64 host after the [host/storage prerequisites](infrastructure.md) are installed. This step belongs to the concrete deployment in #7. It installs local files and reloads systemd; it does not create AWS resources, initialize storage, accept the Minecraft EULA, create instance data, download game artifacts, or start Minecraft.

```sh
sudo bash scripts/install_runtime.sh
```

The installer requires root, Python 3.11+, Docker, systemd, and the installed `minecraft-storage` command. It holds the same bounded host lock as instance operations and refuses installation while a Minecraft Compose service is running. It copies the instance/content scripts to `/opt/minecraft/scripts`, the reviewed `survival` definition and exact content manifest to `/opt/minecraft/runtime`, and installs `/usr/local/sbin/minecraft-instance` plus `minecraft@.service`. It never enables the service. An identical rerun preserves installed files and all private configuration; a differing installed release, desired definition, launcher, or service is refused before replacements.

Prepare these root-owned private regular files with mode `0600` outside Git and transfer them through the reviewed deployment procedure:

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

During this explicit maintenance window, preserve `/opt/minecraft`, `/usr/local/sbin/minecraft-instance`, and `/etc/systemd/system/minecraft@.service` together in a root-owned release archive outside the managed paths. Move those three managed paths aside, then run the candidate installer as a fresh installation. Preserve the existing private operator/deployment/secret files; apply only reviewed configuration changes. Run the new CLI's explicit `update survival` command when changing desired content, so it checks the completed backup and retains the prior active manifest. Never run installation or swap scripts while an instance operation is in progress.

If installation fails, repair it and rerun the identical candidate; existing identical paths are preserved. If the candidate has not launched, the archived code/configuration can be put back during the same stopped maintenance window. Once a new runtime may have changed world data, restoring old binaries alone is not a rollback: use the isolated verified recovery procedure from #6 and retain the source data and backup.

Installer tests use temporary paths and controlled operating-system commands. They verify file installation, repeat behavior, refusal paths, and private configuration preservation; they do not establish actual systemd behavior, a mounted EBS disk, Minecraft readiness, or deployed recovery.
