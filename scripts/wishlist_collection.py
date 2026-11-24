"""W3 durable collection admission. Only the domain service opens SQLite."""

from datetime import UTC, timedelta
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from scripts import wishlist_alza_history as alza_history
from scripts.wishlist_domain import canonical, digest, instant, stamp
from scripts.wishlist_extract import AVAILABILITY, amount

from scripts.wishlist_media import image_url

DAY = 86400
LEASE = 120
ADAPTERS = {"www.momenti.cz": "momenti-microdata-v1", "www.fabini.cz": "fabini-jsonld-v1",
            "www.ifantazie.cz": "ifantazie-microdata-v1", "www.alza.cz": "alza-public-price-v1",
            "www.intimissimi.com": "intimissimi-public-price-v1"}
JOB_KEYS = {"job_id", "attempt_id", "offer_id", "generation", "entry_version", "url_sha256", "adapter"}
REASONS = frozenset({
    "structured_offer_candidate", "unsupported_shop", "source_http_401", "source_http_403",
    "source_http_429", "source_http_error", "destination_denied", "redirect_denied",
    "network_failed", "fetch_timeout", "document_limit", "invalid_document", "unsupported_encoding",
    "unsupported_content_type", "offer_unverified", "lease_expired", "entry_changed", "source_out_of_stock",
})


def validate_result(result, *, shop=None):
    keys = {"status", "reason", "http_status", "retry_after"}
    if (not isinstance(result, dict) or not keys <= set(result) or set(result) - keys - {"offer", "image_url", "history"}
            or result["status"] not in ("ok", "blocked", "unsupported", "failed")
            or result["reason"] not in REASONS
            or (result["http_status"] is not None and (type(result["http_status"]) is not int
                                                      or not 100 <= result["http_status"] <= 599))
            or type(result["retry_after"]) is not int or not 0 <= result["retry_after"] <= 31536000):
        raise ValueError("collection_result")
    if result["status"] != "ok":
        if {"offer", "image_url", "history"} & result.keys() or result["reason"] in {"structured_offer_candidate", "source_out_of_stock"}:
            raise ValueError("collection_failed_offer")
        return
    if "image_url" in result and shop is not None and not image_url(shop, result["image_url"]):
        raise ValueError("collection_image")
    offer = result.get("offer")
    has_history = "history" in result
    if (result["http_status"] != 200 or result["reason"] not in {"structured_offer_candidate", "source_out_of_stock"}
            or not isinstance(offer, dict)
            or set(offer) != {"name", "sku", "price", "currency", "availability", "conditions_verification"}
            or any(not isinstance(offer[key], str) or not 1 <= len(offer[key]) <= maximum
                   or any(ord(c) < 32 for c in offer[key])
                   for key, maximum in (("name", 200), ("sku", 100)))
            or (offer["availability"] != "unknown" if has_history else offer["availability"] not in AVAILABILITY.values())
            or offer["conditions_verification"] != "not_verified"):
        raise ValueError("collection_offer")
    if result["reason"] == "source_out_of_stock":
        if offer["price"] is not None or offer["currency"] is not None or offer["availability"] != "out_of_stock":
            raise ValueError("collection_availability")
    elif (not isinstance(offer["price"], str) or not 1 <= len(offer["price"]) <= 12
          or amount(offer["price"]) != offer["price"] or offer["currency"] != "CZK"):
        raise ValueError("collection_price")
    if has_history:
        alza_history.validate_series(result["history"])
        if ((shop is not None and shop != alza_history.SHOP) or "image_url" in result
                or result["history"][-1][1] != offer["price"]):
            raise ValueError("collection_history")


