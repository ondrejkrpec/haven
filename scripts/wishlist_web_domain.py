"""Owner-only web operations executed by the existing single database writer."""

import base64
import calendar
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import json
import re
import sqlite3

from scripts.wishlist_contract import CATEGORIES, COMMAND_VALIDATOR
from scripts.wishlist_domain import canonical, digest, instant, stamp
from scripts.wishlist_socket import PeerServer

ENTRY = re.compile(r"wi_[a-f0-9]{32}\Z")
REQUEST = re.compile(r"[a-f0-9]{32}\Z")
STATISTICS_ENTRIES = 10000
STATISTICS_PURCHASES = 128000


class StatisticsLimit(ValueError):
    """Refuse an incomplete summary rather than returning a truncated total."""


def statistics(domain, db, args):
    """Read one owner-scoped snapshot; sum exact integer halere without SQL overflow."""
    if set(args) - {"period", "date", "filters"}:
        raise ValueError("statistics_arguments")
    now = domain.clock()
    period = args.get("period", "month")
    selected = args.get("date", now.astimezone(domain.zone).date().isoformat())
    if not isinstance(selected, str) or period not in ("month", "year", "all"):
        raise ValueError("statistics_period")
    date = datetime.strptime(selected, "%Y-%m-%d").date()
    if date.isoformat() != selected or date > now.astimezone(domain.zone).date():
        raise ValueError("statistics_date")
    start, end = None, None
    if period != "all":
        start = date.replace(month=1 if period == "year" else date.month, day=1).isoformat()
        end = date.replace(month=12 if period == "year" else date.month,
                           day=31 if period == "year" else calendar.monthrange(date.year, date.month)[1]).isoformat()
    filters = {"state": "default", "category": "", "search": "", "shop": "", "mode": "", "stock": ""}
    supplied = args.get("filters", {})
    if not isinstance(supplied, dict) or set(supplied) - set(filters) or any(not isinstance(v, str) for v in supplied.values()):
        raise ValueError("statistics_filters")
    filters.update(supplied)
    if (filters["state"] not in ("default", "all", "purchased", "removed")
            or filters["category"] not in ("", *CATEGORIES)
            or filters["mode"] not in ("", "once", "recurring")
            or filters["stock"] not in ("", "unknown", "in_stock", "out_of_stock", "preorder", "backorder", "limited", "discontinued")
            or len(filters["search"]) > 200 or len(filters["shop"]) > 253):
        raise ValueError("statistics_filters")
    candidates = db.execute("""SELECT entry_id FROM entries WHERE owner_id=? AND entry_id IN
        (SELECT entry_id FROM purchases WHERE owner_id=? AND voided_at IS NULL) LIMIT ?""",
                            (domain.owner_id, domain.owner_id, STATISTICS_ENTRIES + 1)).fetchall()
    if len(candidates) > STATISTICS_ENTRIES:
        raise StatisticsLimit("statistics_entries")
    matching = set()
    for row in candidates:
        entry = domain._entry(db, row["entry_id"])
        state = entry["state"]
        expected = filters["state"]
        if (expected == "default" and state != "active"
                or expected == "all" and state == "removed"
                or expected not in ("default", "all") and state != expected):
            continue
        availability = (entry["latest_availability"] or entry["latest_observation"] or {}).get("availability", "unknown")
        if (filters["shop"] and entry["shop_host"] != filters["shop"]
                or filters["mode"] and entry["purchase_mode"] != filters["mode"]
                or filters["stock"] and availability != filters["stock"]):
            continue
        label = entry["label"]
        if not label:
            named = db.execute("SELECT name FROM observations WHERE offer_id=? ORDER BY sequence DESC LIMIT 1", (entry["offer_id"],)).fetchone()
            label = named[0] if named else entry["shop_host"]
        if filters["search"].strip().lower() in label.lower():
            matching.add(entry["entry_id"])
    groups = {category: {"count": 0, "unknown_price": 0, "unknown_reference": 0, "spending": 0, "savings": 0}
              for category in CATEGORIES if not filters["category"] or category == filters["category"]}
    rows = db.execute("SELECT * FROM purchases WHERE owner_id=? AND voided_at IS NULL ORDER BY sequence LIMIT ?",
                      (domain.owner_id, STATISTICS_PURCHASES + 1))
    for index, purchase in enumerate(rows):
        if index == STATISTICS_PURCHASES:
            raise StatisticsLimit("statistics_purchases")
        if purchase["entry_id"] not in matching or purchase["category"] not in groups:
            continue
        purchased = purchase["purchase_date"] or instant(purchase["purchased_at"]).astimezone(domain.zone).date().isoformat()
        if start and not start <= purchased <= end:
            continue
        group = groups[purchase["category"]]
        group["count"] += 1
        price, quantity, reference = purchase["unit_price"], purchase["quantity"], purchase["reference_price"]
        if price is None:
            group["unknown_price"] += 1
        else:
            group["spending"] += price * quantity
            if reference is None:
                group["unknown_reference"] += 1
            else:
                group["savings"] += (reference - price) * quantity
    total = {key: sum(group[key] for group in groups.values()) for key in ("count", "unknown_price", "unknown_reference", "spending", "savings")}

    def project(group):
        # Decimal strings preserve halere beyond JavaScript's safe integer range.
        return {**group,
                "spending": str(group["spending"]) if not group["count"] or group["count"] > group["unknown_price"] else None,
                "savings": str(group["savings"]) if not group["count"] or group["count"] > group["unknown_price"] + group["unknown_reference"] else None}

    return {"period": period, "date": selected, "start": start, "end": end, "generated_at": stamp(now),
            "total": project(total), "categories": [{"category": category, **project(group)} for category, group in groups.items()]}


