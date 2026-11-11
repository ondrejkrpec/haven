"""W3 bounded oneshot scheduler client. No SQLite, model, login or Telegram access."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
from uuid import uuid4

from scripts.wishlist_collection import JOB_KEYS, validate_result
from scripts.wishlist_fetch import failure
from scripts.wishlist_protocol import MAX_FRAME, close_writer, decode, peer_uid, read_frame, write_frame
from scripts.wishlist_socket import PeerServer, socket_path


class CollectionServer(PeerServer):
    def __init__(self, domain, path, **kwargs):
        from scripts.wishlist_collection import Collection
        self.collection = Collection(domain)
        super().__init__(path, **kwargs)

    async def respond(self, request):
        return self.collection.handle(request)


def validate_endpoint(config):
    if (not isinstance(config, dict) or not {"socket", "service_uid"} <= set(config)
            or set(config) - {"socket", "service_uid", "browser"}
            or type(config["service_uid"]) is not int or not 1 <= config["service_uid"] < 2**31):
        raise ValueError("collector_endpoint")
    socket_path(config["socket"])
    if "browser" in config:
        browser = config["browser"]
        if not isinstance(browser, dict) or set(browser) != {"socket", "service_uid"}:
            raise ValueError("collector_browser_endpoint")
        validate_endpoint(browser)
        if browser["socket"] == config["socket"] or browser["service_uid"] == config["service_uid"]:
            raise ValueError("collector_browser_separation")
    return config


async def request(endpoint, message, *, timeout=5):
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(endpoint["socket"], limit=MAX_FRAME)
            if peer_uid(writer.get_extra_info("socket")) != endpoint["service_uid"]:
                raise PermissionError("collector_server_identity")
            await write_frame(writer, message)
            return await read_frame(reader)
    finally:
        if writer is not None:
            await close_writer(writer)


async def run_fetch(admission, *, timeout=40):
    job, url = admission["job"], admission["url"]
    if (set(job) != JOB_KEYS or hashlib.sha256(url.encode()).hexdigest() != job["url_sha256"]):
        raise ValueError("collector_admission")
    # Clean environment, fixed executable/module, no shell; OS isolation is supplied by the unit.
    environment = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    if "PYTHONPATH" in os.environ:
        environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "scripts.wishlist_fetch", env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    try:
        async with asyncio.timeout(timeout):
            output, _ = await process.communicate(json.dumps({"url": url, "job": job}).encode())
        if process.returncode != 0:
            return failure("network_failed")
        result = decode(output)
        validate_result(result)
        return result
    except TimeoutError:
        return failure("fetch_timeout")
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()


async def collect(endpoint, *, maximum=20, fetcher=run_fetch):
    completed = 0
    for _ in range(maximum):
        admission = await request(endpoint, {"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
        if admission.get("code") == "idle":
            break
        if admission.get("code") != "ok":
            raise ValueError("collector_claim_failed")
        if "browser" in endpoint and admission["job"]["adapter"] == "alza-public-price-v1":
            result = await request(endpoint["browser"], {"protocol_version": 1, "operation": "browser.fetch",
                "url": admission["url"], "job": admission["job"]}, timeout=70)
            validate_result(result)
        else:
            result = await fetcher(admission)
        response = await request(endpoint, {"protocol_version": 1, "operation": "collection.complete",
                                            "job": admission["job"], "result": result})
        # Network reads have no purchase side effects. Lost completions reconcile by lease expiry.
        if response.get("code") not in ("ok", "stale"):
            raise ValueError("collector_completion_failed")
        completed += 1
        print(json.dumps({"service": "wishlist-collector", "job_id": admission["job"]["job_id"],
                          "result": result["status"], "reason": result["reason"],
                          "admission": response["code"]}), flush=True)
    return completed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with args.config.open("rb") as stream:
            endpoint = validate_endpoint(decode(stream.read(MAX_FRAME + 1)))
        asyncio.run(collect(endpoint))
    except Exception:
        print('{"service":"wishlist-collector","result":"failed","reason":"collection_unavailable"}', flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
