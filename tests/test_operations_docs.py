from __future__ import annotations

import re
import unittest
from pathlib import Path

DOCS = sorted((Path(__file__).resolve().parents[1] / "docs/operations").glob("*.md"))


class OperationsDocsLint(unittest.TestCase):
    """Guard documented commands whose mistakes silently disable safety checks."""

    def test_cloudtrail_readonly_filters_compare_strings(self) -> None:
        # LookupEvents returns ReadOnly as the string "true"/"false"; a JMESPath boolean never matches.
        for doc in DOCS:
            for match in re.finditer(r"ReadOnly\s*==\s*(\S+?)\]", doc.read_text()):
                with self.subTest(doc=doc.name, filter=match.group(0)):
                    self.assertRegex(match.group(1), r"^('(true|false)'|`\"(true|false)\"`)$")


if __name__ == "__main__":
    unittest.main()
