"""Export a bounded read-only operational view without importing Hermes or credentials."""

from contextlib import closing
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

if __package__:
    from scripts.wishlist_observability import SNAPSHOT, health, read_snapshot
else:
    from wishlist_observability import SNAPSHOT, health, read_snapshot

STATE = Path("/var/lib/haven/.hermes")
OUTPUT = Path("/var/lib/haven-web/dashboard-input")


def query(database, sql):
    if not database.is_file() or database.is_symlink():
        return {"status": "unavailable"}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.row_factory = sqlite3.Row
            return {"status": "ok", "rows": [dict(row) for row in connection.execute(sql)]}
    except sqlite3.Error:
        # No exception/SQL/path output: an unavailable counter is not a zero.
        return {"status": "unavailable"}


def collect(state=STATE, *, wishlist_snapshot=SNAPSHOT):
    result = subprocess.run(["/usr/bin/systemctl", "show", "haven.service",
                             "--property=ActiveState,SubState,NRestarts,ActiveEnterTimestamp"],
                            text=True, capture_output=True, timeout=10, check=False)
    service = dict(row.split("=", 1) for row in result.stdout.splitlines() if "=" in row) if result.returncode == 0 else {"ActiveState": "unknown"}
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "usage": query(state / "state.db", """SELECT
            COALESCE(SUM(api_call_count), 0) AS model_calls,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            SUM(estimated_cost_usd) AS estimated_cost_usd,
            SUM(actual_cost_usd) AS actual_cost_usd
            FROM sessions"""),
        "tasks": query(state / "kanban.db", "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"),
        "wishlist": read_snapshot(wishlist_snapshot),
    }


def render(snapshot):
    def escape(value):
        return html.escape(str(value))
    def table(rows):
        return '<table><tbody>' + ''.join(f'<tr><th scope="row">{escape(key)}</th><td>{escape(value) if value is not None else "nezjištěno"}</td></tr>' for key, value in rows) + '</tbody></table>'
    service = table(snapshot["service"].items())
    usage = snapshot["usage"]
    usage_html = table(usage["rows"][0].items()) if usage["status"] == "ok" else '<p>Počítadla se nepodařilo načíst.</p>'
    tasks = snapshot["tasks"]
    tasks_html = table((row["status"], row["count"]) for row in tasks["rows"]) if tasks["status"] == "ok" and tasks["rows"] else '<p>Board není inicializovaný nebo nejsou dostupná data.</p>'
    wishlist = snapshot.get("wishlist", {"status": "unavailable"})
    if wishlist.get("status") == "ok":
        problems = health(wishlist)
        labels = {"generated_at_utc": "Aktualizace přehledu (UTC)", "last_scan_utc": "Poslední kontrola sběru (UTC)",
                  "retention_last_run_utc": "Poslední údržba záznamů (UTC)", "observations": "Uložená cenová pozorování",
                  "jobs": "Dosavadní úlohy sběru", "interpretation_attempts": "Pokusy o interpretaci", "measured_attempts": "Pokusy se změřenými tokeny",
                  "unknown_usage_attempts": "Pokusy bez měření tokenů", "input_tokens": "Změřené vstupní tokeny", "output_tokens": "Změřené výstupní tokeny",
                  "estimated_cost_usd": "Odhad ceny (USD)", "actual_cost_usd": "Skutečná cena (USD)", "quota_remaining": "Zbývající kvóta",
                  "active_offers": "Aktivní nabídky", "ok": "Úspěšně načtené", "blocked": "Blokované zdrojem", "failed": "Chyba sběru",
                  "unsupported": "Nepodporované", "pending": "Čekající", "due": "Splatné", "overdue": "Po termínu déle než hodinu",
                  "last_attempt_utc": "Poslední pokus (UTC)", "last_observed_utc": "Poslední uložená cena (UTC)"}
        reasons = {"snapshot_stale": "zastaralý přehled", "scan_missing_or_stale": "chybějící kontrola sběru",
                   "retention_missing_or_stale": "chybějící údržba záznamů", "metrics_unavailable": "nedostupná měření",
                   "collection_overdue": "nevyřízené nabídky po termínu", "lease_expired": "nedokončená úloha", "source_failed": "chyba zdroje"}
        wishlist_html = ('<p>' + ('Vyžaduje kontrolu: ' + escape(', '.join(reasons[x] for x in problems)) if problems else 'Sběr i provozní evidence jsou aktuální.') + '</p>'
                         + table((labels[key], wishlist.get(key)) for key in
                                 ("generated_at_utc", "last_scan_utc", "retention_last_run_utc", "observations", "jobs"))
                         + '<h3>Spotřeba wishlistového interpretu</h3>' + table((labels[key], value) for key, value in wishlist["usage"].items())
                         + '<p>Pokusy o interpretaci zahrnují i selhání. Tokeny jsou součty pouze změřených pokusů; '
                         'chybějící měření, cena ani zbývající kvóta se nevydávají za nulu.</p>')
        for source in wishlist["sources"]:
            wishlist_html += '<h3>' + escape(source["source"]) + (' · odložené' if source["deferred"] else '') + '</h3>'
            wishlist_html += table((labels[key], value) for key, value in source.items() if key not in {"source", "deferred"})
        wishlist_html += '<p>Odložené zdroje zůstávají viditelné, ale nejsou součástí alarmu sběru.</p>'
    else:
        wishlist_html = '<p>Provozní údaje wishlistu nejsou dostupné.</p>'
    return ('<!doctype html><html lang="cs"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="60">'
            '<title>Haven · Provozní přehled</title><link rel="stylesheet" href="/site.css">'
            '<body><a class="skip" href="#main">Přejít na obsah</a>'
            '<header>Haven · Provozní přehled</header><main id="main">'
            '<h1>Stav instance</h1><p>Pouze pro čtení. Obnovení stránky každou minutu.</p>'
            '<p>Snapshot UTC: <time>' + escape(snapshot["generated_at_utc"]) + '</time>. '
            'Pokud je starší než dvě minuty, údaje považujte za zastaralé.</p>'
            '<h2>Služba</h2>' + service + '<h2>Modelová spotřeba</h2>' + usage_html +
            '<p>Součty za uchované sessions. Jde o lokální evidenci Hermesu, nikoli stav kvóty '
            'předplatného nebo fakturu. Nezjištěná cena se nezobrazuje jako nula.</p>'
            '<h2>Wishlist</h2>' + wishlist_html + '<h2>Úlohy podle stavu</h2>' + tasks_html +
            '<p>Výchozí board; přehled nespouští dispatcher a chybějící board nevytváří.</p>'
            '</main></body></html>')


def write_snapshot(output=OUTPUT, snapshot=None):
    snapshot = collect() if snapshot is None else snapshot
    if output.is_symlink():
        raise ValueError("Dashboard output must not be a symlink")
    output.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=output)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(snapshot, target, indent=2, allow_nan=False)
            target.write("\n")
        os.chmod(temporary, 0o640)
        os.replace(temporary, output / "snapshot.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    write_snapshot()
