"""Wishlist W1 operations. Callers must supply independently trusted ingress context.

This is an in-process domain API, not a peer authenticator or production service.
It never fetches a URL, starts a collector or calls a model.
"""

import base64
import calendar
from dataclasses import asdict, dataclass
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from scripts.wishlist_contract import REQUEST_VALIDATOR
from scripts.wishlist_store import Store


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class IngressContext:
    """Constructed by trusted admission, never deserialized from a model proposal."""

    bot_id: str
    user_id: str
    chat_id: str
    message_id: str
    text_sha256: str
    platform: str = "telegram"

    def source(self):
        return {key: value for key, value in asdict(self).items() if key != "bot_id"}

    @property
    def request_id(self):
        return digest([self.bot_id, self.platform, self.user_id, self.chat_id, self.message_id])


def classification(details):
    missing = [key for key in ("purpose", "purchase_mode") if not details.get(key)]
    if details.get("purpose") == "gift":
        if not details.get("recipient"):
            missing.append("recipient")
        if details.get("purchase_mode") == "recurring":
            missing.append("gift_recurring")
    elif details.get("purpose") == "self" and details.get("recipient") is not None:
        missing.append("self_recipient")
    return missing


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def stamp(value):
    if value.utcoffset() is None:
        raise ValueError("An aware clock is required")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def next_month(value, zone):
    """Clip calendar day; fold=0 at overlap, move forward across a DST gap."""
    local = value.astimezone(zone)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    target = local.replace(year=year, month=month,
                           day=min(local.day, calendar.monthrange(year, month)[1]), fold=0)
    # The UTC roundtrip normalizes nonexistent local wall times forward.
    return target.astimezone(UTC)


