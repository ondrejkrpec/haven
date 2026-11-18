"""Shared web/Telegram state, monetary history, replay and migration boundaries."""

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from scripts.wishlist_collection import Collection
from scripts.wishlist_domain import Wishlist
from scripts.backup_runtime import PauseJournal
from scripts.wishlist_operations import WEB_UPGRADE_UNITS
from scripts.wishlist_collector import request as socket_request
from scripts.wishlist_telegram import TelegramApplication
from scripts.wishlist_web_domain import WebApplication, WebServer

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
