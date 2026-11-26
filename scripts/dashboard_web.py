"""Render the dashboard from bounded data behind a root-only systemd socket."""

from datetime import datetime
import json
import math
import os
from pathlib import Path
import pwd
import re
import socket
import stat

if __package__:
    from scripts.dashboard_snapshot import OUTPUT, render
    from scripts.wishlist_observability import validate_snapshot
else:
    from dashboard_snapshot import OUTPUT, render
    from wishlist_observability import validate_snapshot

HTTP_SOCKET = "/run/haven-dashboard-http.sock"
INPUT_LIMIT = 65536
USAGE_COUNTERS = {"model_calls", "input_tokens", "output_tokens"}
USAGE_COSTS = {"estimated_cost_usd", "actual_cost_usd"}
STATUS = re.compile(r"[a-z][a-z_-]{0,31}")
SYSTEMD_DATE = re.compile(r"(?:[A-Za-z]{3} )?[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(?: [A-Za-z0-9+:_-]{1,12})?")


def counter(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def table_rows(value, limit):
    if value == {"status": "unavailable"}:
        return []
    if (not isinstance(value, dict) or set(value) != {"status", "rows"} or value["status"] != "ok"
            or not isinstance(value["rows"], list) or len(value["rows"]) > limit
            or any(not isinstance(row, dict) for row in value["rows"])):
        raise ValueError("dashboard_rows")
    return value["rows"]


def validate(data):
    if not isinstance(data, dict) or set(data) != {"generated_at_utc", "service", "usage", "tasks", "wishlist"}:
        raise ValueError("dashboard_fields")
    date = data["generated_at_utc"]
    if not isinstance(date, str) or len(date) > 40 or datetime.fromisoformat(date).utcoffset() is None:
        raise ValueError("dashboard_timestamp")
    service = data["service"]
    if (not isinstance(service, dict) or "ActiveState" not in service
            or set(service) - {"ActiveState", "SubState", "NRestarts", "ActiveEnterTimestamp"}
            or not all(isinstance(value, str) for value in service.values())):
        raise ValueError("dashboard_service")
    if (not STATUS.fullmatch(service["ActiveState"]) or not STATUS.fullmatch(service.get("SubState", "unknown"))
            or not re.fullmatch(r"[0-9]{1,20}", service.get("NRestarts", "0"))
            or (service.get("ActiveEnterTimestamp") and not SYSTEMD_DATE.fullmatch(service["ActiveEnterTimestamp"]))):
        raise ValueError("dashboard_service_value")
    usage_rows = table_rows(data["usage"], 1)
    if data["usage"]["status"] == "ok" and not usage_rows:
        raise ValueError("dashboard_usage")
    for row in usage_rows:
        if (set(row) != USAGE_COUNTERS | USAGE_COSTS or not all(counter(row[key]) for key in USAGE_COUNTERS)
                or any(value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
                       for value in (row[key] for key in USAGE_COSTS))):
            raise ValueError("dashboard_usage")
    for row in table_rows(data["tasks"], 32):
        if (set(row) != {"status", "count"} or not isinstance(row["status"], str)
                or not STATUS.fullmatch(row["status"]) or not counter(row["count"])):
            raise ValueError("dashboard_tasks")
    if data["wishlist"] != {"status": "unavailable"}:
        if not isinstance(data["wishlist"], dict) or data["wishlist"].get("schema_version") != 1:
            raise ValueError("dashboard_wishlist")
        validate_snapshot(data["wishlist"])


def read_snapshot(path, *, producer_uid):
    # The administrator owns the parent of the input directory. The producer
    # can replace the final file, so use a single checked descriptor throughout.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != producer_uid
                or stat.S_IMODE(info.st_mode) != 0o640 or info.st_size > INPUT_LIMIT):
            raise ValueError("dashboard_input")
        raw = stream.read(INPUT_LIMIT + 1)
    if len(raw) > INPUT_LIMIT:
        raise ValueError("dashboard_input")
    data = json.loads(raw)
    validate(data)
    return data


class Application:
    def __init__(self, css, *, producer_uid, snapshot=OUTPUT / "snapshot.json"):
        self.css = css
        self.snapshot = snapshot
        self.producer_uid = producer_uid

    def __call__(self, environ, start_response):
        headers = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Referrer-Policy", "no-referrer"),
                   ("Content-Security-Policy", "default-src 'none'; style-src 'self'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")]

        def reply(status, body, mime="text/plain; charset=utf-8"):
            start_response(status, headers + [("Content-Type", mime), ("Content-Length", str(len(body)))])
            return [b"" if environ["REQUEST_METHOD"] == "HEAD" else body]

        if environ["REQUEST_METHOD"] not in ("GET", "HEAD"):
            headers.append(("Allow", "GET, HEAD"))
            return reply("405 Method Not Allowed", b"Method not allowed\n")
        path = environ.get("PATH_INFO")
        if path == "/site.css":
            return reply("200 OK", self.css, "text/css; charset=utf-8")
        if path not in ("/", "/index.html", "/snapshot.json"):
            return reply("404 Not Found", b"Not found\n")
        try:
            snapshot = read_snapshot(self.snapshot, producer_uid=self.producer_uid)
            if path == "/snapshot.json":
                return reply("200 OK", (json.dumps(snapshot, indent=2, allow_nan=False) + "\n").encode(),
                             "application/json; charset=utf-8")
            return reply("200 OK", render(snapshot).encode(), "text/html; charset=utf-8")
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
            return reply("503 Service Unavailable", b"Snapshot unavailable\n")


def inherited_socket():
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise ValueError("dashboard_requires_systemd_socket")
    listener = socket.socket(fileno=3)
    info = Path(HTTP_SOCKET).lstat()
    if (listener.family != socket.AF_UNIX or listener.getsockname() != HTTP_SOCKET
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("dashboard_requires_root_socket")
    return listener


def main():
    from waitress import serve

    css = Path(__file__).with_name("site.css").read_bytes()
    app = Application(css, producer_uid=pwd.getpwnam("haven").pw_uid)
    serve(app, sockets=[inherited_socket()], threads=2, connection_limit=16, channel_timeout=10,
          cleanup_interval=5, max_request_header_size=8192, max_request_body_size=1024,
          expose_tracebacks=False, url_scheme="https", ident="")


if __name__ == "__main__":
    main()
