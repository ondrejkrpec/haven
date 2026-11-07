"""Deterministic Telegram application; state, clarification and reply commit together."""

from datetime import timedelta
import hashlib
import json
import re
import secrets
import sqlite3

from scripts.wishlist_contract import REQUEST_VALIDATOR
from scripts.wishlist_domain import IngressContext, canonical, digest, stamp
from scripts.wishlist_protocol import decode

EVENT_KEYS = {"bot_id", "user_id", "chat_id", "message_id", "text", "chat_type", "is_bot", "edited"}
UNAVAILABLE = "Výsledek wishlistu teď nemohu ověřit. Před opakováním změny zkontroluj „wl seznam“."
HELP = ("Wishlist: pošli samotný HTTPS odkaz nebo napiš wl přidat ODKAZ.\n"
        "wl seznam · wl detail #1 · wl koupeno #1 · wl obnovit #1\n"
        "wl pauza #1 RRRR-MM-DDTHH:MM:SS+01:00\n"
        'wl upravit #1 {"label":"Nový název"}\n'
        "Doplnění údajů použije kód uvedený v otázce. Denní sběr cen zatím není zapnutý.")
REPLIES = {
    "invalid_request": "Příkaz není platný. Nápověda: wl pomoc.",
    "denied": "Tento požadavek není povolený.",
    "conflict": "Položka nebo požadavek se změnily. Zobraz aktuální detail a použij nový příkaz.",
    "not_found": "Položku nebo platné doplnění jsem nenašla.",
    "unavailable": UNAVAILABLE,
}


def relevant(text):
    return isinstance(text, str) and (text.strip().startswith("https://")
                                     or re.match(r"(?i)^wl(?:\s|$)", text.strip()) is not None)


