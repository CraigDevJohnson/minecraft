#!/usr/bin/env bash
# Usage: DEPLOY_DIR=<dir> [APPROVED_PLAN=<digest>] deploy_tofu.sh plan|apply
#
# Runs the infra root the way the Deploy workflow does. DEPLOY_DIR holds the
# private inputs the workflow wrote from its environment secrets,
# deployment.tfvars and backend.hcl, and the packages it built under packages/.
# The full plan and apply output goes to a file, not the log: attribute values
# show the private inputs, so only the resource headers, the progress lines and
# the totals reach the log and the job summary, and the progress lines lose
# their "[id=...]" suffix, which carries physical ids such as bucket names.
# OpenTofu's own errors still go to the log, because they are written to
# stderr; the workflow masks the private inputs' values first.
#
# Every plan ends with its digest (scripts/plan_digest.py). An apply run plans
# again and applies only when that plan's digest is APPROVED_PLAN, the digest
# Craig copied from the plan run he read, so what applies is exactly what he
# approved; if main, the private inputs or the state changed since, it stops
# before applying and posts the new plan and its digest instead.
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
approved="${APPROVED_PLAN:-}"
if [[ "$mode" == apply && ! "$approved" =~ ^[0-9a-f]{16}$ ]]; then
	printf '::error::An apply run needs the plan digest from the summary of the plan run you read: run Deploy with mode apply and that digest as plan.\n' >&2
	exit 1
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
# "# address will be ..." headers, the progress lines and the totals, drops everything indented deeper,
# which is where the attribute values are, and strips the "[id=...]" suffix from the progress lines,
# which names the private inputs (the bucket, the hostname's record) and other physical ids.
report() {
	local trimmed
	trimmed="$(grep -E '^(Plan:|Apply complete!|No changes\.|  # |[^ ]+: (Creating|Modifying|Destroying|Refreshing state|Reading|Read complete|Still |Creation complete|Modifications complete|Destruction complete))' "$out" |
		sed -E 's/ \[id=[^]]*\]//' || true)"
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
if ! digest="$("$tofu" -chdir="$root_dir/infra" show -json "$deploy_dir/tfplan" | python3 "$root_dir/scripts/plan_digest.py")"; then
	printf '::error::the saved plan could not be read for its digest\n' >&2
	exit 1
fi
printf "\nPlan digest: \`%s\`\n" "$digest" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
if [[ "$mode" == plan ]]; then
	printf "\nTo apply exactly this plan, run Deploy again with mode \`apply\` and plan \`%s\`.\n" "$digest" |
		tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
	exit 0
fi
if [[ "$digest" != "$approved" ]]; then
	printf "\nNothing was applied: this plan (digest \`%s\`) is not the plan you approved (\`%s\`), because main, the private inputs or the state changed since. Read the plan above; to apply it, run Deploy with mode \`apply\` and plan \`%s\`.\n" \
		"$digest" "$approved" "$digest" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
	printf '::error::the plan differs from the approved plan; nothing was applied\n' >&2
	exit 1
fi

"$tofu" -chdir="$root_dir/infra" apply -input=false -no-color -lock-timeout=5m \
	"$deploy_dir/tfplan" >"$out" || status=$?
report apply
if [[ "$status" -ne 0 ]]; then
	printf '::error::tofu apply failed (exit %s); see the messages above\n' "$status" >&2
fi
exit "$status"
