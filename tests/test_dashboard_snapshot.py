"""Snapshots cannot execute work, create missing databases or expose conversations."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.dashboard_snapshot import collect, render, write_snapshot
from scripts.dashboard_web import Application
from scripts import wishlist_observability as metrics


class DashboardTests(unittest.TestCase):
    def test_wishlist_snapshot_is_whitelisted_and_never_grants_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'aggregates.json'
            source.write_text('{"schema_version":1,"status":"ok","PRIVATE_PROMPT":"secret"}')
            with patch('scripts.dashboard_snapshot.subprocess.run') as command:
                command.return_value.returncode = 1
                snapshot = collect(root, wishlist_snapshot=source)
            self.assertEqual(snapshot['wishlist'], {'status': 'unavailable'})
            self.assertNotIn('PRIVATE_PROMPT', str(snapshot))
            source.unlink()
            source.symlink_to(root / 'missing')
            self.assertEqual(metrics.read_snapshot(source), {'status': 'unavailable'})

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
            self.assertEqual({p.name for p in output.iterdir()}, {"snapshot.json"})


class DashboardWebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.snapshot = self.root / "snapshot.json"
        self.data = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                     "service": {"ActiveState": "active", "SubState": "running", "NRestarts": "0",
                                 "ActiveEnterTimestamp": "Thu 2026-11-26 08:00:00 UTC"},
                     "usage": {"status": "unavailable"}, "tasks": {"status": "ok", "rows": []},
                     "wishlist": {"status": "unavailable"}}
        self.app = Application(b"body {}", producer_uid=os.geteuid(), snapshot=self.snapshot)
        write_snapshot(self.root, self.data)

    def request(self, path, method="GET"):
        respond = unittest.mock.Mock()
        body, = self.app({"REQUEST_METHOD": method, "PATH_INFO": path}, respond)
        status, headers = respond.call_args.args
        return status, dict(headers), body

    def test_html_is_rendered_from_data_and_only_fixed_get_routes_are_served(self):
        (self.root / "index.html").write_text("UNTRUSTED_FILE")
        status, headers, body = self.request("/")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"Stav instance", body)
        self.assertNotIn(b"UNTRUSTED_FILE", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(json.loads(self.request("/snapshot.json")[2]), self.data)
        self.assertEqual(self.request("/snapshot.json", "HEAD")[2], b"")
        self.assertEqual(self.request("/site.css")[2], b"body {}")
        self.assertEqual(self.request("/unknown")[0], "404 Not Found")
        self.assertEqual(self.request("/", "POST")[0], "405 Method Not Allowed")

    def test_snapshot_reader_rejects_unknown_fields_and_unsafe_input_types(self):
        for data in ({**self.data, "unexpected": "UNRELATED_DATA"},
                     {**self.data, "service": {"ActiveState": "<script>"}}):
            with self.subTest(data=data):
                write_snapshot(self.root, data)
                self.assertEqual(self.request("/snapshot.json")[0], "503 Service Unavailable")
        self.snapshot.unlink()
        external = self.root / "unrelated.json"
        external.write_text(json.dumps(self.data))
        external.chmod(0o640)
        self.snapshot.symlink_to(external)
        self.assertEqual(self.request("/")[0], "503 Service Unavailable")
        self.snapshot.unlink()
        os.mkfifo(self.snapshot, 0o640)
        self.assertEqual(self.request("/")[0], "503 Service Unavailable")
