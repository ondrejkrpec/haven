"""Shared web/Telegram state, monetary history, replay and migration boundaries."""

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.wishlist_collection import Collection
from scripts.wishlist_domain import Wishlist
from scripts.backup_runtime import PauseJournal
from scripts.wishlist_operations import WEB_UPGRADE_UNITS
from scripts.wishlist_collector import request as socket_request
from scripts.wishlist_telegram import TelegramApplication
from scripts.wishlist_web_domain import WebApplication, WebServer
from scripts.wishlist_store import MIGRATIONS

OWNER, BOT = "100001", "200001"


class WebDomainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="haven-w6-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.now = datetime(2026, 11, 18, 12, tzinfo=UTC)
        self.domain = Wishlist(self.path, bot_id=BOT, owner_id=OWNER, web_enabled=True, clock=lambda: self.now)
        self.addCleanup(lambda: self.domain.close())
        self.application = WebApplication(self.domain)

    def call(self, operation, args, request_id=None):
        return self.application.handle({"protocol_version": 1, "operation": "web." + operation,
                                        "arguments": args, "request_id": request_id or uuid4().hex})

    def add(self, mode="once"):
        result = self.call("add", {"url": "https://www.momenti.cz/synthetic-product/", "details": {"purchase_mode": mode, "category": "home"}})
        self.assertEqual(result["code"], "ok", result)
        return result["data"]["entry"]

    def change(self, operation, entry, **extra):
        return self.call(operation, {"entry_id": entry["entry_id"], "expected_version": entry["version"], **extra})

    def get(self, entry):
        return self.call("get", {"entry_id": entry["entry_id"]})["data"]["entry"]

    def observe(self, price="1000.00", availability="in_stock"):
        collection = Collection(self.domain)
        admitted = collection.handle({"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
        self.assertEqual(admitted["code"], "ok", admitted)
        result = {"status": "ok", "reason": "structured_offer_candidate" if price else "source_out_of_stock",
                  "http_status": 200, "retry_after": 0, "offer": {"name": "Synthetic product", "sku": "SYNTHETIC-1",
                  "price": price, "currency": "CZK" if price else None, "availability": availability, "conditions_verification": "not_verified"}}
        self.assertEqual(collection.handle({"protocol_version": 1, "operation": "collection.complete", "job": admitted["job"], "result": result})["code"], "ok")

    def test_owner_scoped_mutations_are_atomic_replay_safe_and_versioned(self):
        entry = self.add()
        args = {"entry_id": entry["entry_id"], "expected_version": entry["version"], "unit_price": "800", "quantity": 2, "purchase_date": "2026-11-18"}
        identity = uuid4().hex
        first = self.call("purchase", args, identity)
        self.assertEqual(first["code"], "ok", first)
        self.assertEqual(self.call("purchase", args, identity), first)
        self.assertEqual(self.call("purchase", {**args, "quantity": 3}, identity)["code"], "conflict")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM purchases").fetchone()[0], 1)
        self.assertEqual(self.change("update", entry, patch={"label": "stale"})["code"], "conflict")
        self.assertEqual(self.call("get", {"entry_id": "wi_" + "f" * 32})["code"], "not_found")
        self.assertEqual(self.call("update", {**args, "owner_id": "100002"})["code"], "invalid_request")

    def test_first_price_reference_and_unknown_legacy_price_are_not_zero(self):
        entry = self.add()
        self.assertIsNone(self.get(entry)["stats90"]["average"])
        self.observe("800.00")
        current = self.get(entry)
        self.assertEqual(current["stats90"]["average"], 80000)
        self.assertEqual(current["stats90"]["minimum"], 80000)
        bought = self.change("purchase", current, unit_price="750", quantity=2, purchase_date="2026-11-18")
        self.assertEqual(bought["code"], "ok")
        row = self.domain.store.db.execute("SELECT * FROM purchases").fetchone()
        self.assertEqual((row["reference_price"] - row["unit_price"]) * row["quantity"], 10000)

    def test_recurring_purchase_mutes_only_notifications_and_collection_continues(self):
        entry = self.add("recurring")
        self.observe()
        bought = self.change("purchase", self.get(entry), unit_price="120", quantity=3, purchase_date="2026-11-18")
        current = bought["data"]["entry"]
        self.assertEqual(current["state"], "active")
        self.assertIsNotNone(current["notifications_muted_until"])
        self.now += timedelta(days=1, seconds=1)
        self.observe("900.00")
        self.assertEqual(self.get(entry)["latest_observation"]["price"], "900.00")
        self.assertEqual(self.change("unmute", self.get(entry))["code"], "ok")

    def test_daily_average_and_missing_days_use_prague_dates(self):
        entry = self.add()
        self.observe("100.00")
        self.now += timedelta(minutes=11)
        self.change("refresh", self.get(entry))
        self.observe("200.00")
        self.now += timedelta(days=2)
        self.observe("300.00")
        current = self.get(entry)
        self.assertEqual(current["stats90"]["average"], 22500)
        self.assertEqual(current["stats90"]["measured_days"], 2)

    def test_stock_without_price_retains_history_and_hides_current_comparison(self):
        entry = self.add()
        self.observe()
        self.now += timedelta(days=1, seconds=1)
        self.observe(None, "out_of_stock")
        current = self.get(entry)
        self.assertEqual(current["stats90"]["average"], 100000)
        self.assertIsNone(current["stats90"]["current"])
        self.assertIsNone(current["stats90"]["saving"])
        self.assertEqual(current["latest_observation"]["price"], "1000.00")

    def test_purchase_correction_keeps_reference_and_cancellation_restores_state(self):
        entry = self.add("recurring")
        self.observe()
        self.change("purchase", self.get(entry), unit_price="800", quantity=2, purchase_date="2026-11-18")
        purchase = self.call("purchases", {"entry_id": entry["entry_id"]})["data"]["purchases"][0]
        self.now += timedelta(days=1, seconds=1)
        self.observe("2000.00")
        self.change("correct_purchase", self.get(entry), purchase_id=purchase["id"], unit_price="700", quantity=2, purchase_date="2026-11-18")
        row = self.domain.store.db.execute("SELECT * FROM purchases").fetchone()
        self.assertEqual((row["reference_price"], row["unit_price"]), (100000, 70000))
        result = self.change("void_purchase", self.get(entry), purchase_id=purchase["id"])
        self.assertEqual(result["code"], "ok", result)
        self.assertIsNone(result["data"]["entry"]["notifications_muted_until"])
        self.assertEqual(result["data"]["entry"]["state"], "active")

    def test_cancellation_cannot_undo_newer_lifecycle_but_metadata_edits_are_safe(self):
        entry = self.add("recurring")
        self.change("purchase", entry, unit_price="120", purchase_date="2026-11-18")
        purchase = self.call("purchases", {"entry_id": entry["entry_id"]})["data"]["purchases"][0]
        self.change("update", self.get(entry), patch={"note": "new note"})
        self.change("unmute", self.get(entry))
        self.assertEqual(self.change("void_purchase", self.get(entry), purchase_id=purchase["id"])["code"], "conflict")

    def test_manual_refresh_respects_shop_backoff_without_mutating_deadlines(self):
        entry = self.add()
        self.observe()
        before = self.get(entry)
        result = self.change("refresh", before)
        self.assertEqual(result["data"]["refresh_status"], "postponed")
        self.assertEqual(self.get(entry)["next_collection_at"], before["next_collection_at"])
        retry = self.now + timedelta(days=3)
        self.domain.store.db.execute("INSERT INTO collection_hosts VALUES (?,?)", ("www.momenti.cz", retry.isoformat()))
        self.now += timedelta(hours=1)
        result = self.change("refresh", self.get(entry))
        self.assertEqual(result["data"]["refresh_status"], "postponed")
        self.assertIn(retry.date().isoformat(), result["data"]["retry_at"])

    def test_history_pages_are_signed_and_preserve_all_observations(self):
        entry = self.add()
        for _ in range(130):
            self.observe()
            self.now += timedelta(days=1, seconds=1)
        first = self.call("history", {"entry_id": entry["entry_id"]})["data"]
        self.assertEqual(len(first["history"]), 128)
        cursor = first["cursor"]
        second = self.call("history", {"entry_id": entry["entry_id"], "cursor": cursor})["data"]
        self.assertEqual(len(second["history"]), 2)
        self.assertIsNone(second["cursor"])
        self.assertEqual(self.call("history", {"entry_id": entry["entry_id"], "cursor": cursor + "x"})["code"], "invalid_request")

    def test_web_highlights_latest_price_transition_and_verified_restock_without_rewriting_history(self):
        entry = self.add()
        self.observe("1000.00")
        first = self.get(entry)
        self.assertIsNone(first["stats90"]["price_change"])
        self.assertIsNone(first["stock_change"])

        self.now += timedelta(days=1)
        self.observe("750.00")
        drop = self.get(entry)["stats90"]["price_change"]
        self.assertEqual(drop["delta"], -25000)

        self.now += timedelta(days=1)
        self.observe(None, "out_of_stock")
        unavailable = self.get(entry)
        self.assertIsNone(unavailable["stats90"]["current"])
        self.assertEqual(unavailable["stock_change"]["previous"], "in_stock")

        self.now += timedelta(days=1)
        self.observe("750.00")
        restock = self.get(entry)["stock_change"]
        self.assertEqual(restock["previous"], "out_of_stock")

        self.now += timedelta(days=1)
        self.observe("750.00")
        current = self.get(entry)
        self.assertEqual(current["stats90"]["price_change"], drop)
        self.assertEqual(current["stock_change"], restock)
        history = self.call("history", {"entry_id": entry["entry_id"]})["data"]["history"]
        self.assertEqual([row["cents"] for row in history], [100000, 75000, 75000, 75000])

    def test_new_telegram_links_default_to_once_without_purpose_question(self):
        event = {"bot_id": BOT, "user_id": OWNER, "chat_id": OWNER, "chat_type": "private", "is_bot": False,
                 "edited": False, "message_id": "1", "text": "https://shop.example.com/item"}
        result = TelegramApplication(self.domain).handle(event)
        self.assertEqual(result["code"], "ok", result)
        row = self.domain.store.db.execute("SELECT * FROM entries").fetchone()
        self.assertEqual(row["purchase_mode"], "once")
        self.assertEqual(row["classification"], "complete")

    def test_invalid_price_or_url_patch_cannot_partially_write(self):
        entry = self.add()
        for args in ({"unit_price": "-1"}, {"unit_price": "1", "quantity": True}, {"unit_price": "1", "purchase_date": "2027-01-01"}):
            self.assertEqual(self.change("purchase", entry, **args)["code"], "invalid_request")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM purchases").fetchone()[0], 0)
        self.assertEqual(self.change("update", entry, patch={"url": "https://other.example.com/"})["code"], "invalid_request")

    def summary(self, **args):
        result = self.call("statistics", {"filters": {"state": "all"}, **args})
        self.assertEqual(result["code"], "ok", result)
        return result["data"]

    def test_statistics_include_all_purchases_without_opening_details_and_follow_state_filter(self):
        entry = self.add()
        self.observe()
        self.change("purchase", self.get(entry), unit_price="800", quantity=2)
        total = self.summary()["total"]
        self.assertEqual(total, {"count": 1, "spending": "160000", "savings": "40000", "unknown_price": 0, "unknown_reference": 0})
        self.assertEqual(self.summary(filters={})["total"]["count"], 0)
        self.assertEqual(self.summary(filters={"state": "purchased"})["total"], total)
        self.change("remove", self.get(entry))
        self.assertEqual(self.summary()["total"]["count"], 0)
        self.assertEqual(self.summary(filters={"state": "removed"})["total"], total)

    def test_statistics_use_purchase_category_after_moves_and_other_current_filters(self):
        entry = self.add("recurring")
        self.change("update", entry, patch={"label": "Česká pánev"})
        self.change("purchase", self.get(entry), unit_price="200")
        self.change("update", self.get(entry), patch={"category": "gifts"})
        data = self.summary(filters={"category": "home", "search": "ČESKÁ", "mode": "recurring", "shop": "www.momenti.cz", "stock": "unknown"})
        self.assertEqual(data["total"]["count"], 1)
        self.assertEqual(data["categories"][0]["category"], "home")
        self.assertEqual(self.summary(filters={"category": "gifts"})["total"]["count"], 0)
        self.assertEqual(self.summary(filters={"search": "jiný výrobek"})["total"]["count"], 0)
        self.assertEqual(self.summary(filters={"stock": "in_stock"})["total"]["count"], 0)
        self.assertEqual(self.summary(filters={"mode": "once"})["total"]["count"], 0)
        self.assertEqual(self.summary(filters={"shop": "other.example.com"})["total"]["count"], 0)

    def test_unknown_price_reference_zero_and_net_overpayment_are_distinct(self):
        entry = self.add("recurring")
        for _ in range(3):
            self.change("purchase", self.get(entry), unit_price="100")
        db = self.domain.store.db
        rows = db.execute("SELECT sequence FROM purchases ORDER BY sequence").fetchall()
        db.execute("UPDATE purchases SET unit_price=NULL WHERE sequence=?", (rows[0][0],))
        db.execute("UPDATE purchases SET reference_price=50,unit_price=100 WHERE sequence=?", (rows[1][0],))
        total = self.summary()["total"]
        self.assertEqual(total, {"count": 3, "spending": "10100", "savings": "-50", "unknown_price": 1, "unknown_reference": 1})
        db.execute("UPDATE purchases SET unit_price=NULL")
        self.assertIsNone(self.summary()["total"]["spending"])
        self.assertIsNone(self.summary()["total"]["savings"])
        db.execute("UPDATE purchases SET unit_price=0,reference_price=0 WHERE sequence=?", (rows[1][0],))
        self.assertEqual(self.summary()["total"]["spending"], "0")
        self.assertEqual(self.summary()["total"]["savings"], "0")

    def test_month_year_and_all_periods_and_prague_legacy_dates(self):
        entry = self.add("recurring")
        self.change("purchase", entry, unit_price="100", purchase_date="2026-10-31")
        db = self.domain.store.db
        db.execute("UPDATE purchases SET purchase_date=NULL,purchased_at='2026-10-31T23:30:00Z'")
        self.assertEqual(self.summary()["total"]["count"], 1)  # November in Prague.
        self.assertEqual(self.summary(date="2026-10-01")["total"]["count"], 0)
        db.execute("UPDATE purchases SET purchase_date='2025-11-18'")
        self.assertEqual(self.summary(period="year")["total"]["count"], 0)
        self.assertEqual(self.summary(period="year", date="2025-02-01")["total"]["count"], 1)
        self.assertEqual(self.summary(period="all")["total"]["count"], 1)
        self.assertEqual(self.summary(date="2024-02-01")["end"], "2024-02-29")

    def test_corrections_and_voids_update_totals_without_reclassifying_purchase(self):
        entry = self.add("recurring")
        self.observe()
        self.change("purchase", self.get(entry), unit_price="800", quantity=2)
        purchase = self.call("purchases", {"entry_id": entry["entry_id"]})["data"]["purchases"][0]
        self.change("update", self.get(entry), patch={"category": "gifts", "note": "Later metadata"})
        self.change("correct_purchase", self.get(entry), purchase_id=purchase["id"], unit_price="1200", quantity=3, purchase_date="2026-11-18")
        total = self.summary(filters={"category": "home"})["total"]
        self.assertEqual((total["spending"], total["savings"]), ("360000", "-60000"))
        self.change("void_purchase", self.get(entry), purchase_id=purchase["id"])
        self.assertEqual(self.summary()["total"]["count"], 0)
        self.assertEqual(self.summary()["total"]["spending"], "0")
        self.assertEqual(self.get(entry)["note"], "Later metadata")

    def test_statistics_reject_untrusted_filters_are_read_only_and_refuse_partial_totals(self):
        for args in ({"owner_id": "100002"}, {"filters": {"owner_id": "100002"}}, {"filters": []},
                     {"filters": {"state": "paused"}}, {"filters": {"search": "x" * 201}},
                     {"period": "quarter"}, {"date": "2026-02-30"}, {"date": "2027-01-01"}):
            self.assertEqual(self.call("statistics", args)["code"], "invalid_request")
        entry = self.add("recurring")
        self.change("purchase", entry, unit_price="100")
        self.change("purchase", self.get(entry), unit_price="200")
        before = self.domain.store.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        self.summary()
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], before)
        with patch("scripts.wishlist_web_domain.STATISTICS_PURCHASES", 1):
            self.assertEqual(self.call("statistics", {})["code"], "unavailable")
        with patch("scripts.wishlist_web_domain.STATISTICS_ENTRIES", 0):
            self.assertEqual(self.call("statistics", {})["code"], "unavailable")

    def test_statistics_decimal_strings_preserve_large_exact_totals(self):
        entry = self.add("recurring")
        self.change("purchase", entry, unit_price="99999999.99", quantity=9999)
        db = self.domain.store.db
        original = dict(db.execute("SELECT * FROM purchases").fetchone())
        for _ in range(100):
            row = {key: value for key, value in original.items() if key != "sequence"}
            row["request_id"] = uuid4().hex
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)", (row["request_id"], OWNER, "synthetic", "{}", "2026-11-18"))
            db.execute(f"INSERT INTO purchases ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        self.assertEqual(self.summary()["total"]["spending"], str(9999999999 * 9999 * 101))

    def test_statistics_exclude_another_owner_even_with_unfiltered_period(self):
        db = self.domain.store.db
        entry = self.add("recurring")
        self.change("purchase", entry, unit_price="100")
        for table in ("entries", "offers", "receipts", "purchases"):
            original = dict(db.execute(f"SELECT * FROM {table} LIMIT 1").fetchone())
            row = {key: ("100002" if key == "owner_id" else "wi_" + "f" * 32 if key == "entry_id" else
                         "of_" + "f" * 32 if key == "offer_id" else "f" * 64 if key == "request_id" else value)
                   for key, value in original.items() if key != "sequence"}
            db.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        self.assertEqual(self.summary(period="all")["total"]["count"], 1)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_peer_is_distinct_and_cannot_invoke_collector_or_telegram(self):
        with tempfile.TemporaryDirectory(prefix="hw6-") as temporary:
            root = Path(temporary)
            domain = Wishlist(root / "db", bot_id=BOT, owner_id=OWNER, web_enabled=True)
            server = WebServer(domain, root / "web.sock", ingress_uid=os.geteuid())
            await server.start()
            endpoint = {"socket": str(root / "web.sock"), "service_uid": os.geteuid()}
            try:
                result = await socket_request(endpoint, {"protocol_version": 1, "operation": "web.list", "request_id": uuid4().hex, "arguments": {}})
                self.assertEqual(result["code"], "ok")
                result = await socket_request(endpoint, {"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
                self.assertEqual(result["code"], "invalid_request")
                server.ingress_uid += 1
                result = await socket_request(endpoint, {"protocol_version": 1, "operation": "web.list", "request_id": uuid4().hex, "arguments": {}})
                self.assertEqual(result["code"], "denied")
            finally:
                await server.close()
                domain.close()


class RecoveryTests(unittest.TestCase):
    def test_schema_seven_preserves_six_purchases_without_guessing_their_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state"
            with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:6]):
                legacy = Wishlist(path, bot_id=BOT, owner_id=OWNER)
            application = TelegramApplication(legacy)
            def event(text, identity):
                return {"bot_id": BOT, "user_id": OWNER, "chat_id": OWNER, "chat_type": "private",
                        "is_bot": False, "edited": False, "message_id": identity, "text": text}
            application.handle(event("https://shop.example.com/legacy", "1"))
            token = legacy.store.db.execute("SELECT token FROM clarifications").fetchone()[0]
            application.handle(event("wl dopln " + token + " pro mě opakovaně", "2"))
            application.handle(event("wl koupeno #1", "3"))
            legacy.store.db.execute("UPDATE entries SET category='home'")
            original = dict(legacy.store.db.execute("SELECT * FROM purchases").fetchone())
            legacy.close()
            current = Wishlist(path, bot_id=BOT, owner_id=OWNER, web_enabled=True)
            try:
                purchase = dict(current.store.db.execute("SELECT * FROM purchases").fetchone())
                self.assertEqual(purchase.pop("category"), "uncategorized")
                self.assertEqual(purchase, original)
                self.assertEqual(current.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
                self.assertEqual(current.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(current.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                current.close()
            with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:6]), self.assertRaises(ValueError):
                Wishlist(path, bot_id=BOT, owner_id=OWNER)

    def test_web_upgrade_resume_intent_keeps_timer_and_writers_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = PauseJournal(root, WEB_UPGRADE_UNITS)
            journal.arm()
            after_restart = PauseJournal(root)
            self.assertTrue(after_restart.pending())
            self.assertEqual(after_restart.units, WEB_UPGRADE_UNITS)
            after_restart.disarm()
            self.assertFalse(after_restart.pending())

    def test_enabling_web_converts_old_pause_without_discarding_entry_or_purchase(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state'
            legacy = Wishlist(path, bot_id=BOT, owner_id=OWNER)
            application = TelegramApplication(legacy)
            def event(text, identity):
                return {"bot_id":BOT,"user_id":OWNER,"chat_id":OWNER,"chat_type":"private","is_bot":False,"edited":False,"message_id":identity,"text":text}
            application.handle(event('https://shop.example.com/legacy','1'))
            row = legacy.store.db.execute('SELECT * FROM clarifications').fetchone()
            application.handle(event('wl dopln '+row['token']+' pro mě opakovaně','2'))
            application.handle(event('wl koupeno #1','3'))
            before = legacy.store.db.execute('SELECT * FROM entries').fetchone()
            self.assertEqual(before['state'],'paused')
            old_deadline=before['paused_until']
            legacy.close()
            current=Wishlist(path,bot_id=BOT,owner_id=OWNER,web_enabled=True)
            try:
                row=current.store.db.execute('SELECT * FROM entries').fetchone()
                self.assertEqual(row['state'],'active')
                self.assertEqual(row['notifications_muted_until'],old_deadline)
                purchase=current.store.db.execute('SELECT * FROM purchases').fetchone()
                self.assertIsNone(purchase['unit_price'])
                self.assertEqual(row['entry_id'],before['entry_id'])
            finally:
                current.close()
