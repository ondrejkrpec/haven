import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scripts.backup_capture import CaptureError
from scripts.backup_maintenance import retention


class BackupMaintenanceTests(unittest.TestCase):
    def test_dry_run_never_deletes_and_refuses_a_plan_losing_the_accepted_snapshot(self):
        for preserved in (True, False):
            with self.subTest(preserved=preserved), tempfile.TemporaryDirectory() as temporary:
                accepted = {"run_id": "a" * 32, "snapshot_id": "b" * 64}
                repository = Mock(settings={"hostname": "synthetic"})
                repository.snapshots.return_value = [{"id": accepted["snapshot_id"]}]
                plan = [{"keep": [{"id": "b" * 64}] if preserved else [],
                         "remove": None if preserved else [{"id": "b" * 64}]}]
                repository.run.side_effect = [b"", json.dumps(plan).encode()]
                receipt = Path(temporary) / "receipt.json"
                if preserved:
                    retention(repository, accepted, journal={}, receipt=receipt)
                    self.assertTrue(json.loads(receipt.read_text())["accepted_snapshot_preserved"])
                else:
                    with self.assertRaisesRegex(CaptureError, "accepted_snapshot_would_be_removed"):
                        retention(repository, accepted, journal={}, receipt=receipt)
                self.assertEqual(len(repository.run.call_args_list), 2)
                self.assertIn("--dry-run", repository.run.call_args_list[1].args)

    def test_changed_removal_set_refuses_prune_and_keeps_durable_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            accepted = {"run_id": "a" * 32, "snapshot_id": "b" * 64}
            repository = Mock(settings={"hostname": "synthetic"})
            repository.snapshots.return_value = [{"id": accepted["snapshot_id"]}]
            plan = [{"keep": [{"id": "b" * 64}], "remove": [{"id": "c" * 64}]}]
            changed = [{"keep": [{"id": "b" * 64}], "remove": [{"id": "d" * 64}]}]
            repository.run.side_effect = [b"", json.dumps(plan).encode(), json.dumps(changed).encode()]
            receipt = Path(temporary) / "receipt.json"
            with self.assertRaisesRegex(CaptureError, "retention_plan_changed"):
                retention(repository, accepted, apply=True, journal={}, receipt=receipt)
            self.assertEqual(json.loads(receipt.read_text())["stage"], "forget_intended")
            self.assertFalse(any(call.args == ("prune",) for call in repository.run.call_args_list))
