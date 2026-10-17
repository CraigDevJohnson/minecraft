#!/usr/bin/env bash
# Sends one signed request to the Minecraft launcher's Function URL and, for a
# start, a stop or a restore, follows the server until it settles. The Server
# control workflow and the Taskfile's server:* targets use it. Nothing runs where
# it runs: the launcher does the work in AWS. See docs/operations/cloud-controls.md.
#
# Usage: launcher_call.sh status
#        launcher_call.sh start
#        launcher_call.sh stop [--force]
#        launcher_call.sh restore --archive NAME
#        launcher_call.sh allowlist-add|allowlist-remove --edition java|bedrock --player NAME
#
# A restore's NAME is the archive's name as the S3 console shows it, such as
# 20261016T031500Z.tar.gz, or an S3 URI or key that ends in it; only the name is
# sent, and the launcher finds it among its own instance's archives.
#
# LAUNCHER_URL names the Function URL. Credentials come from the environment
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN) or, when they
# are missing and AWS_PROFILE is set, from `aws configure export-credentials`.
# They reach curl through a private configuration file, never its arguments.
set -euo pipefail

usage() {
	printf 'usage: %s status | start | stop [--force] | restore --archive NAME | allowlist-add|allowlist-remove --edition java|bedrock --player NAME\n' "${0##*/}" >&2
	exit 2
}

action="${1:-}"
[[ -n "$action" ]] || usage
shift
force=false
archive=""
edition=""
player=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--force) force=true ;;
	--archive)
		archive="${2:-}"
		shift
		;;
	--edition)
		edition="${2:-}"
		shift
		;;
	--player)
		player="${2:-}"
		shift
		;;
	*) usage ;;
	esac
	# An option missing its value leaves nothing here to shift.
	shift || usage
done

url="${LAUNCHER_URL:-}"
if [[ ! "$url" =~ ^https://[a-z0-9]+\.lambda-url\.([a-z0-9-]+)\.on\.aws/?$ ]]; then
	printf 'LAUNCHER_URL must be the launcher'\''s Function URL (https://<id>.lambda-url.<region>.on.aws/)\n' >&2
	exit 2
fi
region="${BASH_REMATCH[1]}"
url="${url%/}"

if [[ -z "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_PROFILE:-}" ]]; then
	eval "$(aws configure export-credentials --profile "$AWS_PROFILE" --format env)"
fi
if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
	printf 'AWS credentials are required: set AWS_PROFILE, or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY\n' >&2
	exit 2
fi

credentials="$(mktemp)"
trap 'rm -f "$credentials"' EXIT
{
	printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY"
	if [[ -n "${AWS_SESSION_TOKEN:-}" ]]; then
		printf 'header = "x-amz-security-token: %s"\n' "$AWS_SESSION_TOKEN"
	fi
} >"$credentials"

status=""
answer=""

# request METHOD PATH [BODY]: sends one signed request and sets status (the HTTP status) and answer (the body).
request() {
	local method="$1" path="$2" body="${3:-}" output
	local args=(--silent --show-error --config "$credentials" --aws-sigv4 "aws:amz:$region:lambda"
		--request "$method" --header "content-type: application/json" --write-out '\n%{http_code}')
	if [[ -n "$body" ]]; then
		args+=(--data "$body")
	fi
	output="$(curl "${args[@]}" "$url$path")"
	status="${output##*$'\n'}"
	answer="${output%$'\n'*}"
}

# field NAME: prints that field of the last answer, or nothing.
field() {
	printf '%s' "$answer" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get(sys.argv[1], "")
except ValueError:
    value = ""
print(value if isinstance(value, (str, int)) else "")' "$1"
}

# settle WANT WAITING MINUTES: judges a 200 answer, which means the launcher changed nothing. The server is
# already WANT, or someone else's transition to it is under way and gets followed; anything else fails.
settle() {
	local state
	state="$(field state)"
	case "$state" in
	"$1") printf 'nothing to do: the server is already %s\n' "$state" ;;
	"$2")
		printf 'a change to %s is already under way\n' "$1"
		follow "$2" "$1" "$3"
		;;
	*)
		printf 'nothing changed: the server is %s, not %s\n' "${state:-unread}" "$1" >&2
		return 1
		;;
	esac
}

# follow WAITING WANT MINUTES: polls GET /server until the state leaves WAITING. Reaching WANT succeeds; any
# other state, or MINUTES passing, fails. An unknown state (AWS could not be read) is waited out.
follow() {
	local waiting="$1" want="$2" deadline=$((SECONDS + $3 * 60)) state
	while :; do
		sleep 10
		request GET /server
		state="$(field state)"
		printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "${state:-($status)}"
		if [[ "$state" == "$want" ]]; then
			return 0
		fi
		if [[ "$state" != "$waiting" && "$state" != unknown && "$status" == 200 ]]; then
			printf 'the server ended %s, not %s\n' "$state" "$want" >&2
			return 1
		fi
		if ((SECONDS >= deadline)); then
			printf 'gave up after %s minutes; the server is %s\n' "$3" "${state:-unread}" >&2
			return 1
		fi
	done
}

case "$action" in
status)
	request GET /server
	printf '%s\n' "$answer"
	[[ "$status" == 200 ]]
	;;
start)
	request POST /server/start
	printf '%s\n' "$answer"
	case "$status" in
	202) follow starting ready 20 ;;
	200) settle ready starting 20 ;;
	*)
		printf 'the launcher answered %s\n' "$status" >&2
		exit 1
		;;
	esac
	;;
stop)
	body=""
	if [[ "$force" == true ]]; then
		body='{"force": true}'
	fi
	request POST /server/stop "$body"
	printf '%s\n' "$answer"
	case "$status" in
	202) follow stopping stopped 80 ;;
	200) settle stopped stopping 80 ;;
	409)
		printf 'the stop was refused: %s\n' "$(field code)" >&2
		exit 1
		;;
	*)
		printf 'the launcher answered %s\n' "$status" >&2
		exit 1
		;;
	esac
	;;
restore)
	archive="${archive##*/}"
	if [[ ! "$archive" =~ ^[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]]; then
		usage
	fi
	body="$(python3 -c 'import json, sys; print(json.dumps({"archive": sys.argv[1]}))' "$archive")"
	request POST /server/restore "$body"
	printf '%s\n' "$answer"
	case "$status" in
	202) follow starting ready 60 ;;
	409)
		state="$(field state)"
		printf 'the restore was refused: %s; the server is %s\n' "$(field code)" "${state:-unread}" >&2
		exit 1
		;;
	*)
		printf 'the launcher answered %s\n' "$status" >&2
		exit 1
		;;
	esac
	;;
allowlist-add | allowlist-remove)
	if [[ -z "$edition" || -z "$player" ]]; then
		usage
	fi
	body="$(python3 -c 'import json, sys; print(json.dumps({"operation": sys.argv[1], "edition": sys.argv[2], "name": sys.argv[3]}))' \
		"${action#allowlist-}" "$edition" "$player")"
	request POST /server/allowlist "$body"
	printf '%s\n' "$answer"
	[[ "$status" == 200 ]]
	;;
*)
	usage
	;;
esac
