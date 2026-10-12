import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "launcher_call.sh"

# A curl stand-in: answers from the scenario file, one line per request as STATUS<TAB>BODY, and records each
# request's method and path. The script reads the body and the status from curl's output.
FAKE_CURL = """#!/usr/bin/env bash
method=GET
for ((i = 1; i <= $#; i++)); do
  if [[ "${!i}" == --request ]]; then
    j=$((i + 1)); method="${!j}"
  fi
done
url="${!#}"
printf '%s %s\\n' "$method" "${url#https://abc123.lambda-url.us-west-2.on.aws}" >> "$FAKE_CURL_LOG"
line="$(head -n 1 "$FAKE_CURL_SCENARIO")"
tail -n +2 "$FAKE_CURL_SCENARIO" > "$FAKE_CURL_SCENARIO.next" && mv "$FAKE_CURL_SCENARIO.next" "$FAKE_CURL_SCENARIO"
if [[ -z "$line" ]]; then
  echo "no answer scripted for $method $url" >&2
  exit 7
fi
printf '%s\\n%s' "${line#*	}" "${line%%	*}"
"""


class LauncherCallTests(unittest.TestCase):
    """start and stop end green only once the server is ready or stopped, whoever began the change."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="launcher-call-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        sleep = self.bin / "sleep"
        sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
        sleep.chmod(sleep.stat().st_mode | stat.S_IXUSR)

    def call(self, action, answers):
        scenario = self.tmp / "scenario"
        scenario.write_text("".join(f"{status}\t{json.dumps(body)}\n" for status, body in answers))
        log = self.tmp / "requests"
        log.write_text("")
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", LAUNCHER_URL="https://abc123.lambda-url.us-west-2.on.aws/",
                   AWS_ACCESS_KEY_ID="AKIAEXAMPLE", AWS_SECRET_ACCESS_KEY="example", FAKE_CURL_SCENARIO=str(scenario),
                   FAKE_CURL_LOG=str(log))
        env.pop("AWS_SESSION_TOKEN", None)
        completed = subprocess.run(["bash", str(SCRIPT), *action], capture_output=True, text=True, env=env, check=False)
        return completed, log.read_text().splitlines()

    def test_start_follows_a_start_someone_else_began(self):
        completed, requests = self.call(["start"], [(200, {"state": "starting"}), (200, {"state": "starting"}), (200, {"state": "ready"})])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(requests, ["POST /server/start", "GET /server", "GET /server"])
        self.assertIn("already under way", completed.stdout)

    def test_start_is_green_at_once_when_the_server_is_ready(self):
        completed, requests = self.call(["start"], [(200, {"state": "ready"})])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(requests, ["POST /server/start"])
        self.assertIn("already ready", completed.stdout)

    def test_start_fails_when_nothing_started_and_the_server_is_elsewhere(self):
        for state in ["maintenance", "stopping", "failed"]:
            completed, requests = self.call(["start"], [(200, {"state": state})])
            self.assertEqual(completed.returncode, 1, state)
            self.assertEqual(requests, ["POST /server/start"])
            self.assertIn(f"the server is {state}, not ready", completed.stderr)

    def test_start_follows_its_own_start_and_fails_where_it_settles(self):
        completed, requests = self.call(["start"], [(202, {"state": "starting"}), (200, {"state": "starting"}), (200, {"state": "failed"})])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(requests, ["POST /server/start", "GET /server", "GET /server"])
        self.assertIn("ended failed, not ready", completed.stderr)

    def test_stop_follows_a_stop_already_under_way(self):
        completed, requests = self.call(["stop"], [(200, {"state": "stopping"}), (200, {"state": "stopping"}), (200, {"state": "stopped"})])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(requests, ["POST /server/stop", "GET /server", "GET /server"])

    def test_stop_is_green_when_already_stopped_and_red_when_refused(self):
        completed, requests = self.call(["stop"], [(200, {"state": "stopped"})])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(requests, ["POST /server/stop"])
        completed, requests = self.call(["stop", "--force"], [(409, {"code": "players_online", "players": 2})])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(requests, ["POST /server/stop"])
        self.assertIn("players_online", completed.stderr)

    def test_stop_fails_when_nothing_stopped_and_the_state_is_unknown(self):
        completed, requests = self.call(["stop"], [(200, {"state": "unknown"})])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(requests, ["POST /server/stop"])
        self.assertIn("the server is unknown, not stopped", completed.stderr)


if __name__ == "__main__":
    unittest.main()
