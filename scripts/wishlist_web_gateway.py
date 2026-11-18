"""Tailscale-authenticated HTTP adapter; the gateway never opens the database."""

import argparse
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import time
from urllib.parse import urlsplit

from scripts.wishlist_protocol import MAX_FRAME, decode, encode, peer_uid
from scripts.wishlist_socket import socket_path

COOKIE = "__Secure-haven-wishlist"
PREFIX = "/wishlist/"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
          "/model.mjs": ("model.mjs", "text/javascript; charset=utf-8"),
          "/api.mjs": ("api.mjs", "text/javascript; charset=utf-8")}
ROOT = Path(__file__).resolve().parents[1] / "web/wishlist"
OPERATIONS = {"web." + value for value in ("list", "get", "history", "purchases", "add", "update", "purchase",
                                        "correct_purchase", "void_purchase", "remove", "restore", "unmute", "refresh")}


def load_config(path):
    config = decode(path.read_bytes())
    if (set(config) != {"schema_version", "origin", "owner_login", "socket", "service_uid", "http_socket"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1
            or not isinstance(config["origin"], str) or not isinstance(config["owner_login"], str)
            or not re.fullmatch(r"[A-Za-z0-9@._+\-]{1,254}", config["owner_login"])
            or type(config["service_uid"]) is not int or not 1 <= config["service_uid"] < 2**31):
        raise ValueError("web_config")
    origin = urlsplit(config["origin"])
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment or origin.port not in (None, 443)):
        raise ValueError("web_origin")
    socket_path(config["socket"])
    socket_path(config["http_socket"])
    return config


def exchange(config, message):
    deadline = time.monotonic() + 5
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(config["socket"])
        if peer_uid(client) != config["service_uid"]:
            raise PermissionError("domain_identity")
        client.sendall(encode(message))

        def read(size):
            data = b""
            while len(data) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("domain_deadline")
                client.settimeout(remaining)
                chunk = client.recv(size - len(data))
                if not chunk:
                    raise ValueError("domain_frame")
                data += chunk
            return data

        size = struct.unpack("!I", read(4))[0]
        if not 0 < size <= MAX_FRAME:
            raise ValueError("domain_frame")
        return decode(read(size))


