import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import render_backup
from scripts.backup_runtime import validate_settings

ROOT = Path(__file__).resolve().parents[1]


class BackupRenderTests(unittest.TestCase):
    def test_render_binds_exact_revisions_without_resolving_credentials_or_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            (instance / "ops/backup").mkdir(parents=True)
            (instance / "HAVEN_REVISION").write_text("a" * 40 + "\n")
            inputs = json.loads((ROOT / "examples/backup-runtime.json").read_text())
            (instance / "ops/backup/runtime.json").write_text(json.dumps(inputs))
            output = root / "generated.json"
            with patch.object(render_backup, "revision", side_effect=["a" * 40, "b" * 40]):
                render_backup.render(instance, output)
            result = json.loads(output.read_text())
            self.assertEqual(result["public_revision"], "a" * 40)
            self.assertEqual(result["private_revision"], "b" * 40)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            validate_settings(result)
            with patch.object(render_backup, "revision", side_effect=["a" * 40, "b" * 40]), self.assertRaises(ValueError):
                render_backup.render(instance, output)
            self.assertEqual(json.loads(output.read_text()), result)

    def test_self_pinned_or_mismatched_source_inputs_are_refused_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ops/backup").mkdir(parents=True)
            inputs = json.loads((ROOT / "examples/backup-runtime.json").read_text())
            (root / "HAVEN_REVISION").write_text("a" * 40)
            for kind in ("mismatch", "self_pin"):
                values = dict(inputs)
                if kind == "self_pin":
                    values["private_revision"] = "c" * 40
                (root / "ops/backup/runtime.json").write_text(json.dumps(values))
                with patch.object(render_backup, "revision", side_effect=[("d" if kind == "mismatch" else "a") * 40, "b" * 40]), self.assertRaises(Exception):
                    render_backup.render(root, root / "output.json")
                self.assertFalse((root / "output.json").exists())
