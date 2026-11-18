"""Boundary checks for the synthetic, loopback-only web preview."""

from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from scripts.wishlist_web_preview import ASSETS, PreviewHandler


class WishlistWebTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), PreviewHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path="/", method="GET", host=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            client.request(method, path, headers={"Host": host} if host else {})
            result = client.getresponse()
            return result.status, dict(result.headers), result.read()
        finally:
            client.close()

    def test_explicit_assets_only_and_no_directory_or_checkout_access(self):
        for path in ASSETS:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 0)
            self.assertEqual(headers["Content-Type"], ASSETS[path][1])
        for path in ("/.git/config", "/../README.md", "/%2e%2e/README.md", "/scripts/", "/private/", "/favicon.ico"):
            self.assertEqual(self.request(path)[0], 404)

    def test_no_cache_no_remote_resources_no_framing_and_head(self):
        status, headers, body = self.request(method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 100)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_untrusted_host_is_denied_before_serving(self):
        for host in ("evil.example.com", "127.0.0.1:80", "localhost.evil.example.com"):
            self.assertEqual(self.request(host=host)[0], 403)

    def test_allowlisted_filename_cannot_follow_a_symlink(self):
        with tempfile.TemporaryDirectory(prefix="haven-web-test-") as directory:
            root = Path(directory)
            private = root / "private.txt"
            private.write_text("synthetic unserved marker")
            (root / "index.html").symlink_to(private)
            with patch("scripts.wishlist_web_preview.ROOT", root):
                self.assertEqual(self.request()[0], 404)