def activate(domain):
    """One irreversible, receipt-independent feature migration; old data is retained."""
    with domain.store.transaction() as db:
        if db.execute("SELECT 1 FROM metadata WHERE key='web_features_enabled'").fetchone():
            return
        db.execute("""UPDATE offers SET collection_generation=collection_generation+1,
            next_collection_at=CASE WHEN entry_id IN (SELECT entry_id FROM entries WHERE state='paused')
            THEN '' ELSE next_collection_at END WHERE owner_id=?""", (domain.owner_id,))
        db.execute("""UPDATE entries SET category=CASE WHEN purpose='gift' THEN 'gifts' ELSE category END,
            notifications_muted_until=paused_until, paused_until=NULL,
            state=CASE WHEN state='paused' THEN 'active' ELSE state END,
            purchase_mode=coalesce(purchase_mode,'once'), classification='complete',
            version=version+1, lifecycle_revision=lifecycle_revision+1 WHERE owner_id=?""", (domain.owner_id,))
        db.execute("INSERT INTO metadata VALUES ('web_features_enabled', ?)", (stamp(domain.clock()),))


def cents(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,8}(?:[.,][0-9]{1,2})?", value):
        raise ValueError("price")
    return int(Decimal(value.replace(",", ".")) * 100)


def stats(domain, db, entry, at, *, include_change=False):
    first = at.astimezone(domain.zone).date() - timedelta(days=89)
    cutoff = stamp(datetime.combine(first, time(), domain.zone))
    rows = db.execute("""SELECT price, observed_at FROM observations
        WHERE offer_id=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at, sequence LIMIT 20001""",
                      (entry["offer_id"], cutoff, stamp(at))).fetchall()
    if len(rows) > 20000:
        raise ValueError("history_limit")
    days = {}
    minimum = None
    price_change = None
    previous = None
    for row in rows:
        price = cents(row["price"])
        day = instant(row["observed_at"]).astimezone(domain.zone).date().isoformat()
        total, count = days.get(day, (0, 0))
        days[day] = (total + price, count + 1)
        minimum = price if minimum is None else min(minimum, price)
        if include_change and previous is not None and price != previous:
            price_change = {"at": row["observed_at"], "delta": price - previous}
        previous = price
    average = (int((sum(Decimal(total) / count for total, count in days.values()) / len(days))
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP)) if days else None)
    observation = entry["latest_observation"]
    unavailable = (entry["latest_availability"] or {}).get("availability") == "out_of_stock"
    current = None if unavailable or observation is None else cents(observation["price"])
    saving = average - current if average is not None and current is not None else None
    result = {"average": average, "minimum": minimum, "current": current, "saving": saving,
            "percentage": saving / average * 100 if saving is not None and average else None,
            "measured_days": len(days)}
    if include_change and rows:
        # A price immediately preceding the window can establish its first transition.
        if price_change is None:
            older = db.execute("""SELECT price FROM observations WHERE offer_id=? AND observed_at<?
                ORDER BY observed_at DESC, sequence DESC LIMIT 1""", (entry["offer_id"], cutoff)).fetchone()
            if older or len(rows) > 1:
                price_change = {"at": rows[0]["observed_at"], "delta": previous - cents(older["price"]) if older else 0}
        result["price_change"] = price_change
    return result


