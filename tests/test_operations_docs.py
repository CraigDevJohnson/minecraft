from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

DOCS = sorted((Path(__file__).resolve().parents[1] / "docs/operations").glob("*.md"))


class OperationsDocsLint(unittest.TestCase):
    """Guard documented commands whose mistakes silently disable safety checks."""

    def attachment_lookup_filter(self) -> str:
        infrastructure = next(doc for doc in DOCS if doc.name == "infrastructure.md")
        blocks = [block for block in re.findall(r"```sh\n(.*?)\n```", infrastructure.read_text(), re.S)
                  if "AttributeKey=EventId" in block]
        self.assertEqual(len(blocks), 1)
        syntax = subprocess.run(["bash", "-n"], input=blocks[0], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        expression = re.search(r"\|\s+jq -e '([^']+)' \|\| exit 1", blocks[0])
        self.assertIsNotNone(expression)
        return expression.group(1)

    @unittest.skipUnless(shutil.which("jq"), "jq is needed to execute the documented projection")
    def test_attachment_lookup_preserves_four_fields_and_omits_private_event_details(self) -> None:
        expression = self.attachment_lookup_filter()
        for attach_time in (1791026697873, "2026-10-03T11:24:57.873Z"):
            expected = {"requestParameters": {"instanceId": "fixture-host"},
                        "responseElements": {"instanceId": "fixture-host", "volumeId": "fixture-volume",
                                             "attachTime": attach_time}}
            event = {**expected, "userIdentity": {"arn": "must-not-be-output"},
                     "sourceIPAddress": "192.0.2.1"}
            with self.subTest(attach_time=attach_time):
                result = subprocess.run(["jq", "-e", expression], input=json.dumps(event), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    @unittest.skipUnless(shutil.which("jq"), "jq is needed to execute the documented projection")
    def test_attachment_lookup_refuses_missing_or_wrongly_typed_evidence(self) -> None:
        expression = self.attachment_lookup_filter()
        event = {"requestParameters": {"instanceId": "fixture-host"},
                 "responseElements": {"instanceId": "fixture-host", "volumeId": "fixture-volume",
                                      "attachTime": 1791026697873}}
        fields = (("requestParameters", "instanceId"), ("responseElements", "instanceId"),
                  ("responseElements", "volumeId"), ("responseElements", "attachTime"))
        for parent, field in fields:
            for value in (None, False, []):
                invalid = copy.deepcopy(event)
                if value is None:
                    del invalid[parent][field]
                else:
                    invalid[parent][field] = value
                with self.subTest(parent=parent, field=field, value=value):
                    result = subprocess.run(["jq", "-e", expression], input=json.dumps(invalid), capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")

    def test_cloudtrail_readonly_filters_compare_strings(self) -> None:
        # LookupEvents returns ReadOnly as the string "true"/"false"; a JMESPath boolean never matches.
        for doc in DOCS:
            for match in re.finditer(r"ReadOnly\s*==\s*(\S+?)\]", doc.read_text()):
                with self.subTest(doc=doc.name, filter=match.group(0)):
                    self.assertRegex(match.group(1), r"^('(true|false)'|`\"(true|false)\"`)$")

    def test_zero_fill_confirmation_never_comes_from_host_storage_config(self) -> None:
        # storage.conf names whatever disk the current host has, so it cannot confirm the proven volume.
        for doc in DOCS:
            with self.subTest(doc=doc.name):
                self.assertNotRegex(doc.read_text(), r"--confirm-volume-id \"\$DATA_VOLUME_ID\"")


if __name__ == "__main__":
    unittest.main()
