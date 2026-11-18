"""Serve only the synthetic wishlist UI preview on loopback; no domain or shop access."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1] / "web" / "wishlist"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
          "/model.mjs": ("model.mjs", "text/javascript; charset=utf-8"),
          "/demo.mjs": ("demo.mjs", "text/javascript; charset=utf-8")}


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond(body=True)

    def do_HEAD(self):
        self.respond(body=False)

    def respond(self, *, body):
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403)
            return
        asset = ASSETS.get(urlsplit(self.path).path)
        if asset is None:
            self.send_error(404)
            return
        path = ROOT / asset[0]
        if ROOT.is_symlink() or ROOT.parent.is_symlink() or path.is_symlink():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", asset[1])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; img-src 'self' data:; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def log_message(self, *_):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535")
    with HTTPServer(("127.0.0.1", args.port), PreviewHandler) as server:
        print(f"Wishlist preview: http://127.0.0.1:{args.port}/ (synthetic data only)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