def purchase_values(domain, db, entry, args, now, *, require_price=False):
    date = args.get("purchase_date", instant(args["purchased_at"]).astimezone(domain.zone).date().isoformat()
                    if "purchased_at" in args else now.astimezone(domain.zone).date().isoformat())
    parsed = datetime.strptime(date, "%Y-%m-%d").date()
    if parsed.isoformat() != date or parsed > now.astimezone(domain.zone).date():
        raise ValueError("purchase_date")
    if "purchased_at" in args and instant(args["purchased_at"]) > now:
        raise ValueError("purchase_time")
    quantity = args.get("quantity", 1)
    if type(quantity) is not int or not 1 <= quantity <= 9999:
        raise ValueError("purchase_quantity")
    price = cents(args["unit_price"]) if "unit_price" in args else None
    if price is None and require_price:
        raise ValueError("purchase_price")
    at = min(now, datetime.combine(parsed + timedelta(days=1), time(), domain.zone) - timedelta(microseconds=1))
    return {"date": date, "quantity": quantity, "price": price, "reference": stats(domain, db, entry, at)["average"]}


def lifecycle(domain, db, request_id, operation, args, entry, now):
    if entry["state"] == "purchased":
        return domain.response(request_id, "conflict")
    if operation == "wishlist.record_purchase":
        values = purchase_values(domain, db, entry, args, now)
        state = "purchased" if entry["purchase_mode"] == "once" else "active"
        muted = None
        if state == "active":
            purchased = now if values["date"] == now.astimezone(domain.zone).date().isoformat() else datetime.combine(datetime.fromisoformat(values["date"]).date(), time(12), domain.zone)
            muted = stamp(purchased + timedelta(days=30))
        db.execute("""INSERT INTO purchases (owner_id,entry_id,request_id,purchased_at,recorded_at,
            unit_price,quantity,purchase_date,reference_price,previous_state,previous_muted_until,lifecycle_revision,category)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (domain.owner_id, entry["entry_id"], request_id,
            args.get("purchased_at", stamp(now)), stamp(now), values["price"], values["quantity"], values["date"],
            values["reference"], entry["state"], entry["notifications_muted_until"], entry["lifecycle_revision"] + 1, entry["category"]))
    elif operation == "wishlist.pause":
        if instant(args["paused_until"]) <= now:
            raise ValueError("pause_date")
        state, muted = "active", stamp(instant(args["paused_until"]))
    else:
        if not entry["notifications_muted_until"]:
            return domain.response(request_id, "conflict")
        state, muted = "active", None
    db.execute("""UPDATE entries SET state=?, paused_until=NULL, notifications_muted_until=?,
        lifecycle_revision=lifecycle_revision+1,version=version+1,updated_at=?
        WHERE entry_id=? AND owner_id=?""", (state, muted, stamp(now), entry["entry_id"], domain.owner_id))
    if state == "purchased":
        db.execute("UPDATE offers SET collection_generation=collection_generation+1 WHERE entry_id=? AND owner_id=?",
                   (entry["entry_id"], domain.owner_id))
    return domain._result(db, request_id, entry["entry_id"], committed=True)


class WebApplication:
    def __init__(self, domain):
        self.domain = domain

    def project(self, db, entry):
        keys = ("entry_id", "version", "label", "url", "category", "purchase_mode", "note", "state", "removed_at",
                "created_at", "updated_at", "notifications_muted_until", "latest_observation", "latest_availability",
                "collection_status", "collection_reason", "last_attempt_at", "next_collection_at")
        projected = {key: entry[key] for key in keys}
        if not projected["label"]:
            named = db.execute("SELECT name FROM observations WHERE offer_id=? ORDER BY sequence DESC LIMIT 1", (entry["offer_id"],)).fetchone()
            if named:
                projected["label"] = named[0]
        return {**projected, "stock_change": entry.get("stock_change"),
                "stats90": stats(self.domain, db, entry, self.domain.clock(), include_change=True)}

    def cursor(self, kind, entry_id, after, ceiling):
        payload = base64.urlsafe_b64encode(canonical([kind, self.domain.owner_id, entry_id, after, ceiling]).encode()).decode()
        return payload + "." + hmac.new(self.domain.store.cursor_key, payload.encode(), hashlib.sha256).hexdigest()

    def page(self, db, kind, entry, args):
        table = "observations" if kind == "history" else "purchases"
        field, value = ("offer_id", entry["offer_id"]) if kind == "history" else ("entry_id", entry["entry_id"])
        after = 0
        ceiling = db.execute(f"SELECT coalesce(MAX(sequence),0) FROM {table} WHERE {field}=?", (value,)).fetchone()[0]
        if "cursor" in args:
            payload, signature = args["cursor"].split(".")
            expected = hmac.new(self.domain.store.cursor_key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("cursor")
            binding, owner, target, after, ceiling = json.loads(base64.urlsafe_b64decode(payload))
            if (binding, owner, target) != (kind, self.domain.owner_id, entry["entry_id"]) or any(type(v) is not int or v < 0 for v in (after, ceiling)):
                raise ValueError("cursor")
        rows = db.execute(f"SELECT * FROM {table} WHERE {field}=? AND sequence>? AND sequence<=? ORDER BY sequence LIMIT 129",
                          (value, after, ceiling)).fetchall()
        data = []
        for row in rows[:128]:
            if kind == "history":
                data.append({"at": row["observed_at"], "cents": cents(row["price"])})
            else:
                data.append({"id": row["sequence"], "unitCents": row["unit_price"], "quantity": row["quantity"],
                             "date": row["purchase_date"] or instant(row["purchased_at"]).astimezone(self.domain.zone).date().isoformat(),
                             "reference": row["reference_price"], "category": row["category"],
                             "voidedAt": row["voided_at"], "correctedAt": row["corrected_at"],
                             "canVoid": row["previous_state"] is not None and row["lifecycle_revision"] == entry["lifecycle_revision"]})
        cursor = self.cursor(kind, entry["entry_id"], rows[127]["sequence"], ceiling) if len(rows) > 128 else None
        return {kind: data, "cursor": cursor}

    def handle(self, request):
        request_id = None
        try:
            if (not self.domain.web_enabled or not isinstance(request, dict)
                    or set(request) != {"protocol_version", "operation", "arguments", "request_id"}
                    or type(request["protocol_version"]) is not int or request["protocol_version"] != 1
                    or not isinstance(request["request_id"], str) or not REQUEST.fullmatch(request["request_id"])
                    or not isinstance(request["operation"], str) or not request["operation"].startswith("web.")
                    or not isinstance(request["arguments"], dict)):
                raise ValueError("web_request")
            request_id = digest(["web", self.domain.owner_id, request["request_id"]])
            operation, args = request["operation"][4:], request["arguments"]
            read = operation in ("list", "get", "history", "purchases", "statistics")
            with self.domain.store.transaction() as db:
                if read:
                    return self.read(db, request_id, operation, args)
                previous = db.execute("SELECT * FROM receipts WHERE request_id=? AND owner_id=?", (request_id, self.domain.owner_id)).fetchone()
                fingerprint = digest({"operation": operation, "arguments": args})
                if previous:
                    return json.loads(previous["response"]) if previous["fingerprint"] == fingerprint else self.domain.response(request_id, "conflict")
                result = self.change(db, request_id, operation, args)
                if result["committed"]:
                    db.execute("""INSERT INTO events (owner_id,entry_id,request_id,operation,entry_version,occurred_at)
                        VALUES (?,?,?,?,?,?)""", (self.domain.owner_id, result["entry_id"], request_id, "web." + operation,
                        result["version"], stamp(self.domain.clock())))
                db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)", (request_id, self.domain.owner_id, fingerprint, canonical(result), stamp(self.domain.clock())))
                if len(canonical(result).encode()) > 65536:
                    raise ValueError("response_size")
            if result["committed"] and self.domain.collection_wakeup:
                self.domain.collection_wakeup()
            return result
        except StatisticsLimit:
            return self.domain.response(request_id, "unavailable")
        except (ValueError, TypeError, KeyError, OverflowError):
            return self.domain.response(request_id, "invalid_request")
        except sqlite3.Error:
            return self.domain.response(request_id, "unavailable")

    def read(self, db, request_id, operation, args):
        if operation == "statistics":
            return self.domain.response(request_id, "ok", data=statistics(self.domain, db, args))
        if operation == "list":
            if not COMMAND_VALIDATOR.is_valid({"operation": "wishlist.list", "arguments": args}) or args.get("limit", 15) > 20:
                raise ValueError("list")
            result = self.domain._list(db, request_id, {"limit": 15, **args})
            if result["code"] == "ok":
                result["data"]["entries"] = [self.project(db, entry) for entry in result["data"]["entries"]]
            return result
        if set(args) - {"entry_id", "cursor"} or not ENTRY.fullmatch(args.get("entry_id", "")):
            raise ValueError("entry")
        if "cursor" in args and (operation == "get" or not isinstance(args["cursor"], str) or len(args["cursor"]) > 512):
            raise ValueError("cursor")
        entry = self.domain._entry(db, args["entry_id"])
        if entry is None:
            return self.domain.response(request_id, "not_found")
        data = {"entry": self.project(db, entry)} if operation == "get" else self.page(db, operation, entry, args)
        return self.domain.response(request_id, "ok", data=data)

    def change(self, db, request_id, operation, args):
        mapping = {"add": "add", "update": "update", "purchase": "record_purchase", "remove": "remove", "restore": "restore", "unmute": "resume"}
        if operation in mapping:
            command = {"operation": "wishlist." + mapping[operation], "arguments": args}
            if not COMMAND_VALIDATOR.is_valid(command):
                raise ValueError("command")
            if operation == "purchase" and "unit_price" not in args:
                raise ValueError("price_required")
            if operation in ("add", "update"):
                fields = args.get("details", {}) if operation == "add" else args["patch"]
                if set(fields) - {"label", "category", "purchase_mode", "note"}:
                    raise ValueError("legacy_fields")
            result = self.domain._command(db, request_id, command["operation"], args, self.domain.clock())
        else:
            allowed = {"refresh": set(), "correct_purchase": {"purchase_id", "unit_price", "quantity", "purchase_date"}, "void_purchase": {"purchase_id"}}
            if operation not in allowed or set(args) - {"entry_id", "expected_version"} - allowed[operation]:
                raise ValueError("operation")
            if not ENTRY.fullmatch(args.get("entry_id", "")) or type(args.get("expected_version")) is not int:
                raise ValueError("entry_version")
            entry = self.domain._entry(db, args["entry_id"])
            if entry is None or entry["removed_at"]:
                return self.domain.response(request_id, "not_found")
            if entry["version"] != args["expected_version"]:
                return self.domain.response(request_id, "conflict")
            if operation == "refresh":
                return self.refresh(db, request_id, entry)
            result = self.purchase_change(db, request_id, operation, args, entry)
        if result.get("data", {}).get("entry"):
            result["data"]["entry"] = self.project(db, result["data"]["entry"])
        return result

    def refresh(self, db, request_id, entry):
        if entry["state"] != "active":
            return self.domain.response(request_id, "conflict")
        now = self.domain.clock()
        retry = db.execute("SELECT retry_at FROM collection_hosts WHERE shop_host=?", (entry["shop_host"],)).fetchone()
        dates = [instant(retry[0])] if retry else []
        if entry["last_attempt_at"]:
            dates.append(instant(entry["last_attempt_at"]) + timedelta(minutes=10))
        if dates and max(dates) > now:
            return self.domain.response(request_id, "ok", data={"refresh_status": "postponed", "retry_at": stamp(max(dates))})
        if db.execute("SELECT 1 FROM collection_jobs WHERE offer_id=? AND status='running'", (entry["offer_id"],)).fetchone():
            return self.domain.response(request_id, "ok", data={"refresh_status": "running"})
        db.execute("UPDATE offers SET next_collection_at=? WHERE offer_id=?", (stamp(now), entry["offer_id"]))
        db.execute("UPDATE entries SET version=version+1, updated_at=? WHERE entry_id=? AND owner_id=?",
                   (stamp(now), entry["entry_id"], self.domain.owner_id))
        result = self.domain._result(db, request_id, entry["entry_id"], committed=True)
        result["data"] = {"refresh_status": "queued"}
        return result

    def purchase_change(self, db, request_id, operation, args, entry):
        purchase_id = args.get("purchase_id")
        if type(purchase_id) is not int or purchase_id < 1:
            raise ValueError("purchase_id")
        row = db.execute("SELECT * FROM purchases WHERE sequence=? AND owner_id=? AND entry_id=?",
                         (purchase_id, self.domain.owner_id, entry["entry_id"])).fetchone()
        if row is None:
            return self.domain.response(request_id, "not_found")
        if row["voided_at"]:
            return self.domain.response(request_id, "conflict")
        now = self.domain.clock()
        if operation == "correct_purchase":
            values = purchase_values(self.domain, db, entry, args, now, require_price=True)
            reference = row["reference_price"] if values["date"] == row["purchase_date"] else values["reference"]
            db.execute("""UPDATE purchases SET unit_price=?,quantity=?,purchase_date=?,reference_price=?,corrected_at=?
                WHERE sequence=?""", (values["price"], values["quantity"], values["date"], reference, stamp(now), purchase_id))
            if values["date"] != row["purchase_date"] and row["lifecycle_revision"] == entry["lifecycle_revision"] and entry["state"] == "active" and entry["purchase_mode"] == "recurring":
                db.execute("UPDATE entries SET notifications_muted_until=? WHERE entry_id=? AND owner_id=?",
                           (stamp(datetime.combine(datetime.fromisoformat(values["date"]).date(), time(12), self.domain.zone) + timedelta(days=30)),
                            entry["entry_id"], self.domain.owner_id))
        else:
            if row["previous_state"] is None or row["lifecycle_revision"] != entry["lifecycle_revision"]:
                return self.domain.response(request_id, "conflict")
            db.execute("UPDATE purchases SET voided_at=? WHERE sequence=?", (stamp(now), purchase_id))
            db.execute("""UPDATE entries SET state=?,paused_until=NULL,notifications_muted_until=?,lifecycle_revision=lifecycle_revision+1
                WHERE entry_id=? AND owner_id=?""", (row["previous_state"], row["previous_muted_until"], entry["entry_id"], self.domain.owner_id))
            previous = db.execute("SELECT MAX(sequence) FROM purchases WHERE entry_id=? AND owner_id=? AND voided_at IS NULL",
                                  (entry["entry_id"], self.domain.owner_id)).fetchone()[0]
            if previous is not None:
                db.execute("UPDATE purchases SET lifecycle_revision=? WHERE sequence=?", (entry["lifecycle_revision"] + 1, previous))
            db.execute("UPDATE offers SET collection_generation=collection_generation+1, next_collection_at='' WHERE entry_id=? AND owner_id=?",
                       (entry["entry_id"], self.domain.owner_id))
        db.execute("UPDATE entries SET version=version+1,updated_at=? WHERE entry_id=? AND owner_id=?",
                   (stamp(now), entry["entry_id"], self.domain.owner_id))
        return self.domain._result(db, request_id, entry["entry_id"], committed=True)


class WebServer(PeerServer):
    def __init__(self, domain, path, **kwargs):
        self.application = WebApplication(domain)
        super().__init__(path, **kwargs)

    async def respond(self, request):
        return self.application.handle(request)
