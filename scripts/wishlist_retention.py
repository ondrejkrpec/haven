"""Bounded payload compaction by the domain writer; replay identities never expire."""

from datetime import timedelta

from scripts.wishlist_domain import canonical, stamp

DAYS = 30
BATCH = 100
EXPIRED_REPLY = "Výsledek staré zprávy už není k dispozici. Požadavek znovu neprovádím; zkontroluj aktuální seznam."


def tombstone(request_id, *, telegram=False):
    result = {"protocol_version": 1, "request_id": request_id, "code": "unavailable", "committed": False}
    if telegram:
        result["reply"] = EXPIRED_REPLY
    return canonical(result)


def compact(domain):
    """One atomic bounded batch, with no model/network I/O or physical erasure claim."""
    now = domain.clock()
    cutoff = stamp(now - timedelta(days=DAYS))
    changed = 0
    with domain.store.transaction() as db:
        for table in ("receipts", "telegram_receipts"):
            # Request IDs are canonical hex digests, so SQLite replace produces
            # the same JSON as tombstone without depending on JSON extensions.
            template = tombstone("REQUEST_ID", telegram=table == "telegram_receipts")
            rows = db.execute(f"""SELECT request_id, created_at FROM {table}
                WHERE created_at<? AND response!=replace(?, 'REQUEST_ID', request_id)
                ORDER BY created_at, request_id LIMIT ?""", (cutoff, template, BATCH)).fetchall()
            for row in rows:
                db.execute(f"UPDATE {table} SET response=? WHERE request_id=?",
                           (tombstone(row["request_id"], telegram=table == "telegram_receipts"), row["request_id"]))
            changed += len(rows)
        # Unfinished expired requests also need a durable replay barrier before
        # their prompts are discarded; otherwise an old delivery could run again.
        rows = db.execute("""SELECT request_id, owner_id, fingerprint FROM language_runs
            WHERE expires_at<? AND prepared!='{}' ORDER BY expires_at, request_id LIMIT ?""",
                          (cutoff, BATCH)).fetchall()
        for row in rows:
            db.execute("INSERT OR IGNORE INTO telegram_receipts VALUES (?, ?, ?, ?, ?)",
                       (row["request_id"], row["owner_id"], row["fingerprint"],
                        tombstone(row["request_id"], telegram=True), stamp(now)))
            db.execute("UPDATE language_runs SET prepared='{}', status='retained' WHERE request_id=?",
                       (row["request_id"],))
        changed += len(rows)
        # Session generations must survive so late model results stay invalid.
        db.execute("""UPDATE language_sessions SET state='{}' WHERE rowid IN
            (SELECT rowid FROM language_sessions WHERE expires_at<=? AND state!='{}' LIMIT ?)""",
                   (stamp(now), BATCH))
        db.execute("DELETE FROM clarifications WHERE token IN (SELECT token FROM clarifications WHERE expires_at<=? LIMIT ?)",
                   (stamp(now), BATCH))
        db.execute("""UPDATE collection_jobs SET result=NULL WHERE job_id IN
            (SELECT job_id FROM collection_jobs WHERE status!='running' AND completed_at<?
             AND result IS NOT NULL LIMIT ?)""", (cutoff, BATCH))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('retention_last_run', ?)", (stamp(now),))
    return changed
