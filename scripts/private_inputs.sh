#!/usr/bin/env bash
# Copies the private input files of the two OpenTofu roots between the state
# bucket and this checkout, so that planning a root need not depend on Craig's
# computer. The Taskfile's inputs:pull and inputs:push targets use it. See
# docs/operations/cloud-controls.md.
#
# Usage: private_inputs.sh pull [--force]
#        private_inputs.sh push [NAME] [--from PATH]
#
# The four names are deployment.tfvars and backend.hcl, the infra root's inputs
# as the Deploy workflow reads them, and bootstrap.tfvars and
# bootstrap.backend.hcl, infra/state-bootstrap's. Each is the object
# inputs/NAME in the state bucket and the file operator-config/inputs/NAME
# here, which Git ignores.
#
# A pull copies every one of the four the bucket holds. It leaves a local file
# that differs from the bucket's copy as it is, says so and ends with an error,
# unless --force is given. A push uploads every one of the four that exists here, or the one
# NAME; --from uploads another file as that NAME. A push changes AWS, so it
# needs Craig's go-ahead first.
#
# MINECRAFT_STATE_BUCKET names the state bucket, whose name is private and is
# never printed. AWS_PROFILE names a profile that assumes the operator role,
# the only principal the bucket's policy admits. The files hold private names
# and an email address, so only a file's name and its object version are
# printed, never its contents, and a pulled file is readable by its owner only.
set -euo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
inputs_dir="$root_dir/operator-config/inputs"
names=(deployment.tfvars backend.hcl bootstrap.tfvars bootstrap.backend.hcl)

usage() {
	printf 'usage: %s pull [--force] | push [NAME] [--from PATH], where NAME is one of: %s\n' "${0##*/}" "${names[*]}" >&2
	exit 2
}

# known NAME: succeeds when NAME is one of the four.
known() {
	local candidate
	for candidate in "${names[@]}"; do
		if [[ "$candidate" == "$1" ]]; then
			return 0
		fi
	done
	return 1
}

mode="${1:-}"
[[ "$mode" == pull || "$mode" == push ]] || usage
shift
force=""
only=""
from=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--force) force=true ;;
	--from)
		from="${2:-}"
		[[ -n "$from" ]] || usage
		shift
		;;
	-*) usage ;;
	*)
		[[ -z "$only" ]] || usage
		only="$1"
		;;
	esac
	shift
done
if [[ "$mode" == pull ]]; then
	[[ -z "$only" && -z "$from" ]] || usage
else
	[[ -z "$force" ]] || usage
	# Another file can stand in for one name only.
	[[ -z "$from" || -n "$only" ]] || usage
	[[ -z "$only" ]] || known "$only" || usage
fi

bucket="${MINECRAFT_STATE_BUCKET:-}"
if [[ -z "$bucket" ]]; then
	printf 'MINECRAFT_STATE_BUCKET must name the state bucket, the one infra/state-bootstrap owns; its name is private, so this repository does not give it\n' >&2
	exit 2
fi
if [[ -z "${AWS_PROFILE:-}" ]]; then
	printf 'AWS_PROFILE must name a profile that assumes the minecraft-operator role, the only principal the state bucket admits (docs/operations/cloud-controls.md)\n' >&2
	exit 2
fi

work=""
trap '[[ -z "$work" ]] || rm -rf "$work"' EXIT

# s3 OPERATION ARGUMENT...: runs one s3api call on the state bucket and prints the object's version, the only
# part of AWS's answer that leaves this script. What AWS wrote about a failure is kept for failed.
s3() {
	local operation="$1"
	shift
	aws s3api "$operation" --no-cli-pager --bucket "$bucket" --query VersionId --output text "$@" 2>"$work/errors"
}

# failed NAME: ends the run with what AWS wrote about the last call, the bucket's private name taken out.
failed() {
	local message
	message="$(<"$work/errors")"
	printf '%s: the AWS call failed\n%s\n' "$1" "${message//"$bucket"/<the state bucket>}" >&2
	exit 1
}

pull() {
	local name target version held=0 differs=0
	mkdir -p "$inputs_dir"
	# Beside the files it fills, so each lands whole or not at all.
	work="$(mktemp -d "$inputs_dir/.pull.XXXXXX")"
	for name in "${names[@]}"; do
		target="$inputs_dir/$name"
		if ! version="$(s3 get-object --key "inputs/$name" "$work/$name")"; then
			if grep -q NoSuchKey "$work/errors"; then
				printf '%s: not in the bucket\n' "$name"
				continue
			fi
			failed "$name"
		fi
		held=$((held + 1))
		if [[ -e "$target" ]] && cmp -s "$work/$name" "$target"; then
			printf '%s: already the bucket'\''s copy (version %s)\n' "$name" "$version"
			continue
		fi
		if [[ -e "$target" && -z "$force" ]]; then
			printf '%s: left as it is, because the local file differs from the bucket'\''s copy (version %s); FORCE=1 (--force) replaces it\n' \
				"$name" "$version" >&2
			differs=1
			continue
		fi
		chmod 600 "$work/$name"
		mv -f "$work/$name" "$target"
		printf '%s: pulled (version %s)\n' "$name" "$version"
	done
	if ((held == 0)); then
		printf 'the bucket holds none of the four private input files\n' >&2
		exit 1
	fi
	if ((differs)); then
		exit 1
	fi
}

push() {
	local chosen=("${names[@]}") name source where version pushed=0
	if [[ -n "$only" ]]; then
		chosen=("$only")
	fi
	work="$(mktemp -d)"
	for name in "${chosen[@]}"; do
		source="${from:-$inputs_dir/$name}"
		if [[ ! -f "$source" ]]; then
			if [[ -z "$only" ]]; then
				continue
			fi
			where="operator-config/inputs/$name"
			if [[ -n "$from" ]]; then
				where="the FROM path"
			fi
			printf '%s: %s is not a file\n' "$name" "$where" >&2
			exit 1
		fi
		version="$(s3 put-object --key "inputs/$name" --body "$source")" || failed "$name"
		printf '%s: pushed (version %s)\n' "$name" "$version"
		pushed=$((pushed + 1))
	done
	if ((pushed == 0)); then
		printf 'nothing to push: operator-config/inputs/ holds none of the four private input files\n' >&2
		exit 1
	fi
}

"$mode"
