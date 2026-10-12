# ADR 0007: The launcher carries the host adapter

Status: Accepted, 2026-10-12.

[ADR 0006](0006-cloud-controls.md) made the launcher the only operator, and its allowlist route calls the host adapter's `allowlist` action. The Crafty adapter installed on the host ([ADR 0004](0004-crafty-multiverse-rebuild.md), `scripts/install_crafty.sh`) had no such action: it was written for the earlier runtime, which reached the console over RCON, and nothing but a hands-on run of the installer ever updated the adapter on the host. A Deploy changed the launcher and left the host's adapter as it was, so the route could not work after any deploy. Copying the adapter by hand is a workstation step, which ADR 0006 rules out.

## Decision

- **The launcher package carries the adapter.** `scripts/package_idle_stop.py --function launcher` puts `minecraft_crafty.py` and the two modules it imports in the zip's `adapter` folder, with the same fixed timestamp and mode as the binary, so the plan digest Craig approves covers them. The function reads them when it starts and refuses to start without them.
- **The launcher installs it before it uses it.** Every game start and allowlist command begins with lines that check the host's `/opt/minecraft-crafty/scripts` against the carried files' SHA-256. When they match, as root's own regular files that nobody else may write, nothing changes. When any differs, the carried files are unpacked beside the folder, checked again, and renamed into place one at a time, the imported modules before the adapter, so no adapter command ever reads a partly written file. A host without the folder, which the installer creates root's and closed to everyone else, fails the command, and so does a folder that someone other than root could change or, through a folder above it, rename, since root runs what it holds. The stop's commands do not install, because the start that follows does.
- **The adapter's `allowlist` action uses Crafty.** It sends the console command through Crafty's API, as the operator's Crafty user, and confirms the change in the server's `whitelist.json`, with the checks `minecraft_access` already made over RCON. A Java account Mojang no longer knows, as after a rename, can be removed by the name the server holds.

## Consequences

- One Deploy brings an adapter change to the host, the next time the server starts or an allowlist change runs; a running server picks it up at its next allowlist change.
- Each start and allowlist command is about 28 KiB larger. A Go test keeps the install lines under 48 KiB, inside what Run Command accepts.
- The adapter files on the host are owned by root with mode `0644`, as the installer leaves them; the Run Command document runs as root, as every adapter action already does.
- Crafty's audit log records each console command with the player's name in it, on the host's data disk, as the game's own log already did.
- `scripts/install_crafty.sh` still installs the adapter on a new host; `tests/test_launcher_package.py` keeps its file list, the package's and the launcher's the same.
- Rejected: a separate SSM document or S3 object for the adapter (another thing to deploy and keep in step), and installing from the Deploy workflow (it would need Run Command on the host from GitHub, and a stopped host could not take it).
