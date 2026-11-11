"""Fresh sandboxed Chromium/Xvfb for Alza, with peer-authenticated bounded egress."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
from urllib.parse import urlsplit

from scripts.wishlist_alza import public_offer
from scripts.wishlist_collection import JOB_KEYS, validate_result
from scripts.wishlist_egress import HOSTS, tunnel
from scripts.wishlist_extract import MAX_BYTES, identity
from scripts.wishlist_fetch import failure
from scripts.wishlist_protocol import MAX_FRAME, close_writer, decode, peer_uid
from scripts.wishlist_process import clean_managed_group, descendants, kill_snapshot, managed_group, process_start
from scripts.wishlist_socket import PeerServer, socket_path

WORKER_DEADLINE = 65


def validate_admission(request):
    if (not isinstance(request, dict) or set(request) != {"protocol_version", "operation", "url", "job"}
            or type(request["protocol_version"]) is not int or request["protocol_version"] != 1
            or request["operation"] != "browser.fetch" or not isinstance(request["job"], dict)
            or set(request["job"]) != JOB_KEYS or not isinstance(request["url"], str)
            or request["job"]["adapter"] != "alza-public-price-v1"
            or hashlib.sha256(request["url"].encode()).hexdigest() != request["job"]["url_sha256"]):
        raise ValueError("browser_request")
    parsed = urlsplit(request["url"])
    if (parsed.scheme != "https" or parsed.hostname != "www.alza.cz" or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or "\\" in request["url"] or any(ord(c) <= 32 or ord(c) == 127 for c in request["url"])):
        raise ValueError("browser_destination")
    identity(request["url"])


def load_config(path):
    with path.open("rb") as stream:
        config = decode(stream.read(MAX_FRAME + 1))
    if (set(config) != {"socket", "socket_gid", "client_uid", "egress_socket", "egress_uid", "python", "chromium", "xvfb", "xauth"}
            or any(type(config[key]) is not int or not 1 <= config[key] < 2**31
                   for key in ("socket_gid", "client_uid", "egress_uid"))):
        raise ValueError("browser_config")
    for key in ("socket", "egress_socket"):
        socket_path(config[key])
    for key in ("python", "chromium", "xvfb", "xauth"):
        value = Path(config[key])
        if not value.is_absolute() or value.parent.resolve() != value.parent or not os.access(value, os.X_OK):
            raise ValueError("browser_executable")
    return config


async def bridge(config, reader, writer):
    remote = None
    try:
        async with asyncio.timeout(70):
            other, remote = await asyncio.open_unix_connection(config["egress_socket"])
            if peer_uid(remote.get_extra_info("socket")) != config["egress_uid"]:
                raise ValueError("browser_egress_identity")
            await tunnel(reader, writer, other, remote)
    except (OSError, ValueError, TimeoutError):
        pass
    finally:
        if remote is not None:
            await close_writer(remote)
        await close_writer(writer)


async def render(config, admission):
    from playwright.async_api import async_playwright
    validate_admission(admission)
    tasks = set()

    def connected(reader, writer):
        if len(tasks) >= 16:
            writer.close()
            return
        task = asyncio.create_task(bridge(config, reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    proxy = await asyncio.start_server(connected, "127.0.0.1", 0)
    display_process = None
    display_reader = None
    try:
        async with asyncio.timeout(60), AsyncExitStack() as stack:
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="haven-alza-"))
            root = Path(temporary)
            for name in ("home", "cache", "tmp"):
                (root / name).mkdir(mode=0o700)
            auth = root / "Xauthority"
            auth.touch(mode=0o600)
            cookie = secrets.token_hex(16)

            async def authorize(display):
                process = await asyncio.create_subprocess_exec(config["xauth"], "-f", str(auth), "add", display,
                    "MIT-MAGIC-COOKIE-1", cookie, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await process.wait() != 0:
                    raise ValueError("browser_display_auth")

            await authorize(":0")
            read_fd, write_fd = os.pipe()
            try:
                display_process = await asyncio.create_subprocess_exec(config["xvfb"], "-displayfd", str(write_fd),
                    "-screen", "0", "1280x960x24", "-nolisten", "tcp", "-auth", str(auth), "-noreset",
                    pass_fds=(write_fd,), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                display_reader = os.fdopen(read_fd, "rb", buffering=0)
            finally:
                os.close(write_fd)
            # The display pipe is read through asyncio so startup can be cancelled/reaped.
            display_stream = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(display_stream), display_reader)
            try:
                async with asyncio.timeout(5):
                    number = (await display_stream.readline()).decode().strip()
            finally:
                transport.close()
                display_reader = None
            if not number.isdecimal() or len(number) > 5:
                raise ValueError("browser_display")
            display = ":" + number
            await authorize(display)
            browser_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root / "home"),
                           "TMPDIR": str(root / "tmp"), "XDG_CACHE_HOME": str(root / "cache"),
                           "DISPLAY": display, "XAUTHORITY": str(auth)}
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False, chromium_sandbox=True, timeout=15000,
                    executable_path=config["chromium"], env=browser_env,
                    proxy={"server": "http://127.0.0.1:" + str(proxy.sockets[0].getsockname()[1]), "bypass": "<-loopback>"})
                try:
                    context = await browser.new_context(accept_downloads=False, service_workers="block")
                    async def block_websocket(route):
                        await route.close()
                    await context.route_web_socket("**/*", block_websocket)
                    page = await context.new_page()
                    target = identity(admission["url"])

                    async def route_request(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        allowed = (request.method in ("GET", "HEAD") and parsed.scheme == "https"
                            and parsed.hostname in HOSTS and parsed.port in (None, 443)
                            and parsed.username is None and parsed.password is None)
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            allowed = allowed and identity(request.url) == target
                        if allowed:
                            await route.continue_()
                        else:
                            await route.abort()

                    await context.route("**/*", route_request)
                    response = await page.goto(admission["url"], wait_until="domcontentloaded", timeout=30000)
                    status = response.status if response is not None else None
                    if status in (401, 403, 429):
                        return failure("source_http_" + str(status), status="blocked", http_status=status)
                    if status != 200:
                        return failure("source_http_error", http_status=status)
                    if identity(page.url) != target:
                        return failure("redirect_denied", http_status=status)
                    await page.locator('[data-testid="price-primary"]').wait_for(timeout=8000)
                    html = await page.content()
                    if len(html.encode()) > MAX_BYTES:
                        return failure("document_limit", http_status=200)
                    offer = public_offer(html, admission["url"])
                    return {"status": "ok", "reason": "structured_offer_candidate", "http_status": 200,
                            "retry_after": 0, "offer": offer}
                finally:
                    await browser.close()
    finally:
        proxy.close()
        await proxy.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
        if display_reader is not None:
            display_reader.close()
        if display_process is not None and display_process.returncode is None:
            display_process.terminate()
            try:
                await asyncio.wait_for(display_process.wait(), 3)
            except TimeoutError:
                display_process.kill()
                await display_process.wait()


class BrowserServer(PeerServer):
    def __init__(self, config, config_path):
        super().__init__(config["socket"], ingress_uid=config["client_uid"], socket_gid=config["socket_gid"], max_connections=2)
        self.config, self.config_path = config, config_path
        self.deadline = 75  # Fixed browser-only budget; normal W2/W5 deadlines remain unchanged.
        self.busy = False

    async def respond(self, request):
        validate_admission(request)
        if self.busy:
            return failure("network_failed")
        self.busy = True
        process = None
        start = None
        workspace = None
        try:
            workspace = tempfile.TemporaryDirectory(prefix="haven-browser-job-")
            environment = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": workspace.name}
            if "PYTHONPATH" in os.environ:
                environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
            process = await asyncio.create_subprocess_exec(self.config["python"], "-B", "-m", "scripts.wishlist_browser",
                "worker", "--config", str(self.config_path), env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                start = process_start(Path("/proc") / str(process.pid) / "stat")[1]
            except OSError:
                pass
            async with asyncio.timeout(WORKER_DEADLINE):
                output, _ = await process.communicate(json.dumps(request).encode())
            if process.returncode != 0:
                return failure("network_failed")
            result = decode(output)
            validate_result(result)
            return result
        except TimeoutError:
            return failure("fetch_timeout")
        finally:
            if process is not None:
                if process.returncode is None:
                    if start is not None:
                        kill_snapshot(descendants(process.pid, expected_start=start))
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.communicate()
                clean_managed_group()
            try:
                if workspace is not None:
                    workspace.cleanup()
            finally:
                self.busy = False


async def serve(config, path):
    server = BrowserServer(config, path)
    stopped = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        await server.start()
        print('{"service":"wishlist-browser","result":"ready"}', flush=True)
        await stopped.wait()
    finally:
        await server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "worker"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.mode == "serve":
            if not managed_group() or len({os.geteuid(), config["client_uid"], config["egress_uid"]}) != 3:
                raise ValueError("browser_managed_cgroup_required")
            asyncio.run(serve(config, args.config))
        else:
            admission = decode(sys.stdin.buffer.read(MAX_FRAME + 1))
            try:
                result = asyncio.run(render(config, admission))
                validate_result(result)
            except Exception:
                result = failure("offer_unverified")
            print(json.dumps(result))
    except Exception:
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