def source_host(url):
    """Syntax/public-literal admission only; DNS/redirect checks belong to W3."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.fragment or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise ValueError("Invalid source URL")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or "." not in host or host.endswith((".localhost", ".local", ".internal", "."))
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                       for part in host.split("."))):
            raise ValueError("Invalid source host")
    else:
        if not address.is_global:
            raise ValueError("Non-public source literal")
    return host


class Wishlist:
    def __init__(self, path, *, bot_id, owner_id, timezone="Europe/Prague", clock=None):
        if not re.fullmatch(r"[1-9][0-9]{0,15}", owner_id) or not bot_id or len(bot_id) > 100:
            raise ValueError("Invalid wishlist identity")
        self.bot_id, self.owner_id = bot_id, owner_id
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.store = Store(path, bot_id=bot_id, owner_id=owner_id, timezone=timezone)

    def close(self):
        self.store.close()

    @staticmethod
    def response(request_id, code, *, committed=False, **data):
        return {"protocol_version": 1, "request_id": request_id, "code": code,
                "committed": committed, **data}

    def handle(self, envelope, context, *, in_transaction=False):
        """The W2 application can compose a command in its existing atomic transaction."""
        request_id = None
        try:
            encoded = canonical(envelope)
            if len(encoded) > 65536 or not REQUEST_VALIDATOR.is_valid(envelope):
                return self.response(None, "invalid_request")
            request_id = envelope["request_id"]
            if type(envelope["protocol_version"]) is not int:
                return self.response(request_id, "invalid_request")
            if (not isinstance(context, IngressContext) or context.bot_id != self.bot_id
                    or context.platform != "telegram" or context.user_id != self.owner_id
                    or context.chat_id != self.owner_id or context.source() != envelope["source"]
                    or context.request_id != request_id):
                return self.response(request_id, "denied")
            fingerprint = digest(envelope)
            if in_transaction and not self.store.db.in_transaction:
                raise RuntimeError("wishlist_transaction_required")
            transaction = nullcontext(self.store.db) if in_transaction else self.store.transaction()
            with transaction as db:
                previous = db.execute("SELECT * FROM receipts WHERE request_id = ? AND owner_id = ?",
                                      (request_id, self.owner_id)).fetchone()
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        return self.response(request_id, "conflict")
                    return json.loads(previous["response"])
                now = self.clock()
                occurred_at = stamp(now)
                operation = envelope["command"]["operation"]
                arguments = envelope["command"]["arguments"]
                result = self._command(db, request_id, operation, arguments, now)
                if len(canonical(result)) > 65536:
                    raise ValueError("Response exceeds the domain limit")
                if result["committed"]:
                    db.execute("""INSERT INTO events
                        (owner_id, entry_id, request_id, operation, entry_version, occurred_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                               (self.owner_id, result["entry_id"], request_id, operation,
                                result["version"], occurred_at))
                db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                           (request_id, self.owner_id, fingerprint, canonical(result), occurred_at))
                return result
        except (TypeError, ValueError, OverflowError, RecursionError):
            if in_transaction:
                raise
            return self.response(request_id, "invalid_request")
        except sqlite3.Error:
            if in_transaction:
                raise
            return self.response(request_id, "unavailable")

    def _entry(self, db, entry_id):
        row = db.execute("""SELECT e.*, o.*
            FROM entries e JOIN offers o ON o.entry_id = e.entry_id AND o.owner_id = e.owner_id
            WHERE e.entry_id = ? AND e.owner_id = ?""", (entry_id, self.owner_id)).fetchone()
        if row is None:
            return None
        observation = None
        if "next_collection_at" in row.keys():
            observation = db.execute("""SELECT observed_at, price, currency, availability, conditions_verification
                FROM observations WHERE offer_id=? ORDER BY sequence DESC LIMIT 1""", (row["offer_id"],)).fetchone()
        record = {"next_collection_at": "", "last_attempt_at": None, "collection_reason": None,
                  "last_http_status": None, "removed_at": None,
                  **{key: row[key] for key in row.keys() if key not in ("sequence", "owner_id")}}
        overdue = (record["state"] == "active" and record["removed_at"] is None and record["next_collection_at"]
                   and instant(record["next_collection_at"]) + timedelta(hours=1) < self.clock())
        return {**record,
                "state": "removed" if record["removed_at"] else record["state"],
                "collection_status": "stale" if overdue else record["collection_status"],
                "collection_reason": "schedule_overdue" if overdue else record["collection_reason"],
                "latest_observation": dict(observation) if observation else None,
                "reference": f"#{row['sequence']}"}

    def _result(self, db, request_id, entry_id, *, committed):
        entry = self._entry(db, entry_id)
        missing = classification(entry) if entry["state"] != "removed" else []
        return self.response(request_id, "needs_clarification" if missing else "ok",
                             committed=committed, entry_id=entry_id, version=entry["version"],
                             data={"entry": entry}, clarification=missing)

    def _command(self, db, request_id, operation, args, now):
        def reply(code, **data):
            return self.response(request_id, code, **data)
        if operation == "wishlist.add":
            host = source_host(args["url"])
            details = args.get("details", {})
            self._validate_details(details)
            entry_id = "wi_" + uuid4().hex
            values = [details.get(key) for key in ("label", "purpose", "purchase_mode", "recipient", "note")]
            db.execute("""INSERT INTO entries
                (entry_id, owner_id, version, state, classification, label, purpose, purchase_mode,
                 recipient, note, created_at, updated_at) VALUES (?, ?, 1, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (entry_id, self.owner_id, "needs_details" if classification(details) else "complete",
                        *values, stamp(now), stamp(now)))
            # Per-entry permission only. No global shop approval or collection happens here.
            db.execute("""INSERT INTO offers
                (offer_id, entry_id, owner_id, url, shop_host, url_sha256,
                 collection_status, collection_generation) VALUES (?, ?, ?, ?, ?, ?, 'pending', 1)""",
                       ("wo_" + uuid4().hex, entry_id, self.owner_id, args["url"], host,
                        hashlib.sha256(args["url"].encode()).hexdigest()))
            return self._result(db, request_id, entry_id, committed=True)
        if operation == "wishlist.list":
            return self._list(db, request_id, args)
        entry = self._entry(db, args["entry_id"])
        if entry is None:
            return reply("not_found")
        if operation == "wishlist.get":
            result = self._result(db, request_id, entry["entry_id"], committed=False)
            if db.execute("PRAGMA user_version").fetchone()[0] >= 5:
                result["data"]["observations"] = [dict(row) for row in db.execute(
                    """SELECT observed_at, price, currency, availability, conditions_verification FROM observations
                    WHERE offer_id=? ORDER BY sequence DESC LIMIT 10""", (entry["offer_id"],))]
            return result
        if entry["version"] != args["expected_version"]:
            return reply("conflict")
        if entry["state"] == "removed" and operation not in ("wishlist.remove", "wishlist.restore"):
            return reply("not_found")
        updates = {}
        invalidate = False
        if operation == "wishlist.remove":
            if entry["state"] == "removed":
                return self._result(db, request_id, entry["entry_id"], committed=False)
            updates = {"removed_at": stamp(now)}
            db.execute("DELETE FROM clarifications WHERE entry_id=? AND owner_id=?",
                       (entry["entry_id"], self.owner_id))
            invalidate = True
        elif operation == "wishlist.restore":
            if entry["state"] != "removed":
                return reply("conflict")
            updates = {"removed_at": None}
            invalidate = True
        elif operation == "wishlist.update":
            self._validate_details(args["patch"])
            proposed = {**entry, **args["patch"]}
            missing = classification(proposed)
            if any(reason in missing for reason in ("gift_recurring", "self_recipient")):
                return reply("needs_clarification", entry_id=entry["entry_id"],
                             version=entry["version"], clarification=missing)
            updates = {**args["patch"], "classification": "needs_details" if missing else "complete"}
        elif operation == "wishlist.record_purchase":
            missing = classification(entry)
            if missing:
                return reply("needs_clarification", entry_id=entry["entry_id"],
                             version=entry["version"], clarification=missing)
            if entry["state"] == "purchased":
                return reply("conflict")
            purchased_at = instant(args["purchased_at"]) if "purchased_at" in args else now
            if purchased_at > now:
                return reply("invalid_request")
            updates = {"state": "purchased", "paused_until": None}
            if entry["purchase_mode"] == "recurring":
                updates = {"state": "paused", "paused_until": stamp(next_month(purchased_at, self.zone))}
            db.execute("""INSERT INTO purchases
                (owner_id, entry_id, request_id, purchased_at, recorded_at) VALUES (?, ?, ?, ?, ?)""",
                       (self.owner_id, entry["entry_id"], request_id, stamp(purchased_at), stamp(now)))
            invalidate = True
        elif operation == "wishlist.pause":
            if entry["state"] == "purchased" or instant(args["paused_until"]) <= now:
                return reply("invalid_request")
            updates = {"state": "paused", "paused_until": stamp(instant(args["paused_until"]))}
            invalidate = True
        elif operation == "wishlist.resume":
            if entry["state"] != "paused":
                return reply("conflict")
            updates = {"state": "active", "paused_until": None}
            invalidate = True
        updates.update(version=entry["version"] + 1, updated_at=stamp(now))
        # All keys are schema-limited fields or literals chosen above, never SQL from input.
        db.execute("UPDATE entries SET " + ", ".join(f"{key} = ?" for key in updates)
                   + " WHERE entry_id = ? AND owner_id = ?",
                   (*updates.values(), entry["entry_id"], self.owner_id))
        if invalidate:
            active = self._entry(db, entry["entry_id"])["state"] == "active"
            db.execute("""UPDATE offers SET collection_generation = collection_generation + 1,
                collection_status = CASE WHEN ? THEN 'pending' ELSE collection_status END,
                next_collection_at = CASE WHEN ? THEN '' ELSE next_collection_at END
                WHERE entry_id = ? AND owner_id = ?""",
                       (active, active, entry["entry_id"], self.owner_id))
        return self._result(db, request_id, entry["entry_id"], committed=True)

    @staticmethod
    def _validate_details(details):
        for key in ("label", "recipient"):
            if details.get(key) is not None and not details[key].strip():
                raise ValueError("Blank entry detail")

    def _list(self, db, request_id, args):
        state, purpose, limit = args.get("state", "default"), args.get("purpose"), args.get("limit", 20)
        binding = digest([self.owner_id, state, purpose, limit])[:32]
        after, ceiling = 0, db.execute("SELECT COALESCE(MAX(sequence), 0) FROM entries").fetchone()[0]
        if "cursor" in args:
            token = args["cursor"]
            try:
                payload, signature = token.split(".")
                expected = hmac.new(self.store.cursor_key, payload.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    raise ValueError("Invalid cursor")
                cursor_binding, after, ceiling = json.loads(base64.urlsafe_b64decode(payload))
                if cursor_binding != binding or type(after) is not int or type(ceiling) is not int:
                    raise ValueError("Invalid cursor binding")
            except (ValueError, TypeError, UnicodeError):
                return self.response(request_id, "invalid_request")
        where, params = ["owner_id = ?", "sequence > ?", "sequence <= ?"], [self.owner_id, after, ceiling]
        where.append("removed_at IS NOT NULL" if state == "removed" else "removed_at IS NULL")
        if state == "default":
            where.append("state IN ('active', 'paused')")
        elif state not in ("all", "removed"):
            where.append("state = ?")
            params.append(state)
        if purpose:
            where.append("purpose = ?")
            params.append(purpose)
        rows = db.execute("SELECT entry_id, sequence FROM entries WHERE " + " AND ".join(where)
                          + " ORDER BY sequence LIMIT ?", (*params, limit + 1)).fetchall()
        entries = []
        for row in rows[:limit]:
            entry = self._entry(db, row["entry_id"])
            if entries and len(canonical(entries + [entry])) > 48000:
                break
            entries.append(entry)
        cursor = None
        if len(rows) > len(entries):
            payload = base64.urlsafe_b64encode(canonical([binding, rows[len(entries) - 1]["sequence"], ceiling]).encode()).decode()
            cursor = payload + "." + hmac.new(self.store.cursor_key, payload.encode(), hashlib.sha256).hexdigest()
        return self.response(request_id, "ok", data={
            "entries": entries, "cursor": cursor})
