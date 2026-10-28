"""Ansible checks must also work through Git's nonblocking hook output."""

import subprocess
import sys
import unittest

from scripts.check import run


class CheckRunnerTests(unittest.TestCase):
    def test_buffered_child_has_blocking_io_under_nonblocking_parent(self):
        program = """
import os, sys
from scripts.check import run
os.set_blocking(1, False)
os.set_blocking(2, False)
run(sys.executable, '-c', 'import os; assert all(os.get_blocking(fd) for fd in (0,1,2)); print("child passed")', buffer_output=True)
assert not os.get_blocking(1) and not os.get_blocking(2)
"""
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("child passed", result.stdout)

    def test_buffering_preserves_failure_status_and_diagnostics(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            run(sys.executable, "-c", "print('synthetic check failure'); raise SystemExit(17)",
                buffer_output=True)
        self.assertEqual(caught.exception.returncode, 17)
