"""Alza display price and bounded peer/browser/egress boundaries; no external access."""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.wishlist_alza import public_offer
from scripts.wishlist_browser import BrowserServer, validate_admission
from scripts.wishlist_collector import request, validate_endpoint
from scripts.wishlist_egress import Broker, authority
from scripts.wishlist_extract import ExtractionError
from scripts import wishlist_process as processes
from scripts import wishlist_egress as egress
from scripts import wishlist_browser as browser_module
from tests.test_wishlist_fetch import alza_page, product

URL = "https://www.alza.cz/synthetic-product-d100000000.htm"


def admission():
    return {"protocol_version": 1, "operation": "browser.fetch", "url": URL,
        "job": {"job_id": "a" * 32, "attempt_id": "b" * 32, "offer_id": "wo_synthetic",
                "generation": 1, "entry_version": 1, "adapter": "alza-public-price-v1",
                "url_sha256": hashlib.sha256(URL.encode()).hexdigest()}}


class PriceTests(unittest.TestCase):
    def record(self, **fields):
        return product(url=URL, itemCondition="https://schema.org/NewCondition", **fields)

    def test_visible_whole_crown_price_is_checked_against_structured_rounding(self):
        result = public_offer(alza_page(self.record(price="466.65"), display="467,-"), URL)
        self.assertEqual(result["price"], "467.00")
        self.assertEqual(result["sku"], "SYNTHETIC-2")
        self.assertEqual(result["conditions_verification"], "not_verified")

    def test_decimal_and_grouped_prices_exclude_strike_vat_and_other_products(self):
        html = alza_page(self.record(price="1234.50"), display="1\u00a0234,50")
        html += b'<span data-slot="pb-price">1,-</span>'
        self.assertEqual(public_offer(html, URL)["price"], "1234.50")

    def test_mismatches_ambiguous_primary_boxes_and_conditional_prices_are_refused(self):
        cases = [alza_page(self.record(), display="100,-"), alza_page(self.record(), display="0,-"),
                 alza_page(self.record(), classes="ads-pb--member"),
                 alza_page(self.record(), extra="Cena s kódem KUPON"),
                 alza_page(self.record(), extra="Cena pro AlzaPlus"),
                 alza_page(self.record()) * 2,
                 alza_page(self.record(validForMemberTier="club"))]
        for html in cases:
            with self.subTest(html=html[:80]), self.assertRaises(ExtractionError):
                public_offer(html, URL)

    def test_wrong_variant_or_missing_condition_cannot_become_a_public_price(self):
        for record in (product(url=URL), product(url="https://www.alza.cz/other-d100000001.htm", itemCondition="NewCondition")):
            with self.assertRaises(ExtractionError):
                public_offer(alza_page(record), URL)


