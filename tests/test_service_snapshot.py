"""Application rollback receipts cover only fixed files and detect tampering before restore."""

import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from scripts import service_snapshot


class ServiceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "config.yaml"
        self.file.write_text("synthetic configuration")
        self.file.chmod(0o600)
        self.saved = self.root / "receipt"
        self.saved.mkdir(mode=0o700)
        self.scope = (str(self.file), str(self.root / "absent.service"))

    def take_snapshot(self):
        inactive = subprocess.CompletedProcess([], 3, stdout="inactive", stderr="")
        with patch.object(service_snapshot, "FILES", self.scope):
            with patch.object(service_snapshot.tempfile, "mkdtemp", return_value=str(self.saved)):
                with patch.object(service_snapshot, "systemctl", return_value=inactive), \
                        patch.object(service_snapshot.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=123)), \
                        patch.object(service_snapshot.subprocess, "run", return_value=inactive):
                    service_snapshot.snapshot()

    def test_snapshot_preserves_contents_permissions_and_absence_without_auth(self):
        self.take_snapshot()
        with patch.object(service_snapshot, "FILES", self.scope):
            manifest = service_snapshot.read_manifest(self.saved)
        self.assertEqual((self.saved / "0").read_text(), self.file.read_text())
        self.assertEqual(manifest["files"][0]["mode"], 0o600)
        self.assertFalse(manifest["files"][1]["present"])
        self.assertFalse(manifest["active"])
        self.assertTrue(all("auth.json" not in name for name in service_snapshot.FILES))

    def test_changed_backup_and_broadened_scope_are_rejected(self):
        self.take_snapshot()
        with patch.object(service_snapshot, "FILES", self.scope):
            (self.saved / "0").write_text("changed contents")
            with self.assertRaisesRegex(ValueError, "contents differ"):
                service_snapshot.read_manifest(self.saved)
            (self.saved / "0").write_text(self.file.read_text())
            path = self.saved / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["files"][0]["path"] = "/etc/ssh/sshd_config"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "scope differs"):
                service_snapshot.read_manifest(self.saved)

    def test_untrusted_snapshot_is_rejected_before_service_mutation(self):
        with patch.object(service_snapshot, "systemctl") as service:
            with self.assertRaisesRegex(ValueError, "Untrusted"):
                service_snapshot.restore(self.saved)
        service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
