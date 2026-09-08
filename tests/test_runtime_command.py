from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import minecraft_instance as runtime  # noqa: E402


OUTPUT_LIMIT = 1024 * 1024


class RuntimeCommandTests(unittest.TestCase):
    def run_python(self, source, *arguments, timeout=3, input_text=None):
        return runtime.command([sys.executable, "-c", source, *arguments],
                               timeout=timeout, input_text=input_text)

    def test_exact_stdout_byte_limit_is_accepted(self):
        output = self.run_python("import sys; sys.stdout.buffer.write(b'x' * 1048576)")
        self.assertEqual(len(output), OUTPUT_LIMIT)
        self.assertEqual(set(output), {"x"})

    def test_overflow_is_rejected_before_a_sleeping_process_exits(self):
        cases = {
            "stdout": "sys.stdout.buffer.write(b'x' * 1048577); sys.stdout.flush()",
            "stderr": "sys.stderr.buffer.write(b'x' * 1048577); sys.stderr.flush()",
            "combined": (
                "sys.stdout.buffer.write(b'x' * 524289); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'x' * 524289); sys.stderr.flush()"
            ),
            "multibyte stdout": "sys.stdout.buffer.write(('雪' * 349526).encode()); sys.stdout.flush()",
        }
        for name, writes in cases.items():
            with self.subTest(stream=name), self.assertRaises(runtime.OperationError) as failure:
                self.run_python(f"import sys, time; {writes}; time.sleep(2)", timeout=0.5)
            self.assertEqual(str(failure.exception), "command output exceeded its limit")

    def test_duplex_io_preserves_split_unicode_and_large_input_but_discards_stderr(self):
        request = json.dumps({"receipt": "雪🌲" * 200000}, ensure_ascii=False)
        source = r'''
import os, sys
os.write(1, b'{"text":"\xe9')
sys.stderr.buffer.write(b'private stderr\xff' * 8192)
sys.stderr.flush()
request = sys.stdin.buffer.read()
os.write(1, b'\x9b\xaa\xf0\x9f\x8c\xb2","input_bytes":' + str(len(request)).encode() + b'}\n')
'''
        output = self.run_python(source, input_text=request)
        self.assertEqual(json.loads(output), {"text": "雪🌲", "input_bytes": len(request.encode("utf-8"))})
        self.assertNotIn("private stderr", output)

    def test_child_can_close_stdin_before_consuming_the_request(self):
        output = self.run_python("import os; os.close(0); os.write(1, b'closed\\n')",
                                 input_text="x" * (2 * OUTPUT_LIMIT))
        self.assertEqual(output, "closed\n")

    def test_invalid_stdout_utf8_is_a_sanitized_operation_error(self):
        with self.assertRaises(runtime.OperationError) as failure:
            self.run_python("import os; os.write(1, b'private output\\xff')")
        self.assertEqual(str(failure.exception), "command returned invalid UTF-8 output")

    def test_timeout_applies_after_both_output_pipes_close(self):
        with self.assertRaises(runtime.OperationError) as failure:
            self.run_python("import os, time; os.close(1); os.close(2); time.sleep(2)", timeout=0.3)
        self.assertEqual(str(failure.exception), f"{Path(sys.executable).name} command timed out")

    def test_timeout_kills_descendant_holding_pipes_after_leader_exits(self):
        self.assert_process_group_cleaned_up(overflow=False)

    def test_output_limit_kills_the_process_group(self):
        self.assert_process_group_cleaned_up(overflow=True)

    def assert_process_group_cleaned_up(self, *, overflow):
        # A delayed file write proves whether the descendant survived, without
        # sending signals to a PID that could have been reused after the test.
        descendant = """
import pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text('ready')
time.sleep(1)
pathlib.Path(sys.argv[2]).write_text('survived')
"""
        leader = """
import pathlib, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 1
while not pathlib.Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.005)
if sys.argv[4] == 'overflow':
    sys.stdout.buffer.write(b'x' * 1048577)
    sys.stdout.flush()
    time.sleep(2)
"""
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            survived = Path(directory) / "survived"
            with self.assertRaises(runtime.OperationError) as failure:
                self.run_python(leader, descendant, str(ready), str(survived),
                                "overflow" if overflow else "exit", timeout=0.4)
            expected = ("command output exceeded its limit" if overflow else
                        f"{Path(sys.executable).name} command timed out")
            self.assertEqual(str(failure.exception), expected)
            self.assertTrue(ready.exists(), "descendant did not reach the cleanup fixture")
            time.sleep(1.1)
            self.assertFalse(survived.exists(), "descendant survived command cleanup")

    def test_nonzero_failure_exposes_neither_output_nor_arguments(self):
        captured_stdout, captured_stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            with self.assertRaises(runtime.OperationError) as failure:
                self.run_python("import sys; print('private stdout'); "
                                "print('private stderr', file=sys.stderr); sys.exit(7)", "private argument")
        self.assertEqual(str(failure.exception), f"{Path(sys.executable).name} command failed (exit 7)")
        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertEqual(captured_stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