def validate_event(event, *, bot_id, owner_id):
    if (not isinstance(event, dict) or set(event) != EVENT_KEYS
            or event["bot_id"] != bot_id or event["user_id"] != owner_id
            or event["chat_id"] != owner_id or event["chat_type"] != "private"
            or event["is_bot"] is not False or event["edited"] is not False
            or not isinstance(event["message_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", event["message_id"])):
        raise PermissionError("telegram_identity_denied")
    text = event["text"]
    if (not isinstance(text, str) or not text or len(text.encode("utf-16-le")) > 8192
            or any(ord(c) < 32 for c in text)):
        raise ValueError("telegram_text_invalid")
    return IngressContext(bot_id, owner_id, owner_id, event["message_id"],
                          hashlib.sha256(text.encode()).hexdigest())


def _summary(entry):
    label = entry["label"] or entry["shop_host"]
    states = {"active": "aktivní", "paused": "pozastaveno", "purchased": "koupeno"}
    return f"{entry['reference']} {label[:100]} — {states[entry['state']]}"


def _reply_text(result, operation, arguments):
    code = result["code"]
    if code in REPLIES:
        return REPLIES[code]
    data = result.get("data", {})
    if "entries" in data:
        lines = [_summary(entry) for entry in data["entries"]]
        reply = "\n".join(lines) if lines else "Wishlist je prázdný."
        if data.get("cursor"):
            selection = {"all": " vše", "purchased": " koupené"}.get(arguments.get("state"), "")
            reply += "\nDalší položky: wl dále " + data["cursor"] + selection
        return reply
    entry = data["entry"]
    prefix = "Uloženo. " if result["committed"] else ""
    reply = prefix + _summary(entry)
    if operation == "wishlist.get":
        reply += "\n" + entry["url"][:800]
        reply += "\nÚčel: " + {"self": "pro mě", "gift": "dárek", None: "k doplnění"}[entry["purpose"]]
        reply += "\nNákup: " + {"once": "jednorázově", "recurring": "opakovaně",
                                  None: "k doplnění"}[entry["purchase_mode"]]
        if entry["recipient"]:
            reply += "\nPro: " + entry["recipient"]
        if entry["note"]:
            reply += "\nPoznámka: " + entry["note"][:400]
    if entry["paused_until"]:
        reply += "\nPozastaveno do: " + entry["paused_until"]
    return reply


class TelegramApplication:
    def __init__(self, domain):
        self.domain = domain

    @staticmethod
    def response(request_id, code, *, reply=None, committed=False):
        return {"protocol_version": 1, "request_id": request_id, "code": code,
                "committed": committed, "reply": reply or REPLIES.get(code, UNAVAILABLE)}

    def handle(self, event):
        request_id = None
        try:
            context = validate_event(event, bot_id=self.domain.bot_id, owner_id=self.domain.owner_id)
            request_id = context.request_id
            fingerprint = digest(event)
            with self.domain.store.transaction() as db:
                previous = db.execute("SELECT * FROM telegram_receipts WHERE request_id = ? AND owner_id = ?",
                                      (request_id, self.domain.owner_id)).fetchone()
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        return self.response(request_id, "conflict")
                    return json.loads(previous["response"])
                db.execute("SAVEPOINT telegram_operation")
                try:
                    result = self._execute(db, context, event["text"].strip())
                    if len(result["reply"].encode("utf-16-le")) > 7600:
                        raise ValueError("telegram_reply_size")
                except (ValueError, TypeError, OverflowError, RecursionError):
                    db.execute("ROLLBACK TO telegram_operation")
                    result = self.response(request_id, "invalid_request")
                db.execute("RELEASE telegram_operation")
                db.execute("INSERT INTO telegram_receipts VALUES (?, ?, ?, ?, ?)",
                           (request_id, self.domain.owner_id, fingerprint,
                            canonical(result), stamp(self.domain.clock())))
                return result
        except PermissionError:
            return self.response(request_id, "denied")
        except (ValueError, TypeError, OverflowError, RecursionError):
            return self.response(request_id, "invalid_request")
        except sqlite3.Error:
            return self.response(request_id, "unavailable")
        except Exception:
            return self.response(request_id, "unavailable")

    def _entry_ref(self, db, reference):
        if not re.fullmatch(r"#[1-9][0-9]{0,15}", reference):
            raise ValueError("entry_reference")
        row = db.execute("SELECT entry_id, version FROM entries WHERE sequence = ? AND owner_id = ?",
                         (int(reference[1:]), self.domain.owner_id)).fetchone()
        return dict(row) if row else None

    def _parse(self, db, text):
        if text.startswith("https://"):
            return "wishlist.add", {"url": text}, None
        parts = text.split(maxsplit=2)
        if not parts or parts[0].casefold() != "wl" or len(parts) < 2:
            raise ValueError("command_syntax")
        verb = parts[1].casefold()
        tail = parts[2] if len(parts) == 3 else ""
        if verb in ("pomoc", "help") and not tail:
            return "help", {}, None
        if verb in ("seznam", "list"):
            if tail not in ("", "vše", "vse", "koupené", "koupene", "koupeno"):
                raise ValueError("list_filter")
            return "wishlist.list", {"limit": 10, **({"state": "all"} if tail in ("vše", "vse") else
                                                      {"state": "purchased"} if tail else {})}, None
        if verb in ("dále", "dale"):
            continuation = tail.split()
            if not continuation or len(continuation) > 2:
                raise ValueError("cursor_syntax")
            filters = {"vše": "all", "vse": "all", "koupené": "purchased", "koupene": "purchased"}
            args = {"limit": 10, "cursor": continuation[0]}
            if len(continuation) == 2:
                if continuation[1] not in filters:
                    raise ValueError("cursor_filter")
                args["state"] = filters[continuation[1]]
            return "wishlist.list", args, None
        if verb in ("přidat", "pridat", "add"):
            return "wishlist.add", {"url": tail}, None
        if verb in ("dopln", "doplň"):
            token, _, answer = tail.partition(" ")
            pending = db.execute("SELECT * FROM clarifications WHERE token = ? AND owner_id = ? AND chat_id = ?",
                                 (token, self.domain.owner_id, self.domain.owner_id)).fetchone()
            if pending is None or pending["expires_at"] <= stamp(self.domain.clock()):
                return "not_found", {}, None
            choices = {
                "pro mě jednorázově": {"purpose": "self", "purchase_mode": "once", "recipient": None},
                "pro me jednorazove": {"purpose": "self", "purchase_mode": "once", "recipient": None},
                "pro mě opakovaně": {"purpose": "self", "purchase_mode": "recurring", "recipient": None},
                "pro me opakovane": {"purpose": "self", "purchase_mode": "recurring", "recipient": None},
            }
            fields = choices.get(answer.casefold())
            if fields is None:
                gift = re.fullmatch(r"(?i)(?:dárek pro|darek pro) (.{1,100})", answer)
                fields = ({"purpose": "gift", "purchase_mode": "once", "recipient": gift[1]}
                          if gift else decode(answer.encode()))
            return "wishlist.update", {"entry_id": pending["entry_id"],
                                        "expected_version": pending["entry_version"], "patch": fields}, token
        reference, _, detail = tail.partition(" ")
        entry = self._entry_ref(db, reference)
        if entry is None:
            return "not_found", {}, None
        args = {"entry_id": entry["entry_id"]}
        if verb in ("detail", "get") and not detail:
            return "wishlist.get", args, None
        args["expected_version"] = entry["version"]
        if verb in ("upravit", "update"):
            return "wishlist.update", {**args, "patch": decode(detail.encode())}, None
        if verb in ("koupeno", "purchase") and not detail:
            return "wishlist.record_purchase", args, None
        if verb in ("obnovit", "resume") and not detail:
            return "wishlist.resume", args, None
        if verb in ("pauza", "pause"):
            return "wishlist.pause", {**args, "paused_until": detail}, None
        raise ValueError("command_syntax")

    def _execute(self, db, context, text):
        operation, arguments, token = self._parse(db, text)
        if operation == "help":
            return self.response(context.request_id, "ok", reply=HELP)
        if operation == "not_found":
            return self.response(context.request_id, "not_found")
        envelope = {"protocol_version": 1, "request_id": context.request_id, "source": context.source(),
                    "command": {"operation": operation, "arguments": arguments}}
        if not REQUEST_VALIDATOR.is_valid(envelope):
            return self.response(context.request_id, "invalid_request")
        result = self.domain.handle(envelope, context, in_transaction=True)
        if result["code"] == "needs_clarification" and "data" not in result and "entry_id" in result:
            result = {**result, "data": {"entry": self.domain._entry(db, result["entry_id"])}}
        if token and (result["committed"] or result["code"] in ("conflict", "not_found")):
            db.execute("DELETE FROM clarifications WHERE token = ? AND owner_id = ?",
                       (token, self.domain.owner_id))
        reply = _reply_text(result, operation, arguments)
        if result.get("clarification") and "entry_id" in result and "data" in result:
            entry_id, version = result["entry_id"], result["version"]
            db.execute("DELETE FROM clarifications WHERE owner_id = ? AND chat_id = ? AND entry_id = ?",
                       (self.domain.owner_id, context.chat_id, entry_id))
            token = secrets.token_hex(8)
            db.execute("INSERT INTO clarifications VALUES (?, ?, ?, ?, ?, ?)",
                       (token, self.domain.owner_id, context.chat_id, entry_id, version,
                        stamp(self.domain.clock() + timedelta(days=1))))
            reply += (f"\nDoplň údaje do 24 hodin: wl dopln {token} pro mě jednorázově"
                      "\nMůžeš použít také „pro mě opakovaně“ nebo „dárek pro JMÉNO“.")
        return self.response(context.request_id, result["code"], committed=result["committed"], reply=reply)
