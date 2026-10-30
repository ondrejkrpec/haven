"""Private Markdown is opt-in, bounded, escaped and absent from public output."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_private_docs import build
from scripts.pre_push import git
from scripts.verify_private_site import verify


class PrivateDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="haven-private-doc-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "--quiet", "-b", "main")
        git(self.root, "config", "user.name", "Synthetic Tester")
        git(self.root, "config", "user.email", "tester@example.invalid")
        (self.root / "docs").mkdir()
        (self.root / "runbooks").mkdir()
        (self.root / "runbooks/test.md").write_text('# Český návod\n\n<script>bad()</script>\n\n[Section](#český-návod)\n')
        (self.root / "docs/site.json").write_text(json.dumps({"documents": ["runbooks/test.md"]}))
        git(self.root, "add", ".")
        git(self.root, "commit", "--quiet", "-m", "Synthetic private docs")

    def test_separate_output_has_no_undeclared_credentials_or_raw_html(self):
        (self.root / ".env").write_text("SYNTHETIC_DO_NOT_EXPORT")
        output = build(self.root)
        page = (output / "runbooks/test.html").read_text()
        self.assertIn('id="český-návod"', page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertFalse(any(b"SYNTHETIC_DO_NOT_EXPORT" in p.read_bytes() for p in output.rglob("*") if p.is_file()))
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_manifest_cannot_export_untracked_or_non_document_inputs(self):
        for source in [".env", "../secret.md", "config/instance.yaml", "runbooks/missing.md"]:
            (self.root / "docs/site.json").write_text(json.dumps({"documents": [source]}))
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "explicitly tracked"):
                build(self.root)

    def test_private_source_symlink_is_rejected(self):
        source = self.root / "runbooks/test.md"
        source.unlink()
        source.symlink_to(self.root / "docs/site.json")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            build(self.root)

    def test_grouped_navigation_and_real_homepage_keep_links_working(self):
        (self.root / "runbooks/second.md").write_text('# Druhá stránka\n')
        (self.root / "runbooks/test.md").write_text('# Domů\n\n[Druhá](second.md)\n')
        selection = {"documents": ["runbooks/test.md", "runbooks/second.md"],
                     "home": "runbooks/test.md", "navigation": [
                         {"title": "Přehled", "document": "runbooks/test.md"},
                         {"title": "Infrastruktura <unsafe>", "children": [
                             {"title": "Druhá", "document": "runbooks/second.md"}]}]}
        (self.root / "docs/site.json").write_text(json.dumps(selection))
        git(self.root, "add", ".")
        output = build(self.root)
        page = (output / "index.html").read_text()
        self.assertIn('<h1 id="domů">Domů</h1>', page)
        self.assertIn('href="/runbooks/second.html"', page)
        self.assertIn("Infrastruktura &lt;unsafe&gt;", page)
        self.assertNotIn('<details open>', page)
        self.assertIn('<details open>', (output / "runbooks/second.html").read_text())
        self.assertIn('aria-current="page"', page)

    def test_navigation_cannot_admit_unselected_sources_or_hide_selected_pages(self):
        for item in [{"title": "Secret", "document": "config/instance.yaml"},
                     {"title": "Hidden", "children": []}]:
            selection = {"documents": ["runbooks/test.md"], "navigation": [item]}
            (self.root / "docs/site.json").write_text(json.dumps(selection))
            with self.subTest(item=item), self.assertRaises(ValueError):
                build(self.root)

    def test_deployment_verifier_rejects_dirty_or_changed_artifacts(self):
        output = build(self.root)
        metadata = json.loads((output / "publication.json").read_text())
        # Generated output belongs in an ignored private directory.
        (self.root / ".gitignore").write_text("build/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "--quiet", "-m", "Ignore synthetic private output")
        output = build(self.root)
        verify(output, metadata["public_revision"])
        (output / "index.html").write_text("changed after verification")
        with self.assertRaisesRegex(ValueError, "contents differ"):
            verify(output, metadata["public_revision"])
        (self.root / "runbooks/test.md").write_text("uncommitted preview")
        output = build(self.root)
        with self.assertRaisesRegex(ValueError, "clean private sources"):
            verify(output, metadata["public_revision"])
