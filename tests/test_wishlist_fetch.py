"""W3 public destinations, bounded HTTP and shop-specific synthetic extraction."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import io
import os
import socket
import sys
import unittest
from unittest.mock import Mock, patch

from scripts import wishlist_fetch as fetch
from scripts.wishlist_collector import run_fetch
from tests.test_wishlist_extract import page, product

URL = "https://www.fabini.cz/products/synthetic?variant=two"
ADAPTER = "fabini-jsonld-v1"


class FetchTests(unittest.TestCase):
    def test_special_private_and_tunnel_destinations_are_denied(self):
        for address in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254",
                        "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "fe80::1",
                        "fc00::1", "ff02::1", "::ffff:8.8.8.8", "64:ff9b::a00:1", "2002:0808:0808::1",
                        "2001::1", "fe80::1%eth0"):
            with self.subTest(address=address):
                self.assertFalse(fetch.public_address(address))
        for address in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(fetch.public_address(address))

    def test_entire_dns_answer_must_be_public(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))
        for answers in ([], [private], [public, private], [public] * 33):
            with self.subTest(answers=answers), patch.object(socket, "getaddrinfo", return_value=answers):
                with self.assertRaises(fetch.FetchError):
                    fetch.resolve("shop.example")
        with patch.object(socket, "getaddrinfo", return_value=[public]):
            self.assertEqual(fetch.resolve("shop.example"), public)

    def test_connect_pins_checked_ip_but_tls_checks_original_hostname(self):
        endpoint = ("8.8.8.8", 443)
        raw, context = Mock(), Mock()
        raw.getpeername.return_value = endpoint
        answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint)
        with patch.object(socket, "getaddrinfo", return_value=[answer]) as resolver, \
                patch.object(socket, "socket", return_value=raw), \
                patch.object(fetch.ssl, "create_default_context", return_value=context):
            client = fetch.connection("shop.example")
            raw.connect.assert_called_once_with(endpoint)
            resolver.assert_called_once()
            context.wrap_socket.assert_called_once_with(raw, server_hostname="shop.example")
            self.assertIs(client.sock, context.wrap_socket.return_value)
            self.assertEqual(client.host, "shop.example")
            client.close()

    @contextmanager
    def responses(self, *responses):
        clients = []
        for status, headers, body in responses:
            response = Mock()
            response.status = status
            response.getheader.side_effect = lambda key, default=None, headers=headers: headers.get(key, default)
            response.getheaders.return_value = list(headers.items())
            response.read.side_effect = lambda size, body=body: body[:size]
            client = Mock()
            client.getresponse.return_value = response
            clients.append(client)
        with patch.object(fetch, "connection", side_effect=clients) as connect:
            yield clients, connect

    def test_structured_variant_extraction_is_not_a_verified_discount(self):
        html = page([product(url=URL.replace("two", "one"), price="1"), product(url=URL)]).encode()
        with self.responses((200, {"Content-Type": "text/html; charset=utf-8"}, html)) as (clients, _), \
                patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:1234", "HTTP_PROXY": "synthetic"}):
            result = fetch.fetch(URL, ADAPTER)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["offer"]["price"], "249.90")
            self.assertEqual(result["offer"]["conditions_verification"], "not_verified")
            headers = clients[0].request.call_args.kwargs["headers"]
            self.assertNotIn("Cookie", headers)
            self.assertEqual(headers["Accept-Encoding"], "identity")
            clients[0].close.assert_called_once()

    def test_redirect_rechecks_identity_and_refuses_other_hosts_private_ips_and_variants(self):
        for target in ("http://www.fabini.cz/products/synthetic?variant=two", "https://127.0.0.1/",
                       "https://other.example/products/synthetic?variant=two", "?variant=one",
                       "https://user:secret@www.fabini.cz/products/synthetic?variant=two", "/other"):
            with self.subTest(target=target), self.responses((302, {"Location": target}, b"")) as (_, connect):
                self.assertEqual(fetch.fetch(URL, ADAPTER)["status"], "failed")
                self.assertEqual(connect.call_count, 1)
        body = page(product(url=URL)).encode()
        with self.responses((301, {"Location": URL + "&utm_source=synthetic"}, b""),
                            (200, {"Content-Type": "text/html"}, body)) as (_, connect):
            self.assertEqual(fetch.fetch(URL, ADAPTER)["status"], "ok")
            self.assertEqual(connect.call_count, 2)

    def test_rebinding_between_redirects_is_rejected_before_connect(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        client = Mock()
        response = client.getresponse.return_value
        response.status = 301
        response.getheader.side_effect = lambda key, default=None: {"Location": URL + "&utm_source=x"}.get(key, default)
        raw = Mock()
        raw.getpeername.return_value = public[-1]
        with patch.object(socket, "getaddrinfo", side_effect=[[public], [private]]) as resolver, \
                patch.object(socket, "socket", return_value=raw), \
                patch.object(fetch.ssl, "create_default_context"), \
                patch.object(fetch.http.client, "HTTPSConnection", return_value=client):
            self.assertEqual(fetch.fetch(URL, ADAPTER)["reason"], "destination_denied")
            self.assertEqual(resolver.call_count, 2)
            raw.connect.assert_called_once_with(public[-1])

    def test_blocked_rate_limited_and_error_pages_are_not_parsed_or_retried(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), self.responses((status, {"Retry-After": "172800"}, b"PRIVATE_PAGE")) as (clients, connect):
                result = fetch.fetch(URL, ADAPTER)
                self.assertEqual(result["status"], "blocked" if status != 500 else "failed")
                self.assertEqual(result["retry_after"], 172800)
                self.assertNotIn("offer", result)
                self.assertEqual(connect.call_count, 1)
                clients[0].getresponse.return_value.read.assert_not_called()

    def test_response_size_encoding_content_type_and_product_conditions_fail_closed(self):
        for headers, body in (({"Content-Type": "text/html", "Content-Length": str(fetch.MAX_BYTES + 1)}, b""),
                              ({"Content-Type": "text/html", "Content-Length": "1000"}, b"short"),
                              ({"Content-Type": "text/html", "Content-Encoding": "gzip"}, b""),
                              ({"Content-Type": "application/json"}, b"{}"),
                              ({"Content-Type": "text/html"}, b"x" * (fetch.MAX_BYTES + 1)),
                              ({"Content-Type": "text/html"}, page(product(url=URL, validForMemberTier="club")).encode()),
                              ({"Content-Type": "text/html"}, page(product(url=URL, eligibleQuantity={"value": 3})).encode())):
            with self.subTest(headers=headers), self.responses((200, headers, body)):
                self.assertEqual(fetch.fetch(URL, ADAPTER)["status"], "failed")

    def test_unknown_shops_never_make_a_network_request(self):
        with patch.object(fetch, "connection", side_effect=AssertionError("network forbidden")):
            self.assertEqual(fetch.fetch("https://shop.example/item", "unsupported")["status"], "unsupported")
            self.assertEqual(fetch.fetch("https://other.example/item", ADAPTER)["status"], "failed")

    def test_retry_after_accepts_http_dates_and_rejects_invalid_values(self):
        now = datetime(2026, 11, 11, 12, tzinfo=UTC)
        self.assertEqual(fetch.retry_delay("Thu, 12 Nov 2026 12:00:00 GMT", now), 86401)
        for value in (None, "-1", "not-a-date", "x" * 101):
            self.assertEqual(fetch.retry_delay(value, now), 0)

    def test_real_http_parser_preserves_read_bound(self):
        class BufferedSocket:
            def makefile(self, *args):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 100)
        response = fetch.http.client.HTTPResponse(BufferedSocket())
        response.begin()
        self.assertEqual(len(response.read(17)), 17)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def admission(self):
        return {"url": URL, "job": {"job_id": "a" * 32, "attempt_id": "b" * 32,
                "offer_id": "wo_synthetic", "generation": 1, "entry_version": 1,
                "url_sha256": hashlib.sha256(URL.encode()).hexdigest(), "adapter": "unsupported"}}

    async def test_real_subprocess_is_model_free_and_does_not_inherit_proxy_or_auth(self):
        result = await run_fetch(self.admission())
        self.assertEqual(result["status"], "unsupported")

    async def test_timeout_and_cancellation_reap_the_process(self):
        create = asyncio.create_subprocess_exec
        processes = []
        async def sleeper(*args, **kwargs):
            process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
            processes.append(process)
            return process
        with patch("scripts.wishlist_collector.asyncio.create_subprocess_exec", side_effect=sleeper):
            result = await run_fetch(self.admission(), timeout=0.05)
            self.assertEqual(result["reason"], "fetch_timeout")
            self.assertIsNotNone(processes[-1].returncode)
            task = asyncio.create_task(run_fetch(self.admission()))
            while len(processes) < 2:
                await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(processes[-1].returncode)


if __name__ == "__main__":
    unittest.main()
