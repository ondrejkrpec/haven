"""Read-only aggregate export. Never exports identities, URLs, prompts or credentials."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

DATABASE = Path("/var/lib/haven-wishlist/state/wishlist.sqlite3")
SNAPSHOT = Path("/var/lib/haven-wishlist-observability/snapshot.json")
HOSTS = ("www.momenti.cz", "www.fabini.cz", "www.ifantazie.cz", "www.intimissimi.com", "www.alza.cz")


def utc(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


def health(snapshot, now=None):
    """Re-evaluate freshness at the consumer's clock, not at export time."""
    now = now or datetime.now(timezone.utc)
    problems = []
    for key, maximum, code in (("generated_at_utc", 120, "snapshot_stale"),
                               ("last_scan_utc", 1800, "scan_missing_or_stale"),
                               ("retention_last_run_utc", 7200, "retention_missing_or_stale")):
        value = snapshot.get(key)
        try:
            if value is None or not 0 <= (now - utc(value)).total_seconds() < maximum:
                problems.append(code)
        except (ValueError, TypeError):
            problems.append(code)
    if snapshot.get("status") != "ok":
        problems.append("metrics_unavailable")
    if snapshot.get("overdue_offers", 0):
        problems.append("collection_overdue")
    if snapshot.get("expired_jobs", 0):
        problems.append("lease_expired")
    if snapshot.get("problem_sources", 0):
        problems.append("source_failed")
    return problems


def collect(database=DATABASE, *, deferred_hosts=(), now=None):
    now = now or datetime.now(timezone.utc)
    result = {"schema_version": 1, "generated_at_utc": now.isoformat(), "status": "unavailable"}
    if set(deferred_hosts) - set(HOSTS):
        raise ValueError("unknown_deferred_source")
    try:
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or database.resolve() != database:
            return result
        deadline = time.monotonic() + 5
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (5, 6, 7, 8):
                return result
            metadata = dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('collection_last_scan','retention_last_run')"))
            # CASE prevents malformed historical usage from breaking the export.
            usage = dict(db.execute("""WITH measured AS (
                SELECT CASE WHEN json_valid(usage) THEN CASE WHEN
                    json_type(usage,'$.input_tokens')='integer' AND json_type(usage,'$.output_tokens')='integer'
                    AND json_extract(usage,'$.input_tokens') BETWEEN 0 AND 9007199254740992
                    AND json_extract(usage,'$.output_tokens') BETWEEN 0 AND 9007199254740992
                    THEN usage END END AS valid FROM language_attempts)
                SELECT COUNT(*) AS interpretation_attempts, COUNT(valid) AS measured_attempts,
                    COUNT(*)-COUNT(valid) AS unknown_usage_attempts,
                    SUM(json_extract(valid,'$.input_tokens')) AS input_tokens,
                    SUM(json_extract(valid,'$.output_tokens')) AS output_tokens FROM measured""").fetchone())
            usage.update(estimated_cost_usd=None, actual_cost_usd=None, quota_remaining=None)
            sources = []
            host_keys = ','.join('?' for _ in HOSTS)
            for row in db.execute(f"""SELECT CASE WHEN o.shop_host IN ({host_keys}) THEN o.shop_host ELSE 'other' END AS shop_host,
                COUNT(*) AS active_offers,
                SUM(o.collection_status='ok') AS ok,
                SUM(o.collection_status='blocked') AS blocked,
                SUM(o.collection_status='failed') AS failed,
                SUM(o.collection_status='unsupported') AS unsupported,
                SUM(o.collection_status IN ('pending','stale')) AS pending,
                SUM(max(o.next_collection_at, coalesce(h.retry_at,''))<=?) AS due,
                SUM(max(o.next_collection_at, coalesce(h.retry_at,''))<=?) AS overdue,
                MAX(o.last_attempt_at) AS last_attempt_utc, MAX(o.last_observed_at) AS last_observed_utc
                FROM offers o JOIN entries e ON e.entry_id=o.entry_id AND e.owner_id=o.owner_id
                LEFT JOIN collection_hosts h ON h.shop_host=o.shop_host
                WHERE e.state='active' AND e.removed_at IS NULL
                GROUP BY 1""", (*HOSTS, now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                                (now - timedelta(hours=1)).isoformat(timespec="microseconds").replace("+00:00", "Z"))):
                source = dict(row)
                host = source.pop("shop_host")
                source["source"] = host if host in HOSTS else "other"
                source["deferred"] = host in deferred_hosts
                for field in ("last_attempt_utc", "last_observed_utc"):
                    source[field] = utc(source[field]).isoformat() if source[field] else None
                sources.append(source)
            expired = db.execute("SELECT COUNT(*) FROM collection_jobs WHERE status='running' AND expires_at<=?",
                                 (now.isoformat(timespec="microseconds").replace("+00:00", "Z"),)).fetchone()[0]
            observations = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            jobs = db.execute("SELECT COUNT(*) FROM collection_jobs").fetchone()[0]
        result.update(status="ok", usage=usage, sources=sources, observations=observations, jobs=jobs,
                      last_scan_utc=metadata.get("collection_last_scan"),
                      retention_last_run_utc=metadata.get("retention_last_run"), expired_jobs=expired,
                      overdue_offers=sum(x["overdue"] for x in sources if not x["deferred"]),
                      problem_sources=sum(bool(x["blocked"] or x["failed"] or x["unsupported"])
                                          for x in sources if not x["deferred"]))
        # Normalize all database timestamps before allowing them into JSON/HTML.
        for key in ("last_scan_utc", "retention_last_run_utc"):
            if result[key] is not None:
                result[key] = utc(result[key]).isoformat()
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {"schema_version": 1, "generated_at_utc": now.isoformat(), "status": "unavailable"}
    return result


