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
# request's method and path, and each request body it sends. The script reads the body and the status from curl's
# output.
FAKE_CURL = """#!/usr/bin/env bash
method=GET
for ((i = 1; i <= $#; i++)); do
  if [[ "${!i}" == --request ]]; then
    j=$((i + 1)); method="${!j}"
  fi
  if [[ "${!i}" == --data ]]; then
    j=$((i + 1)); printf '%s\\n' "${!j}" >> "$FAKE_CURL_BODIES"
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
    """start, stop and restore end green only once the server is ready or stopped, whoever began the change."""

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
        self.bodies = self.tmp / "bodies"
        self.bodies.write_text("")
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", LAUNCHER_URL="https://abc123.lambda-url.us-west-2.on.aws/",
                   AWS_ACCESS_KEY_ID="AKIAEXAMPLE", AWS_SECRET_ACCESS_KEY="example", FAKE_CURL_SCENARIO=str(scenario),
                   FAKE_CURL_LOG=str(log), FAKE_CURL_BODIES=str(self.bodies))
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

    def test_restore_follows_the_start_until_the_server_is_ready(self):
        completed, requests = self.call(["restore", "--archive", "20261016T031500Z.tar.gz"],
                                        [(202, {"state": "starting"}), (200, {"state": "starting"}), (200, {"state": "ready"})])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(requests, ["POST /server/restore", "GET /server", "GET /server"])
        self.assertEqual([json.loads(line) for line in self.bodies.read_text().splitlines()], [{"archive": "20261016T031500Z.tar.gz"}])

    def test_restore_fails_where_it_settles_when_that_is_not_ready(self):
        completed, requests = self.call(["restore", "--archive", "20261016T031500Z.tar.gz"],
                                        [(202, {"state": "starting"}), (200, {"state": "starting"}), (200, {"state": "failed"})])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(requests, ["POST /server/restore", "GET /server", "GET /server"])
        self.assertIn("ended failed, not ready", completed.stderr)

    def test_restore_is_red_when_refused_or_unconfirmed(self):
        completed, requests = self.call(["restore", "--archive", "20261016T031500Z.tar.gz"], [(409, {"code": "not_stopped", "state": "ready"})])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(requests, ["POST /server/restore"])
        self.assertIn("the restore was refused: not_stopped; the server is ready", completed.stderr)
        completed, requests = self.call(["restore", "--archive", "20261016T031500Z.tar.gz"], [(502, {"code": "restore_unconfirmed"})])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(requests, ["POST /server/restore"])
        self.assertIn("the launcher answered 502", completed.stderr)
        self.assertIn("restore_unconfirmed", completed.stdout)

    def test_restore_refuses_anything_but_an_archive_name_before_any_request(self):
        for action in (["restore"], ["restore", "--archive"], ["restore", "--archive", ""],
                       ["restore", "--archive", "20261016T031500Z.tar.gz.sha256"], ["restore", "--archive", "20261016T031500Z.tar"],
                       ["restore", "--archive", "2026101T031500Z.tar.gz"], ["restore", "--archive", "20261016T031500.tar.gz"],
                       ["restore", "--archive", "s3://bucket/instances/family/crafty/"],
                       ["restore", "--archive", "20261016T031500Z.tar.gz\nx"], ["restore", "--archive", "x\n20261016T031500Z.tar.gz"]):
            with self.subTest(action=action):
                completed, requests = self.call(action, [])
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(requests, [])
                self.assertIn("restore --archive NAME", completed.stderr)

    def test_restore_sends_only_the_name_of_an_s3_uri_or_key(self):
        for archive in ("s3://minecraft-archives/instances/family/crafty/20261016T031500Z.tar.gz",
                        "instances/family/crafty/20261016T031500Z.tar.gz"):
            with self.subTest(archive=archive):
                completed, requests = self.call(["restore", "--archive", archive], [(202, {"state": "starting"}), (200, {"state": "ready"})])
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(requests, ["POST /server/restore", "GET /server"])
                self.assertEqual([json.loads(line) for line in self.bodies.read_text().splitlines()], [{"archive": "20261016T031500Z.tar.gz"}])


if __name__ == "__main__":
    unittest.main()
