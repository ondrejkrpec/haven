import json
import unittest
from unittest.mock import Mock

from scripts.backup_fetch import discover_snapshot


class BackupDiscoveryTests(unittest.TestCase):
    def test_lost_host_recovery_pins_a_full_id_before_reading_its_authenticated_manifest(self):
        repo = Mock(settings={"hostname": "synthetic"})
        rows = [{"id": "a" * 64, "hostname": "synthetic", "time": "2026-01-01T00:00:00Z",
                 "tags": ["haven-production", "haven-run-" + "c" * 32]},
                {"id": "b" * 64, "hostname": "synthetic", "time": "2026-01-02T00:00:00Z",
                 "tags": ["haven-production", "haven-run-" + "d" * 32]}]
        manifest = {"run_id": "d" * 32, "capture": {"captured_at_utc": "2026-01-02T00:00:00+00:00"}}
        repo.run.side_effect = [json.dumps(rows).encode(), json.dumps(manifest).encode()]
        receipt = discover_snapshot(repo)
        self.assertEqual(receipt["snapshot_id"], "b" * 64)
        self.assertEqual(repo.run.call_args_list[1].args, ("dump", "b" * 64, "/payload/restore-manifest.json"))
        self.assertEqual(receipt["server_acceptance_status"], "unavailable")

    def test_cross_run_manifest_is_rejected(self):
        repo = Mock(settings={"hostname": "synthetic"})
        row = {"id": "a" * 64, "hostname": "synthetic", "time": "2026-01-01T00:00:00Z",
               "tags": ["haven-production", "haven-run-" + "b" * 32]}
        repo.run.side_effect = [json.dumps([row]).encode(), b'{"run_id":"unrelated"}']
        with self.assertRaises(Exception):
            discover_snapshot(repo)
