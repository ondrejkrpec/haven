"""Synthetic recovery password enrollment; no real password or repository."""

from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import enroll_restic

VALUE = "synthetic-recovery-" + "x" * 32


class ResticEnrollmentTests(unittest.TestCase):
    def test_private_single_file_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recovery"
            enroll_restic.collect(path, prompt=lambda _: VALUE)
            self.assertEqual(list(x.name for x in path.iterdir()), ["password"])
            self.assertEqual((path / "password").read_text(), VALUE + "\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((path / "password").stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                enroll_restic.collect(path, prompt=lambda _: self.fail("existing destination"))

    def test_mismatch_short_or_multiline_value_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recovery"
            for first, second in ((VALUE, VALUE + "y"), ("short", "short"),
                                  (VALUE + "\n", VALUE + "\n"), ("é" * 32, "é" * 32)):
                values = iter((first, second))
                with self.assertRaises(ValueError):
                    enroll_restic.collect(path, prompt=lambda _: next(values))
                self.assertFalse(path.exists())

    def test_cli_requires_terminal_and_diagnostics_do_not_repeat_password(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with patch.object(enroll_restic.sys.stdin, "isatty", return_value=False), \
                    patch.object(enroll_restic, "collect", side_effect=AssertionError("no input")):
                self.assertEqual(enroll_restic.main(["--directory", "/synthetic"]), 1)
            with self.assertRaises(SystemExit):
                enroll_restic.main(["--directory", "/synthetic", "--password", VALUE])
            with patch.object(enroll_restic.sys.stdin, "isatty", return_value=True), \
                    patch.object(enroll_restic.sys.stderr, "isatty", return_value=True), \
                    patch.object(enroll_restic, "collect", side_effect=OSError(VALUE)):
                self.assertEqual(enroll_restic.main(["--directory", "/synthetic"]), 1)
        self.assertNotIn(VALUE, output.getvalue())


if __name__ == "__main__":
    unittest.main()
