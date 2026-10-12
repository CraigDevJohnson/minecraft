import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plan_digest import digest  # noqa: E402


def plan(*changes, outputs=None):
    return {"resource_changes": [{"address": address, "change": change} for address, change in changes],
            "output_changes": outputs or {}}


UPDATE = ("aws_iam_role.launcher", {"actions": ["update"], "before": {"name": "launcher", "tags": {}},
                                    "after": {"name": "launcher", "tags": {"owner": "minecraft"}}, "after_unknown": {}})
HOST = ("aws_instance.host", {"actions": ["no-op"], "before": {"instance_state": "running"}, "after": {"instance_state": "running"}})


class PlanDigestTests(unittest.TestCase):
    """The digest names what an apply would do, so an apply run can require the plan Craig read."""

    def test_the_digest_covers_the_changes(self):
        base = digest(plan(UPDATE))
        self.assertRegex(base, r"^[0-9a-f]{16}$")
        self.assertEqual(base, digest(plan(UPDATE)))
        other = ("aws_iam_role.launcher", {**UPDATE[1], "after": {"name": "launcher", "tags": {"owner": "someone"}}})
        replaced = ("aws_iam_role.launcher", {**UPDATE[1], "actions": ["delete", "create"]})
        added = ("aws_lambda_permission.site", {"actions": ["create"], "before": None, "after": {"action": "lambda:InvokeFunctionUrl"}})
        output = {"launchers": {"actions": ["update"], "after": {"dev": "url"}}}
        for changed in [plan(other), plan(replaced), plan(UPDATE, added), plan(UPDATE, outputs=output), plan()]:
            self.assertNotEqual(base, digest(changed))

    def test_the_present_does_not_change_it(self):
        # Starting or stopping the server between the plan run and the apply run changes only what the plan
        # starts from: unchanged resources and the values before each change.
        base = digest(plan(UPDATE, HOST))
        stopped = ("aws_instance.host", {"actions": ["no-op"], "before": {"instance_state": "stopped"}, "after": {"instance_state": "stopped"}})
        drifted = ("aws_iam_role.launcher", {**UPDATE[1], "before": {"name": "launcher", "tags": {"edited": "by hand"}}})
        self.assertEqual(base, digest(plan(UPDATE, stopped)))
        self.assertEqual(base, digest(plan(drifted)))
        self.assertEqual(base, digest(plan(HOST, UPDATE)))

    def test_the_script_prints_only_the_digest(self):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "plan_digest.py")], input=json.dumps(plan(UPDATE)),
                                   capture_output=True, text=True, check=False)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, digest(plan(UPDATE)) + "\n", ""))
        for unreadable in ["", "not json", "[]"]:
            completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "plan_digest.py")], input=unreadable,
                                       capture_output=True, text=True, check=False)
            self.assertEqual((completed.returncode, completed.stdout), (1, ""), unreadable)


if __name__ == "__main__":
    unittest.main()
