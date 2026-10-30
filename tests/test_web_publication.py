"""Exercise publication against disposable Git remotes, never a live service."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pre_push import git
from scripts.publish_docs import copy_site, publish


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="haven-publication-test-")
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "source"
        self.remote = self.parent / "remote.git"
        self.root.mkdir()
        git(self.remote.parent, "init", "--quiet", "--bare", str(self.remote))
        git(self.root, "init", "--quiet", "-b", "main")
        git(self.root, "config", "user.name", "Synthetic Tester")
        git(self.root, "config", "user.email", "tester@example.invalid")
        (self.root / ".gitignore").write_text("build/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "--quiet", "-m", "Synthetic source")
        git(self.root, "remote", "add", "origin", str(self.remote))
        git(self.root, "push", "--quiet", "origin", "main")
        self.site = self.root / "build/site"
        self.site.mkdir(parents=True)
        (self.site / "index.html").write_text('<h1 id="hello">Synthetic site</h1>')

    def checked_publish(self, check=None):
        original = subprocess.run
        def run(command, *args, **kwargs):
            if list(command)[1:] == ["-m", "scripts.check"]:
                if check:
                    check()
                return subprocess.CompletedProcess(command, 0)
            return original(command, *args, **kwargs)
        with patch("scripts.publish_docs.subprocess.run", side_effect=run):
            return publish(self.root)

    def test_only_generated_public_files_and_revision_are_pushed(self):
        private = self.parent / "private"
        private.mkdir()
        (private / "secret.txt").write_text("SYNTHETIC_PRIVATE_SENTINEL")
        first = self.checked_publish()
        self.assertEqual(git(self.remote, "show", first + ":index.html"), (self.site / "index.html").read_text())
        metadata = json.loads(git(self.remote, "show", first + ":publication.json"))
        self.assertEqual(metadata["source_revision"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(set(metadata["files_sha256"]), {"index.html"})
        self.assertEqual(set(git(self.remote, "ls-tree", "--name-only", first).splitlines()),
                         {".nojekyll", "index.html", "publication.json"})
        second = self.checked_publish()
        self.assertEqual(git(self.remote, "rev-parse", second + "^"), first)

    def test_topic_dirty_and_stale_main_are_rejected(self):
        git(self.root, "switch", "--quiet", "-c", "topic")
        with self.assertRaisesRegex(ValueError, "main branch"):
            self.checked_publish()
        git(self.root, "switch", "--quiet", "main")
        (self.root / "unreviewed").write_text("synthetic")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            self.checked_publish()
        git(self.root, "add", "unreviewed")
        git(self.root, "commit", "--quiet", "-m", "Unpushed synthetic change")
        with self.assertRaisesRegex(ValueError, "Remote main changed"):
            self.checked_publish()

    def test_changes_during_checks_abort_before_publication(self):
        def mutate():
            (self.root / "concurrent").write_text("synthetic")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            self.checked_publish(mutate)
        self.assertEqual(git(self.remote, "for-each-ref", "refs/heads/gh-pages"), "")

    def test_symlink_and_hidden_generated_inputs_are_rejected(self):
        destination = self.parent / "artifact"
        destination.mkdir()
        for name in [".env", "leak.html"]:
            with self.subTest(name=name):
                target = self.site / name
                if name == ".env":
                    target.write_text("synthetic")
                else:
                    target.symlink_to(self.root / ".gitignore")
                with self.assertRaisesRegex(ValueError, "hidden file or symlink"):
                    copy_site(self.site, destination, "synthetic")
                target.unlink()
