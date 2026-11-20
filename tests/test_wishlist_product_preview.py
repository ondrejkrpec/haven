"""Preview reuses collection without obtaining any write authority."""

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from unittest.mock import Mock
from types import SimpleNamespace

from scripts.wishlist_interpreter import CategoryServer
from scripts.wishlist_model import category_login, suggest_category
from scripts.wishlist_product_preview import PreviewServer
from scripts.wishlist_web_gateway import exchange


def request(url):
    return {"protocol_version": 1, "operation": "web.preview", "request_id": "1" * 32,
            "arguments": {"url": url}}


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def test_category_login_uses_latest_explicit_wishlist_grant_on_pinned_origin(self):
        origin = "https://provider.example/route"
        good = {"label": "wishlist", "auth_type": "oauth", "source": "manual:oauth", "id": "new",
                "access_token": "synthetic"}
        entries = [good | {"id": "old"}, good, good | {"base_url": "https://different.example"},
                   good | {"label": "personal"}, good | {"source": "singleton"}]
        self.assertEqual(category_login("openai-codex", origin, entries), good)
        self.assertIsNone(category_login("openai-codex", origin, entries[2:]))

    async def test_category_only_accepts_public_title_and_checks_taxonomy(self):
        backend = AsyncMock()
        backend.interpret.return_value = {"category": "home"}
        clock = 100
        server = CategoryServer("/tmp/not-used.sock", client_uid=1000, backend=backend, clock=lambda: clock)
        request_ = {"name": "Pánev"}
        with self.assertRaises(ValueError):
            await server.respond(request_ | {"url": "https://example.com"})
        self.assertEqual(await server.respond(request_), {"category": "home"})
        self.assertEqual(await server.respond(request_), {"category": None})
        backend.interpret.assert_awaited_once_with(request_, category=True)
        clock += 6
        backend.interpret.return_value = {"category": "anything-else"}
        self.assertEqual(await server.respond(request_), {"category": None})

    def test_category_model_request_uses_no_tools_and_only_the_title(self):
        client = Mock()
        generate = client.chat.completions.create
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(tool_calls=[], content="home"))
        generate.return_value = SimpleNamespace(choices=[choice])
        self.assertEqual(suggest_category(client, "Pánev"), {"category": "home"})
        sent = generate.call_args.kwargs
        self.assertEqual(sent["messages"][1]["role"], "user")
        self.assertEqual(json.loads(sent["messages"][1]["content"]), {"name": "Pánev"})
        self.assertEqual(sent["tools"], [])
        self.assertEqual(sent["model"], "gpt-6.1-terra")
        self.assertEqual(sent["extra_body"], {"reasoning": {"enabled": False}})
        choice.message.content = "home and gifts"
        self.assertEqual(suggest_category(client, "Pánev"), {"category": None})

    async def test_socket_returns_partial_offer_and_validates_peer(self):
        outcome = {"status": "ok", "http_status": 200, "retry_after": 0,
                   "offer": {"name": "Syntetický produkt", "price": None, "availability": "out_of_stock"}}
        fetcher = AsyncMock(return_value=outcome)
        url = "https://www.intimissimi.com/cz/product/example.html"
        with TemporaryDirectory() as root:
            path = Path(root) / "request.sock"
            server = PreviewServer(path, ingress_uid=os.getuid(), fetcher=fetcher)
            await server.start()
            try:
                endpoint = {"socket": str(path), "service_uid": os.getuid()}
                result = await asyncio.to_thread(exchange, endpoint, request(url))
                self.assertFalse(result["committed"])
                self.assertEqual(result["data"], {"status": "ready", "name": "Syntetický produkt",
                                                  "price": None, "availability": "out_of_stock"})
                fetcher.assert_awaited_once_with({"url": url, "adapter": "intimissimi-public-price-v1"},
                                                timeout=8, preview=True)
                server.ingress_uid = os.getuid() + 1
                denied = await asyncio.to_thread(exchange, endpoint, request(url))
                self.assertEqual(denied["code"], "denied")
            finally:
                await server.close()

    async def test_address_admission_and_shop_retry_after(self):
        fetcher = AsyncMock(return_value={"status": "blocked", "http_status": 429, "retry_after": 180})
        clock = 100
        server = PreviewServer("/tmp/not-used.sock", ingress_uid=1000, fetcher=fetcher, clock=lambda: clock)
        for url in ("https://somewhere.example/item", "https://www.alza.cz/example.htm"):
            self.assertEqual((await server.respond(request(url)))["data"]["status"], "unsupported")
        for url in ("http://www.fabini.cz/product", "https://www.fabini.cz@localhost/item", "https://127.0.0.1/item"):
            with self.assertRaises(ValueError):
                await server.respond(request(url))
        fetcher.assert_not_awaited()
        url = "https://www.fabini.cz/products/example"
        self.assertEqual((await server.respond(request(url)))["data"]["status"], "retry_later")
        clock += 3600
        self.assertEqual((await server.respond(request(url + "-other")))["data"]["status"], "retry_later")
        fetcher.assert_awaited_once()
        clock += 86400
        await server.respond(request(url))
        self.assertEqual(fetcher.await_count, 2)
