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

# The guard reads plain block YAML. An anchor, alias, merge key, tag or complex key could hide a trigger or a
# permission from it, so no workflow may use one: at the start of a value, a list item or a flow item.
SHORTCUT = r"""(^[ \t]*(-[ \t]+)*|:[ \t]+|[\[{,][ \t]*)([&*!]|<<[ \t]*:)|^[ \t]*\?[ \t]"""


def workflows():
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def triggers(text):
    on = r"""^["']?on["']?[ \t]*:[ \t]*"""
    inline = re.search(r"(?m)" + on + r"(\S.*)$", text)
    if inline:
        return set(re.findall(r"[a-z_]+", inline.group(1)))
    block = re.search(r"(?ms)" + on + r"\n(.*?)(?=^\S)", text + "\nend:\n")
    return set(re.findall(r"(?m)^  " + KEY, block.group(1))) if block else set()


def yaml_shortcuts(text):
    """(line number, line) for every anchor, alias, merge key, tag or complex key outside a block scalar."""
    found, scalar = [], None
    for number, line in enumerate(text.splitlines(), 1):
        # A list item's keys sit after its dash, so that is where its block scalar's indent starts.
        indent = len(re.match(r"^[ \t]*(-[ \t]+)*", line).group(0))
        if scalar is not None and (not line.strip() or len(line) - len(line.lstrip()) > scalar):
            continue
        scalar = None
        code = re.sub(r"\$\{\{.*?\}\}", "x", line)
        code = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "x", code)
        code = re.sub(r"(^|\s)#.*$", "", code).rstrip()
        if re.search(SHORTCUT, code):
            found.append((number, line.strip()))
        if re.search(r"(:[ \t]+|^[ \t]*(-[ \t]+)*)[|>][-+0-9]*$", code):
            scalar = indent
    return found


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

    def test_workflows_are_plain_yaml_the_guard_can_read(self):
        for workflow in workflows():
            with self.subTest(workflow=workflow.name):
                self.assertEqual(yaml_shortcuts(workflow.read_text()), [])

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
        # Anchors, aliases, merge keys, tags and complex keys are refused; shell text in a block scalar is not YAML.
        aliased = "run-name: &push pull_request_target\non: *push\njobs:\n"
        self.assertEqual([n for n, _ in yaml_shortcuts(aliased)], [1, 2])
        shared = "jobs:\n  a:\n    permissions: &oidc\n      id-token: write\n  b:\n    permissions: *oidc\n"
        self.assertEqual([n for n, _ in yaml_shortcuts(shared)], [3, 6])
        for line in ("  - *x", "a: [b, *x]", "a: {b: &x c}", "a: !!str b", "  <<: *x", "? a", "a: !b"):
            self.assertTrue(yaml_shortcuts(line), line)
        plain = "if: ${{ !cancelled() }}\nrun: echo a && b\nb: 'x: *y'\nc: |\n  test ! -f a && rm *.tmp\n  [ -n x ] || echo &\nd: e # *f\n"
        self.assertEqual(yaml_shortcuts(plain), [])
        # Only a lone | or > opens a block scalar, and a list item's sibling keys are still read.
        self.assertTrue(yaml_shortcuts("jobs:\n  b:\n    name: a |\n    permissions: *oidc\n"))
        self.assertTrue(yaml_shortcuts("steps:\n  - run: |\n      echo hi\n    with: *x\n"))


if __name__ == "__main__":
    unittest.main()
