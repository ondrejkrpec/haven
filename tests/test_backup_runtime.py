from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import backup_capture as capture
from scripts import backup_restore as restore
from scripts import backup_runtime as runtime


POLICY = {"schema_version": 1, "include": ["config.yaml", "state.db", "skills"], "exclude": [],
          "required": ["config.yaml", "state.db"], "sqlite": ["state.db"]}


class BackupRuntimeTests(unittest.TestCase):
    def test_repository_timeout_allows_interrupt_cleanup_before_reporting_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "released"
            code = (
                "import signal,sys; from pathlib import Path; "
                f"marker=Path({str(marker)!r}); "
                "signal.signal(signal.SIGINT, lambda *_: (marker.write_text('released'), sys.exit(0))); "
                "signal.pause()"
            )
            with self.assertRaisesRegex(capture.CaptureError, "command_timeout"):
                runtime.command([os.sys.executable, "-c", code], timeout=0.5)
            self.assertEqual(marker.read_text(), "released")

    def test_repository_timeout_kills_and_reaps_an_unresponsive_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "pid"
            code = (
                "import os,signal; from pathlib import Path; "
                "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid())); signal.pause()"
            )
            with patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 0.1):
                with self.assertRaisesRegex(capture.CaptureError, "command_timeout"):
                    runtime.command([os.sys.executable, "-c", code], timeout=0.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_path.read_text()), 0)

    def test_stopped_requires_no_main_process_and_an_empty_service_cgroup(self):
        controller = runtime.SystemController({})
        cases = [({"ActiveState": "inactive", "MainPID": "0", "ControlGroup": ""}, True),
                 ({"ActiveState": "failed", "MainPID": "0", "ControlGroup": ""}, True),
                 ({"ActiveState": "failed", "MainPID": "0", "ControlGroup": "/fixture", "TasksCurrent": "2"}, False),
                 ({"ActiveState": "active", "MainPID": "42", "ControlGroup": "/fixture"}, False),
                 ({"ActiveState": "deactivating", "MainPID": "0", "ControlGroup": ""}, False)]
        for state, expected in cases:
            with self.subTest(state=state), patch.object(controller, "state", return_value=state):
                self.assertEqual(controller.stopped(), expected)

    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, payload, state = root / "source", root / "payload", root / "state"
            for directory in (source, payload, state):
                directory.mkdir(mode=0o700)
            (source / "config.yaml").write_text("synthetic: true\n")
            (source / "config.yaml").chmod(0o600)
            (source / "skills").mkdir(mode=0o700)
            (source / "skills/task.sh").write_text("#!/bin/sh\nexit 0\n")
            (source / "skills/task.sh").chmod(0o750)
            with closing(sqlite3.connect(source / "state.db")) as db:
                db.execute("CREATE TABLE events(value TEXT)")
                db.execute("INSERT INTO events VALUES ('committed synthetic row')")
                db.commit()
            receipt = capture.stage_stopped(source, payload / "home", POLICY)
            manifest = {"schema_version": 1, "run_id": "a" * 32, "capture": receipt, "policy": POLICY}
            runtime.save(payload / "restore-manifest.json", manifest)
            digest = hashlib.sha256((payload / "restore-manifest.json").read_bytes()).hexdigest()
            yield root, payload, state, manifest, digest

    def test_durable_resume_survives_process_exit_and_is_not_erased_by_failed_recovery(self):
        with self.fixture() as (_, _, state, _, _):
            code = ("from pathlib import Path; from scripts.backup_runtime import PauseJournal; "
                    f"PauseJournal(Path({str(state)!r})).arm()")
            subprocess.run([os.sys.executable, "-c", code], check=True)
            journal = runtime.PauseJournal(state)
            self.assertTrue(journal.pending())
            controller = unittest.mock.Mock()
            controller.healthy.return_value = False
            with self.assertRaises(capture.CaptureError):
                capture.recover_pause(controller, journal)
            self.assertTrue(journal.pending())
            controller.healthy.return_value = True
            capture.recover_pause(controller, journal)
            self.assertFalse(journal.pending())

    def test_maintenance_lock_excludes_a_separate_process_and_releases_after_death(self):
        with self.fixture() as (root, _, _, _, _):
            lock = root / "maintenance.lock"
            code = ("from pathlib import Path; from scripts.backup_runtime import maintenance_lock; "
                    f"ctx=maintenance_lock(Path({str(lock)!r})); ctx.__enter__(); print('locked',flush=True); "
                    "import signal; signal.pause()")
            process = subprocess.Popen([os.sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(capture.CaptureError, "maintenance_busy"):
                    with runtime.maintenance_lock(lock):
                        self.fail("concurrent maintenance was admitted")
            finally:
                process.kill()
                process.wait()
                process.stdout.close()
            with runtime.maintenance_lock(lock):
                pass

    def test_exact_restore_detects_missing_extra_corrupt_and_linked_files(self):
        for mutation in ("missing", "extra", "corrupt", "symlink", "hardlink", "manifest"):
            with self.subTest(mutation=mutation), self.fixture() as (root, payload, _, _, digest):
                path = payload / "home/config.yaml"
                if mutation == "missing":
                    path.unlink()
                elif mutation == "extra":
                    (payload / "home/extra").write_text("SYNTHETIC")
                elif mutation == "corrupt":
                    path.write_text("WRONG")
                elif mutation == "symlink":
                    path.unlink()
                    path.symlink_to(root / "source/config.yaml")
                elif mutation == "hardlink":
                    os.link(path, root / "outside-link")
                else:
                    (payload / "restore-manifest.json").write_text("{}")
                with self.assertRaises(Exception):
                    runtime.validate_payload(payload, digest)

    def test_restore_rebuilds_modes_owners_database_and_keeps_existing_targets(self):
        with self.fixture() as (root, payload, _, manifest, digest):
            destination = root / "recovered"
            owners = {"uids": {str(os.getuid()): os.getuid()}, "gids": {str(os.getgid()): os.getgid()}}
            result = restore.rebuild(payload, destination, digest, owners)
            self.assertFalse(result["services_started"])
            self.assertEqual((destination / "skills/task.sh").stat().st_mode & 0o777, 0o750)
            with closing(sqlite3.connect(destination / "state.db")) as db:
                self.assertEqual(db.execute("SELECT value FROM events").fetchall(), [("committed synthetic row",)])
            self.assertEqual((destination / "config.yaml").stat().st_mtime_ns,
                             manifest["capture"]["files"]["config.yaml"]["mtime_ns"])
            with self.assertRaises(Exception):
                restore.rebuild(payload, destination, digest, owners)
            self.assertTrue((destination / "config.yaml").exists())

    def test_partial_backup_exit_never_accepts_or_sends_success(self):
        with self.fixture() as (_, payload, state, _, _):
            settings = {"source_home": "/synthetic", "hostname": "synthetic", "public_revision": "a" * 40,
                        "private_revision": "b" * 40, "runtime_source": "/synthetic/source", "runtime_python": "/synthetic/python"}
            repository = unittest.mock.Mock()
            repository.run.side_effect = runtime.CommandError(3)
            receipt = capture.stage_stopped
            def staged(*args, **kwargs):
                # Create a real staging tree from the fixture under the requested
                # destination while the service controller is a harmless double.
                return receipt(payload / "home", args[3], POLICY)
            with patch.object(runtime, "recover"), patch.object(runtime, "Repository", return_value=repository), \
                    patch.object(runtime, "ping") as ping, patch.object(runtime, "capture_with_pause", side_effect=staged), \
                    patch.object(runtime, "SystemController"):
                with self.assertRaises(capture.CaptureError):
                    runtime.backup(settings, state, state, POLICY)
                self.assertFalse((state / "accepted.json").exists())
                self.assertEqual(ping.call_args_list, [unittest.mock.call(state, "backup", "/start")])
                self.assertEqual(runtime.load(state / "run.json")["stage"], "rejected_partial_snapshot")

    def test_uncertain_upload_is_refused_before_any_new_repository_write(self):
        with self.fixture() as (_, _, state, _, _):
            runtime.save(state / "run.json", {"stage": "uploading", "run_id": "a" * 32})
            with patch.object(runtime, "recover"), patch.object(runtime, "Repository") as repository:
                with self.assertRaisesRegex(capture.CaptureError, "reconcile_required"):
                    runtime.backup({}, state, state, POLICY)
                repository.assert_not_called()

    def test_missing_stale_and_future_backup_never_report_service_success(self):
        with self.fixture() as (_, _, state, _, _):
            for age in (None, 86401, -100):
                if age is not None:
                    runtime.save(state / "accepted.json", {"restored_and_verified": True,
                                 "captured_at_utc": runtime.datetime.fromtimestamp(time.time() - age, runtime.timezone.utc).isoformat()})
                with patch.object(runtime, "SystemController") as controller, patch.object(runtime, "ping") as ping:
                    controller.return_value.healthy.return_value = True
                    with self.assertRaises(Exception):
                        runtime.monitor({}, state, state)
                    ping.assert_not_called()

    def test_reconciliation_requires_exact_run_snapshot_before_check_and_restore(self):
        with self.fixture() as (_, _, state, _, _):
            runtime.save(state / "run.json", {"stage": "uploading", "run_id": "a" * 32})
            repository = unittest.mock.Mock()
            repository.snapshots.return_value = []
            with patch.object(runtime, "recover"), patch.object(runtime, "Repository", return_value=repository), \
                    patch.object(runtime, "ping") as ping:
                runtime.reconcile({}, state, state)
                repository.run.assert_not_called()
                ping.assert_not_called()
                self.assertEqual(runtime.load(state / "run.json")["stage"], "interrupted_without_snapshot")

    def test_private_file_refuses_symlink_fifo_and_permissive_mode(self):
        with self.fixture() as (root, _, _, _, _):
            path = root / "secret"
            path.symlink_to(root / "source/config.yaml")
            with self.assertRaises(Exception):
                runtime.read_private(path)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(Exception):
                runtime.read_private(path)
            path.unlink()
            path.write_text("SYNTHETIC")
            path.chmod(0o644)
            with self.assertRaises(Exception):
                runtime.read_private(path)
