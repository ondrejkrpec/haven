"""Check push gating against real disposable Git repositories."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pre_push import check_push, git


class PushTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="haven-push-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "public"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(self.root)], check=True)
        (self.root / "tracked.txt").write_text("public\n")
        self.commit(self.root)
        self.head = git(self.root, "rev-parse", "HEAD")
        self.line = f"refs/heads/main {self.head} refs/heads/main {'0' * 40}\n"
        self.receipt = self.root / ".git/haven-checks/last-success.json"

    def commit(self, root):
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Haven fixture", "-c",
                        "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "-c",
                        "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Synthetic fixture"], check=True)

    def check(self, lines=None):
        check_push([self.line] if lines is None else lines, root=self.root, public=self.root)

    def test_success_receipt_binds_the_checked_revision(self):
        with patch("scripts.pre_push.run_check") as runner:
            self.check()
        runner.assert_called_once()
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["revision"], self.head)
        self.assertEqual(receipt["public_revision"], self.head)
        self.assertEqual(receipt["result"], "passed")
        self.assertFalse(git(self.root, "status", "--porcelain"))

    def test_dirty_or_untracked_sources_block_checks(self):
        for path in ["tracked.txt", "untracked.txt"]:
            with self.subTest(path=path), patch("scripts.pre_push.run_check") as runner:
                (self.root / path).write_text("changed\n")
                with self.assertRaisesRegex(ValueError, "clean checkout"):
                    self.check()
                runner.assert_not_called()
                if path == "tracked.txt":
                    (self.root / path).write_text("public\n")
                else:
                    (self.root / path).unlink()

    def test_check_failure_prevents_success_receipt(self):
        with patch("scripts.pre_push.run_check", side_effect=subprocess.CalledProcessError(1, "check")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.check()
        self.assertFalse(self.receipt.exists())

    def test_checkout_changed_during_checks_is_rejected(self):
        def mutate(*args):
            (self.root / "tracked.txt").write_text("changed during checks\n")
        with patch("scripts.pre_push.run_check", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "clean checkout"):
                self.check()
        self.assertFalse(self.receipt.exists())

    def test_push_of_a_different_commit_is_rejected(self):
        (self.root / "tracked.txt").write_text("next commit\n")
        self.commit(self.root)
        with patch("scripts.pre_push.run_check") as runner:
            with self.assertRaisesRegex(ValueError, "Check out the revision"):
                self.check()
            runner.assert_not_called()

    def test_empty_push_and_deletion_need_no_source_checks(self):
        with patch("scripts.pre_push.run_check") as runner:
            self.check([])
            self.check([f"(delete) {'0' * 40} refs/heads/old {self.head}\n"])
            runner.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_private_instance_requires_matching_public_revision(self):
        private = self.root.parent / "private"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(private)], check=True)
        (private / "HAVEN_REVISION").write_text("incorrect\n")
        self.commit(private)
        revision = git(private, "rev-parse", "HEAD")
        line = f"refs/heads/main {revision} refs/heads/main {'0' * 40}\n"
        with patch("scripts.pre_push.run_check") as runner:
            with self.assertRaisesRegex(ValueError, "HAVEN_REVISION"):
                check_push([line], root=private, public=self.root, instance=True)
            runner.assert_not_called()
        (private / "HAVEN_REVISION").write_text(self.head + "\n")
        self.commit(private)
        revision = git(private, "rev-parse", "HEAD")
        line = f"refs/heads/main {revision} refs/heads/main {'0' * 40}\n"
        with patch("scripts.pre_push.run_check") as runner:
            check_push([line], root=private, public=self.root, instance=True)
        self.assertEqual(runner.call_count, 4)
        self.assertIn(str(private / "config/instance.yaml"), runner.call_args_list[1].args[0])
        self.assertFalse((self.root / ".git/haven-checks/last-success.json").exists())


if __name__ == "__main__":
    unittest.main()
