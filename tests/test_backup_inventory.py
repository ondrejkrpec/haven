"""Inventory reports bounded metadata without disclosing private names/content or following links."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.backup_inventory import inventory


class BackupInventoryTests(unittest.TestCase):
    def test_metadata_only_and_external_links_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "instance"
            home.mkdir()
            (home / "credentials").mkdir()
            token = home / "credentials/telegram-bot-token"
            token.write_text("SYNTHETIC_PRIVATE_VALUE")
            token.chmod(0o600)
            (home / "PRIVATE_FILENAME").write_bytes(b"unknown")
            outside = base / "outside"
            outside.mkdir()
            (outside / "PRIVATE_EXTERNAL_FILENAME").write_bytes(b"external" * 100)
            (home / "cache").symlink_to(outside, target_is_directory=True)
            with patch("builtins.open", side_effect=AssertionError("content opened")):
                result = inventory(home)
            self.assertTrue(result["complete"])
            self.assertEqual(result["groups"]["rebuildable"]["symlinks"], 1)
            self.assertEqual(result["groups"]["rebuildable"]["files"], 0)
            self.assertEqual(result["groups"]["credentials"]["bytes"], token.stat().st_size)
            self.assertEqual(result["known_files"]["credentials/telegram-bot-token"]["mode"], "0o600")
            self.assertNotIn("PRIVATE_", json.dumps(result))
            self.assertEqual(result["known_files"]["state.db"], {"present": False})

    def test_known_path_cannot_escape_via_linked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home, outside = base / "instance", base / "outside"
            home.mkdir()
            outside.mkdir()
            (outside / "telegram-bot-token").write_bytes(b"synthetic")
            (home / "credentials").symlink_to(outside, target_is_directory=True)
            result = inventory(home)
            self.assertIsNone(result["known_files"]["credentials/telegram-bot-token"]["present"])
            self.assertTrue(result["known_files"]["credentials/telegram-bot-token"]["parent_unavailable"])

    def test_limits_and_access_errors_cannot_report_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "one").write_bytes(b"1")
            (home / "two").mkdir()
            self.assertFalse(inventory(home, max_entries=1)["complete"])
            self.assertFalse(inventory(home, max_depth=0)["complete"])
            with patch("scripts.backup_inventory.os.scandir", side_effect=PermissionError):
                result = inventory(home)
            self.assertFalse(result["complete"])
            self.assertGreater(result["errors"], 0)

    def test_root_must_be_existing_canonical_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            linked = base / "link"
            linked.symlink_to(base, target_is_directory=True)
            with self.assertRaises(ValueError):
                inventory(linked)
            with self.assertRaises(ValueError):
                inventory(Path("."))
            with self.assertRaises(OSError):
                inventory(base / "missing")

    def test_directory_replacement_during_scan_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home, outside = base / "instance", base / "outside"
            home.mkdir()
            outside.mkdir()
            (home / "credentials").mkdir()
            (outside / "telegram-bot-token").write_text("PRIVATE_EXTERNAL_CONTENT")
            original_open = os.open
            replaced = False

            def replace_then_open(path, flags, *args, **kwargs):
                nonlocal replaced
                if path == "credentials" and not replaced:
                    replaced = True
                    (home / "credentials").rename(home / "previous")
                    (home / "credentials").symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)

            with patch("scripts.backup_inventory.os.open", side_effect=replace_then_open):
                result = inventory(home)
            self.assertTrue(replaced)
            self.assertFalse(result["complete"])
            self.assertEqual(result["groups"]["credentials"]["files"], 0)
            self.assertTrue(result["known_files"]["credentials/telegram-bot-token"]["parent_unavailable"])