class Application:
    def __init__(self, config, *, transport=exchange, clock=time.time):
        self.config, self.transport, self.clock = config, transport, clock
        self.secret = secrets.token_bytes(32)
        self.host = urlsplit(config["origin"]).netloc
        self.assets = {}
        for route, (name, mime) in ASSETS.items():
            path = ROOT / name
            if path.is_symlink() or ROOT.is_symlink() or ROOT.parent.is_symlink():
                raise ValueError("web_asset_symlink")
            body = path.read_bytes()
            if name == "index.html":
                body = body.replace(b'<meta name="viewport"', b'<meta name="haven-mode" content="production">\n  <meta name="viewport"')
                body = body.replace('Interaktivní návrh · ukázková data'.encode(), 'Soukromý wishlist'.encode())
                body = body.replace(b'<div class="preview-note">', b'<div class="preview-note" hidden>')
            self.assets[route] = (body, mime)

    def signature(self, payload):
        return hmac.new(self.secret, (self.config["owner_login"] + ":" + payload).encode(), hashlib.sha256).hexdigest()

    def session(self, environ):
        try:
            cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
            value = cookie[COOKIE].value
            expires, nonce, signature = value.split(".")
            payload = expires + "." + nonce
            if (not re.fullmatch(r"[0-9]{1,12}", expires) or not re.fullmatch(r"[a-f0-9]{64}", nonce)
                    or not hmac.compare_digest(signature, self.signature(payload))
                    or not self.clock() < int(expires) <= self.clock() + 43201):
                return None
            return value, nonce
        except (KeyError, ValueError):
            return None

    def __call__(self, environ, start_response):
        extra = []

        def respond(code, value, mime="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if not isinstance(value, bytes) else value
            headers = [("Content-Type", mime), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                       ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                       ("Cross-Origin-Resource-Policy", "same-origin"),
                       ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")]
            start_response(code, headers + extra)
            return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]

        # The root-owned mode-0600 systemd HTTP socket is mandatory. Only the
        # root Tailscale proxy can reach it; arbitrary local HTTP headers grant nothing.
        if (environ.get("HTTP_TAILSCALE_USER_LOGIN") != self.config["owner_login"]
                or environ.get("HTTP_HOST") not in (self.host, self.host + ":443")):
            return respond("403 Forbidden", {"code": "denied"})
        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        # Tailscale strips /wishlist from proxied request paths.
        if path == "":
            extra.append(("Location", PREFIX))
            return respond("308 Permanent Redirect", b"")
        if method in ("GET", "HEAD") and path in self.assets:
            body, mime = self.assets[path]
            return respond("200 OK", body, mime)
        if method == "GET" and path == "/api/bootstrap":
            session = self.session(environ)
            if session is None:
                nonce = secrets.token_hex(32)
                payload = str(int(self.clock()) + 43200) + "." + nonce
                session = (payload + "." + self.signature(payload), nonce)
            extra.append(("Set-Cookie", f"{COOKIE}={session[0]}; Path={PREFIX}; Max-Age=43200; Secure; HttpOnly; SameSite=Strict"))
            return respond("200 OK", {"code": "ok", "csrf": session[1]})
        if path != "/api/command":
            return respond("404 Not Found", {"code": "not_found"})
        if method != "POST":
            extra.append(("Allow", "POST"))
            return respond("405 Method Not Allowed", {"code": "invalid_request"})
        if (environ.get("HTTP_ORIGIN") != self.config["origin"]
                or environ.get("HTTP_SEC_FETCH_SITE", "same-origin") not in ("same-origin", "none")):
            return respond("403 Forbidden", {"code": "denied"})
        session = self.session(environ)
        if session is None or not hmac.compare_digest(environ.get("HTTP_X_CSRF_TOKEN", ""), session[1]):
            return respond("403 Forbidden", {"code": "csrf_expired"})
        try:
            size = int(environ.get("CONTENT_LENGTH", "0"))
            if not 0 < size <= 16384 or environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
                raise ValueError("body")
            raw = environ["wsgi.input"].read(size)
            if len(raw) != size:
                raise ValueError("body")
            request = decode(raw)
            if request.get("operation") not in OPERATIONS:
                return respond("403 Forbidden", {"code": "denied"})
            result = self.transport(self.config, request)
            code = {"ok": "200 OK", "needs_clarification": "200 OK", "conflict": "409 Conflict",
                    "invalid_request": "400 Bad Request", "not_found": "404 Not Found", "denied": "403 Forbidden"}.get(result.get("code"), "503 Service Unavailable")
            return respond(code, result)
        except (ValueError, TypeError, OverflowError):
            return respond("400 Bad Request", {"code": "invalid_request"})
        except (OSError, TimeoutError):
            return respond("503 Service Unavailable", {"code": "unavailable"})


def inherited_socket(config):
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise ValueError("web_requires_systemd_socket")
    listener = socket.socket(fileno=3)
    info = Path(config["http_socket"]).lstat()
    if (listener.family != socket.AF_UNIX or listener.getsockname() != config["http_socket"]
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600
            or Path(config["http_socket"]).resolve() != Path(config["http_socket"])):
        raise ValueError("web_requires_root_proxy_socket")
    return listener


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from waitress import serve
        config = load_config(args.config)
        listener = inherited_socket(config)
        serve(Application(config), sockets=[listener], threads=4, connection_limit=32, channel_timeout=10,
              cleanup_interval=5, max_request_header_size=8192, max_request_body_size=16384,
              outbuf_high_watermark=131072, expose_tracebacks=False, clear_untrusted_proxy_headers=True,
              url_scheme="https", ident="")
    except Exception:
        print('{"service":"wishlist-web","code":"startup_failed"}', flush=True)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
