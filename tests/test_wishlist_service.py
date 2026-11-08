"""W2 acceptance: strict frames, real peer-authenticated sockets and durable Telegram state."""

import asyncio
from contextlib import closing
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import io
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts.model_contract import ModelConfigError, check_service, service_config
from scripts import render_model
from scripts.validate_config import load_config
from scripts.wishlist_domain import Wishlist, canonical
from scripts.wishlist_ingress import Bridge, Client
from scripts.wishlist_protocol import MAX_FRAME, close_writer, decode, encode, read_frame
from scripts.wishlist_service import Server, load_service_config, private_database, validate_settings
from scripts.wishlist_store import MIGRATIONS
from scripts.wishlist_telegram import HELP, TelegramApplication

ROOT = Path(__file__).resolve().parents[1]
OWNER, BOT = "100001", "200001"


def event(text, message_id="1", **changes):
    return {"bot_id": BOT, "user_id": OWNER, "chat_id": OWNER, "chat_type": "private", "is_bot": False,
            "edited": False, "message_id": message_id, "text": text, **changes}


class ProtocolTests(unittest.TestCase):
    def test_frames_reject_duplicate_keys_nonfinite_invalid_unicode_and_wrong_shapes(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":1e9999}', b'{"x":"\\ud800"}', b'{"x":"\xff"}', b'[]', b'null',
                    b'{} extra', b'\xef\xbb\xbf{}', b'{}' + b' ' * MAX_FRAME):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                decode(raw)

    def test_depth_is_checked_before_json_parser_and_brackets_in_strings_are_literal(self):
        self.assertEqual(decode(b'{"x":"[\\\"{}]"}'), {"x": '["{}]'})
        with self.assertRaises(ValueError):
            decode(b'{"x":' + b'[' * 33 + b'0' + b']' * 33 + b'}')
        value = {"text": "Příliš žluťoučký 🐈"}
        frame = encode(value)
        self.assertEqual(struct.unpack("!I", frame[:4])[0], len(frame) - 4)
        self.assertEqual(decode(frame[4:]), value)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hw2-app-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "wishlist.sqlite3"
        self.now = datetime(2026, 11, 7, 12, tzinfo=UTC)
        self.domain = Wishlist(self.path, bot_id=BOT, owner_id=OWNER, clock=lambda: self.now)
        self.addCleanup(lambda: self.domain.close())
        self.application = TelegramApplication(self.domain)
        self.sequence = 0

    def send(self, text, **changes):
        self.sequence += 1
        return self.application.handle(event(text, str(self.sequence), **changes))

    def row(self, table="entries"):
        return self.domain.store.db.execute(f"SELECT * FROM {table}").fetchone()

    def pending_token(self):
        return self.row("clarifications")["token"]

    def complete(self, *, recurring=False):
        self.send("https://shop.example/item")
        result = self.send(f"wl dopln {self.pending_token()} pro mě " + ("opakovaně" if recurring else "jednorázově"))
        self.assertEqual(result["code"], "ok", result)
        return self.row()["sequence"]

    def test_incomplete_link_and_question_survive_restart_and_repeat_the_exact_reply(self):
        request = event("https://shop.example/item")
        first = self.application.handle(request)
        self.assertEqual((first["code"], first["committed"]), ("needs_clarification", True))
        token = self.pending_token()
        self.assertIn(token, first["reply"])
        self.domain.close()
        self.domain = Wishlist(self.path, bot_id=BOT, owner_id=OWNER, clock=lambda: self.now)
        self.application = TelegramApplication(self.domain)
        self.assertEqual(self.application.handle(request), first)
        result = self.application.handle(event(f"wl dopln {token} dárek pro Osoba", "2"))
        self.assertEqual((result["code"], result["committed"]), ("ok", True))
        row = self.row()
        self.assertEqual((row["purpose"], row["purchase_mode"], row["recipient"]), ("gift", "once", "Osoba"))
        self.assertIsNone(self.row("clarifications"))
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

    def test_unrelated_wrong_owner_and_edited_messages_cannot_answer_a_question(self):
        first = self.send("https://shop.example/item")
        token = self.pending_token()
        for text in ("pro mě jednorázově", "Co umíš?", "wl dopln unknown pro mě jednorázově"):
            self.assertFalse(self.send(text)["committed"])
        for change in ({"user_id": "100002"}, {"chat_id": "100002"}, {"chat_type": "group"},
                       {"is_bot": True}, {"edited": True}, {"bot_id": "200002"}):
            self.assertEqual(self.send(f"wl dopln {token} pro mě jednorázově", **change)["code"], "denied")
        self.assertEqual(self.row()["version"], 1)
        changed = event("https://shop.example/other")
        self.assertEqual(self.application.handle(changed)["code"], "conflict")
        self.assertEqual(self.application.handle(event("https://shop.example/item")), first)

    def test_question_version_and_expiry_prevent_stale_answers(self):
        self.send("https://shop.example/item")
        token = self.pending_token()
        self.send('wl upravit #1 {"label":"Nový název"}')
        self.assertEqual(self.send(f"wl dopln {token} pro mě jednorázově")["code"], "not_found")
        token = self.pending_token()
        # A separate owner-scoped command can advance state without replacing the question.
        self.domain.store.db.execute("UPDATE entries SET version = version + 1")
        self.assertEqual(self.send(f"wl dopln {token} pro mě jednorázově")["code"], "conflict")
        self.send("wl detail #1")
        token = self.pending_token()
        self.now += timedelta(days=2)
        self.assertEqual(self.send(f"wl dopln {token} pro mě jednorázově")["code"], "not_found")
        self.assertIsNone(self.row()["purpose"])

    def test_every_deterministic_operation_uses_owner_scoped_references(self):
        reference = self.complete(recurring=True)
        self.assertEqual(reference, 1)
        self.assertIn("#1", self.send("wl seznam")["reply"])
        self.assertIn("https://shop.example/item", self.send("wl detail #1")["reply"])
        self.assertTrue(self.send('wl upravit #1 {"label":"Pánev","note":"Větší"}')["committed"])
        self.assertTrue(self.send("wl pauza #1 2026-12-01T12:00:00+01:00")["committed"])
        self.assertTrue(self.send("wl obnovit #1")["committed"])
        self.assertTrue(self.send("wl koupeno #1")["committed"])
        self.assertEqual(self.row()["state"], "paused")
        self.assertEqual(self.row("purchases")["purchased_at"], "2026-11-07T12:00:00.000000Z")
        self.assertEqual(self.send("wl detail #999")["code"], "not_found")
        self.assertEqual(self.send("wl pomoc")["reply"], HELP)
        self.assertEqual(self.send('wl upravit #1 {"owner_id":"100002"}')["code"], "invalid_request")
        self.assertEqual(self.send("wl pauza #1 včera")["code"], "invalid_request")

    def test_atomic_reply_failure_rolls_back_domain_receipt_event_and_question(self):
        db = self.domain.store.db
        db.execute("""CREATE TRIGGER fail_telegram BEFORE INSERT ON telegram_receipts
            BEGIN SELECT RAISE(ABORT, 'synthetic private failure'); END""")
        first = self.send("https://shop.example/item")
        self.assertEqual((first["code"], first["committed"]), ("unavailable", False))
        for table in ("entries", "offers", "events", "receipts", "telegram_receipts", "clarifications"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        db.execute("DROP TRIGGER fail_telegram")
        first = self.application.handle(event("https://shop.example/item"))
        self.assertTrue(first["committed"])

    def test_invalid_command_receipt_also_prevents_changed_body_from_executing(self):
        request = event("wl nesmysl")
        original = self.application.handle(request)
        self.assertEqual(original["code"], "invalid_request")
        self.assertEqual(self.application.handle(request), original)
        self.assertEqual(self.application.handle(event("https://shop.example/item"))["code"], "conflict")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    def test_semantic_failure_after_domain_work_keeps_only_a_refusal_receipt(self):
        execute = self.application._execute

        def fail_after_write(*args):
            execute(*args)
            raise ValueError("synthetic invalid response")

        with patch.object(self.application, "_execute", side_effect=fail_after_write):
            result = self.send("https://shop.example/item")
        self.assertEqual((result["code"], result["committed"]), ("invalid_request", False))
        for table in ("entries", "offers", "events", "receipts", "clarifications"):
            self.assertIsNone(self.row(table))
        self.assertIsNotNone(self.row("telegram_receipts"))

    def test_successful_clarification_reply_is_replayed_after_question_was_removed(self):
        self.send("https://shop.example/item")
        request = event(f"wl dopln {self.pending_token()} pro mě jednorázově", "2")
        result = self.application.handle(request)
        self.assertIsNone(self.row("clarifications"))
        self.assertEqual(self.application.handle(request), result)
        self.assertEqual(self.row()["version"], 2)

    def test_purchase_and_conflicting_update_request_clarification_without_mutating_entry(self):
        self.send("https://shop.example/item")
        result = self.send("wl koupeno #1")
        self.assertEqual((result["code"], result["committed"]), ("needs_clarification", False))
        self.assertIn(self.pending_token(), result["reply"])
        self.send(f"wl dopln {self.pending_token()} pro mě opakovaně")
        version = self.row()["version"]
        result = self.send('wl upravit #1 {"purpose":"gift","recipient":"Osoba"}')
        self.assertEqual((result["code"], result["committed"]), ("needs_clarification", False))
        self.assertIn(self.pending_token(), result["reply"])
        self.assertEqual((self.row()["version"], self.row()["purpose"]), (version, "self"))

    def test_list_pagination_and_maximum_unicode_details_keep_replies_bounded(self):
        for index in range(12):
            self.send(f"https://shop.example/item{index}")
            self.send(f'wl upravit #{index + 1} ' + canonical({"label": "🐈" * 100, "note": "🐈" * 2000}))
        listing = self.send("wl seznam")
        self.assertLessEqual(len(listing["reply"].encode("utf-16-le")), 7600)
        cursor = listing["reply"].split("Další položky: ")[1]
        self.assertIn("#11", self.send(cursor)["reply"])
        listing = self.send("wl seznam vše")
        continuation = listing["reply"].split("Další položky: ")[1]
        self.assertTrue(continuation.endswith(" vše"))
        self.assertIn("#11", self.send(continuation)["reply"])
        self.assertLessEqual(len(self.send("wl detail #1")["reply"].encode("utf-16-le")), 7600)

    def test_w1_database_migrates_without_losing_entries_receipts_or_cursor_key(self):
        old_path = self.root / "w1.sqlite3"
        with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:1]):
            old = Wishlist(old_path, bot_id=BOT, owner_id=OWNER)
        try:
            old.store.db.execute("""INSERT INTO entries
                (entry_id,owner_id,version,state,classification,created_at,updated_at)
                VALUES ('wi_synthetic',?,1,'active','needs_details','past','past')""", (OWNER,))
            key = old.store.cursor_key
        finally:
            old.close()
        upgraded = Wishlist(old_path, bot_id=BOT, owner_id=OWNER)
        try:
            self.assertEqual(upgraded.store.cursor_key, key)
            self.assertEqual(upgraded.store.db.execute("SELECT entry_id FROM entries").fetchone()[0], "wi_synthetic")
            self.assertEqual(upgraded.store.db.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
        finally:
            upgraded.close()

    def test_source_links_are_exact_and_long_links_are_not_silently_truncated(self):
        url = "https://shop.example/original?variant=blue&pack=4"
        self.send(url)
        self.assertIn(url, self.send("wl seznam")["reply"])
        self.assertIn(url, self.send("wl detail 1")["reply"])
        long_url = "https://shop.example/" + "x" * 3000
        self.send(long_url)
        result = self.send("wl detail 2")
        self.assertIn("Celý odkaz přesahuje limit", result["reply"])
        self.assertNotIn(long_url[:800], result["reply"])
        self.assertEqual(self.domain.store.db.execute(
            "SELECT url FROM offers WHERE url=?", (long_url,)).fetchone()[0], long_url)

    def test_deterministic_remove_and_restore_are_reversible_and_replay_safe(self):
        self.complete(recurring=True)
        removed = self.send("wl odstranit 1")
        self.assertTrue(removed["committed"])
        self.assertIsNotNone(self.row()["removed_at"])
        self.assertNotIn("#1", self.send("wl seznam vše")["reply"])
        self.assertIn("#1", self.send("wl seznam odstraněné")["reply"])
        self.assertTrue(self.send("wl vrátit #1")["committed"])
        self.assertIsNone(self.row()["removed_at"])
        self.assertEqual(self.row()["purchase_mode"], "recurring")


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hw2-sock-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "ingress.sock"
        self.domain = Wishlist(self.root / "wishlist.sqlite3", bot_id=BOT, owner_id=OWNER)
        self.addCleanup(lambda: self.domain.close())
        self.server = Server(self.domain, self.path, ingress_uid=os.getuid(), deadline=0.5)
        await self.server.start()
        self.client = Client({"socket": str(self.path), "service_uid": os.getuid(), "bot_id": BOT})

    async def asyncTearDown(self):
        await self.server.close()
        self.domain.close()
        self.temporary.cleanup()

    async def raw(self, frame):
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            writer.write(frame)
            await writer.drain()
            return await read_frame(reader)
        finally:
            await close_writer(writer)

    async def test_real_socket_roundtrip_peer_credentials_permissions_and_restart(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o660)
        first = await self.client.request(event("https://shop.example/item"))
        self.assertTrue(first["committed"])
        await self.server.close()
        self.assertFalse(self.path.exists())
        self.domain.close()
        self.domain = Wishlist(self.root / "wishlist.sqlite3", bot_id=BOT, owner_id=OWNER)
        self.server = Server(self.domain, self.path, ingress_uid=os.getuid())
        await self.server.start()
        self.assertEqual(await self.client.request(event("https://shop.example/item")), first)

    async def test_peer_denial_precedes_reading_any_claimed_identity_or_role(self):
        self.server.ingress_uid = os.getuid() + 1
        reply = await self.raw(encode({"role": "ingress", "uid": os.getuid() + 1}))
        self.assertEqual(reply["code"], "denied")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    async def test_bad_frame_sizes_duplicate_keys_and_role_claims_never_mutate_state(self):
        for frame in (struct.pack("!I", MAX_FRAME + 1), struct.pack("!I", 0),
                      struct.pack("!I", 13) + b'{"x":1,"x":2}', encode({"role": "collector"}),
                      encode({"protocol_version": 1, "operation": "collector.result", "event": {}})):
            with self.subTest(frame=frame[:25]):
                response = await self.raw(frame)
                self.assertIn(response["code"], ("invalid_request", "denied"))
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    async def test_partial_frame_deadline_releases_connection_and_next_request_succeeds(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            writer.write(struct.pack("!I", 20) + b'{')
            await writer.drain()
            async with asyncio.timeout(3):
                self.assertEqual(await reader.read(), b"")
        finally:
            await close_writer(writer)
        self.assertEqual((await self.client.request(event("wl pomoc")))["code"], "ok")

    async def test_only_one_request_executes_per_connection(self):
        first = {"protocol_version": 1, "operation": "telegram.message", "event": event("https://shop.example/1")}
        second = {**first, "event": event("https://shop.example/2", "2")}
        self.assertTrue((await self.raw(encode(first) + encode(second)))["committed"])
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

    async def test_lost_client_response_is_replayed_without_repeating_the_write(self):
        request = {"protocol_version": 1, "operation": "telegram.message", "event": event("https://shop.example/item")}
        _, writer = await asyncio.open_unix_connection(self.path)
        writer.write(encode(request))
        await writer.drain()
        await close_writer(writer)  # Deliberately discard the response.
        result = await self.client.request(request["event"])
        self.assertTrue(result["committed"])
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

    async def test_connection_admission_has_a_hard_cap_and_recovers_after_close(self):
        self.server.max_connections = 1
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            # A roundtrip header guarantees the first callback was admitted before the next connect.
            writer.write(b'\x00')
            await writer.drain()
            other_reader, other_writer = await asyncio.open_unix_connection(self.path)
            try:
                async with asyncio.timeout(3):
                    self.assertEqual(await other_reader.read(), b"")
            finally:
                await close_writer(other_writer)
            async with asyncio.timeout(3):
                self.assertEqual(await reader.read(), b"")
        finally:
            await close_writer(writer)
        self.assertEqual((await self.client.request(event("wl pomoc")))["code"], "ok")

    async def test_client_rejects_world_accessible_socket_wrong_service_uid_and_symlinks(self):
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            await self.client.request(event("wl pomoc"))
        self.path.chmod(0o660)
        other = Client({"socket": str(self.path), "service_uid": os.getuid() + 1, "bot_id": BOT})
        with self.assertRaises(ValueError):
            await other.request(event("wl pomoc"))
        link = self.root / "other.sock"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            Client({"socket": str(link), "service_uid": os.getuid(), "bot_id": BOT})

    async def test_second_server_and_unrelated_socket_path_are_preserved(self):
        other = Server(self.domain, self.path, ingress_uid=os.getuid())
        with self.assertRaises(ValueError):
            await other.start()
        self.assertTrue(self.path.exists())
        self.assertEqual((await self.client.request(event("wl pomoc")))["code"], "ok")

    def native_event(self, text="https://shop.example/item", **changes):
        bot = SimpleNamespace(id=int(BOT), send_message=AsyncMock())
        raw = SimpleNamespace(get_bot=lambda: bot, from_user=SimpleNamespace(id=int(OWNER), is_bot=False),
                              chat=SimpleNamespace(id=int(OWNER), type="private"), message_id=1,
                              text=text, edit_date=None)
        source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_type="dm", thread_id=None,
                                 user_id=OWNER, chat_id=OWNER)
        native = SimpleNamespace(raw_message=raw, source=source, internal=False, media_urls=[],
                                 message_id="1", text=text, edit_revision=None, **changes)
        return native, bot

    async def consume(self, bridge, native):
        return await bridge.consume(session_key="synthetic", platform="telegram", source={
            "platform": "telegram", "chat_type": "dm", "user_id": OWNER, "chat_id": OWNER},
            message_id=native.message_id, text=native.text)

    async def test_bridge_uses_existing_bot_and_plain_text_only_after_database_commit(self):
        bridge = Bridge(OWNER, self.client.settings)
        native, bot = self.native_event()

        async def sent(**kwargs):
            with closing(sqlite3.connect(self.root / "wishlist.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_receipts").fetchone()[0], 1)
            self.assertIsNone(kwargs["parse_mode"])
            self.assertTrue(kwargs["disable_web_page_preview"])
            self.assertEqual(kwargs["chat_id"], int(OWNER))

        bot.send_message.side_effect = sent
        reply_parameters = SimpleNamespace(ReplyParameters=lambda **kwargs: kwargs)
        with patch.dict(sys.modules, {"telegram": reply_parameters}):
            bridge.capture(native)
            self.assertEqual(await self.consume(bridge, native), {"action": "handled"})
            bridge.capture(native)
            self.assertEqual(await self.consume(bridge, native), {"action": "handled"})
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(bot.send_message.call_args_list[0], bot.send_message.call_args_list[1])

    async def test_native_capture_rejects_edited_batched_group_bot_and_rewritten_messages(self):
        bridge = Bridge(OWNER, self.client.settings)
        for kind in ("edit", "bot", "group", "rewrite", "batch", "wrong_bot", "internal"):
            native, bot = self.native_event()
            if kind == "edit":
                native.raw_message.edit_date = datetime.now(UTC)
            elif kind == "bot":
                native.raw_message.from_user.is_bot = True
            elif kind == "group":
                native.raw_message.chat.type = "group"
            elif kind == "rewrite":
                native.text += "?other=1"
            elif kind == "batch":
                native.text += "\nhttps://shop.example/second"
            elif kind == "wrong_bot":
                bot.id += 1
            else:
                native.internal = True
            with self.subTest(kind=kind):
                bridge.capture(native)
                self.assertIn("reply", await self.consume(bridge, native))
                bot.send_message.assert_not_called()
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    async def test_failed_delivery_preserves_receipt_and_retry_does_not_repeat_mutation(self):
        bridge = Bridge(OWNER, self.client.settings)
        native, bot = self.native_event()
        bot.send_message.side_effect = OSError("synthetic uncertain delivery")
        with patch.dict(sys.modules, {"telegram": SimpleNamespace(ReplyParameters=lambda **kwargs: kwargs)}):
            bridge.capture(native)
            self.assertIn("reply", await self.consume(bridge, native))
            bot.send_message.side_effect = None
            bridge.capture(native)
            self.assertEqual(await self.consume(bridge, native), {"action": "handled"})
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_daemon_recovers_after_sigkill_without_losing_the_committed_reply(self):
        with tempfile.TemporaryDirectory(prefix="hw2-proc-") as temporary:
            root = Path(temporary)
            state, ipc = root / "state", root / "ipc"
            state.mkdir(mode=0o700)
            ipc.mkdir(mode=0o700)
            config = {"schema_version": 1, "database": str(state / "wishlist.sqlite3"),
                      "socket": str(ipc / "ingress.sock"), "socket_gid": os.getgid(),
                      "ingress_uid": os.getuid(), "bot_id": BOT, "owner_id": OWNER, "timezone": "Europe/Prague"}
            path = root / "service.json"
            path.write_text(json.dumps(config))
            self.assertEqual(load_service_config(path), config)

            async def start():
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "scripts.wishlist_service", "--config", str(path),
                    cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                async with asyncio.timeout(5):
                    ready = await process.stdout.readline()
                self.assertIn(b'"ready"', ready)
                return process

            client = Client({"socket": config["socket"], "service_uid": os.getuid(), "bot_id": BOT})
            first = await start()
            try:
                response = await client.request(event("https://shop.example/item"))
                self.assertTrue(response["committed"])
            finally:
                first.kill()
                await first.communicate()
            self.assertTrue(Path(config["socket"]).exists())
            second = await start()
            try:
                self.assertEqual(await client.request(event("https://shop.example/item")), response)
            finally:
                second.terminate()
                async with asyncio.timeout(5):
                    _, errors = await second.communicate()
            self.assertEqual(second.returncode, 0, errors)
            self.assertFalse(Path(config["socket"]).exists())


class ConfigurationTests(unittest.TestCase):
    def test_native_renderer_requires_explicit_wishlist_flags_and_fresh_output(self):
        import yaml
        with tempfile.TemporaryDirectory(prefix="hw2-render-") as temporary:
            root = Path(temporary)
            config = root / "instance.yaml"
            config.write_text("""schema_version: 1
instance:
  name: synthetic
  timezone: Europe/Prague
host:
  address: vps.example.invalid
model:
  provider: openai-codex
  name: gpt-synthetic
telegram:
  enabled: true
  owner_id: '100001'
""")
            settings = {"socket": str(root / "ingress.sock"), "service_uid": os.getuid(), "bot_id": BOT}
            settings_file = root / "ingress.json"
            settings_file.write_text(json.dumps(settings))
            output = root / "native.yaml"
            with self.assertRaises(ModelConfigError):
                render_model.render(config, output, wishlist=settings)
            self.assertFalse(output.exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(render_model.main([str(config), "--output", str(output), "--service",
                                                    "--capabilities", "--wishlist-settings", str(settings_file)]), 0)
            native = yaml.safe_load(output.read_text())
            self.assertEqual(check_service(native), "gpt-synthetic")
            self.assertEqual(native["plugins"]["entries"]["haven-capabilities"]["settings"]["wishlist"], settings)
            with self.assertRaises(FileExistsError):
                render_model.render(config, output, service=True, capabilities=True, wishlist=settings)

    def test_native_wishlist_is_explicit_and_keeps_the_existing_guarded_profile(self):
        config = load_config(ROOT / "examples/instance.yaml")
        config["model"]["name"] = "gpt-synthetic"
        config["telegram"] = {"enabled": True, "owner_id": OWNER}
        settings = {"socket": "/run/haven-wishlist/ingress.sock", "service_uid": 2002, "bot_id": BOT}
        with self.assertRaises(ModelConfigError):
            service_config(config, wishlist=settings)
        native = service_config(config, capabilities=True, wishlist=settings)
        self.assertEqual(check_service(native), "gpt-synthetic")
        self.assertEqual(native["platform_toolsets"]["telegram"], [])
        self.assertFalse(native["kanban"]["dispatch_in_gateway"])
        changed = deepcopy(native)
        changed["plugins"]["entries"]["haven-capabilities"]["settings"]["wishlist"]["role"] = "admin"
        with self.assertRaises(ModelConfigError):
            check_service(changed)
        for bad in ({**settings, "service_uid": True}, {**settings, "socket": "relative"},
                    {**settings, "bot_id": "SYNTHETIC_NOT_A_TOKEN"}):
            with self.assertRaises(ValueError):
                validate_settings(bad)

    def test_database_permissions_links_and_shared_files_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="hw2-mode-") as temporary:
            directory = Path(temporary)
            path = directory / "wishlist.sqlite3"
            private_database(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                private_database(path)
            path.chmod(0o600)
            link = directory / "hardlink"
            os.link(path, link)
            with self.assertRaises(ValueError):
                private_database(path)
            link.unlink()
            path.unlink()
            path.symlink_to(directory / "target")
            with self.assertRaises(OSError):
                private_database(path)


if __name__ == "__main__":
    unittest.main()
