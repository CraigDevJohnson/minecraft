#!/usr/bin/env bash
set -euo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_dir=/opt/minecraft
launcher=/usr/local/sbin/minecraft-instance
backup_launcher=/usr/local/sbin/minecraft-backup
restore_launcher=/usr/local/sbin/minecraft-restore
unit=/etc/systemd/system/minecraft@.service
private_dir=/etc/minecraft
lock_dir=/run/minecraft
lock_file="$lock_dir/runtime.lock"

fail() {
	printf '%s\n' "$1" >&2
	exit 1
}

trusted_path() {
	local metadata owner mode
	[[ ! -L "$1" ]] || fail 'Managed installation paths must not be symlinks.'
	metadata="$(stat -c '%u:%a' -- "$1")"
	owner="${metadata%%:*}"
	mode="${metadata#*:}"
	if [[ "$owner" != 0 ]] || (((8#$mode & 0022) != 0)); then
		fail 'Managed installation paths must be root-owned and not writable by other accounts.'
	fi
}

[[ $# == 0 ]] || fail 'usage: sudo bash scripts/install_runtime.sh'
[[ "$(id -u)" == 0 ]] || fail 'Runtime installation requires root on the reviewed Linux host.'
[[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] || fail 'Runtime installation requires Linux ARM64.'
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
command -v timeout >/dev/null
command -v flock >/dev/null
command -v docker >/dev/null
command -v systemctl >/dev/null
command -v aws >/dev/null || fail 'Install a reviewed AWS CLI v2 with conditional backup upload and checksum support first.'
aws_version="$(timeout --kill-after=10s 30s aws --version)" || fail 'Cannot establish the installed AWS CLI v2 version.'
[[ "$aws_version" == aws-cli/2.* ]] || fail 'Runtime backup requires AWS CLI v2.'
for operation in put-object complete-multipart-upload create-multipart-upload head-object; do
	case "$operation" in
	put-object) required_fields=(IfNoneMatch IfMatch ChecksumSHA256) ;;
	complete-multipart-upload) required_fields=(IfNoneMatch) ;;
	create-multipart-upload) required_fields=(ChecksumType ChecksumAlgorithm) ;;
	head-object) required_fields=(VersionId IfMatch) ;;
	esac
	aws_skeleton="$(AWS_EC2_METADATA_DISABLED=true AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off timeout --kill-after=10s 30s aws s3api "$operation" --generate-cli-skeleton input --no-cli-pager --no-cli-auto-prompt)" || fail 'Cannot establish the AWS CLI v2 backup capabilities without credentials.'
	python3 -c 'import json,sys; value=json.load(sys.stdin); sys.exit(not isinstance(value, dict) or not all(key in value for key in sys.argv[1:]))' "${required_fields[@]}" <<<"$aws_skeleton" || fail 'AWS CLI v2 lacks required conditional backup writes, version reads or checksum support; install a compatible reviewed CLI first.'
done
# get-object has a custom outfile parser and does not support input skeletons.
# Missing option values prove these flags exist and always fail before an API request.
for option in --version-id --if-match; do
	if aws_parser_output="$(LC_ALL=C AWS_EC2_METADATA_DISABLED=true AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off timeout --kill-after=10s 30s aws s3api get-object "$option" 2>&1)"; then
		fail 'AWS CLI v2 get-object preflight unexpectedly succeeded.'
	fi
	[[ "$aws_parser_output" == *"argument $option: expected one argument"* ]] || fail 'AWS CLI v2 lacks the required offline get-object version/condition parser; install a compatible reviewed CLI first.'
done
[[ -x /usr/local/sbin/minecraft-storage ]] || fail 'Install the reviewed host/storage prerequisites first.'

for directory in /opt /usr/local/sbin /etc/systemd/system \
	"$(dirname /usr/local/sbin)" "$(dirname "$(dirname /usr/local/sbin)")" \
	"$(dirname /etc/systemd/system)" "$(dirname "$(dirname /etc/systemd/system)")" \
	"$private_dir" "$install_dir"; do
	[[ ! -L "$directory" ]] || fail 'Installation paths must not be symlinks.'
	[[ ! -e "$directory" || -d "$directory" ]] || fail 'An installation directory is occupied by a non-directory.'
	if [[ -e "$directory" ]]; then
		if [[ "$directory" == "$private_dir" ]]; then
			[[ "$(stat -c '%u:%a' -- "$directory")" == 0:700 ]] || fail 'The existing private directory must be root-owned mode 0700; inspect and repair it explicitly before installation.'
		else
			trusted_path "$directory"
		fi
	fi
done
trusted_path /usr/local/sbin/minecraft-storage
[[ -d /run && ! -L /run ]] || fail 'The runtime lock parent must be a trusted directory.'
trusted_path /run
if [[ ! -e "$lock_dir" && ! -L "$lock_dir" ]]; then
	mkdir -m 0700 "$lock_dir" 2>/dev/null || [[ -d "$lock_dir" ]] || fail 'Cannot create the private runtime lock directory.'
fi
[[ -d "$lock_dir" && ! -L "$lock_dir" ]] || fail 'The runtime lock directory must be a trusted directory.'
[[ "$(stat -c '%u:%a' -- "$lock_dir")" == 0:700 ]] || fail 'The runtime lock directory must be root-owned mode 0700; inspect and repair it explicitly.'
if [[ -e "$lock_file" || -L "$lock_file" ]]; then
	[[ -f "$lock_file" ]] || fail 'The runtime lock must be a trusted regular file.'
	trusted_path "$lock_file"
fi

exec 9>>"$lock_file"
flock -w 10 9 || fail 'Another runtime operation holds the host lock; retry later.'

stage="$(mktemp -d)"
copy_dir=''
trap 'rm -rf "$stage" "$copy_dir"' EXIT
install -d -m 0755 "$stage/scripts" "$stage/runtime" "$stage/bin"
for name in minecraft_instance.py minecraft_content.py minecraft_access.py minecraft_backup.py minecraft_backup_store.py minecraft_restore.py minecraft_retention.py; do
	install -m 0644 "$root_dir/scripts/$name" "$stage/scripts/$name"
done
for name in survival.json content.json creative.json content-raknet.json operator.json.example minecraft@.service; do
	install -m 0644 "$root_dir/runtime/$name" "$stage/runtime/$name"
done
for name in minecraft-instance minecraft-backup minecraft-restore; do
	cat >"$stage/bin/$name" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/python3 -B /opt/minecraft/scripts/${name//-/_}.py "\$@"
LAUNCHER
	chmod 0755 "$stage/bin/$name"
done
chmod 0755 "$stage"

# Preflight every managed path before writing any installed content.
if [[ -d "$install_dir" ]]; then
	diff -rq --no-dereference "$stage" "$install_dir" >/dev/null || fail 'Installed release or desired content differs. Follow docs/operations/runtime-installation.md for a staged upgrade; nothing was replaced.'
	for relative in scripts runtime bin scripts/minecraft_instance.py scripts/minecraft_content.py scripts/minecraft_access.py scripts/minecraft_backup.py scripts/minecraft_backup_store.py scripts/minecraft_restore.py scripts/minecraft_retention.py runtime/survival.json runtime/content.json runtime/creative.json runtime/content-raknet.json runtime/operator.json.example runtime/minecraft@.service bin/minecraft-instance bin/minecraft-backup bin/minecraft-restore; do
		trusted_path "$install_dir/$relative"
	done
fi
for pair in "bin/minecraft-instance:$launcher" "bin/minecraft-backup:$backup_launcher" "bin/minecraft-restore:$restore_launcher" "runtime/minecraft@.service:$unit"; do
	relative="${pair%%:*}"
	target="${pair#*:}"
	[[ ! -L "$target" ]] || fail 'Installed launcher/service must not be symlinks.'
	if [[ -e "$target" ]]; then
		trusted_path "$target"
		if [[ ! -f "$target" ]] || ! cmp -s "$stage/$relative" "$target"; then
			fail 'Installed launcher/service differs. Use the staged upgrade procedure; nothing was replaced.'
		fi
	fi
done

running="$(timeout --kill-after=10s 30s docker ps --quiet --filter label=com.docker.compose.service=minecraft)"
[[ -z "$running" ]] || fail 'A Minecraft runtime is running. Complete its safe stop and backup before installation.'
timeout --kill-after=10s 180s python3 -B "$root_dir/scripts/check_runtime_install.py" --deployment "$private_dir/deployment.json"

if [[ ! -d "$install_dir" ]]; then
	install -d -m 0755 /opt
	copy_dir="$(mktemp -d /opt/.minecraft-install.XXXXXX)"
	cp -R "$stage/." "$copy_dir"
	chmod 0755 "$copy_dir"
	mv "$copy_dir" "$install_dir"
fi
[[ -e "$launcher" ]] || install -m 0755 "$stage/bin/minecraft-instance" "$launcher"
[[ -e "$backup_launcher" ]] || install -m 0755 "$stage/bin/minecraft-backup" "$backup_launcher"
[[ -e "$restore_launcher" ]] || install -m 0755 "$stage/bin/minecraft-restore" "$restore_launcher"
[[ -e "$unit" ]] || install -m 0644 "$stage/runtime/minecraft@.service" "$unit"
[[ -d "$private_dir" ]] || install -d -m 0700 "$private_dir"
timeout --kill-after=10s 30s systemctl daemon-reload
printf 'Runtime release installed or already identical. Supply private operator configuration before an explicit start; no runtime was started.\n'
