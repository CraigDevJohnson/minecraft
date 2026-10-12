import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# The repository is public. A fork's pull request runs with a read-only token and no OIDC token, but
# pull_request_target, workflow_run, issue_comment and the other events below run the default branch's workflow
# as main on an event anyone can raise, which is the subject the health role trusts. Only these triggers are allowed.
ALLOWED_TRIGGERS = {"pull_request", "push", "schedule", "workflow_dispatch"}

# The only jobs that may request an OIDC token, each for the one role that trusts it.
OIDC_JOBS = {("deploy.yml", "deploy"), ("server-control.yml", "server"), ("minecraft-health.yml", "health")}

# A mapping key, which YAML also allows in quotes.
KEY = r"""["']?([A-Za-z0-9_-]+)["']?[ \t]*:"""


def workflows():
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def triggers(text):
    on = r"""^["']?on["']?[ \t]*:[ \t]*"""
    inline = re.search(r"(?m)" + on + r"(\S.*)$", text)
    if inline:
        return set(re.findall(r"[a-z_]+", inline.group(1)))
    block = re.search(r"(?ms)" + on + r"\n(.*?)(?=^\S)", text + "\nend:\n")
    return set(re.findall(r"(?m)^  " + KEY, block.group(1))) if block else set()


def oidc_requests(text):
    """(job, line) for every id-token permission, with the job it sits in (None outside any job)."""
    requests, job, in_jobs = [], None, False
    for line in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).rstrip()
        if not line:
            continue
        if re.match(r"^\S", line):
            top = re.match(r"^" + KEY + r"\s*$", line)
            in_jobs, job = bool(top) and top.group(1) == "jobs", None
        named = re.match(r"^  " + KEY + r"\s*$", line)
        if in_jobs and named:
            job = named.group(1)
        if re.search(r"\bid-token\b", line) or re.search(r"\bwrite-all\b", line):
            requests.append((job, line.strip()))
    return requests


class WorkflowTriggerTests(unittest.TestCase):
    def test_the_known_workflows_are_checked(self):
        names = {workflow.name for workflow in workflows()}
        self.assertTrue({"deploy.yml", "server-control.yml", "minecraft-health.yml", "validate.yml"} <= names)

    def test_no_workflow_runs_as_main_on_someone_elses_event(self):
        for workflow in workflows():
            with self.subTest(workflow=workflow.name):
                found = triggers(workflow.read_text())
                self.assertTrue(found, "no triggers parsed")
                self.assertLessEqual(found, ALLOWED_TRIGGERS)

    def test_only_the_three_role_jobs_request_an_oidc_token(self):
        seen = set()
        for workflow in workflows():
            for job, line in oidc_requests(workflow.read_text()):
                with self.subTest(workflow=workflow.name, job=job):
                    self.assertIn((workflow.name, job), OIDC_JOBS)
                    self.assertEqual(line, "id-token: write")
                    seen.add((workflow.name, job))
        self.assertEqual(seen, OIDC_JOBS)

    def test_the_parsers_catch_what_they_guard_against(self):
        self.assertEqual(triggers("on: [push, pull_request_target]\njobs:\n"), {"push", "pull_request_target"})
        self.assertEqual(triggers("on:\n  issue_comment:\n    types: [created]\n  push:\njobs:\n"), {"issue_comment", "push"})
        self.assertEqual(oidc_requests("permissions:\n  id-token: write\njobs:\n  x:\n    runs-on: a\n"), [(None, "id-token: write")])
        self.assertEqual(oidc_requests("jobs:\n  x:\n    permissions: write-all\n"), [("x", "permissions: write-all")])
        # YAML allows quoted keys; they count the same.
        self.assertEqual(triggers('on:\n  push:\n  "pull_request_target":\njobs:\n'), {"push", "pull_request_target"})
        self.assertEqual(triggers("'on':\n  workflow_run:\njobs:\n"), {"workflow_run"})
        quoted = 'jobs:\n  a:\n    runs-on: x\n  "b":\n    permissions:\n      "id-token": write\n'
        self.assertEqual(oidc_requests(quoted), [("b", '"id-token": write')])
        self.assertEqual(oidc_requests("jobs:\n  a:\n    # id-token: write is not needed\n"), [])
        self.assertEqual(oidc_requests("jobs:\n  x:\n    runs-on: a\npermissions: write-all\n"), [(None, "permissions: write-all")])


if __name__ == "__main__":
    unittest.main()
