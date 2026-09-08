#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_dir=/opt/minecraft
launcher=/usr/local/sbin/minecraft-instance
unit=/etc/systemd/system/minecraft@.service
private_dir=/etc/minecraft
lock_file=/run/lock/minecraft-runtime.lock

fail() {
	printf '%s\n' "$1" >&2
	exit 1
}

[[ $# == 0 ]] || fail 'usage: sudo bash scripts/install_runtime.sh'
[[ "$(id -u)" == 0 ]] || fail 'Runtime installation requires root on the reviewed Linux host.'
[[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] || fail 'Runtime installation requires Linux ARM64.'
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
command -v timeout >/dev/null
command -v flock >/dev/null
command -v docker >/dev/null
command -v systemctl >/dev/null
[[ -x /usr/local/sbin/minecraft-storage ]] || fail 'Install the reviewed host/storage prerequisites first.'

exec 9>"$lock_file"
flock -w 10 9 || fail 'Another runtime operation holds the host lock; retry later.'

stage="$(mktemp -d)"
copy_dir=''
trap 'rm -rf "$stage" "$copy_dir"' EXIT
install -d -m 0755 "$stage/scripts" "$stage/runtime" "$stage/bin"
for name in minecraft_instance.py minecraft_content.py minecraft_access.py; do
	install -m 0644 "$root_dir/scripts/$name" "$stage/scripts/$name"
done
for name in survival.json content.json operator.json.example minecraft@.service; do
	install -m 0644 "$root_dir/runtime/$name" "$stage/runtime/$name"
done
cat >"$stage/bin/minecraft-instance" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/python3 -B /opt/minecraft/scripts/minecraft_instance.py "$@"
LAUNCHER
chmod 0755 "$stage/bin/minecraft-instance"
chmod 0755 "$stage"

# Preflight every managed path before writing any installed content.
for directory in /opt /usr/local/sbin /etc/systemd/system "$private_dir" "$install_dir"; do
	[[ ! -L "$directory" ]] || fail 'Installation paths must not be symlinks.'
	[[ ! -e "$directory" || -d "$directory" ]] || fail 'An installation directory is occupied by a non-directory.'
done
if [[ -d "$install_dir" ]]; then
	diff -rq --no-dereference "$stage" "$install_dir" >/dev/null || fail 'Installed release or desired content differs. Follow docs/operations/runtime-installation.md for a staged upgrade; nothing was replaced.'
	for relative in scripts runtime bin; do
		[[ ! -L "$install_dir/$relative" ]] || fail 'Installed release directories must not be symlinks.'
	done
fi
for pair in "bin/minecraft-instance:$launcher" "runtime/minecraft@.service:$unit"; do
	relative="${pair%%:*}"
	target="${pair#*:}"
	[[ ! -L "$target" ]] || fail 'Installed launcher/service must not be symlinks.'
	if [[ -e "$target" ]]; then
		if [[ ! -f "$target" ]] || ! cmp -s "$stage/$relative" "$target"; then
			fail 'Installed launcher/service differs. Use the staged upgrade procedure; nothing was replaced.'
		fi
	fi
done

running="$(timeout --kill-after=10s 30s docker ps --quiet --filter label=com.docker.compose.service=minecraft)"
[[ -z "$running" ]] || fail 'A Minecraft runtime is running. Complete its safe stop and backup before installation.'

if [[ ! -d "$install_dir" ]]; then
	install -d -m 0755 /opt
	copy_dir="$(mktemp -d /opt/.minecraft-install.XXXXXX)"
	cp -R "$stage/." "$copy_dir"
	chmod 0755 "$copy_dir"
	mv "$copy_dir" "$install_dir"
fi
[[ -e "$launcher" ]] || install -m 0755 "$stage/bin/minecraft-instance" "$launcher"
[[ -e "$unit" ]] || install -m 0644 "$stage/runtime/minecraft@.service" "$unit"
[[ -d "$private_dir" ]] || install -d -m 0700 "$private_dir"
timeout --kill-after=10s 30s systemctl daemon-reload
printf 'Runtime release installed or already identical. Supply private operator configuration before an explicit start; no runtime was started.\n'
