#!/usr/bin/env bash
# Sends one signed request to the Minecraft launcher's Function URL and, for a
# start, a stop or a restore, follows that world's server until it settles. The
# Server control workflow and the Taskfile's server:* targets use it. Nothing runs
# where it runs: the launcher does the work in AWS. See docs/operations/cloud-controls.md.
#
# Usage: launcher_call.sh status [--world ID]
#        launcher_call.sh start [--world ID]
#        launcher_call.sh stop [--world ID] [--force]
#        launcher_call.sh restore --archive NAME [--world ID]
#        launcher_call.sh mods
#        launcher_call.sh mod-on|mod-off --world ID --mod ID
#        launcher_call.sh allowlist-add|allowlist-remove --edition java|bedrock --player NAME
#
# Each world has its own host. Without --world the launcher acts on its first
# world, and status lists every world. A mod switch applies at that world's next
# start. The allowlist is one list for every world, so its actions take no world.
#
# A restore's NAME is the archive's name as the S3 console shows it, such as
# 20261016T031500Z.tar.gz, or an S3 URI or key that ends in it; only the name is
# sent, and the launcher finds it among that world's archives.
#
# LAUNCHER_URL names the Function URL. Credentials come from the environment
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN) or, when they
# are missing and AWS_PROFILE is set, from `aws configure export-credentials`.
# They reach curl through a private configuration file, never its arguments.
set -euo pipefail

usage() {
	printf 'usage: %s status [--world ID] | start [--world ID] | stop [--world ID] [--force] | restore --archive NAME [--world ID] | mods | mod-on|mod-off --world ID --mod ID | allowlist-add|allowlist-remove --edition java|bedrock --player NAME\n' "${0##*/}" >&2
	exit 2
}

action="${1:-}"
[[ -n "$action" ]] || usage
shift
force=""
world=""
mod=""
archive=""
edition=""
player=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--force) force=true ;;
	--world)
		world="${2:-}"
		[[ "$world" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage
		shift
		;;
	--mod)
		mod="${2:-}"
		[[ "$mod" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage
		shift
		;;
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
# The allowlist is one list for every world, and the mods list shows them all.
case "$action" in
mods | allowlist-add | allowlist-remove) [[ -z "$world" ]] || usage ;;
esac
# Where the chosen world's status is read; the launcher answers for its first world when none is named.
server="/server${world:+?world=$world}"

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
	if [[ -n "$body" && "$body" != "{}" ]]; then
		args+=(--data "$body")
	fi
	output="$(curl "${args[@]}" "$url$path")"
	status="${output##*$'\n'}"
	answer="${output%$'\n'*}"
}

# object NAME VALUE...: prints a JSON object of those names and values. An empty value is left out, and force
# and enabled are sent as booleans.
object() {
	python3 -c '
import json, sys
pairs = zip(sys.argv[1::2], sys.argv[2::2])
print(json.dumps({name: value == "true" if name in ("force", "enabled") else value for name, value in pairs if value}))' "$@"
}

# show KIND: prints the last answer for reading. worlds is a line for each world of a status, mods each
# world's mods and whether each is on, and allowlist which running worlds an allowlist change also reached.
# An answer that is not what KIND expects is printed as it came.
show() {
	printf '%s' "$answer" | python3 -c '
import json, sys
text = sys.stdin.read()
lines = []
try:
    answer = json.loads(text)
    if sys.argv[1] == "worlds":
        for world in answer["worlds"]:
            address = world.get("connection", {}).get("java", {}).get("address", "")
            lines.append("{:<12} {:<12} {}".format(world["id"], world["state"], address).rstrip())
    elif sys.argv[1] == "mods":
        for world in answer["worlds"]:
            lines.append("{} ({})".format(world["id"], world["name"]))
            for mod in world["mods"]:
                setting = "on" if mod["enabled"] else "off"
                usual = "" if mod["enabled"] == mod["default"] else " (usually {})".format("on" if mod["default"] else "off")
                lines.append("  {:<16} {:<3} {} {}{}".format(mod["id"], setting, mod["name"], mod["version"], usual))
    else:
        reached = ", ".join(answer["worlds"])
        lines = [text, "running worlds changed now: " + reached if reached else "no world is running; each takes the list at its start"]
except (ValueError, KeyError, TypeError, AttributeError):
    lines = [text]
print("\n".join(lines))' "$1"
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
		request GET "$server"
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
	request GET "$server"
	if [[ -z "$world" && "$status" == 200 ]]; then
		show worlds
	else
		printf '%s\n' "$answer"
	fi
	[[ "$status" == 200 ]]
	;;
start)
	request POST /server/start "$(object world "$world")"
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
	request POST /server/stop "$(object world "$world" force "$force")"
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
	request POST /server/restore "$(object world "$world" archive "$archive")"
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
	show allowlist
	[[ "$status" == 200 ]]
	;;
mods)
	request GET /mods
	show mods
	[[ "$status" == 200 ]]
	;;
mod-on | mod-off)
	if [[ -z "$world" || -z "$mod" ]]; then
		usage
	fi
	enabled=false
	if [[ "$action" == mod-on ]]; then
		enabled=true
	fi
	request POST /mods "$(object world "$world" mod "$mod" enabled "$enabled")"
	show mods
	if [[ "$status" != 200 ]]; then
		printf 'the launcher answered %s\n' "$status" >&2
		exit 1
	fi
	printf '%s is %s for %s from its next start\n' "$mod" "${action#mod-}" "$world"
	;;
*)
	usage
	;;
esac
