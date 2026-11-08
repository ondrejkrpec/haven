"""Conversational wishlist acceptance with real state and an untrusted scripted interpreter."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.wishlist_domain import Wishlist
from scripts.wishlist_interpreter import InterpreterServer
from scripts.wishlist_language import LanguageFlow
from scripts.wishlist_service import Server, load_service_config
from scripts.wishlist_ingress import Client
from scripts.wishlist_telegram import TelegramApplication
from tests.test_wishlist_service import BOT, OWNER, event


def command(operation, **arguments):
    return {"kind": "command", "command": {"operation": "wishlist." + operation, "arguments": arguments}}


class Interpreter:
    def __init__(self, domain):
        self.domain, self.requests, self.decision = domain, [], None

    async def interpret(self, prepared):
        assert not self.domain.store.db.in_transaction
        self.requests.append(deepcopy(prepared))
        selected = self.decision(prepared) if callable(self.decision) else deepcopy(self.decision)
        if isinstance(selected, Exception):
            raise selected
        if asyncio.iscoroutine(selected):
            selected = await selected
        return {"request_id": prepared["request_id"], "attempt_id": prepared["attempt_id"],
                "code": "ok", "decision": selected, "usage": None}


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hw5-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "wishlist.sqlite3"
        self.now = datetime(2026, 11, 7, 12, tzinfo=UTC)
        self.domain = Wishlist(self.path, bot_id=BOT, owner_id=OWNER, clock=lambda: self.now)
        self.addCleanup(lambda: self.domain.close())
        self.application = TelegramApplication(self.domain)
        self.interpreter = Interpreter(self.domain)
        self.flow = LanguageFlow(self.domain, self.application, self.interpreter)
        self.counter = 0

    async def send(self, text, **changes):
        self.counter += 1
        return await self.flow.handle(event(text, str(self.counter), **changes))

    def row(self, number=1):
        return self.domain.store.db.execute("SELECT * FROM entries WHERE sequence=?", (number,)).fetchone()

    def pending(self):
        row = self.domain.store.db.execute("SELECT state FROM language_sessions").fetchone()
        return json.loads(row[0]) if row else {}

    async def add(self, label="Pánev", url="https://shop.example/item"):
        self.interpreter.decision = command("add", url=url,
            details={"label": label, "purpose": "self", "purchase_mode": "once"})
        result = await self.send(f"Přidej mi {label}: {url}")
        self.assertTrue(result["committed"], result)
        return self.domain.store.db.execute("SELECT * FROM entries ORDER BY sequence DESC LIMIT 1").fetchone()

    async def test_bare_link_and_natural_followup_survive_restart_without_question_codes(self):
        result = await self.send("https://shop.example/item")
        self.assertTrue(result["committed"])
        self.assertNotIn("wl ", result["reply"])
        self.assertIn("dárek", result["reply"])
        self.assertEqual(len(self.interpreter.requests), 0)
        self.domain.close()
        self.domain = Wishlist(self.path, bot_id=BOT, owner_id=OWNER, clock=lambda: self.now)
        self.application = TelegramApplication(self.domain)
        self.interpreter = Interpreter(self.domain)
        self.flow = LanguageFlow(self.domain, self.application, self.interpreter)
        self.interpreter.decision = lambda p: command("update", entry_id=p["context"]["focus"],
            expected_version=p["entries"][0]["version"], patch={"purpose": "gift", "purchase_mode": "once", "recipient": "máma"})
        answer = await self.send("Je to dárek pro mámu")
        self.assertTrue(answer["committed"], answer)
        self.assertEqual((self.row()["purpose"], self.row()["recipient"]), ("gift", "máma"))
        prepared = self.interpreter.requests[0]
        self.assertEqual(prepared["context"]["pending"]["reason"], "purpose")
        self.assertNotIn("owner_id", prepared)
        self.assertNotIn("note", prepared["entries"][0])
        self.assertNotIn("wl ", answer["reply"])

    async def test_shared_link_is_saved_before_clarification_then_short_replies_complete_it(self):
        url = "https://shop.example/sponge?variant=2"
        self.interpreter.decision = {"kind": "clarify", "reason": "purpose", "entry_ids": []}
        first = await self.send("Odesláno z aplikace Obchod: " + url + " dej mi to na wishlist")
        self.assertEqual((first["code"], first["committed"]), ("needs_clarification", True))
        self.assertIn(url, first["reply"])
        self.assertEqual(self.row()["state"], "active")
        self.interpreter.decision = lambda p: command("update", entry_id=p["context"]["focus"],
            expected_version=p["entries"][0]["version"], patch={"purpose": "self", "recipient": None})
        self.assertTrue((await self.send("Pro me"))["committed"])
        self.domain.close()
        self.domain = Wishlist(self.path, bot_id=BOT, owner_id=OWNER, clock=lambda: self.now)
        self.interpreter = Interpreter(self.domain)
        self.flow = LanguageFlow(self.domain, TelegramApplication(self.domain), self.interpreter)
        self.interpreter.decision = lambda p: command("update", entry_id=p["context"]["focus"],
            expected_version=p["entries"][0]["version"], patch={"purchase_mode": "recurring"})
        result = await self.send("Opakovany")
        self.assertTrue(result["committed"])
        self.assertEqual((self.row()["classification"], self.row()["purchase_mode"]), ("complete", "recurring"))
        self.assertEqual(self.domain.store.db.execute("SELECT url FROM offers").fetchone()[0], url)
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

    async def test_shared_link_help_or_price_question_never_adds_an_entry(self):
        for reason in ("help", "unsupported", "prices_unavailable", "request"):
            self.interpreter.decision = {"kind": "clarify", "reason": reason, "entry_ids": []}
            result = await self.send("Co víš o https://shop.example/item ?")
            self.assertFalse(result["committed"])
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    async def test_natural_remove_uses_explicit_bare_number_not_recent_focus(self):
        blue = await self.add("Modrá pánev", "https://shop.example/blue")
        await self.add("Červená pánev", "https://shop.example/red")
        self.interpreter.decision = command("remove", entry_id=blue["entry_id"], expected_version=1)
        result = await self.send("Dej pryc 1")
        self.assertTrue(result["committed"], result)
        self.assertIsNotNone(self.row(1)["removed_at"])
        self.assertIsNone(self.row(2)["removed_at"])
        self.interpreter.decision = command("restore", entry_id=blue["entry_id"], expected_version=2)
        self.assertTrue((await self.send("Vrat 1"))["committed"])
        self.assertIsNone(self.row(1)["removed_at"])

    async def test_unknown_or_multiple_numbered_targets_never_remove_focused_item(self):
        item = await self.add("Pánev")
        for text in ("Odstran 999", "Odstran #999", "Odstran 1 a 999"):
            self.interpreter.decision = command("remove", entry_id=item["entry_id"], expected_version=1)
            result = await self.send(text)
            self.assertFalse(result["committed"])
            self.assertIsNone(self.row()["removed_at"])

    async def test_combined_link_and_gift_message_commits_and_never_uses_model_confirmation(self):
        self.interpreter.decision = command("add", url="https://shop.example/gift?variant=2",
            details={"label": "Pánev", "purpose": "gift", "purchase_mode": "once", "recipient": "máma"})
        result = await self.send("Přidej tuhle pánev jako dárek pro mámu https://shop.example/gift?variant=2")
        self.assertEqual((result["code"], result["committed"]), ("ok", True))
        self.assertIn("Přidala", result["reply"])
        self.assertEqual(self.row()["recipient"], "máma")
        replay = await self.flow.handle(event("Přidej tuhle pánev jako dárek pro mámu https://shop.example/gift?variant=2"))
        self.assertEqual(replay, result)
        self.assertEqual(len(self.interpreter.requests), 1)

    async def test_failed_reply_commit_rolls_back_session_domain_and_attempt_completion(self):
        db = self.domain.store.db
        db.execute("""CREATE TRIGGER fail_telegram BEFORE INSERT ON telegram_receipts
            BEGIN SELECT RAISE(ABORT, 'synthetic private failure'); END""")
        self.interpreter.decision = command("add", url="https://shop.example/item")
        request = event("Přidej https://shop.example/item")
        result = await self.flow.handle(request)
        self.assertEqual((result["code"], result["committed"]), ("unavailable", False))
        for table in ("entries", "offers", "events", "receipts", "telegram_receipts", "language_sessions"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(db.execute("SELECT status FROM language_runs").fetchone()[0], "pending")
        self.assertEqual(db.execute("SELECT result FROM language_attempts").fetchone()[0], "pending")
        db.execute("DROP TRIGGER fail_telegram")
        self.now += timedelta(seconds=31)
        result = await self.flow.handle(request)
        self.assertTrue(result["committed"], result)
        self.assertEqual(len(self.interpreter.requests), 2)
        self.assertEqual(await self.flow.handle(request), result)

    async def test_upgrade_of_populated_w2_preserves_receipts_questions_and_cursor_key(self):
        from scripts.wishlist_store import MIGRATIONS
        path = self.path.with_name("w2.sqlite3")
        with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:2]):
            old = Wishlist(path, bot_id=BOT, owner_id=OWNER)
            application = TelegramApplication(old)
            reply = application.handle(event("https://shop.example/item"))
            tables = ("metadata", "entries", "offers", "events", "receipts", "telegram_receipts", "clarifications")
            previous = {table: [tuple(row) for row in old.store.db.execute(f"SELECT * FROM {table}")] for table in tables}
            old.close()
        upgraded = Wishlist(path, bot_id=BOT, owner_id=OWNER)
        try:
            self.assertEqual(upgraded.store.db.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
            for table in tables:
                self.assertEqual([tuple(row)[:len(previous[table][0])] if previous[table] else tuple(row) for row in upgraded.store.db.execute(f"SELECT * FROM {table}")], previous[table], table)
            self.assertEqual(TelegramApplication(upgraded).handle(event("https://shop.example/item")), reply)
        finally:
            upgraded.close()

    async def test_schema_three_upgrade_preserves_conversation_and_old_code_refuses_new_schema(self):
        from scripts.wishlist_store import MIGRATIONS
        path = self.path.with_name("schema-three.sqlite3")
        with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:3]):
            old = Wishlist(path, bot_id=BOT, owner_id=OWNER)
            old.store.db.execute("INSERT INTO language_sessions VALUES (?,?,1,?,?)",
                                 (OWNER, OWNER, '{"pending":{"reason":"purchase_mode"}}', "2030-01-01"))
            reply = TelegramApplication(old).handle(event("https://shop.example/existing?exact=1"))
            tables = [row[0] for row in old.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            columns = {table: [row[1] for row in old.store.db.execute(f"PRAGMA table_info({table})")]
                       for table in tables}
            previous = {table: [tuple(row) for row in old.store.db.execute(f"SELECT * FROM {table}")]
                        for table in tables}
            old.close()
        upgraded = Wishlist(path, bot_id=BOT, owner_id=OWNER)
        try:
            for table in tables:
                projection = ','.join(columns[table])
                self.assertEqual([tuple(row) for row in upgraded.store.db.execute(
                    f"SELECT {projection} FROM {table}")], previous[table], table)
            self.assertIsNone(upgraded.store.db.execute("SELECT removed_at FROM entries").fetchone()[0])
            self.assertEqual(TelegramApplication(upgraded).handle(event("https://shop.example/existing?exact=1")), reply)
        finally:
            upgraded.close()
        before = path.read_bytes()
        with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:3]):
            with self.assertRaisesRegex(ValueError, "Unsupported wishlist database version"):
                Wishlist(path, bot_id=BOT, owner_id=OWNER)
        self.assertEqual(path.read_bytes(), before)

    async def test_vague_pan_is_disambiguated_even_when_model_guesses_and_followup_keeps_intent(self):
        blue = await self.add("Modrá pánev", "https://shop.example/blue")
        red = await self.add("Červená pánev", "https://shop.example/red")
        self.interpreter.decision = command("record_purchase", entry_id=red["entry_id"], expected_version=1)
        result = await self.send("Tu pánev už jsem koupil")
        self.assertEqual((result["code"], result["committed"]), ("needs_clarification", False))
        self.assertIn("Kterou", result["reply"])
        self.assertIn("Modrá", result["reply"])
        self.assertEqual(self.pending()["pending"]["intent"], "Tu pánev už jsem koupil")
        self.interpreter.decision = command("record_purchase", entry_id=blue["entry_id"], expected_version=1)
        result = await self.send("Tu modrou")
        self.assertTrue(result["committed"], result)
        self.assertEqual((self.row(1)["state"], self.row(2)["state"]), ("purchased", "active"))

    async def test_wrong_model_target_is_never_written_when_user_names_another_item(self):
        await self.add("Modrá pánev", "https://shop.example/blue")
        red = await self.add("Červená pánev", "https://shop.example/red")
        self.interpreter.decision = command("record_purchase", entry_id=red["entry_id"], expected_version=1)
        result = await self.send("Modrou pánev už mám koupenou")
        self.assertFalse(result["committed"])
        self.assertEqual((self.row(1)["state"], self.row(2)["state"]), ("active", "active"))

    async def test_provider_failure_is_backed_off_and_deterministic_link_intake_still_works(self):
        self.interpreter.decision = TimeoutError("SYNTHETIC_PRIVATE_PROVIDER_ERROR")
        result = await self.send("Co mám na wishlistu?")
        self.assertEqual((result["code"], result["committed"]), ("unavailable", False))
        self.assertNotIn("SYNTHETIC", result["reply"])
        repeat = await self.flow.handle(event("Co mám na wishlistu?"))
        self.assertEqual(repeat, result)
        self.assertEqual(len(self.interpreter.requests), 1)
        self.assertTrue((await self.send("https://shop.example/item"))["committed"])
        self.assertEqual(len(self.interpreter.requests), 1)
        self.now += timedelta(seconds=61)
        self.interpreter.decision = command("list", limit=10)
        self.assertEqual((await self.flow.handle(event("Co mám na wishlistu?")))["code"], "ok")
        self.assertEqual(len(self.interpreter.requests), 2)

    async def test_single_inflight_attempt_is_replayed_and_mutation_during_model_io_conflicts(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def decide(prepared):
            started.set()
            await release.wait()
            return command("add", url="https://shop.example/item")
        self.interpreter.decision = decide
        request = event("Přidej https://shop.example/item")
        pending = asyncio.create_task(self.flow.handle(request))
        await started.wait()
        self.assertEqual((await self.flow.handle(request))["code"], "unavailable")
        self.assertEqual(len(self.interpreter.requests), 1)
        await self.flow.handle(event("wl seznam", "2"))
        release.set()
        result = await pending
        self.assertEqual((result["code"], result["committed"]), ("conflict", False))
        self.assertIsNone(self.row())
        self.assertEqual(await self.flow.handle(request), result)

    async def test_old_attempt_callback_cannot_poison_a_new_attempt_receipt(self):
        def replaced(prepared):
            self.domain.store.db.execute("UPDATE language_runs SET attempt_id=? WHERE request_id=?",
                                         (uuid4().hex, prepared["request_id"]))
            return command("add", url="https://shop.example/item")
        self.interpreter.decision = replaced
        response = await self.send("Přidej https://shop.example/item")
        self.assertFalse(response["committed"])
        self.assertIsNone(self.row())
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM telegram_receipts").fetchone()[0], 0)

    async def test_stale_pending_version_is_cleared_and_does_not_block_all_later_messages(self):
        await self.send("https://shop.example/item")
        self.application.handle(event('wl upravit #1 {"label":"Změněno"}', "100"))
        stale = await self.send("Je pro mámu")
        self.assertEqual(stale["code"], "conflict")
        self.assertIsNone(self.pending()["pending"])
        self.interpreter.decision = command("list", limit=10)
        result = await self.send("Ukaž wishlist")
        self.assertEqual(result["code"], "ok")

    async def test_untrusted_output_cannot_add_invented_url_change_owner_or_execute_tools(self):
        bad = [command("add", url="https://invented.example/item"),
               {"kind": "command", "command": {"operation": "system.shell", "arguments": {"cmd": "synthetic"}}},
               {"kind": "clarify", "reason": "request", "entry_ids": [], "reply": "Already saved"},
               {**command("list"), "owner_id": "100002"}, command("get", entry_id="wi_" + "f" * 32)]
        for item in bad:
            self.interpreter.decision = item
            result = await self.send("Přidej https://shop.example/item")
            self.assertFalse(result["committed"], result)
        self.assertIsNone(self.row())

    async def test_wrong_sender_edit_and_group_are_denied_before_any_model_call(self):
        for change in ({"user_id": "100002"}, {"chat_id": "100002"}, {"edited": True},
                       {"is_bot": True}, {"chat_type": "group"}, {"bot_id": "300001"}):
            self.assertEqual((await self.send("Ukaž wishlist", **change))["code"], "denied")
        self.assertEqual(self.interpreter.requests, [])

    async def test_model_questions_are_fixed_and_out_of_scope_chat_has_no_side_effect(self):
        self.interpreter.decision = {"kind": "clarify", "reason": "unsupported", "entry_ids": []}
        result = await self.send("Restartuj server a pak si popovídáme o politice")
        self.assertIn("wishlist", result["reply"])
        self.assertFalse(result["committed"])
        self.assertIsNone(self.row())
        self.assertIsNone(self.pending()["pending"])

    async def test_natural_pagination_keeps_its_page_size_without_commands(self):
        for index in range(12):
            self.application.handle(event(f"https://shop.example/item{index}", str(100 + index)))
        self.interpreter.decision = command("list")
        result = await self.send("Co mám na wishlistu?")
        self.assertEqual(result["code"], "ok")
        self.assertEqual(len(self.pending()["listed"]), 10)
        self.assertIn("další", result["reply"])
        self.assertNotIn("wl ", result["reply"])
        self.interpreter.decision = lambda p: command("list", cursor=p["context"]["cursor"])
        result = await self.send("Další prosím")
        self.assertEqual(result["code"], "ok")
        self.assertEqual(len(self.pending()["listed"]), 2)

    async def test_changed_body_cannot_bypass_pending_language_identity_via_deterministic_intake(self):
        self.interpreter.decision = TimeoutError()
        await self.flow.handle(event("Ukaž wishlist"))
        result = await self.flow.handle(event("https://shop.example/item"))
        self.assertEqual(result["code"], "conflict")
        self.assertIsNone(self.row())

    async def test_hourly_budget_counts_failures_and_never_disables_deterministic_intake(self):
        self.interpreter.decision = {"kind": "clarify", "reason": "request", "entry_ids": []}
        for _ in range(30):
            await self.send("Ukaž mi něco z wishlistu")
        result = await self.send("A co dál?")
        self.assertEqual(result["code"], "unavailable")
        self.assertEqual(len(self.interpreter.requests), 30)
        self.assertTrue((await self.send("https://shop.example/item"))["committed"])

    async def test_session_expiry_removes_previous_intent_from_model_context(self):
        await self.send("https://shop.example/item")
        self.now += timedelta(days=2)
        self.interpreter.decision = {"kind": "clarify", "reason": "request", "entry_ids": []}
        await self.send("Ano")
        self.assertEqual(self.interpreter.requests[-1]["context"], {})


class LanguageSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_real_socket_services_keep_identity_out_of_proposal_and_commit_only_in_domain(self):
        with tempfile.TemporaryDirectory(prefix="hw5-ipc-") as temporary:
            root = Path(temporary)
            for name in ("language", "domain"):
                (root / name).mkdir(mode=0o700)
            language_path, domain_path = root / "language/socket", root / "domain/socket"
            domain = Wishlist(root / "state.sqlite3", bot_id=BOT, owner_id=OWNER)
            backend = Interpreter(domain)
            backend.decision = command("add", url="https://shop.example/item", details={"label": "Pánev"})
            interpreter = InterpreterServer(language_path, client_uid=os.getuid(), backend=backend)
            await interpreter.start()
            service = Server(domain, domain_path, ingress_uid=os.getuid(),
                language={"socket": str(language_path), "service_uid": os.getuid()})
            await service.start()
            try:
                client = Client({"socket": str(domain_path), "service_uid": os.getuid(), "bot_id": BOT, "natural_language": True})
                response = await client.request(event("Přidej mi pánev https://shop.example/item"))
                self.assertTrue(response["committed"], response)
                self.assertNotIn("wl ", response["reply"])
                self.assertEqual(domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
                self.assertNotIn("owner_id", backend.requests[0])
            finally:
                await service.close()
                await interpreter.close()
                domain.close()

    async def test_production_config_refuses_model_uid_shared_with_ingress_or_database_owner(self):
        with tempfile.TemporaryDirectory(prefix="hw5-conf-") as temporary:
            root = Path(temporary)
            config = {"schema_version": 1, "database": str(root / "wishlist.sqlite3"),
                      "socket": str(root / "ingress.sock"), "socket_gid": os.getgid(), "ingress_uid": os.getuid() + 10,
                      "bot_id": BOT, "owner_id": OWNER, "timezone": "Europe/Prague",
                      "language": {"socket": str(root / "language.sock"), "service_uid": os.getuid()}}
            path = root / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_service_config(path)
            config["language"]["service_uid"] = config["ingress_uid"]
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_service_config(path)


if __name__ == "__main__":
    unittest.main()
