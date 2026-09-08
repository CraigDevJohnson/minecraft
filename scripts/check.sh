#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
tools_dir="$root_dir/.tools/bin"
mode="${1:-check}"

require_tool() {
	if [[ ! -x "$tools_dir/$1" ]]; then
		printf 'missing project tool: %s (run python3 scripts/bootstrap_tools.py)\n' "$1" >&2
		exit 1
	fi
}

validate_infra() (
	local tofu_data_dir
	tofu_data_dir="$(mktemp -d "${TMPDIR:-/tmp}/minecraft-tofu.XXXXXX")"
	trap 'rm -rf -- "$tofu_data_dir"' EXIT

	local -a tofu_env=(
		env
		-u AWS_ACCESS_KEY_ID
		-u AWS_SECRET_ACCESS_KEY
		-u AWS_SESSION_TOKEN
		-u AWS_SECURITY_TOKEN
		-u AWS_PROFILE
		-u AWS_DEFAULT_PROFILE
		-u AWS_ROLE_ARN
		-u AWS_WEB_IDENTITY_TOKEN_FILE
		-u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
		-u AWS_CONTAINER_CREDENTIALS_FULL_URI
		-u TF_CLI_CONFIG_FILE
		-u TOFU_CONFIG_FILE
		-u TF_PLUGIN_CACHE_DIR
		AWS_EC2_METADATA_DISABLED=true
		AWS_CONFIG_FILE="$tofu_data_dir/nonexistent-aws-config"
		AWS_SHARED_CREDENTIALS_FILE="$tofu_data_dir/nonexistent-aws-credentials"
		TF_DATA_DIR="$tofu_data_dir"
	)

	"${tofu_env[@]}" "$tools_dir/tofu" -chdir="$root_dir/infra" init \
		-backend=false -input=false -lockfile=readonly

	"${tofu_env[@]}" "$tools_dir/tofu" -chdir="$root_dir/infra" validate -no-color
)

require_tool tofu

case "$mode" in
check)
	require_tool shellcheck
	require_tool shfmt
	bash -n "$root_dir"/scripts/*.sh
	"$tools_dir/shellcheck" "$root_dir"/scripts/*.sh
	"$tools_dir/shfmt" -d "$root_dir"/scripts/*.sh
	"$tools_dir/tofu" -chdir="$root_dir/infra" fmt -check -diff -recursive
	python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
	python3 -m unittest discover -s "$root_dir/tests" -p 'test_*.py'
	validate_infra
	;;
infra:validate)
	validate_infra
	;;
*)
	printf 'usage: %s [check|infra:validate]\n' "$0" >&2
	exit 2
	;;
esac
