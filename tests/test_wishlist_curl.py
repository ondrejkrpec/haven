"""Fabini's HTTP/2 transport keeps the bounded worker's trust boundaries."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

from scripts import wishlist_fetch as fetch
from tests.test_wishlist_extract import page, product

URL = "https://www.fabini.cz/products/synthetic?variant=two"
ADAPTER = "fabini-jsonld-v1"
VERSION = b"curl 8.18.0 (linux) libcurl/8.18.0\nFeatures: SSL HTTP2 IPv6\n"
PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
HTML = page(product(url=URL)).encode()
RESOLVE = fetch.resolve


class CurlTests(unittest.TestCase):
    @contextmanager
    def responses(self, *rows, version=VERSION):
        pending, calls = list(rows), []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, version)
            status, headers, body, code = pending.pop(0)
            Path(argv[argv.index("--dump-header") + 1]).write_bytes(
                b"HTTP/2 " + str(status).encode() + b" \r\n" + headers + b"\r\n")
            return subprocess.CompletedProcess(argv, code, body)

        with patch.object(fetch, "resolve", return_value=PUBLIC), patch.object(fetch.subprocess, "run", side_effect=run):
            yield calls

    def test_exact_variant_uses_checked_ip_without_proxy_config_credentials_or_url_in_argv(self):
        with self.responses((200, b"Content-Type: text/html\r\n", HTML, 0)) as calls, \
                patch.dict(os.environ, {"HTTPS_PROXY": "http://private.example", "CURL_CA_BUNDLE": "untrusted"}):
            result = fetch.fetch(URL, ADAPTER)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["offer"]["price"], "249.90")
        self.assertEqual(len(calls), 2)
        argv, options = calls[1]
        self.assertEqual(argv[:2], ["/usr/bin/curl", "--disable"])
        self.assertIn("--http2", argv)
        self.assertEqual(argv[argv.index("--resolve") + 1], "www.fabini.cz:443:8.8.8.8")
        self.assertEqual(argv[argv.index("--proxy") + 1], "")
        self.assertEqual(argv[argv.index("--proto") + 1], "=https")
        self.assertEqual(argv[argv.index("--max-filesize") + 1], str(fetch.MAX_BYTES))
        self.assertEqual(argv[argv.index("--max-redirs") + 1], "0")
        self.assertEqual(argv[argv.index("--retry") + 1], "0")
        self.assertIn("--no-netrc", argv)
        self.assertIn("--globoff", argv)
        self.assertNotIn("--location", argv)
        self.assertNotIn("--insecure", argv)
        self.assertNotIn(URL, argv)
        self.assertEqual(options["input"], ('url = "' + URL + '"\n').encode())
        self.assertEqual(options["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertEqual(options["stderr"], subprocess.DEVNULL)

    def test_old_or_non_http2_curl_fails_before_request(self):
        for version in (VERSION.replace(b"8.18.0", b"8.3.0"), VERSION.replace(b"HTTP2", b""), b"unknown"):
            with self.subTest(version=version), self.responses(version=version) as calls:
                self.assertEqual(fetch.fetch(URL, ADAPTER)["reason"], "network_failed")
                self.assertEqual(len(calls), 1)

    def test_rate_limit_retains_retry_after_without_body_parsing_or_fallback(self):
        with self.responses((429, b"Retry-After: 172800\r\n", b"", 22)) as calls, \
                patch.object(fetch, "extract", side_effect=AssertionError("error body must not be parsed")):
            result = fetch.fetch(URL, ADAPTER)
        self.assertEqual(result, fetch.failure("source_http_429", status="blocked", http_status=429, retry_after=172800))
        self.assertEqual(len(calls), 2)

    def test_redirects_still_require_same_variant_and_public_fresh_dns(self):
        for target in (URL.replace("two", "one"), "https://other.example/item", "https://127.0.0.1/"):
            with self.subTest(target=target), self.responses((302, b"Location: " + target.encode() + b"\r\n", b"", 0)) as calls:
                self.assertEqual(fetch.fetch(URL, ADAPTER)["status"], "failed")
                self.assertEqual(len(calls), 2)
        private = (*PUBLIC[:-1], ("127.0.0.1", 443))
        responses = [(302, b"Location: " + URL.encode() + b"&utm_source=x\r\n", b"", 0)]
        with self.responses(*responses) as calls, patch.object(socket, "getaddrinfo", side_effect=[[PUBLIC], [private]]), \
                patch.object(fetch, "resolve", wraps=RESOLVE):
            self.assertEqual(fetch.fetch(URL, ADAPTER)["reason"], "destination_denied")
        self.assertEqual(len(calls), 2)

    def test_partial_oversized_compressed_duplicate_length_and_transport_failures_fail_closed(self):
        cases = [
            (200, b"Content-Type: text/html\r\n", HTML, 63, "document_limit"),
            (200, b"Content-Type: text/html\r\n", HTML, 28, "fetch_timeout"),
            (200, b"Content-Type: text/html\r\n", HTML, 18, "network_failed"),
            (200, b"Content-Type: text/html\r\nContent-Encoding: gzip\r\n", HTML, 0, "unsupported_encoding"),
            (200, b"Content-Type: text/html\r\nContent-Encoding: identity\r\nContent-Encoding: gzip\r\n", HTML, 0, "unsupported_encoding"),
            (200, b"Content-Type: text/html\r\nContent-Length: 2\r\nContent-Length: 2\r\n", b"xx", 0, "document_limit"),
            (200, b"Content-Type: text/html\r\nContent-Length: 100\r\n", b"short", 0, "invalid_document"),
            (200, b"X-Long: " + b"x" * 65536 + b"\r\n", HTML, 0, "document_limit"),
            (200, b"Content-Type: text/html\r\n\r\nHTTP/2 200\r\nX: y\r\n", HTML, 0, "invalid_document"),
        ]
        for status, headers, body, code, reason in cases:
            with self.subTest(reason=reason, code=code), self.responses((status, headers, body, code)):
                self.assertEqual(fetch.fetch(URL, ADAPTER)["reason"], reason)

    def test_config_cannot_add_urls_and_ipv6_is_pinned(self):
        url = URL + '&literal={one,two}&quote="value"'
        html = page(product(url=url)).encode()
        address = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 0))
        with self.responses((200, b"Content-Type: text/html\r\n", html, 0)) as calls, \
                patch.object(fetch, "resolve", return_value=address):
            self.assertEqual(fetch.fetch(url, ADAPTER)["status"], "ok")
        argv, options = calls[1]
        self.assertEqual(argv[argv.index("--resolve") + 1], "www.fabini.cz:443:[2606:4700:4700::1111]")
        self.assertEqual(options["input"].count(b"\n"), 1)
        self.assertIn(b'quote=\\"value\\"', options["input"])
        for endpoint in (("127.0.0.1", 443), ("::ffff:8.8.8.8", 443), ("8.8.8.8", 80)):
            with self.subTest(endpoint=endpoint), self.assertRaises(fetch.FetchError):
                fetch.CurlConnection("www.fabini.cz", endpoint)


if __name__ == "__main__":
    unittest.main()
