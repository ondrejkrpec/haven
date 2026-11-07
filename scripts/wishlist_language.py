"""Trusted two-phase wishlist interpretation. Model I/O never holds a DB transaction."""

from datetime import timedelta
import json
import re
import sqlite3
from uuid import uuid4

from scripts.wishlist_domain import canonical, classification, digest, stamp
from scripts.wishlist_language_contract import QUESTIONS, resolve_target, validate_decision
from scripts.wishlist_telegram import _reply_text, validate_event

LANGUAGE_UNAVAILABLE = "Teď zprávě nemohu spolehlivě porozumět. Samotný odkaz můžu uložit i tak; ostatní změny zatím neprovedu."
CONFLICT = "Mezitím se wishlist nebo rozhovor změnil. Napiš prosím znovu, kterou položku chceš řešit."


class LanguageFlow:
    def __init__(self, domain, application, client):
        self.domain, self.application = domain, application
        if isinstance(client, dict):
            from scripts.wishlist_interpreter import LanguageClient
            client = LanguageClient(client)
        self.client = client

    def _session(self, db):
        row = db.execute("SELECT * FROM language_sessions WHERE owner_id = ? AND chat_id = ?",
                         (self.domain.owner_id, self.domain.owner_id)).fetchone()
        if row is None:
            return 0, {}
        return row["version"], json.loads(row["state"]) if row["expires_at"] > stamp(self.domain.clock()) else {}

    def _save_session(self, db, state, text, reply):
        version, _ = self._session(db)
        state["history"] = (state.get("history", []) + [{"user": text[:500], "reply": reply[:500]}])[-6:]
        db.execute("""INSERT INTO language_sessions VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_id,chat_id) DO UPDATE SET version=excluded.version,
            state=excluded.state, expires_at=excluded.expires_at""",
                   (self.domain.owner_id, self.domain.owner_id, version + 1, canonical(state),
                    stamp(self.domain.clock() + timedelta(days=1))))

    def _entry_result(self, db, result):
        if result.get("entry_id") and "data" not in result:
            return {**result, "data": {"entry": self.domain._entry(db, result["entry_id"])}}
        return result

    def _compose(self, db, result, operation, arguments, text):
        result = self._entry_result(db, result)
        _, state = self._session(db)
        state["pending"] = None
        data = result.get("data", {})
        entry = data.get("entry")
        if "entries" in data:
            state.update(listed=[x["entry_id"] for x in data["entries"]],
                         cursor=data.get("cursor"), list_state=arguments.get("state"),
                         list_purpose=arguments.get("purpose"), focus=None)
        if entry:
            state["focus"] = entry["entry_id"]
        if result["code"] == "conflict":
            reply = CONFLICT
        elif result["code"] == "invalid_request":
            reply = "Tomu jsem nerozuměla dost přesně. Jak chceš položku upravit?"
        elif result["code"] == "needs_clarification" and entry:
            missing = result.get("clarification") or classification(entry)
            reason = next((key for key in ("purpose", "purchase_mode", "recipient") if key in missing), "request")
            state["pending"] = {"reason": reason, "entry_ids": [entry["entry_id"]],
                                "versions": {entry["entry_id"]: entry["version"]}, "intent": text[:1000]}
            reply = ("Odkaz jsem uložila. " if result["committed"] else "") + QUESTIONS[reason]
        else:
            reply = _reply_text(result, operation, arguments)
            if entry and result["committed"]:
                label = (entry["label"] or entry["shop_host"])[:100]
                verbs = {"wishlist.add": f"Přidala jsem do wishlistu: {label}.",
                         "wishlist.update": f"Upravila jsem položku: {label}.",
                         "wishlist.record_purchase": f"Zaznamenala jsem nákup: {label}.",
                         "wishlist.pause": f"Pozastavila jsem sledování: {label}.",
                         "wishlist.resume": f"Obnovila jsem sledování: {label}."}
                reply = verbs.get(operation, reply)
                if entry["paused_until"]:
                    reply += " Pozastaveno do " + entry["paused_until"] + "."
            if "Další položky:" in reply:
                reply = reply.split("\nDalší položky:")[0] + "\nMám ukázat další položky?"
        if entry and entry["classification"] == "complete":
            db.execute("DELETE FROM clarifications WHERE entry_id = ? AND owner_id = ?",
                       (entry["entry_id"], self.domain.owner_id))
        self._save_session(db, state, text, reply)
        return reply

    def _deterministic(self, db, context, text):
        operation, arguments, _ = self.application._parse(db, text)
        response = self.application._execute(db, context, text)
        row = db.execute("SELECT response FROM receipts WHERE request_id = ? AND owner_id = ?",
                         (context.request_id, self.domain.owner_id)).fetchone()
        if row:
            result = json.loads(row[0])
            natural = self._compose(db, result, operation, arguments, text)
            if not re.match(r"(?i)^wl(?:\s|$)", text):
                response["reply"] = natural
        else:
            _, state = self._session(db)
            self._save_session(db, state, text, response["reply"])
        return response

    def _prepare(self, event, context):
        db = self.domain.store.db
        now = self.domain.clock()
        fingerprint = digest(event)
        with self.domain.store.transaction():
            old = db.execute("SELECT * FROM telegram_receipts WHERE request_id = ? AND owner_id = ?",
                             (context.request_id, self.domain.owner_id)).fetchone()
            if old:
                return None, json.loads(old["response"]) if old["fingerprint"] == fingerprint else self.application.response(
                    context.request_id, "conflict", reply=CONFLICT)
            run = db.execute("SELECT * FROM language_runs WHERE request_id = ?", (context.request_id,)).fetchone()
            if run and run["fingerprint"] != fingerprint:
                return None, self.application.response(context.request_id, "conflict", reply=CONFLICT)
            busy = db.execute("""SELECT 1 FROM language_runs WHERE owner_id = ? AND chat_id = ?
                AND status = 'pending' AND expires_at > ?""",
                              (self.domain.owner_id, self.domain.owner_id, stamp(now))).fetchone()
            if busy or (run and run["status"] == "failed" and run["expires_at"] > stamp(now)):
                return None, self.application.response(context.request_id, "unavailable", reply=LANGUAGE_UNAVAILABLE)
            used = db.execute("SELECT COUNT(*) FROM language_attempts WHERE created_at >= ?",
                              (stamp(now - timedelta(hours=1)),)).fetchone()[0]
            if used >= 30:
                return None, self.application.response(context.request_id, "unavailable", reply=LANGUAGE_UNAVAILABLE)
            version, state = self._session(db)
            wanted = [state.get("focus"), *(state.get("pending") or {}).get("entry_ids", []), *state.get("listed", [])]
            wanted += [row[0] for row in db.execute(
                "SELECT entry_id FROM entries WHERE owner_id = ? ORDER BY sequence DESC LIMIT 20",
                (self.domain.owner_id,))]
            entries, seen = [], set()
            fields = ("entry_id", "version", "reference", "label", "purpose", "purchase_mode", "recipient", "state", "url", "shop_host")
            for entry_id in wanted:
                if entry_id is None or entry_id in seen or len(entries) >= 20:
                    continue
                entry = self.domain._entry(db, entry_id)
                if entry:
                    selected = {key: entry[key] for key in fields}
                    selected["url"] = selected["url"][:1024]
                    entries.append(selected)
                    seen.add(entry_id)
            pending = state.get("pending") or {}
            if any(self.domain._entry(db, key) is None or self.domain._entry(db, key)["version"] != value
                   for key, value in pending.get("versions", {}).items()):
                response = self.application.response(context.request_id, "conflict", reply=CONFLICT)
                self._save_session(db, {"pending": None, "history": []}, event["text"], CONFLICT)
                db.execute("INSERT INTO telegram_receipts VALUES (?, ?, ?, ?, ?)",
                           (context.request_id, self.domain.owner_id, fingerprint, canonical(response), stamp(now)))
                return None, response
            while len(canonical({"entries": entries, "context": state}).encode()) > 24000 and entries:
                entries.pop()
            attempt = uuid4().hex
            prepared = {"request_id": context.request_id, "attempt_id": attempt, "text": event["text"],
                        "now": stamp(now), "timezone": str(self.domain.zone), "entries": entries, "context": state}
            db.execute("""INSERT INTO language_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(request_id) DO UPDATE SET attempt_id=excluded.attempt_id,
                session_version=excluded.session_version, prepared=excluded.prepared,
                expires_at=excluded.expires_at, status='pending'""",
                       (context.request_id, self.domain.owner_id, context.chat_id, fingerprint, attempt, version,
                        canonical(prepared), stamp(now + timedelta(seconds=30))))
            db.execute("INSERT INTO language_attempts VALUES (?, ?, ?, 'pending', NULL)",
                       (attempt, context.request_id, stamp(now)))
            return prepared, None

    def _failed(self, prepared, code):
        with self.domain.store.transaction() as db:
            db.execute("UPDATE language_attempts SET result = ? WHERE attempt_id = ?",
                       (code, prepared["attempt_id"]))
            db.execute("""UPDATE language_runs SET status='failed', expires_at=?
                WHERE request_id=? AND attempt_id=? AND status='pending'""",
                       (stamp(self.domain.clock() + timedelta(seconds=60)), prepared["request_id"], prepared["attempt_id"]))

    async def handle(self, event):
        try:
            context = validate_event(event, bot_id=self.domain.bot_id, owner_id=self.domain.owner_id)
            text = event["text"].strip()
            known = self.domain.store.db.execute("SELECT fingerprint FROM language_runs WHERE request_id=?",
                                                (context.request_id,)).fetchone()
            if known and known[0] != digest(event):
                return self.application.response(context.request_id, "conflict", reply=CONFLICT)
            if re.match(r"(?i)^wl(?:\s|$)", text) or re.fullmatch(r"https://\S+", text):
                return self.application.handle(event, executor=self._deterministic)
            prepared, previous = self._prepare(event, context)
            if previous is not None:
                return previous
            try:
                answer = await self.client.interpret(prepared)
                if (not isinstance(answer, dict) or set(answer) != {"request_id", "attempt_id", "code", "decision", "usage"}
                        or answer["request_id"] != prepared["request_id"] or answer["attempt_id"] != prepared["attempt_id"]
                        or answer["code"] != "ok"):
                    raise ValueError("language_answer")
                decision = resolve_target(validate_decision(answer["decision"], prepared), prepared)
            except Exception:
                self._failed(prepared, "unavailable")
                return self.application.response(context.request_id, "unavailable", reply=LANGUAGE_UNAVAILABLE)

            def finish(db, current, text):
                run = db.execute("SELECT * FROM language_runs WHERE request_id = ?", (current.request_id,)).fetchone()
                if run["attempt_id"] != prepared["attempt_id"] or run["status"] != "pending" or run["expires_at"] <= stamp(self.domain.clock()):
                    raise RuntimeError("language_attempt_expired")
                version, state = self._session(db)
                if version != run["session_version"]:
                    response = self.application.response(current.request_id, "conflict", reply=CONFLICT)
                elif decision["kind"] == "clarify":
                    ids = decision["entry_ids"]
                    by_id = {x["entry_id"]: x for x in prepared["entries"]}
                    reply = QUESTIONS[decision["reason"]]
                    for key in ids:
                        item = by_id[key]
                        reply += "\n" + item["reference"] + " " + (item["label"] or item["shop_host"])[:100]
                    state["pending"] = ({"reason": decision["reason"], "entry_ids": ids,
                                         "versions": {key: by_id[key]["version"] for key in ids},
                                         "intent": (state.get("pending") or {}).get("intent", text[:1000])}
                                        if decision["reason"] not in ("unsupported", "prices_unavailable") else None)
                    self._save_session(db, state, text, reply)
                    response = self.application.response(current.request_id, "needs_clarification", reply=reply)
                else:
                    envelope = {"protocol_version": 1, "request_id": current.request_id, "source": current.source(),
                                "command": decision["command"]}
                    result = self.domain.handle(envelope, current, in_transaction=True)
                    reply = self._compose(db, result, decision["command"]["operation"], decision["command"]["arguments"], text)
                    response = self.application.response(current.request_id, result["code"],
                                                         committed=result["committed"], reply=reply)
                db.execute("UPDATE language_runs SET status='complete' WHERE request_id=? AND attempt_id=?",
                           (current.request_id, prepared["attempt_id"]))
                usage = answer["usage"]
                if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}
                        or any(type(x) is not int or x < 0 for x in usage.values())):
                    usage = None
                db.execute("UPDATE language_attempts SET result=?, usage=? WHERE attempt_id=?",
                           (response["code"], canonical(usage), prepared["attempt_id"]))
                return response

            return self.application.handle(event, executor=finish)
        except PermissionError:
            return self.application.response(None, "denied")
        except (ValueError, TypeError, sqlite3.Error):
            return self.application.response(None, "unavailable", reply=LANGUAGE_UNAVAILABLE)
