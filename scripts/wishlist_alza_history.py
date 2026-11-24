"""Validate Alza product identities and daily prices from the historical data source."""

from datetime import UTC, date, datetime, time, timedelta
import json
import re

from scripts.wishlist_extract import amount, identity

ADAPTER = "alza-history-v1"
SHOP = "www.alza.cz"
SOURCE = "api.hlidacshopu.cz"
MAX_BYTES = 512 * 1024
WINDOW = 90


def product(url):
    host, path, query = identity(url)
    match = re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]*-d([1-9][0-9]{0,11})\.htm", path, re.IGNORECASE)
    if host != SHOP or query or not match:
        raise ValueError("alza_history_product")
    return match[1], f"https://{SHOP}{path}"


def price(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", value)):
        raise ValueError("alza_history_price")
    return amount(value)


def read_series(payload, item_id, now):
    meta = payload["metadata"]
    if not isinstance(meta, dict) or str(meta["itemId"]) != item_id or not isinstance(meta["name"], str):
        raise ValueError("alza_history_identity")
    name = " ".join(meta["name"].split())[:200]
    records = payload["data"]["currentPrice"]
    if not isinstance(records, list) or not 1 <= len(records) <= 367:
        raise ValueError("alza_history_series")
    days = {}
    previous = datetime.min.replace(tzinfo=UTC)
    today = now.date()
    for row in records:
        raw = row["x"]
        if not isinstance(raw, str) or not raw.endswith("Z"):
            raise ValueError("alza_history_timestamp")
        moment = datetime.fromisoformat(raw)
        if moment < previous or moment.date() > today:
            raise ValueError("alza_history_timestamp")
        previous = moment
        value = row["y"]
        if type(value) not in (int, float) and value is not None:
            raise ValueError("alza_history_price")
        days[moment.date()] = None if value is None else price(str(value))
    current = days[previous.date()]
    if not name or current is None or previous.date() < today - timedelta(days=1):
        raise ValueError("alza_history_current")
    return ({"name": name, "sku": item_id, "price": current, "currency": "CZK", "availability": "unknown",
             "conditions_verification": "not_verified"},
            [[day.isoformat(), value] for day, value in days.items()
             if value is not None and day >= today - timedelta(days=WINDOW - 1)])


def validate_series(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= WINDOW:
        raise ValueError("alza_history_series")
    previous = ""
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("alza_history_row")
        day, value = row
        if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                or day <= previous or price(value) != value):
            raise ValueError("alza_history_row")
        previous = day


def save_backfill(db, offer_id, rows, now, zone):
    key = "collection_history:" + offer_id
    stored = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    today = now.astimezone(zone).date()
    first, days = json.loads(stored[0]) if stored else (today.isoformat(), {})
    start = (today - timedelta(days=WINDOW - 1)).isoformat()
    for day, value in rows:
        if start <= day < first:
            days[day] = value
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps([first, days], sort_keys=True)))


def backfilled_prices(db, offer_id, zone):
    stored = db.execute("SELECT value FROM metadata WHERE key=?", ("collection_history:" + offer_id,)).fetchone()
    if not stored:
        return []
    from scripts.wishlist_domain import instant, stamp
    _, days = json.loads(stored[0])
    if not days:
        return []
    start = stamp(datetime.combine(date.fromisoformat(min(days)), time(), zone))
    end = stamp(datetime.combine(date.fromisoformat(max(days)) + timedelta(days=1), time(), zone))
    observed = {instant(row[0]).astimezone(zone).date().isoformat() for row in db.execute(
        "SELECT observed_at FROM observations WHERE offer_id=? AND observed_at>=? AND observed_at<?", (offer_id, start, end))}
    return [{"observed_at": stamp(datetime.combine(date.fromisoformat(day), time(), zone)), "price": value}
            for day, value in days.items() if day not in observed]
