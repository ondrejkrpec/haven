"""Peer-authenticated HTTPS CONNECT broker for a network-isolated browser."""

import argparse
import asyncio
import os
from pathlib import Path
import re
import signal
import socket
import time

from scripts.wishlist_fetch import resolve
from scripts.wishlist_protocol import MAX_FRAME, close_writer, decode, peer_uid
from scripts.wishlist_socket import PeerServer

HOSTS = frozenset({"www.alza.cz", "cdn.alza.cz", "image.alza.cz", "webapi.alza.cz", "c.alza.cz"})
MAX_TUNNEL = 16 * 1024 * 1024
MAX_WINDOW_BYTES = 64 * 1024 * 1024
DEADLINE = 75


def authority(raw):
    if len(raw) > 8192:
        raise ValueError("egress_header_limit")
    text = raw.decode("ascii")
    lines = text.split("\r\n")
    match = re.fullmatch(r"CONNECT ([a-z0-9.-]+):443 HTTP/1\.[01]", lines[0])
    if match is None or match[1] not in HOSTS:
        raise ValueError("egress_destination")
    hosts = [line[5:].strip() for line in lines[1:] if line.lower().startswith("host:")]
    if hosts != [match[1] + ":443"]:
        raise ValueError("egress_authority")
    return match[1]


async def relay(reader, writer, budget, total=None):
    while True:
        data = await reader.read(32768)
        if not data:
            return
        budget[0] += len(data)
        if total is not None:
            total[0] += len(data)
        if budget[0] > MAX_TUNNEL or total is not None and total[0] > MAX_WINDOW_BYTES:
            raise ValueError("egress_limit")
        writer.write(data)
        await writer.drain()


async def tunnel(reader, writer, other_reader, other_writer, *, total=None):
    budget = [0]
    tasks = [asyncio.create_task(relay(reader, other_writer, budget, total)),
             asyncio.create_task(relay(other_reader, writer, budget, total))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Broker(PeerServer):
    def __init__(self, path, *, client_uid, socket_gid=None):
        super().__init__(path, ingress_uid=client_uid, socket_gid=socket_gid)
        self.window, self.connections, self.bytes = time.monotonic(), 0, [0]

    async def _serve(self, reader, writer):
        upstream = None
        raw_socket = None
        try:
            async with asyncio.timeout(DEADLINE):
                if peer_uid(writer.get_extra_info("socket")) != self.ingress_uid:
                    raise ValueError("egress_peer")
                if time.monotonic() - self.window >= DEADLINE:
                    self.window, self.connections, self.bytes[0] = time.monotonic(), 0, 0
                if self.connections >= 64 or self.bytes[0] >= MAX_WINDOW_BYTES:
                    raise ValueError("egress_limit")
                self.connections += 1
                async with asyncio.timeout(5):
                    host = authority(await reader.readuntil(b"\r\n\r\n"))
                family, kind, protocol, _, endpoint = await asyncio.wait_for(asyncio.to_thread(resolve, host), 10)
                raw_socket = socket.socket(family, kind, protocol)
                raw_socket.setblocking(False)
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(raw_socket, endpoint), 10)
                if raw_socket.getpeername()[0] != endpoint[0]:
                    raise ValueError("egress_peer_address")
                upstream_reader, upstream = await asyncio.open_connection(sock=raw_socket)
                raw_socket = None
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await tunnel(reader, writer, upstream_reader, upstream, total=self.bytes)
        except (OSError, ValueError, UnicodeError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            if raw_socket is not None:
                raw_socket.close()
            if upstream is not None:
                await close_writer(upstream)
            await close_writer(writer)

async def serve(config):
    broker = Broker(config["socket"], client_uid=config["client_uid"], socket_gid=config["socket_gid"])
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        await broker.start()
        print('{"service":"wishlist-egress","result":"ready"}', flush=True)
        await stopped.wait()
    finally:
        await broker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        os.umask(0o077)
        with args.config.open("rb") as stream:
            config = decode(stream.read(MAX_FRAME + 1))
        if (set(config) != {"socket", "socket_gid", "client_uid"}
                or any(type(config[key]) is not int or not 1 <= config[key] < 2**31 for key in ("socket_gid", "client_uid"))
                or config["client_uid"] == os.geteuid()):
            raise ValueError("egress_config")
        asyncio.run(serve(config))
    except Exception:
        print('{"service":"wishlist-egress","result":"failed"}', flush=True)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
