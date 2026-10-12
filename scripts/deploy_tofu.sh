#!/usr/bin/env bash
# Usage: DEPLOY_DIR=<dir> deploy_tofu.sh plan|apply
#
# Runs the infra root the way the Deploy workflow does. DEPLOY_DIR holds the
# private inputs the workflow wrote from its environment secrets,
# deployment.tfvars and backend.hcl, and the packages it built under packages/.
# The full plan and apply output goes to a file, not the log: attribute values
# show the private inputs, so only the resource headers, the progress lines and
# the totals reach the log and the job summary. OpenTofu's own errors still go
# to the log, because they are written to stderr.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
tofu="$root_dir/.tools/bin/tofu"
mode="${1:-}"
case "$mode" in
plan | apply) ;;
*)
	printf 'usage: DEPLOY_DIR=<dir> %s plan|apply\n' "${0##*/}" >&2
	exit 2
	;;
esac
deploy_dir="${DEPLOY_DIR:-}"
if [[ -z "$deploy_dir" ]]; then
	printf 'DEPLOY_DIR must name the directory holding deployment.tfvars, backend.hcl and packages/\n' >&2
	exit 2
fi
for required in deployment.tfvars backend.hcl packages/launcher.zip packages/idle-stop.zip; do
	if [[ ! -f "$deploy_dir/$required" ]]; then
		printf '::error::%s is missing from the deploy directory\n' "$required" >&2
		exit 1
	fi
done
if [[ ! -x "$tofu" ]]; then
	printf 'missing project tool: tofu (run python3 scripts/bootstrap_tools.py)\n' >&2
	exit 1
fi

"$tofu" -chdir="$root_dir/infra" init -input=false -no-color -lockfile=readonly \
	-backend-config="$deploy_dir/backend.hcl"

out="$(mktemp)"
trap 'rm -f "$out"' EXIT

# report STEP: writes the trimmed output of the last step to the log and the job summary. It keeps the
# "# address will be ..." headers, the progress lines and the totals, and drops everything indented deeper,
# which is where the attribute values are.
report() {
	local trimmed
	trimmed="$(grep -E '^(Plan:|Apply complete!|No changes\.|  # |[^ ]+: (Creating|Modifying|Destroying|Refreshing state|Reading|Read complete|Still |Creation complete|Modifications complete|Destruction complete))' "$out" || true)"
	{
		printf '## OpenTofu %s\n\n' "$1"
		printf '%s\n' "\`\`\`text" "${trimmed:-(nothing to report)}" "\`\`\`"
	} | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

status=0
"$tofu" -chdir="$root_dir/infra" plan -input=false -no-color -lock-timeout=5m \
	-var-file="$deploy_dir/deployment.tfvars" -var "package_dir=$deploy_dir/packages" \
	-out="$deploy_dir/tfplan" >"$out" || status=$?
report plan
if [[ "$status" -ne 0 ]]; then
	printf '::error::tofu plan failed (exit %s); see the messages above\n' "$status" >&2
	exit "$status"
fi
if [[ "$mode" == plan ]]; then
	exit 0
fi

"$tofu" -chdir="$root_dir/infra" apply -input=false -no-color -lock-timeout=5m \
	"$deploy_dir/tfplan" >"$out" || status=$?
report apply
if [[ "$status" -ne 0 ]]; then
	printf '::error::tofu apply failed (exit %s); see the messages above\n' "$status" >&2
fi
exit "$status"
