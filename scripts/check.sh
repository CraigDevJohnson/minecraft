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

validate_infra() {
	python3 "$root_dir/scripts/validate_infra.py"
}

require_tool tofu
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'

case "$mode" in
check)
	require_tool shellcheck
	require_tool shfmt
	for script in "$root_dir"/scripts/*.sh "$root_dir"/scripts/*.sh.tftpl; do
		[[ -f "$script" ]] || continue
		bash -n "$script"
		"$tools_dir/shellcheck" "$script"
		"$tools_dir/shfmt" -d "$script"
	done
	"$tools_dir/tofu" -chdir="$root_dir/infra" fmt -check -diff -recursive
	python3 -m compileall -q "$root_dir/scripts" "$root_dir/tests"
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
