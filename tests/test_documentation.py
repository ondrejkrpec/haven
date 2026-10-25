"""Public export boundaries and offline link-integrity failure checks."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.build_docs import ATTACHMENTS, prepare_content
from scripts.check_links import check_site, check_sources
from scripts.public_files import ROOT, public_files


class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="haven-doc-test-")
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "public"
        self.root.mkdir()
        for relative in public_files():
            if relative.parts[0] == "docs" or str(relative) in ATTACHMENTS or str(relative) == "research/h0.1/README.adoc":
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(self.root)], check=True)
        self.output = self.parent / "export"

    def test_export_rewrites_nested_links_and_preserves_attachments(self):
        count = prepare_content(self.root, self.output)
        self.assertGreater(count, 5)
        pages = self.output / "modules/ROOT/pages"
        self.assertIn("xref:ROOT:configuration.adoc[", (pages / "adr/0001-configuration-validation.adoc").read_text())
        self.assertIn("xref:ROOT:local-development.adoc[", (pages / "research/h0.1/README.adoc").read_text())
        self.assertIn("xref:ROOT:attachment$LICENSE.txt[", (pages / "index.adoc").read_text())
        licenses = {"LICENSE": "LICENSE.txt", "research/h0.1/UPSTREAM-LICENSE": "research/h0.1/UPSTREAM-LICENSE.txt"}
        for source in ATTACHMENTS:
            target = self.output / "modules/ROOT/attachments" / licenses.get(source, source.removeprefix("."))
            self.assertEqual((self.root / source).read_bytes(), target.read_bytes())
        # An unborn repository suffices; export must not make a source commit.
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.root), "rev-parse", "--verify", "HEAD"],
                                          capture_output=True).returncode, 0)

    def test_private_sibling_and_ignored_inputs_are_absent(self):
        private = self.parent / "haven-private"
        private.mkdir()
        marker = "SYNTHETIC_PRIVATE_BUILD_SENTINEL"
        (private / "note.adoc").write_text(marker)
        (self.root / ".gitignore").write_text("docs/ignored.adoc\n")
        (self.root / "docs/ignored.adoc").write_text(marker)
        prepare_content(self.root, self.output)
        self.assertFalse(any(marker.encode() in path.read_bytes() for path in self.output.rglob("*") if path.is_file()))

    def test_symlink_to_private_input_is_rejected(self):
        private = self.parent / "private.adoc"
        private.write_text("SYNTHETIC_PRIVATE_BUILD_SENTINEL")
        (self.root / "docs/unsafe.adoc").symlink_to(private)
        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare_content(self.root, self.output)

    def test_escaping_undeclared_and_missing_targets_fail(self):
        for target in ["../../private.adoc", "../undeclared.txt", "missing.adoc"]:
            with self.subTest(target=target):
                (self.root / "docs/extra.adoc").write_text(f"= Extra\n\nlink:{target}[Target]\n")
                with self.assertRaisesRegex(ValueError, "escapes|Undeclared"):
                    prepare_content(self.root, self.output)

    def test_include_indirection_is_rejected(self):
        (self.root / "docs/extra.adoc").write_text("= Extra\n\ninclude::../../private.adoc[]\n")
        with self.assertRaisesRegex(ValueError, "Includes are unsupported"):
            prepare_content(self.root, self.output)

    def test_source_link_check_detects_a_missing_markdown_target(self):
        (self.root / "extra.md").write_text("[Missing](missing.md)\n")
        with self.assertRaisesRegex(ValueError, "Broken public source link"):
            check_sources(self.root)

    def test_generated_link_check_rejects_missing_files_and_fragments(self):
        site = self.parent / "site"
        site.mkdir()
        (site / "target.html").write_text('<h1 id="section">Target</h1>')
        (site / "index.html").write_text('<a href="target.html#section">Valid</a>')
        self.assertEqual(check_site(site), 1)
        for target in ["missing.html", "target.html#missing", "../private.adoc"]:
            with self.subTest(target=target):
                (site / "index.html").write_text(f'<a href="{target}">Invalid</a>')
                with self.assertRaisesRegex(ValueError, "Broken generated"):
                    check_site(site)


if __name__ == "__main__":
    unittest.main()
