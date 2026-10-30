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

STATE = Path("/var/lib/haven/.hermes")
OUTPUT = Path("/var/lib/haven/web/dashboard")


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


def collect(state=STATE):
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
            '<h2>Úlohy podle stavu</h2>' + tasks_html +
            '<p>Výchozí board; přehled nespouští dispatcher a chybějící board nevytváří.</p>'
            '</main></body></html>')


def write_snapshot(output=OUTPUT, snapshot=None):
    snapshot = collect() if snapshot is None else snapshot
    if output.is_symlink():
        raise ValueError("Dashboard output must not be a symlink")
    output.mkdir(mode=0o750, parents=True, exist_ok=True)
    for name, content in (("snapshot.json", json.dumps(snapshot, indent=2) + "\n"),
                          ("index.html", render(snapshot))):
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=output)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
            os.chmod(temporary, 0o640)
            os.replace(temporary, output / name)
        finally:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    write_snapshot()
