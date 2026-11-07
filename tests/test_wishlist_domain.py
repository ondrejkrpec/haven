"""W1 acceptance against real disposable SQLite databases and deterministic time."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.wishlist_domain import IngressContext, Wishlist, canonical, next_month
from scripts.wishlist_store import MIGRATIONS


OWNER = "100001"
BOT = "synthetic-bot"
COMPLETE = {"purpose": "self", "purchase_mode": "once"}


class WishlistTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="haven-wishlist-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "wishlist.sqlite3"
        self.now = datetime(2026, 11, 7, 12, tzinfo=UTC)
        self.message_id = 0
        self.domain = self.open()
        self.addCleanup(lambda: self.domain.close())

    def open(self, **kwargs):
        return Wishlist(self.path, bot_id=BOT, owner_id=OWNER,
                        clock=lambda: self.now, **kwargs)

    def request(self, operation, arguments):
        self.message_id += 1
        command = {"operation": operation, "arguments": arguments}
        context = IngressContext(BOT, OWNER, OWNER, str(self.message_id),
                                 hashlib.sha256(canonical(command).encode()).hexdigest())
        envelope = {"protocol_version": 1, "request_id": context.request_id,
                    "source": context.source(), "command": command}
        return envelope, context

    def send(self, operation, arguments):
        return self.domain.handle(*self.request(operation, arguments))

    def add(self, details=None, url="https://shop.example/item?variant=2"):
        result = self.send("wishlist.add", {"url": url, "details": details or COMPLETE})
        self.assertTrue(result["committed"], result)
        return result["data"]["entry"]

    def change(self, operation, entry, **args):
        return self.send(operation, {"entry_id": entry["entry_id"],
                                     "expected_version": entry["version"], **args})

    def count(self, table):
        return self.domain.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_incomplete_intake_keeps_link_offer_and_receipt_after_restart(self):
        envelope, context = self.request("wishlist.add", {"url": "https://shop.example/x"})
        result = self.domain.handle(envelope, context)
        self.assertEqual((result["code"], result["committed"]), ("needs_clarification", True))
        self.assertEqual(result["clarification"], ["purpose", "purchase_mode"])
        entry = result["data"]["entry"]
        self.assertEqual((entry["state"], entry["classification"], entry["collection_status"]),
                         ("active", "needs_details", "pending"))
        self.assertEqual(entry["url"], "https://shop.example/x")
        self.domain.close()
        self.domain = self.open()
        self.assertEqual(self.domain.handle(envelope, context), result)
        self.assertEqual([self.count(table) for table in ("entries", "offers", "events", "receipts")], [1] * 4)

    def test_source_and_request_identity_are_checked_before_receipt_replay(self):
        envelope, context = self.request("wishlist.add", {"url": "https://shop.example/x"})
        self.assertTrue(self.domain.handle(envelope, context)["committed"])
        for untrusted in (replace(context, user_id="100002"), replace(context, chat_id="100002"),
                          replace(context, bot_id="other-bot"), replace(context, message_id="2"),
                          replace(context, text_sha256="0" * 64), context.source()):
            with self.subTest(context=untrusted):
                self.assertEqual(self.domain.handle(envelope, untrusted)["code"], "denied")
        forged = deepcopy(envelope)
        forged["request_id"] = "f" * 64
        self.assertEqual(self.domain.handle(forged, context)["code"], "denied")
        self.assertEqual(self.count("receipts"), 1)

    def test_unenrolled_sender_cannot_choose_owner_in_envelope(self):
        envelope, context = self.request("wishlist.list", {})
        other = replace(context, user_id="100002", chat_id="100002")
        envelope.update(request_id=other.request_id, source=other.source())
        self.assertEqual(self.domain.handle(envelope, other)["code"], "denied")
        self.assertEqual(self.count("receipts"), 0)

    def test_duplicate_and_edited_message_never_execute_twice(self):
        envelope, context = self.request("wishlist.add", {"url": "https://shop.example/x"})
        first = self.domain.handle(envelope, context)
        self.assertEqual(self.domain.handle(deepcopy(envelope), context), first)
        changed = deepcopy(envelope)
        changed["command"]["arguments"]["url"] = "https://shop.example/other"
        self.assertEqual(self.domain.handle(changed, context)["code"], "conflict")
        edited_context = replace(context, text_sha256="a" * 64)
        changed["source"] = edited_context.source()
        self.assertEqual(self.domain.handle(changed, edited_context)["code"], "conflict")
        first["data"]["entry"]["note"] = "caller mutation"
        self.assertIsNone(self.domain.handle(envelope, context)["data"]["entry"]["note"])
        self.assertEqual([self.count(table) for table in ("entries", "events", "receipts")], [1] * 3)

    def test_entry_event_receipt_and_purchase_rollback_together(self):
        entry = self.add()
        db = self.domain.store.db
        db.execute("""CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts
            BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END""")
        envelope, context = self.request("wishlist.record_purchase", {
            "entry_id": entry["entry_id"], "expected_version": 1})
        result = self.domain.handle(envelope, context)
        self.assertEqual(result["code"], "unavailable")
        self.assertFalse(result["committed"])
        self.assertNotIn("synthetic", canonical(result))
        self.assertEqual([self.count(table) for table in ("purchases", "events", "receipts")], [0, 1, 1])
        self.assertEqual(db.execute("SELECT state, version FROM entries").fetchone()[:], ("active", 1))
        self.assertEqual(db.execute("SELECT collection_generation FROM offers").fetchone()[0], 1)
        db.execute("DROP TRIGGER fail_receipt")
        self.assertTrue(self.domain.handle(envelope, context)["committed"])
        self.assertEqual(self.count("purchases"), 1)

    def test_add_is_rolled_back_if_its_event_cannot_be_written(self):
        self.domain.store.db.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON events
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        result = self.send("wishlist.add", {"url": "https://shop.example/x"})
        self.assertEqual(result["code"], "unavailable")
        self.assertEqual([self.count(table) for table in ("entries", "offers", "events", "receipts")], [0] * 4)

    def foreign_entry(self):
        entry = self.add()
        foreign = "wi_" + "f" * 32
        db = self.domain.store.db
        with self.domain.store.transaction():
            db.execute("""INSERT INTO entries (entry_id, owner_id, version, state, classification,
                purpose, purchase_mode, created_at, updated_at)
                SELECT ?, '100002', version, state, classification, purpose, purchase_mode,
                created_at, updated_at FROM entries WHERE entry_id = ?""", (foreign, entry["entry_id"]))
            db.execute("""INSERT INTO offers
                SELECT 'wo_foreign', ?, '100002', url, shop_host, url_sha256,
                collection_status, collection_generation, last_observed_at FROM offers WHERE entry_id = ?""",
                       (foreign, entry["entry_id"]))
        return foreign

    def test_foreign_and_missing_entries_have_identical_owner_scoped_results(self):
        foreign = self.foreign_entry()
        missing = "wi_" + "e" * 32
        operations = [("wishlist.get", {}), ("wishlist.update", {"patch": {"note": "x"}}),
                      ("wishlist.record_purchase", {}),
                      ("wishlist.pause", {"paused_until": "2026-12-07T12:00:00Z"}),
                      ("wishlist.resume", {})]
        for operation, args in operations:
            for entry_id in (foreign, missing):
                arguments = {"entry_id": entry_id, **args}
                if operation != "wishlist.get":
                    arguments["expected_version"] = 1
                with self.subTest(operation=operation, entry_id=entry_id):
                    result = self.send(operation, arguments)
                    self.assertEqual(result["code"], "not_found")
                    self.assertEqual(set(result), {"protocol_version", "request_id", "code", "committed"})
        listing = self.send("wishlist.list", {"state": "all"})["data"]["entries"]
        self.assertEqual(len(listing), 1)
        self.assertNotEqual(listing[0]["entry_id"], foreign)
        self.assertEqual(self.count("events"), 1)

    def test_update_patches_only_named_fields_and_checks_expected_version(self):
        entry = self.add({**COMPLETE, "label": "Pan", "note": "keep"})
        result = self.change("wishlist.update", entry, patch={"label": "Other", "note": None})
        updated = result["data"]["entry"]
        self.assertEqual((updated["label"], updated["note"], updated["purpose"], updated["version"]),
                         ("Other", None, "self", 2))
        self.assertEqual(self.change("wishlist.update", entry, patch={"label": "stale"})["code"], "conflict")
        self.assertEqual(self.count("events"), 2)

    def test_gift_and_recurring_contradictions_require_explicit_clarification(self):
        entry = self.add({"purpose": "gift", "purchase_mode": "recurring", "recipient": "Person"})
        self.assertEqual(entry["classification"], "needs_details")
        self.assertEqual(self.change("wishlist.record_purchase", entry)["code"], "needs_clarification")
        fixed = self.change("wishlist.update", entry, patch={"purchase_mode": "once"})["data"]["entry"]
        denied = self.change("wishlist.update", fixed, patch={"purpose": "self"})
        self.assertEqual((denied["code"], denied["committed"]), ("needs_clarification", False))
        self.assertEqual(self.count("events"), 2)
        fixed = self.change("wishlist.update", fixed, patch={"purpose": "self", "recipient": None})
        self.assertTrue(fixed["committed"])
        self.assertEqual(fixed["data"]["entry"]["classification"], "complete")

    def test_unknown_purchase_classification_does_not_change_state(self):
        result = self.send("wishlist.add", {"url": "https://shop.example/x"})
        entry = result["data"]["entry"]
        result = self.change("wishlist.record_purchase", entry)
        self.assertEqual((result["code"], result["committed"]), ("needs_clarification", False))
        self.assertEqual(self.count("purchases"), 0)
        gift = self.add({"purpose": "gift", "purchase_mode": "once"})
        self.assertEqual(self.change("wishlist.record_purchase", gift)["clarification"], ["recipient"])

    def test_one_time_purchase_preserves_offer_and_cannot_be_silently_reopened(self):
        entry = self.add()
        envelope, context = self.request("wishlist.record_purchase", {
            "entry_id": entry["entry_id"], "expected_version": 1})
        result = self.domain.handle(envelope, context)
        purchased = result["data"]["entry"]
        self.assertEqual((purchased["state"], purchased["paused_until"], purchased["version"],
                          purchased["collection_generation"]), ("purchased", None, 2, 2))
        self.assertEqual(self.domain.handle(envelope, context), result)
        self.assertEqual(self.count("purchases"), 1)
        self.assertEqual(self.change("wishlist.resume", purchased)["code"], "conflict")
        self.assertEqual(self.change("wishlist.record_purchase", purchased)["code"], "conflict")
        self.assertEqual(self.send("wishlist.list", {})["data"]["entries"], [])
        self.assertEqual(len(self.send("wishlist.list", {"state": "purchased"})["data"]["entries"]), 1)

    def test_pause_resume_invalidate_admission_and_keep_diagnostic_history(self):
        entry = self.add()
        db = self.domain.store.db
        db.execute("UPDATE offers SET collection_status = 'blocked', last_observed_at = '2026-11-01T12:00:00Z'")
        result = self.change("wishlist.pause", entry, paused_until="2026-11-08T13:00:00+01:00")
        paused = result["data"]["entry"]
        self.assertEqual((paused["state"], paused["paused_until"], paused["collection_generation"]),
                         ("paused", "2026-11-08T12:00:00.000000Z", 2))
        self.assertEqual(paused["collection_status"], "blocked")
        self.assertEqual(len(self.send("wishlist.list", {})["data"]["entries"]), 1)
        active = self.change("wishlist.resume", paused)["data"]["entry"]
        self.assertEqual((active["state"], active["collection_status"], active["collection_generation"]),
                         ("active", "pending", 3))
        self.assertEqual(active["last_observed_at"], "2026-11-01T12:00:00Z")
        self.assertEqual(self.change("wishlist.pause", active, paused_until="2026-11-07T12:00:00Z")["code"],
                         "invalid_request")

    def test_future_purchase_is_rejected_and_explicit_past_timestamp_is_preserved(self):
        entry = self.add()
        self.assertEqual(self.change("wishlist.record_purchase", entry,
                                     purchased_at="2026-11-07T12:00:01Z")["code"], "invalid_request")
        result = self.change("wishlist.record_purchase", entry, purchased_at="2026-11-06T12:30:00+01:00")
        self.assertTrue(result["committed"])
        self.assertEqual(self.domain.store.db.execute("SELECT purchased_at FROM purchases").fetchone()[0],
                         "2026-11-06T11:30:00.000000Z")

    def test_recurring_purchase_uses_one_calendar_month_in_instance_timezone(self):
        self.now = datetime(2028, 1, 31, 12, tzinfo=UTC)
        entry = self.add({"purpose": "self", "purchase_mode": "recurring"})
        result = self.change("wishlist.record_purchase", entry)
        paused = result["data"]["entry"]
        self.assertEqual((paused["state"], paused["paused_until"]), ("paused", "2028-02-29T12:00:00.000000Z"))
        self.assertEqual(self.count("purchases"), 1)
        active = self.change("wishlist.resume", paused)["data"]["entry"]
        self.now = datetime(2028, 3, 31, 11, tzinfo=UTC)
        self.assertEqual(self.change("wishlist.record_purchase", active)["data"]["entry"]["paused_until"],
                         "2028-04-30T11:00:00.000000Z")
        self.assertEqual(self.count("purchases"), 2)

    def test_calendar_year_nonleap_and_dst_gap_overlap_boundaries(self):
        cases = [
            ("2026-01-31T12:00:00Z", "2026-02-28T12:00:00Z"),
            ("2026-12-31T12:00:00Z", "2027-01-31T12:00:00Z"),
            ("2026-03-15T12:00:00Z", "2026-04-15T11:00:00Z"),
            ("2026-02-28T01:30:00Z", "2026-03-28T01:30:00Z"),
            ("2026-02-28T23:30:00Z", "2026-03-31T22:30:00Z"),
            # October 25 02:30 occurs twice: select fold=0, UTC+02.
            ("2026-09-25T00:30:00Z", "2026-10-25T00:30:00Z"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(next_month(datetime.fromisoformat(source), self.domain.zone),
                                 datetime.fromisoformat(expected))
        # February cannot produce March 29; exercise a real gap in a different zone.
        from zoneinfo import ZoneInfo
        self.assertEqual(next_month(datetime.fromisoformat("2026-02-08T07:30:00Z"),
                                    ZoneInfo("America/New_York")),
                         datetime.fromisoformat("2026-03-08T07:30:00Z"))

    def test_keyset_cursor_survives_restart_and_is_bound_to_filters_and_database(self):
        entries = [self.add() for _ in range(3)]
        first = self.send("wishlist.list", {"limit": 1})["data"]
        cursor = first["cursor"]
        self.assertEqual(first["entries"][0]["entry_id"], entries[0]["entry_id"])
        later = self.add()
        self.domain.close()
        self.domain = self.open()
        second = self.send("wishlist.list", {"limit": 1, "cursor": cursor})["data"]
        third = self.send("wishlist.list", {"limit": 1, "cursor": second["cursor"]})["data"]
        self.assertEqual([second["entries"][0]["entry_id"], third["entries"][0]["entry_id"]],
                         [entries[1]["entry_id"], entries[2]["entry_id"]])
        self.assertIsNone(third["cursor"])
        self.assertNotEqual(third["entries"][0]["entry_id"], later["entry_id"])
        for args in ({"limit": 2}, {"limit": 1, "state": "all"}, {"limit": 1, "purpose": "gift"}):
            self.assertEqual(self.send("wishlist.list", {**args, "cursor": cursor})["code"], "invalid_request")
        for bad in (cursor + "x", "malformed"):
            self.assertEqual(self.send("wishlist.list", {"limit": 1, "cursor": bad})["code"], "invalid_request")
        other = Wishlist(Path(self.temporary.name) / "other.sqlite3", bot_id=BOT, owner_id="100002")
        try:
            context = IngressContext(BOT, "100002", "100002", "1", "a" * 64)
            envelope = {"protocol_version": 1, "request_id": context.request_id, "source": context.source(),
                        "command": {"operation": "wishlist.list", "arguments": {"limit": 1, "cursor": cursor}}}
            self.assertEqual(other.handle(envelope, context)["code"], "invalid_request")
        finally:
            other.close()

    def test_large_list_is_bounded_without_skipping_entries(self):
        expected = [self.add({**COMPLETE, "note": "\U0001f431" * 2000})["entry_id"] for _ in range(5)]
        args, found = {"limit": 50}, []
        while True:
            result = self.send("wishlist.list", args)
            self.assertLessEqual(len(canonical(result).encode()), 65536)
            self.assertEqual(result["code"], "ok")
            found.extend(entry["entry_id"] for entry in result["data"]["entries"])
            if result["data"]["cursor"] is None:
                break
            args["cursor"] = result["data"]["cursor"]
        self.assertEqual(found, expected)

    def test_invalid_commands_and_source_urls_never_write_entries(self):
        bad_urls = ["file:///x", "https://127.0.0.1/x", "https://[::1]/x", "https://10.1.2.3/x",
                    "https://user:pass@shop.example/x", "https://shop.example:8443/x", "https://localhost/x",
                    "https://shop.local/x", "https://shop.example/#x", "https://shop.example:bad/x",
                    "https://shop.example/\\x", "https://shop.example/\x00x"]
        for url in bad_urls:
            with self.subTest(url=url):
                self.assertEqual(self.send("wishlist.add", {"url": url})["code"], "invalid_request")
        self.assertEqual(self.send("wishlist.add", {"url": "https://shop.example/x",
                                                    "details": {"label": "   "}})["code"], "invalid_request")
        self.assertEqual(self.count("entries"), 0)
        envelope, context = self.request("wishlist.list", {})
        for bad in ({**envelope, "protocol_version": True}, {**envelope, "command": {"sql": "synthetic"}},
                    {**envelope, "extra": float("nan")}, None, []):
            self.assertEqual(self.domain.handle(bad, context)["code"], "invalid_request")

    def test_real_database_lock_returns_retryable_unavailable_without_receipt(self):
        other = sqlite3.connect(self.path, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            result = self.send("wishlist.add", {"url": "https://shop.example/x"})
            self.assertEqual((result["code"], result["committed"]), ("unavailable", False))
        finally:
            other.close()
        self.assertEqual(self.count("receipts"), 0)

    def test_concurrent_duplicate_and_version_race_are_serialized(self):
        envelope, context = self.request("wishlist.add", {"url": "https://shop.example/x", "details": COMPLETE})

        def run(request):
            domain = self.open()
            try:
                return domain.handle(*request)
            finally:
                domain.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [(envelope, context)] * 2))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count("entries"), 1)
        entry = results[0]["data"]["entry"]
        requests = [self.request("wishlist.update", {"entry_id": entry["entry_id"], "expected_version": 1,
                                                     "patch": {"label": label}}) for label in ("First", "Second")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, requests))
        self.assertEqual(sorted(result["code"] for result in results), ["conflict", "ok"])
        self.assertEqual(self.count("events"), 2)

    def test_database_binding_newer_schema_and_unrelated_database_are_rejected(self):
        for kwargs in ({"owner_id": "100002", "bot_id": BOT}, {"owner_id": OWNER, "bot_id": "other"},
                       {"owner_id": OWNER, "bot_id": BOT, "timezone": "UTC"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Wishlist(self.path, **kwargs)
        other_path = Path(self.temporary.name) / "unknown.sqlite3"
        with closing(sqlite3.connect(other_path)) as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaises(ValueError):
            Wishlist(other_path, bot_id=BOT, owner_id=OWNER)
        with closing(sqlite3.connect(other_path)) as db:
            db.execute("PRAGMA user_version = 0")
            db.execute("CREATE TABLE unrelated (data TEXT)")
        with self.assertRaises(ValueError):
            Wishlist(other_path, bot_id=BOT, owner_id=OWNER)

    def test_failed_migration_leaves_existing_data_and_version_untouched(self):
        entry = self.add()
        with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS + ((
                "CREATE TABLE future_table (value TEXT)", "INVALID SQL"),)):
            with self.assertRaises(sqlite3.Error):
                self.open()
        self.assertEqual(self.domain.store.db.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
        self.assertIsNone(self.domain.store.db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'future_table'").fetchone())
        self.assertEqual(self.send("wishlist.get", {"entry_id": entry["entry_id"]})["data"]["entry"], entry)


if __name__ == "__main__":
    unittest.main()
