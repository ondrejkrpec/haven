"""Snapshots cannot execute work, create missing databases or expose conversations."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.dashboard_snapshot import collect, render, write_snapshot


class DashboardTests(unittest.TestCase):
    def test_missing_board_is_unavailable_and_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.dashboard_snapshot.subprocess.run") as command:
                command.return_value.returncode = 0
                command.return_value.stdout = "ActiveState=active\nSubState=running\n"
                snapshot = collect(root)
            self.assertEqual(snapshot["tasks"], {"status": "unavailable"})
            self.assertFalse(list(root.iterdir()))
            self.assertIn("nezjištěno", render({**snapshot, "service": {"test": None}}))

    def test_aggregate_only_snapshot_and_html_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "state.db")) as db, db:
                db.execute("CREATE TABLE sessions(api_call_count,input_tokens,output_tokens,estimated_cost_usd,actual_cost_usd,title)")
                db.execute("INSERT INTO sessions VALUES(2,10,20,NULL,NULL,'PRIVATE_CONVERSATION')")
            with closing(sqlite3.connect(root / "kanban.db")) as db, db:
                db.execute("CREATE TABLE tasks(status,title)")
                db.execute("INSERT INTO tasks VALUES('<script>','PRIVATE_TASK_BODY')")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with patch("scripts.dashboard_snapshot.subprocess.run") as command:
                command.return_value.returncode = 1
                snapshot = collect(root)
            self.assertEqual(snapshot["usage"]["rows"][0]["model_calls"], 2)
            page = render(snapshot)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("PRIVATE_", str(snapshot))
            self.assertNotIn("<script>", page)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            output = root / "output"
            write_snapshot(output, snapshot)
            self.assertEqual({p.name for p in output.iterdir()}, {"index.html", "snapshot.json"})
