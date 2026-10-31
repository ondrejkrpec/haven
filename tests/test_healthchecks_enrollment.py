"""Synthetic coverage of private enrollment boundaries; never contact Healthchecks."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings

from scripts import enroll_healthchecks as enrollment

URLS = (
    "https://hc-ping.com/00000000-0000-4000-8000-000000000001",
    "https://hc-ping.com/00000000-0000-4000-8000-000000000002",
)


class HealthchecksEnrollmentTests(unittest.TestCase):
    def test_private_files_and_no_overwrite_under_permissive_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "urls"
            old_umask = os.umask(0)
            try:
                with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                    enrollment.collect(target, prompt=lambda prompt: URLS["service" in prompt])
            finally:
                os.umask(old_umask)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertEqual(set(p.name for p in target.iterdir()), set(enrollment.NAMES))
            for name, value in zip(enrollment.NAMES, URLS, strict=True):
                self.assertEqual((target / name).read_text(), value + "\n")
                self.assertEqual(stat.S_IMODE((target / name).stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                enrollment.collect(target, prompt=lambda _: self.fail("must reject before input"))

    def test_invalid_or_duplicate_urls_leave_no_destination(self):
        bad = [URLS[0] + "/fail", URLS[0] + "?x=1", URLS[0] + "#x", "http" + URLS[0][5:],
               URLS[0].replace("hc-ping.com", "hc-ping.com.evil.test"),
               URLS[0].replace("hc-ping.com", "user@hc-ping.com"), " " + URLS[0],
               URLS[0] + "\n", "https://hc-ping.com/project-key/check"]
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "urls"
            for value in [*bad, URLS[1]]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    values = iter((value, URLS[1]))
                    enrollment.collect(target, prompt=lambda _: next(values))
                self.assertFalse(target.exists())

    def test_git_roots_worktrees_and_bare_repositories_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for marker in ("normal", "worktree", "bare"):
                repository = root / marker
                repository.mkdir()
                if marker == "normal":
                    (repository / ".git").mkdir()
                    (repository / ".git/HEAD").touch()
                elif marker == "worktree":
                    (repository / ".git").write_text("gitdir: synthetic")
                else:
                    for name in ("objects", "refs"):
                        (repository / name).mkdir()
                    (repository / "HEAD").touch()
                (repository / "ignored-build").mkdir()
                with self.assertRaises(ValueError):
                    enrollment.collect(repository / "ignored-build/urls",
                                       prompt=lambda _: self.fail("must reject before input"))
                self.assertFalse((repository / "ignored-build/urls").exists())

    def test_symlinked_ancestors_and_destinations_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "actual").mkdir()
            (root / "link").symlink_to(root / "actual", target_is_directory=True)
            (root / "destination").symlink_to(root / "absent")
            for target in (root / "link/urls", root / "destination", Path("relative"),
                           root / "actual/../urls"):
                with self.assertRaises((ValueError, OSError)):
                    enrollment.collect(target, prompt=lambda _: self.fail("unsafe path"))
            self.assertEqual(list((root / "actual").iterdir()), [])

    def test_untrusted_parent_and_changed_directory_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o777)
            unsafe.chmod(0o777)
            with self.assertRaises(ValueError):
                enrollment.collect(unsafe / "urls", prompt=lambda _: self.fail("unsafe parent"))
            parent, outside = root / "parent", root / "outside"
            parent.mkdir()
            outside.mkdir()
            count = 0

            def replace_during_input(prompt):
                nonlocal count
                if count == 0:
                    parent.rename(root / "original")
                    parent.symlink_to(outside, target_is_directory=True)
                value = URLS[count]
                count += 1
                return value

            with self.assertRaises((ValueError, OSError)):
                enrollment.collect(parent / "urls", prompt=replace_during_input)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list((root / "original").iterdir()), [])

    def test_cancellation_and_second_file_failure_clean_only_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "urls"
            with self.assertRaises(KeyboardInterrupt):
                enrollment.collect(target, prompt=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
            self.assertFalse(target.exists())
            original_open = os.open

            def fail_second_file(path, flags, *args, **kwargs):
                if path == "service.url":
                    raise OSError("SYNTHETIC_PRIVATE_ERROR")
                return original_open(path, flags, *args, **kwargs)

            with patch("scripts.private_enrollment.os.open", side_effect=fail_second_file):
                with self.assertRaises(OSError):
                    enrollment.collect(target, prompt=lambda prompt: URLS["service" in prompt])
            self.assertFalse(target.exists())

    def test_cli_requires_tty_and_never_echoes_private_errors(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with patch.object(enrollment.sys.stdin, "isatty", return_value=False):
                with patch.object(enrollment, "collect", side_effect=AssertionError("must not read")):
                    self.assertEqual(enrollment.main(["--directory", "/synthetic"]), 1)
            with patch.object(enrollment.sys.stdin, "isatty", return_value=True), \
                    patch.object(enrollment.sys.stderr, "isatty", return_value=True), \
                    patch.object(enrollment, "collect", side_effect=OSError(URLS[0])):
                self.assertEqual(enrollment.main(["--directory", "/synthetic"]), 1)
        self.assertNotIn(URLS[0], output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_getpass_echo_fallback_is_an_error(self):
        def fallback(*args, **kwargs):
            warnings.warn("echo unavailable", enrollment.getpass.GetPassWarning)
            self.fail("warning must become an error before reading")
        with patch.object(enrollment.getpass, "getpass", side_effect=fallback):
            with self.assertRaises(enrollment.getpass.GetPassWarning):
                enrollment.hidden_url("hidden: ")

    def test_cli_success_and_bad_arguments_do_not_print_urls(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "urls"
            with redirect_stdout(output), redirect_stderr(output):
                with self.assertRaises(SystemExit) as stopped:
                    enrollment.main(["--directory", str(target), "--url", URLS[0]])
                self.assertEqual(stopped.exception.code, 2)
                with patch.object(enrollment.sys.stdin, "isatty", return_value=True), \
                        patch.object(enrollment.sys.stderr, "isatty", return_value=True), \
                        patch.object(enrollment.getpass, "getpass", side_effect=URLS):
                    self.assertEqual(enrollment.main(["--directory", str(target)]), 0)
            for value in URLS:
                self.assertNotIn(value, output.getvalue())
            self.assertIn("No request was sent", output.getvalue())


if __name__ == "__main__":
    unittest.main()