class ContractTests(unittest.TestCase):
    def test_browser_endpoint_is_explicit_and_separate(self):
        endpoint = {"socket": "/run/synthetic-domain/request.sock", "service_uid": 2001,
                    "browser": {"socket": "/run/synthetic-browser/request.sock", "service_uid": 2002}}
        self.assertEqual(validate_endpoint(endpoint), endpoint)
        for update in ({"service_uid": 2001}, {"service_uid": True}, {"socket": endpoint["socket"]}, {"extra": True}):
            invalid = deepcopy(endpoint)
            invalid["browser"].update(update)
            with self.assertRaises(ValueError):
                validate_endpoint(invalid)

    def test_browser_only_accepts_bound_alza_jobs(self):
        validate_admission(admission())
        for url in ("https://127.0.0.1/private", "https://other.example/item", "http://www.alza.cz/item",
                    "https://user:password@www.alza.cz/item", URL + "#fragment"):
            invalid = admission()
            invalid["url"] = url
            invalid["job"]["url_sha256"] = hashlib.sha256(url.encode()).hexdigest()
            with self.assertRaises(ValueError):
                validate_admission(invalid)
        invalid = admission()
        invalid["job"]["url_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_admission(invalid)

    def test_connect_authority_is_not_a_general_proxy(self):
        valid = b"CONNECT www.alza.cz:443 HTTP/1.1\r\nHost: www.alza.cz:443\r\n\r\n"
        self.assertEqual(authority(valid), "www.alza.cz")
        for raw in (valid.replace(b"www.alza.cz", b"127.0.0.1"), valid.replace(b":443", b":80"),
                    valid.replace(b"Host: www.alza.cz:443", b"Host: other.example:443"),
                    valid.replace(b"CONNECT", b"GET"), valid + b"x" * 8192,
                    b"CONNECT www.alza.cz:443 HTTP/1.1\r\nHost: www.alza.cz:443\r\nHost: www.alza.cz:443\r\n\r\n"):
            with self.assertRaises(ValueError):
                authority(raw)


class EgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_per_tunnel_and_shared_byte_limits_stop_before_forwarding(self):
        class Writer:
            data = b""
            def write(self, data):
                self.data += data
            async def drain(self):
                pass
        for tunnel_limit, window_limit in ((4, 20), (20, 4)):
            reader = asyncio.StreamReader()
            reader.feed_data(b"SYNTHETIC")
            reader.feed_eof()
            writer = Writer()
            with patch.object(egress, "MAX_TUNNEL", tunnel_limit), patch.object(egress, "MAX_WINDOW_BYTES", window_limit):
                with self.assertRaises(ValueError):
                    await egress.relay(reader, writer, [0], [0])
            self.assertEqual(writer.data, b"")

    async def test_real_broker_authenticates_before_resolving_and_pins_the_checked_endpoint(self):
        with tempfile.TemporaryDirectory(prefix="hw3-eg-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            received = []
            async def echo(reader, writer):
                try:
                    data = await reader.readexactly(4)
                    received.append(data)
                    writer.write(data)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
            endpoint = upstream.sockets[0].getsockname()
            broker = Broker(root / "proxy.sock", client_uid=os.getuid())
            await broker.start()
            try:
                # Only the resolver is doubled to reach a local synthetic echo server.
                with patch("scripts.wishlist_egress.resolve", return_value=(socket.AF_INET, socket.SOCK_STREAM,
                           socket.IPPROTO_TCP, "", endpoint)) as resolve:
                    reader, writer = await asyncio.open_unix_connection(str(broker.path))
                    writer.write(b"CONNECT www.alza.cz:443 HTTP/1.1\r\nHost: www.alza.cz:443\r\n\r\n")
                    await writer.drain()
                    self.assertIn(b"200", await reader.readuntil(b"\r\n\r\n"))
                    writer.write(b"TEST")
                    await writer.drain()
                    self.assertEqual(await reader.readexactly(4), b"TEST")
                    writer.close()
                    await writer.wait_closed()
                    resolve.assert_called_once_with("www.alza.cz")
                self.assertEqual(received, [b"TEST"])
            finally:
                await broker.close()
                upstream.close()
                await upstream.wait_closed()
            denied = Broker(root / "denied.sock", client_uid=os.getuid() + 1)
            await denied.start()
            try:
                with patch("scripts.wishlist_egress.resolve", side_effect=AssertionError("no DNS")):
                    reader, writer = await asyncio.open_unix_connection(str(denied.path))
                    self.assertEqual(await reader.read(), b"")
                    writer.close()
                    await writer.wait_closed()
            finally:
                await denied.close()


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_socket_returns_validated_worker_output_and_checks_server_identity(self):
        with tempfile.TemporaryDirectory(prefix="hw3-bs-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            server = BrowserServer({"socket": str(root / "request.sock"), "socket_gid": os.getgid(),
                                    "client_uid": os.getuid(), "python": sys.executable}, root / "config.json")
            endpoint = {"socket": str(server.path), "service_uid": os.getuid()}
            result = {"status": "failed", "reason": "fetch_timeout", "http_status": None, "retry_after": 0}
            create = asyncio.create_subprocess_exec
            environments = []
            async def worker(*args, **kwargs):
                environments.append(kwargs["env"])
                return await create(sys.executable, "-c", "import sys; sys.stdin.read(); print(" + repr(json.dumps(result)) + ")", **kwargs)
            await server.start()
            try:
                with patch("scripts.wishlist_browser.asyncio.create_subprocess_exec", side_effect=worker), \
                        patch.dict(os.environ, {"GH_TOKEN": "SYNTHETIC_DO_NOT_INHERIT"}):
                    self.assertEqual(await request(endpoint, admission()), result)
                    with self.assertRaises(PermissionError):
                        await request({**endpoint, "service_uid": os.getuid() + 1}, admission())
                self.assertEqual(len(environments), 1)
                self.assertNotIn("GH_TOKEN", environments[0])
                self.assertFalse(Path(environments[0]["TMPDIR"]).exists())
            finally:
                await server.close()

    async def test_cancelled_browser_request_kills_and_reaps_its_process_group(self):
        with tempfile.TemporaryDirectory(prefix="hw3-bw-") as temporary:
            root = Path(temporary)
            server = BrowserServer({"socket": str(root / "request.sock"), "socket_gid": os.getgid(),
                                    "client_uid": os.getuid(), "python": sys.executable}, root / "config.json")
            create = asyncio.create_subprocess_exec
            processes = []
            async def sleeper(*args, **kwargs):
                process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
                processes.append(process)
                return process
            with patch("scripts.wishlist_browser.asyncio.create_subprocess_exec", side_effect=sleeper):
                task = asyncio.create_task(server.respond(admission()))
                while not processes:
                    await asyncio.sleep(0.005)
                self.assertTrue(server.busy)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(processes[0].returncode)
                self.assertFalse(server.busy)


class ProcessTests(unittest.TestCase):
    def test_production_daemon_refuses_a_shared_cgroup_or_shared_role_uid(self):
        config = {"client_uid": os.geteuid() + 1, "egress_uid": os.geteuid() + 2}
        old = os.umask(0o077)
        try:
            for managed, roles in ((False, config), (True, {**config, "client_uid": os.geteuid()})):
                with patch.object(browser_module, "load_config", return_value=roles), \
                        patch.object(browser_module, "managed_group", return_value=managed), \
                        patch.object(browser_module, "serve") as serve:
                    self.assertEqual(browser_module.main(["serve", "--config", "/synthetic/config.json"]), 78)
                    serve.assert_not_called()
        finally:
            os.umask(old)

    def process(self, root, pid, parent, start, group):
        path = root / str(pid)
        path.mkdir()
        fields = ["0"] * 24
        fields[0], fields[1], fields[19] = "S", str(parent), str(start)
        (path / "stat").write_text(f"{pid} (synthetic) " + " ".join(fields))
        (path / "cgroup").write_text(group)

    def test_pid_reuse_and_shared_or_other_service_cgroups_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "self").mkdir()
            (proc / "self/cgroup").write_text("0::/user.slice/shared.service\n")
            self.process(proc, 90001, 1, 10, processes.GROUP)
            self.process(proc, 90002, 90001, 20, processes.GROUP)
            self.process(proc, 90003, 1, 30, "0::/system.slice/haven-wishlist-egress.service\n")
            with patch.object(processes.os, "kill") as kill:
                processes.clean_managed_group(proc)
                kill.assert_not_called()
                self.assertEqual(processes.descendants(90001, proc, expected_start=11), {})
                self.assertEqual(processes.descendants(90001, proc, expected_start=10), {90002: 20})
                processes.kill_snapshot({90002: 19}, proc)
                kill.assert_not_called()
                (proc / "self/cgroup").write_text(processes.GROUP)
                processes.clean_managed_group(proc)
                self.assertEqual({call.args[0] for call in kill.call_args_list}, {90001, 90002})


if __name__ == "__main__":
    unittest.main()
