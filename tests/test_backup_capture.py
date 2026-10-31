"""Consistent staging and pause recovery with synthetic files and service doubles."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_capture as capture

POLICY = {"schema_version": 1, "include": ["config.json", "state.db", "state", "optional.db", "skills"],
          "exclude": ["cache", "state/gateway.heartbeat"], "required": ["config.json", "state.db"],
          "sqlite": ["state.db", "optional.db"]}


class JournalDouble:
    armed = False

    def pending(self):
        return self.armed

    def arm(self):
        self.armed = True

    def disarm(self):
        self.armed = False


class ControllerDouble:
    def __init__(self, journal, *, fail_stop=False, fail_start=False, initially_healthy=True):
        self.journal, self.calls = journal, []
        self.running, self.fail_stop, self.fail_start = initially_healthy, fail_stop, fail_start

    def healthy(self):
        return self.running

    def stop(self):
        assert self.journal.pending()
        self.calls.append("stop")
        self.running = False
        if self.fail_stop:
            raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE")

    def stopped(self):
        return not self.running

    def start(self):
        self.calls.append("start")
        self.running = not self.fail_start


class CaptureTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "target"
            source.mkdir(mode=0o700)
            (source / "config.json").write_text('{"revision":1}')
            (source / "state").mkdir()
            (source / "state/gateway.heartbeat").write_text("SYNTHETIC_TRANSIENT")
            (source / "skills").mkdir()
            (source / "skills/custom.sh").write_text("SYNTHETIC_CUSTOM_SKILL")
            (source / "skills/custom.sh").chmod(0o755)
            (source / "cache").mkdir()
            (source / "cache/ignored-link").symlink_to(root / "outside")
            with closing(sqlite3.connect(source / "state.db")) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE events(revision INTEGER)")
                db.execute("INSERT INTO events VALUES (1)")
                db.commit()
                yield root, source, target, db

    def test_exact_persistent_state_wal_permissions_and_custom_skill_metadata(self):
        with self.fixture() as (_, source, target, db):
            self.assertGreater((source / "state.db-wal").stat().st_size, 0)
            receipt = capture.stage_stopped(source, target, POLICY)
            self.assertEqual(set(receipt["files"]), {"config.json", "state", "state.db", "skills", "skills/custom.sh"})
            self.assertEqual(receipt["files"]["skills/custom.sh"]["mode"], 0o755)
            self.assertEqual(receipt["absent_optional"], ["optional.db"])
            self.assertFalse((target / "state.db-wal").exists())
            self.assertFalse((target / "state/gateway.heartbeat").exists())
            self.assertFalse((target / "cache").exists())
            with closing(sqlite3.connect(target / "state.db")) as restored:
                self.assertEqual(restored.execute("SELECT revision FROM events").fetchall(), [(1,)])
            self.assertEqual(db.execute("SELECT revision FROM events").fetchall(), [(1,)])
            for path in [target, *target.rglob("*")]:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    def test_mutation_after_copy_is_rejected_and_partial_plaintext_removed(self):
        with self.fixture() as (_, source, target, db):
            original = capture.read_tree

            def changed(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1] is not None:
                    db.execute("INSERT INTO events VALUES (2)")
                    db.commit()
                return result

            with patch.object(capture, "read_tree", side_effect=changed), self.assertRaises(capture.CaptureError):
                capture.stage_stopped(source, target, POLICY)
            self.assertFalse(target.exists())

    def test_unknown_files_links_special_files_and_hardlinks_fail_closed(self):
        for kind in ("unknown", "link", "fifo", "hardlink"):
            with self.subTest(kind=kind), self.fixture() as (root, source, target, _):
                outside = root / "outside"
                outside.write_text("SYNTHETIC_OUTSIDE_SECRET")
                if kind == "unknown":
                    (source / "unreviewed").write_text("SYNTHETIC_NEW_STATE")
                elif kind == "link":
                    (source / "state/linked").symlink_to(outside)
                elif kind == "fifo":
                    os.mkfifo(source / "state/fifo")
                else:
                    os.link(outside, source / "state/hardlink")
                with self.assertRaises(capture.CaptureError):
                    capture.stage_stopped(source, target, POLICY)
                self.assertFalse(target.exists())
                self.assertEqual(outside.read_text(), "SYNTHETIC_OUTSIDE_SECRET")

    def test_missing_required_or_unreviewed_database_refuses_capture(self):
        for kind in ("missing", "unknown_database", "orphan_sidecar", "corrupt"):
            with self.subTest(kind=kind), self.fixture() as (_, source, target, _):
                if kind == "missing":
                    (source / "config.json").unlink()
                elif kind == "unknown_database":
                    with closing(sqlite3.connect(source / "state/new.db")) as db:
                        db.execute("CREATE TABLE data(value TEXT)")
                elif kind == "orphan_sidecar":
                    (source / "optional.db-wal").write_bytes(b"SYNTHETIC_ORPHAN")
                else:
                    (source / "optional.db").write_bytes(b"SYNTHETIC_NOT_A_DATABASE")
                with self.assertRaises((capture.CaptureError, sqlite3.Error)):
                    capture.stage_stopped(source, target, POLICY)
                self.assertFalse(target.exists())

    def test_bounds_and_existing_destination_preserve_other_files(self):
        for limit in ({"max_bytes": 1}, {"max_entries": 1}, {"max_seconds": 0}):
            with self.fixture() as (_, source, target, _), self.assertRaises(capture.CaptureError):
                capture.stage_stopped(source, target, POLICY, **limit)
            self.assertFalse(target.exists())
        with self.fixture() as (_, source, target, _):
            target.mkdir()
            (target / "keep").write_text("USER_FILE")
            with self.assertRaises(ValueError):
                capture.stage_stopped(source, target, POLICY)
            self.assertEqual((target / "keep").read_text(), "USER_FILE")

    def test_symlinked_source_parent_and_destination_parent_are_refused(self):
        with self.fixture() as (root, source, target, _):
            (root / "alias").symlink_to(source, target_is_directory=True)
            with self.assertRaises(OSError):
                capture.stage_stopped(root / "alias", target, POLICY)
            (root / "output-alias").symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                capture.stage_stopped(source, root / "output-alias/target", POLICY)

    def test_policy_rejects_traversal_globs_overlap_and_excluded_required_paths(self):
        variants = [dict(POLICY, include=["../outside"]), dict(POLICY, include=["state/*"]),
                    dict(POLICY, include=["state", "state/child"]), dict(POLICY, exclude=["config.json"]),
                    dict(POLICY, include=[{}]), dict(POLICY, extra=True)]
        for policy in variants:
            with self.assertRaises(capture.CaptureError):
                capture.validate_policy(policy)
        capture.validate_policy(json.loads((Path(__file__).resolve().parents[1] / "examples/backup-capture.json").read_text()))

    def test_pause_restores_service_before_return_and_on_stop_or_capture_failure(self):
        for mode in ("success", "stop_failure", "capture_failure"):
            with self.subTest(mode=mode), self.fixture() as (_, source, target, _):
                journal = JournalDouble()
                controller = ControllerDouble(journal, fail_stop=mode == "stop_failure")
                if mode == "capture_failure":
                    (source / "unreviewed").write_text("SYNTHETIC_NEW_STATE")
                if mode == "success":
                    capture.capture_with_pause(controller, journal, source, target, POLICY)
                else:
                    with self.assertRaises(Exception):
                        capture.capture_with_pause(controller, journal, source, target, POLICY)
                self.assertEqual(controller.calls, ["stop", "start"])
                self.assertFalse(journal.pending())
                self.assertTrue(controller.healthy())

    def test_failed_resume_retains_recovery_intent_and_discards_capture(self):
        with self.fixture() as (_, source, target, _):
            journal = JournalDouble()
            controller = ControllerDouble(journal, fail_start=True)
            with self.assertRaisesRegex(capture.CaptureError, "service_resume_failed"):
                capture.capture_with_pause(controller, journal, source, target, POLICY)
            self.assertTrue(journal.pending())
            self.assertFalse(target.exists())
            controller.fail_start = False
            capture.recover_pause(controller, journal)
            self.assertFalse(journal.pending())
            self.assertTrue(controller.healthy())

    def test_pending_intent_or_unhealthy_service_never_stops_again(self):
        for pending in (True, False):
            with self.fixture() as (_, source, target, _):
                journal = JournalDouble()
                journal.armed = pending
                controller = ControllerDouble(journal, initially_healthy=False)
                with self.assertRaises(capture.CaptureError):
                    capture.capture_with_pause(controller, journal, source, target, POLICY)
                self.assertFalse(controller.calls)
                self.assertFalse(target.exists())

    def test_swapped_destination_parent_does_not_write_or_clean_outside_tree(self):
        with self.fixture() as (root, source, _, _):
            parent, moved, outside = root / "output", root / "moved", root / "outside"
            parent.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            (outside / "keep").write_text("USER_FILE")
            target = parent / "capture"
            original = capture.read_tree
            swapped = False

            def swap(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    parent.rename(moved)
                    parent.symlink_to(outside, target_is_directory=True)
                return original(*args, **kwargs)

            with patch.object(capture, "read_tree", side_effect=swap), self.assertRaises(Exception):
                capture.stage_stopped(source, target, POLICY)
            self.assertEqual(list(outside.iterdir()), [outside / "keep"])
            self.assertFalse((moved / "capture").exists())

    def test_unrepresented_extended_attributes_are_refused(self):
        with self.fixture() as (_, source, target, _):
            with patch.object(capture.os, "listxattr", return_value=["user.synthetic"]):
                with self.assertRaisesRegex(capture.CaptureError, "unsupported_source_metadata"):
                    capture.stage_stopped(source, target, POLICY)
            self.assertFalse(target.exists())