class Collection:
    def __init__(self, domain, *, alza_history_enabled=False):
        self.domain = domain
        self.adapters = ADAPTERS | ({alza_history.SHOP: alza_history.ADAPTER} if alza_history_enabled else {})

    def handle(self, request):
        try:
            if (not isinstance(request, dict) or type(request.get("protocol_version")) is not int
                    or request["protocol_version"] != 1):
                raise ValueError("collection_request")
            with self.domain.store.transaction() as db:
                if (set(request) == {"protocol_version", "operation", "claim_id"}
                        and request["operation"] == "collection.claim"
                        and isinstance(request["claim_id"], str)
                        and re.fullmatch(r"[0-9a-f]{32}", request["claim_id"])):
                    return self.claim(db, request["claim_id"])
                if (set(request) == {"protocol_version", "operation", "job", "result"}
                        and request["operation"] == "collection.complete"):
                    return self.complete(db, request["job"], request["result"])
                raise ValueError("collection_request")
        except (TypeError, ValueError, KeyError, OverflowError):
            return {"protocol_version": 1, "code": "invalid_request"}
        except sqlite3.Error:
            return {"protocol_version": 1, "code": "unavailable"}

    @staticmethod
    def job(row):
        return {key: row[key] for key in JOB_KEYS}

    @staticmethod
    def expire(db, row, now):
        db.execute("UPDATE collection_jobs SET status='expired', completed_at=? WHERE job_id=?",
                   (stamp(now), row["job_id"]))
        db.execute("""UPDATE offers SET collection_status='failed', collection_reason='lease_expired',
            last_attempt_at=?, last_http_status=NULL, next_collection_at=?
            WHERE offer_id=? AND collection_generation=?""",
                   (row["started_at"], stamp(now + timedelta(hours=1)), row["offer_id"], row["generation"]))

    def claim(self, db, claim_id):
        now = self.domain.clock()
        current = stamp(now)
        # The authenticated scan is recorded even when there is no due offer.
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('collection_last_scan', ?)", (current,))
        # Expired work cannot overwrite a later run. Retry a crashed process after an hour.
        expired = db.execute("SELECT * FROM collection_jobs WHERE status='running' AND expires_at<=?",
                             (current,)).fetchall()
        for row in expired:
            self.expire(db, row, now)
        # A repeated claim never grants another job, including after completion/expiry.
        previous = db.execute("SELECT * FROM collection_jobs WHERE claim_id=?", (claim_id,)).fetchone()
        if previous:
            if previous["status"] != "running":
                return {"protocol_version": 1, "code": "idle"}
            offer = db.execute("SELECT url FROM offers WHERE offer_id=?", (previous["offer_id"],)).fetchone()
            return {"protocol_version": 1, "code": "ok", "job": self.job(previous),
                    "url": offer["url"], "expires_at": previous["expires_at"]}
        if db.execute("SELECT 1 FROM collection_jobs WHERE status='running'").fetchone():
            return {"protocol_version": 1, "code": "idle"}
        # Resume calendar pauses without depending on a model or touching removed entries.
        paused = db.execute("""SELECT entry_id FROM entries WHERE owner_id=? AND state='paused'
            AND removed_at IS NULL AND paused_until<=? LIMIT 20""", (self.domain.owner_id, current)).fetchall()
        for row in paused:
            db.execute("""UPDATE entries SET state='active', paused_until=NULL, version=version+1,
                updated_at=? WHERE entry_id=?""", (current, row["entry_id"]))
            db.execute("""UPDATE offers SET collection_generation=collection_generation+1,
                collection_status='pending', next_collection_at='' WHERE entry_id=?""", (row["entry_id"],))
            db.execute("""INSERT INTO schedule_events (entry_id, entry_version, operation, occurred_at)
                SELECT entry_id, version, 'pause_expired', ? FROM entries WHERE entry_id=?""",
                       (current, row["entry_id"]))
        row = db.execute("""SELECT o.*, e.version AS entry_version FROM offers o JOIN entries e
            ON e.entry_id=o.entry_id AND e.owner_id=o.owner_id
            WHERE e.owner_id=? AND e.state='active' AND e.removed_at IS NULL AND o.next_collection_at<=?
            AND NOT EXISTS (SELECT 1 FROM collection_hosts h WHERE h.shop_host=o.shop_host AND h.retry_at>?)
            AND NOT EXISTS (SELECT 1 FROM collection_jobs j WHERE j.offer_id=o.offer_id AND j.status='running')
            ORDER BY o.next_collection_at, e.sequence LIMIT 1""", (self.domain.owner_id, current, current)).fetchone()
        if row is None:
            return {"protocol_version": 1, "code": "idle"}
        job = {"job_id": uuid4().hex, "attempt_id": uuid4().hex, "offer_id": row["offer_id"],
               "generation": row["collection_generation"], "entry_version": row["entry_version"],
               "url_sha256": row["url_sha256"], "adapter": self.adapters.get(row["shop_host"], "unsupported")}
        expires_at = stamp(now + timedelta(seconds=LEASE))
        db.execute("""INSERT INTO collection_jobs
            (job_id, claim_id, attempt_id, offer_id, generation, entry_version, url_sha256, adapter,
             started_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                   (job["job_id"], claim_id, job["attempt_id"], job["offer_id"], job["generation"],
                    job["entry_version"], job["url_sha256"], job["adapter"], current, expires_at))
        return {"protocol_version": 1, "code": "ok", "job": job, "url": row["url"], "expires_at": expires_at}

    def complete(self, db, job, result):
        if not isinstance(job, dict) or set(job) != JOB_KEYS:
            raise ValueError("collection_job")
        validate_result(result)
        row = db.execute("SELECT * FROM collection_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        if row is None or canonical(self.job(row)) != canonical(job):
            return {"protocol_version": 1, "code": "denied"}
        fingerprint = digest(result)
        if row["status"] == "completed":
            return {"protocol_version": 1, "code": "ok" if row["result_sha256"] == fingerprint else "conflict"}
        now = self.domain.clock()
        current = stamp(now)
        if row["status"] == "running" and instant(row["expires_at"]) <= now:
            self.expire(db, row, now)
            return {"protocol_version": 1, "code": "stale"}
        offer = db.execute("""SELECT o.*, e.version, e.state, e.removed_at FROM offers o
            JOIN entries e ON e.entry_id=o.entry_id AND e.owner_id=o.owner_id
            WHERE o.offer_id=? AND e.owner_id=?""", (row["offer_id"], self.domain.owner_id)).fetchone()
        if (row["status"] != "running" or offer is None
                or offer["state"] != "active" or offer["removed_at"] is not None
                or offer["version"] != row["entry_version"]
                or offer["collection_generation"] != row["generation"]
                or offer["url_sha256"] != row["url_sha256"]
                or hashlib.sha256(offer["url"].encode()).hexdigest() != row["url_sha256"]):
            if row["status"] == "running":
                db.execute("UPDATE collection_jobs SET status='discarded', completed_at=? WHERE job_id=?",
                           (current, row["job_id"]))
            return {"protocol_version": 1, "code": "stale"}
        if "image_url" in result and not image_url(offer["shop_host"], result["image_url"]):
            raise ValueError("collection_image")
        if result["status"] == "ok":
            candidate = result["offer"]
            imported = row["adapter"] == alza_history.ADAPTER
            if row["adapter"] not in {*ADAPTERS.values(), alza_history.ADAPTER} or imported != ("history" in result):
                raise ValueError("unsupported_adapter")
            if imported:
                today = now.astimezone(UTC).date()
                if (alza_history.product(offer["url"])[0] != candidate["sku"]
                        or not (today - timedelta(days=1)).isoformat() <= result["history"][-1][0] <= today.isoformat()):
                    raise ValueError("collection_history_identity")
            # A catalog ID verifies the requested URL independently from a merchant SKU.
            previous = db.execute("""SELECT sku, currency FROM observations WHERE offer_id=? AND adapter<>?
                ORDER BY sequence DESC LIMIT 1""", (row["offer_id"], alza_history.ADAPTER)).fetchone()
            key = "collection_availability:" + row["offer_id"]
            saved = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            availability = json.loads(saved[0]) if saved else {}
            pinned_sku = previous["sku"] if previous else (availability.get("sku") if availability.get("availability") != "unknown" else None)
            if ((not imported and pinned_sku is not None and candidate["sku"] != pinned_sku) or (
                    previous and candidate["price"] is not None and candidate["currency"] != previous["currency"])):
                result = {"status": "failed", "reason": "offer_unverified", "http_status": 200, "retry_after": 0}
            else:
                # Retain the last verified stock state independently of price
                # history and compactable worker payloads, in this transaction.
                if imported:
                    alza_history.save_backfill(db, row["offer_id"], result["history"], now, self.domain.zone)
                current_stock = candidate["availability"]
                prior_stock = availability.get("availability")
                stock_change = availability.get("change") if current_stock == prior_stock else None
                if current_stock != "unknown" and prior_stock not in (None, "unknown", current_stock):
                    stock_change = {"at": current, "previous": prior_stock}
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, canonical({
                    "sku": candidate["sku"], "availability": current_stock, "checked_at": current,
                    **({"change": stock_change} if stock_change else {}),
                    **({"image_url": result["image_url"]} if "image_url" in result else {})})))
                if candidate["price"] is not None:
                    db.execute("""INSERT INTO observations
                    (job_id, offer_id, observed_at, adapter, price, currency, availability, name, sku, conditions_verification)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (row["job_id"], row["offer_id"], current, row["adapter"], candidate["price"],
                            candidate["currency"], candidate["availability"], candidate["name"], candidate["sku"],
                            candidate["conditions_verification"]))
                    db.execute("UPDATE offers SET last_observed_at=? WHERE offer_id=?", (current, row["offer_id"]))
        # Keep the submitted digest even if domain validation downgraded the observation.
        db.execute("""UPDATE collection_jobs SET status='completed', completed_at=?, result_sha256=?, result=?
            WHERE job_id=?""", (current, fingerprint, canonical(result), row["job_id"]))
        db.execute("""UPDATE offers SET collection_status=?, collection_reason=?, last_http_status=?,
            last_attempt_at=?, next_collection_at=? WHERE offer_id=?""",
                   (result["status"], result["reason"], result["http_status"], current,
                    stamp(now + timedelta(seconds=max(DAY, result["retry_after"]))), row["offer_id"]))
        if result["http_status"] == 429 or result["retry_after"]:
            retry_at = stamp(now + timedelta(seconds=max(DAY, result["retry_after"])))
            db.execute("""INSERT INTO collection_hosts VALUES (?, ?) ON CONFLICT(shop_host)
                DO UPDATE SET retry_at=MAX(retry_at, excluded.retry_at)""", (offer["shop_host"], retry_at))
        return {"protocol_version": 1, "code": "ok"}
