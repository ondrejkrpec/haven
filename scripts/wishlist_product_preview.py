"""Read-only product preview. The worker has no wishlist database or account access."""

import argparse
import asyncio
import signal
import time

from scripts.wishlist_collection import ADAPTERS
from scripts.wishlist_collector import run_fetch_process
from scripts.wishlist_domain import source_host
from scripts.wishlist_socket import PeerServer


class PreviewServer(PeerServer):
    def __init__(self, path, *, ingress_uid, fetcher=run_fetch_process, clock=time.monotonic):
        super().__init__(path, ingress_uid=ingress_uid, deadline=11, max_connections=4)
        self.fetcher = fetcher
        self.clock = clock
        self.active_hosts = set()
        self.next_attempt = {}

    async def respond(self, request):
        if (set(request) != {"protocol_version", "operation", "arguments", "request_id"}
                or type(request["protocol_version"]) is not int or request["protocol_version"] != 1
                or request["operation"] != "web.preview"
                or not isinstance(request["arguments"], dict) or set(request["arguments"]) != {"url"}):
            raise ValueError("preview_request")
        request_id = request["request_id"]
        url = request["arguments"]["url"]
        if (not isinstance(request_id, str) or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)
                or not isinstance(url, str) or not 1 <= len(url) <= 4096):
            raise ValueError("preview_arguments")
        host = source_host(url)
        adapter = ADAPTERS.get(host)
        result = self.response(request_id, "ok")
        if adapter is None or host == "www.alza.cz":
            return result | {"data": {"status": "unsupported"}}
        if host in self.active_hosts or self.clock() < self.next_attempt.get(host, 0):
            return result | {"data": {"status": "retry_later"}}
        self.active_hosts.add(host)
        self.next_attempt[host] = self.clock() + 2
        try:
            outcome = await self.fetcher({"url": url, "adapter": adapter}, timeout=8, preview=True)
            delay = outcome["retry_after"]
            if outcome["http_status"] == 429:
                delay = max(delay, 86400)
            if delay:
                self.next_attempt[host] = max(self.next_attempt[host], self.clock() + delay)
        finally:
            self.active_hosts.remove(host)
        if outcome["status"] != "ok":
            return result | {"data": {"status": "retry_later" if delay else "unavailable"}}
        offer = outcome["offer"]
        return result | {"data": {"status": "ready", "name": offer["name"], "price": offer["price"],
                                  "availability": offer["availability"]}}


async def serve(path, ingress_uid):
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    server = PreviewServer(path, ingress_uid=ingress_uid)
    await server.start()
    try:
        await stopped.wait()
    finally:
        await server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--client-uid", required=True, type=int)
    args = parser.parse_args()
    asyncio.run(serve(args.socket, args.client_uid))


if __name__ == "__main__":
    main()
