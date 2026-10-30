"""Generated manifests must scan cleanly without suppressing new credential findings."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.public_files import ROOT
from scripts.publish_docs import copy_site


class PublicationScanTests(unittest.TestCase):
    def scan(self, directory):
        return subprocess.run([str(ROOT / "build/tools/gitleaks"), "dir", str(directory),
                               "--config", str(ROOT / ".gitleaks.toml"), "--redact",
                               "--no-banner", "--ignore-gitleaks-allow"],
                              cwd=ROOT, capture_output=True, text=True, check=False)

    def test_access_filename_digest_is_not_misclassified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "site"
            site.mkdir()
            (site / "index.html").write_text("<h1>Synthetic documentation</h1>")
            (site / "private-access.html").write_text("<h1>Synthetic access guide</h1>")
            artifact = root / "artifact"
            artifact.mkdir()
            copy_site(site, artifact, "synthetic")
            result = self.scan(artifact)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = json.loads((artifact / "publication.json").read_text())
            self.assertEqual(len(metadata["files"]), 2)

    def test_new_manifest_credential_is_still_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Deterministic synthetic control, never a real credential.
            value = hashlib.sha256(b"synthetic credential scanner positive control").hexdigest()
            (root / "publication.json").write_text(json.dumps({"access_token": value}))
            result = self.scan(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("leaks found", result.stderr)