def read_snapshot(path=SNAPSHOT):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise ValueError("snapshot_file")
            result = json.loads(stream.read(65537))
        if not isinstance(result, dict) or result.get("schema_version") != 1:
            raise ValueError("snapshot_schema")
        validate_snapshot(result)
        return result
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {"status": "unavailable"}


def validate_snapshot(result):
    """Whitelist the public projection again before embedding a shared file."""
    base = {"schema_version", "generated_at_utc", "status"}
    utc(result["generated_at_utc"])
    if result["status"] == "unavailable" and set(result) == base:
        return
    counts = {"observations", "jobs", "expired_jobs", "overdue_offers", "problem_sources"}
    if (result["status"] != "ok" or set(result) != base | counts | {
            "usage", "sources", "last_scan_utc", "retention_last_run_utc"}
            or any(type(result[key]) is not int or result[key] < 0 for key in counts)):
        raise ValueError("snapshot_fields")
    for key in ("last_scan_utc", "retention_last_run_utc"):
        if result[key] is not None:
            utc(result[key])
    usage = result["usage"]
    if not isinstance(usage, dict) or set(usage) != {
            "interpretation_attempts", "measured_attempts", "unknown_usage_attempts",
            "input_tokens", "output_tokens", "estimated_cost_usd", "actual_cost_usd", "quota_remaining"}:
        raise ValueError("usage_fields")
    for key, value in usage.items():
        if key in {"estimated_cost_usd", "actual_cost_usd", "quota_remaining"}:
            if value is not None:
                raise ValueError("unknown_cost_or_quota")
        elif value is not None and (type(value) is not int or value < 0):
            raise ValueError("usage_value")
    sources = result["sources"]
    if not isinstance(sources, list) or len(sources) > len(HOSTS) + 1:
        raise ValueError("source_limit")
    source_counts = {"active_offers", "ok", "blocked", "failed", "unsupported", "pending", "due", "overdue"}
    for source in sources:
        if (not isinstance(source, dict) or set(source) != source_counts | {
                "source", "deferred", "last_attempt_utc", "last_observed_utc"}
                or source["source"] not in (*HOSTS, "other") or type(source["deferred"]) is not bool
                or any(type(source[key]) is not int or source[key] < 0 for key in source_counts)):
            raise ValueError("source_fields")
        for key in ("last_attempt_utc", "last_observed_utc"):
            if source[key] is not None:
                utc(source[key])


def write_snapshot(snapshot, path=SNAPSHOT):
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(snapshot, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deferred-host", action="append", choices=HOSTS, default=[])
    args = parser.parse_args(argv)
    snapshot = collect(deferred_hosts=args.deferred_host)
    write_snapshot(snapshot)
    return 0 if snapshot["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
