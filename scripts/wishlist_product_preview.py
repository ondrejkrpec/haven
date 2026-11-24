"""Read-only product preview. The worker has no wishlist database or account access."""

import argparse
import asyncio
from collections import OrderedDict
import re
import signal
import time

from scripts import wishlist_alza_history as alza_history
from scripts.wishlist_collection import ADAPTERS
from scripts.wishlist_collector import request, run_fetch_process, run_image_process
from scripts.wishlist_domain import source_host
from scripts.wishlist_language_contract import PRODUCT_CATEGORIES
from scripts.wishlist_media import IMAGE_FRAME_LIMIT, image_url
from scripts.wishlist_socket import PeerServer


class PreviewServer(PeerServer):
    def __init__(self, path, *, ingress_uid, fetcher=run_fetch_process, clock=time.monotonic, category_endpoint=None,
                 image_fetcher=run_image_process, alza_history_enabled=False):
        super().__init__(path, ingress_uid=ingress_uid, deadline=25, max_connections=4, response_limit=IMAGE_FRAME_LIMIT)
        self.adapters = {host: adapter for host, adapter in ADAPTERS.items() if host != alza_history.SHOP}
        if alza_history_enabled:
            self.adapters[alza_history.SHOP] = alza_history.ADAPTER
        self.fetcher = fetcher
        self.clock = clock
        self.active_hosts = set()
        self.next_attempt = {}
        self.category_endpoint = category_endpoint
        self.categories = OrderedDict()
        self.category_active = False
        self.preview_images = OrderedDict()
        self.image_cache = OrderedDict()
        self.image_fetcher = image_fetcher
        self.retry_until = {}
        self.image_locks = {}

    async def category_for(self, name):
        if self.category_endpoint is None:
            return None
        try:
            reply = await request(self.category_endpoint, {"name": name}, timeout=14.5)
            value = reply.get("category")
            return value if value in PRODUCT_CATEGORIES else None
        except (OSError, ValueError, TimeoutError, EOFError):
            return None

    async def respond(self, request):
        if (set(request) != {"protocol_version", "operation", "arguments", "request_id"}
                or type(request["protocol_version"]) is not int or request["protocol_version"] != 1
                or not isinstance(request["arguments"], dict)
                or not isinstance(request["request_id"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", request["request_id"])):
            raise ValueError("preview_request")
        request_id = request["request_id"]
        result = self.response(request_id, "ok")
        args = request["arguments"]
        if request["operation"] == "web.preview_category" and set(args) == {"token"}:
            return result | {"data": {"category": await self.category_proposal(args["token"])}}
        if request["operation"] == "web.preview_image":
            data = await self.preview_image(args)
            return result | {"data": data}
        if (request["operation"] != "web.preview" or set(args) != {"url"}
                or not isinstance(args["url"], str) or not 1 <= len(args["url"]) <= 4096):
            raise ValueError("preview_arguments")
        url = args["url"]
        host = source_host(url)
        adapter = self.adapters.get(host)
        if adapter is None:
            return result | {"data": {"status": "unsupported"}}
        if host in self.active_hosts or self.clock() < max(self.next_attempt.get(host, 0), self.retry_until.get(host, 0)):
            return result | {"data": {"status": "retry_later"}}
        self.active_hosts.add(host)
        self.next_attempt[host] = self.clock() + 2
        try:
            outcome = await self.fetcher({"url": url, "adapter": adapter}, timeout=8, preview=True)
            delay = outcome["retry_after"]
            if outcome["http_status"] == 429:
                delay = max(delay, 86400)
            if delay:
                self.retry_until[host] = max(self.retry_until.get(host, 0), self.clock() + delay)
        finally:
            self.active_hosts.remove(host)
        if delay == 0 and host == "www.intimissimi.com" and (variants := outcome.get("variants")):
            return result | {"data": {"status": "choose_variant", "variants": variants}}
        if outcome["status"] != "ok":
            return result | {"data": {"status": "retry_later" if delay else "unavailable"}}
        offer = outcome["offer"]
        data = {"status": "ready", "name": offer["name"], "price": offer["price"], "availability": offer["availability"]}
        if "variant_url" in outcome:
            data["variant_url"] = outcome["variant_url"]
        if picture := image_url(host, outcome.get("image_url")):
            if len(self.preview_images) >= 64:
                self.preview_images.popitem(last=False)
            self.preview_images[request_id] = (self.clock() + 600, host, picture)
            data["image"] = "/wishlist/api/image/p/" + request_id
        if self.category_endpoint is not None:
            if len(self.categories) >= 64:
                self.categories.popitem(last=False)
            self.categories[request_id] = (self.clock() + 45, offer["name"])
            data["category_token"] = request_id
        return result | {"data": data}

    async def category_proposal(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("category_token")
        saved = self.categories.pop(token, None)
        if saved is None or self.category_active or self.clock() >= saved[0]:
            return None
        self.category_active = True
        try:
            category = await self.category_for(saved[1])
            return category if category != "uncategorized" else None
        finally:
            self.category_active = False


    async def preview_image(self, args):
        if set(args) == {"token"}:
            token = args["token"]
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
                raise ValueError("image_token")
            registered = self.preview_images.get(token)
            if not registered or registered[0] <= self.clock():
                return {}
            _, shop, url = registered
        elif set(args) == {"shop", "url"} and isinstance(args["shop"], str):
            shop, url = args["shop"], args["url"]
        else:
            raise ValueError("image_arguments")
        if not image_url(shop, url):
            return {}
        key = (shop, url)
        # Share short-lived results, including failures, across tabs and detail views.
        lock, users = self.image_locks.get(key, (asyncio.Lock(), 0))
        self.image_locks[key] = lock, users + 1
        try:
            async with lock:
                now = self.clock()
                cached = self.image_cache.get(key)
                if cached and cached[0] > now:
                    self.image_cache.move_to_end(key)
                    return cached[1]
                host = source_host(url)
                if now < self.retry_until.get(host, 0):
                    return {}
                data = await self.image_fetcher({"shop": shop, "url": url}, timeout=3)
                delay = data.get("retry_after")
                if type(delay) is int and 0 < delay <= 31536000:
                    self.retry_until[host] = max(self.retry_until.get(host, 0), self.clock() + delay)
                image = {key: data[key] for key in ("mime", "body")} if {"mime", "body"} <= data.keys() else {}
                if key not in self.image_cache and len(self.image_cache) >= 32:
                    self.image_cache.popitem(last=False)
                self.image_cache[key] = (self.clock() + (1800 if image else 120), image)
                self.image_cache.move_to_end(key)
                return image
        finally:
            _, users = self.image_locks[key]
            if users == 1:
                del self.image_locks[key]
            else:
                self.image_locks[key] = lock, users - 1


async def serve(path, ingress_uid, language_uid, *, alza_history_enabled=False):
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    category_endpoint = None if language_uid is None else {
        "socket": "/run/haven-wishlist-language/category/request.sock", "service_uid": language_uid,
    }
    server = PreviewServer(path, ingress_uid=ingress_uid, category_endpoint=category_endpoint,
                           alza_history_enabled=alza_history_enabled)
    await server.start()
    try:
        await stopped.wait()
    finally:
        await server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--client-uid", required=True, type=int)
    parser.add_argument("--language-uid", type=int)
    parser.add_argument("--alza-history", action="store_true")
    args = parser.parse_args()
    asyncio.run(serve(args.socket, args.client_uid, args.language_uid, alza_history_enabled=args.alza_history))


if __name__ == "__main__":
    main()
