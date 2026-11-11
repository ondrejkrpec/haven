"""W3 durable collection admission. Only the domain service opens SQLite."""

from datetime import timedelta
import hashlib
import re
import sqlite3
from uuid import uuid4

from scripts.wishlist_domain import canonical, digest, instant, stamp
from scripts.wishlist_extract import AVAILABILITY, amount

DAY = 86400
LEASE = 120
ADAPTERS = {"www.momenti.cz": "momenti-microdata-v1", "www.fabini.cz": "fabini-jsonld-v1",
            "www.ifantazie.cz": "ifantazie-microdata-v1", "www.alza.cz": "alza-public-price-v1"}
JOB_KEYS = {"job_id", "attempt_id", "offer_id", "generation", "entry_version", "url_sha256", "adapter"}
REASONS = frozenset({
    "structured_offer_candidate", "unsupported_shop", "source_http_401", "source_http_403",
    "source_http_429", "source_http_error", "destination_denied", "redirect_denied",
    "network_failed", "fetch_timeout", "document_limit", "invalid_document", "unsupported_encoding",
    "unsupported_content_type", "offer_unverified", "lease_expired", "entry_changed",
})


def validate_result(result):
    keys = {"status", "reason", "http_status", "retry_after"}
    if (not isinstance(result, dict) or not keys <= set(result) or set(result) - keys - {"offer"}
            or result["status"] not in ("ok", "blocked", "unsupported", "failed")
            or result["reason"] not in REASONS
            or (result["http_status"] is not None and (type(result["http_status"]) is not int
                                                      or not 100 <= result["http_status"] <= 599))
            or type(result["retry_after"]) is not int or not 0 <= result["retry_after"] <= 31536000):
        raise ValueError("collection_result")
    if result["status"] != "ok":
        if "offer" in result or result["reason"] == "structured_offer_candidate":
            raise ValueError("collection_failed_offer")
        return
    offer = result.get("offer")
    if (result["http_status"] != 200 or result["reason"] != "structured_offer_candidate"
            or not isinstance(offer, dict)
            or set(offer) != {"name", "sku", "price", "currency", "availability", "conditions_verification"}
            or any(not isinstance(offer[key], str) or not 1 <= len(offer[key]) <= maximum
                   or any(ord(c) < 32 for c in offer[key])
                   for key, maximum in (("name", 200), ("sku", 100), ("price", 12)))
            or amount(offer["price"]) != offer["price"] or offer["currency"] != "CZK"
            or offer["availability"] not in AVAILABILITY.values()
            or offer["conditions_verification"] != "not_verified"):
        raise ValueError("collection_offer")


class Collection:
    def __init__(self, domain):
        self.domain = domain

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
               "url_sha256": row["url_sha256"], "adapter": ADAPTERS.get(row["shop_host"], "unsupported")}
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
        if result["status"] == "ok" and row["adapter"] not in ADAPTERS.values():
            raise ValueError("unsupported_adapter")
        if result["status"] == "ok":
            candidate = result["offer"]
            # Pin product SKU and currency after the first observation; changed variants need review.
            previous = db.execute("SELECT sku, currency FROM observations WHERE offer_id=? ORDER BY sequence DESC LIMIT 1",
                                  (row["offer_id"],)).fetchone()
            if previous and (candidate["sku"], candidate["currency"]) != (previous["sku"], previous["currency"]):
                result = {"status": "failed", "reason": "offer_unverified", "http_status": 200, "retry_after": 0}
            else:
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
