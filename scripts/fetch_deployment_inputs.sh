#!/usr/bin/env bash
# Usage: DEPLOY_DIR=<dir> fetch_deployment_inputs.sh
#
# Reads the infra root's inputs for the Deploy workflow from the state bucket:
# the object inputs/deployment.tfvars, the file `task inputs:push` uploads,
# lands in DEPLOY_DIR as deployment.tfvars. The bucket is the one
# DEPLOY_DIR/backend.hcl names, which the workflow wrote from its
# BACKEND_CONFIG secret, so the bucket's copy is the only copy of the inputs
# and a pushed file is what deploys. The workflow runs it as the operator role,
# the only principal the bucket's policy admits. See
# docs/operations/cloud-controls.md.
#
# The file holds private names and an email address, and the bucket's name is
# private, so only the object's version is printed, never the file or the name.
set -euo pipefail
umask 077

deploy_dir="${DEPLOY_DIR:-}"
if [[ -z "$deploy_dir" || ! -f "$deploy_dir/backend.hcl" ]]; then
	printf 'DEPLOY_DIR must name the directory holding backend.hcl\n' >&2
	exit 2
fi

# The backend settings name the bucket on one line, as bucket = "<name>".
bucket="$(sed -nE 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"([^"]+)"[[:space:]]*(#.*)?$/\1/p' "$deploy_dir/backend.hcl")"
if [[ -z "$bucket" || "$bucket" == *$'\n'* ]]; then
	printf '::error::backend.hcl must name the state bucket once, as bucket = "<name>"\n' >&2
	exit 1
fi

# Beside the file it fills, so the file lands whole or not at all.
work="$(mktemp -d "$deploy_dir/.fetch.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if ! version="$(aws s3api get-object --no-cli-pager --bucket "$bucket" --key inputs/deployment.tfvars \
	--query VersionId --output text "$work/deployment.tfvars" 2>"$work/errors")"; then
	message="$(<"$work/errors")"
	printf '::error::deployment.tfvars could not be read from the state bucket; task inputs:push uploads it (docs/operations/cloud-controls.md)\n' >&2
	printf '%s\n' "${message//"$bucket"/<the state bucket>}" >&2
	exit 1
fi
mv -f "$work/deployment.tfvars" "$deploy_dir/deployment.tfvars"
printf 'deployment.tfvars: read from the state bucket (version %s)\n' "$version"
