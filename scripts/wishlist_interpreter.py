"""Separate peer-authenticated interpretation service; never owns domain state."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

from scripts.wishlist_language_contract import PRODUCT_CATEGORIES, validate_category_request, validate_decision, validate_input
from scripts.wishlist_protocol import close_writer, peer_uid, read_frame, write_frame
from scripts.wishlist_socket import PeerServer, private_directory, socket_path

ROOT = Path(__file__).resolve().parents[1]
WORKER_TIMEOUT = 16


def validate_language_settings(value):
    if (not isinstance(value, dict) or set(value) != {"socket", "service_uid"}
            or type(value["service_uid"]) is not int or not 1 <= value["service_uid"] <= 2**31 - 1):
        raise ValueError("language_settings")
    socket_path(value["socket"])
    return dict(value)


class LanguageClient:
    def __init__(self, settings):
        self.settings = validate_language_settings(settings)

    async def interpret(self, prepared):
        validate_input(prepared)
        path = socket_path(self.settings["socket"])
        private_directory(path.parent, uid=self.settings["service_uid"], socket_directory=True)
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.settings["service_uid"]
                or stat.S_IMODE(info.st_mode) != 0o660):
            raise ValueError("language_socket_untrusted")
        writer = None
        try:
            async with asyncio.timeout(18):
                reader, writer = await asyncio.open_unix_connection(path, limit=65536)
                if peer_uid(writer.get_extra_info("socket")) != self.settings["service_uid"]:
                    raise PermissionError("language_server_denied")
                await write_frame(writer, prepared)
                return await read_frame(reader)
        finally:
            if writer:
                await close_writer(writer)


class NativeWorker:
    def __init__(self, *, source, home, python):
        self.source, self.home, self.python = Path(source), Path(home), Path(python)
        self.credential_lock = asyncio.Lock()

    async def interpret(self, prepared, *, category=False):
        if category and self.credential_lock.locked():
            return {"category": None}
        async with self.credential_lock:
            return await self.run(prepared, category=category)

    async def run(self, prepared, *, category):
        command = [str(self.python), "-B", "-m", "scripts.wishlist_model",
                   "--source", str(self.source), "--home", str(self.home)]
        if category:
            command.append("--category")
        environment = {"PATH": str(self.python.parent) + ":/usr/bin:/bin", "HOME": str(self.home.parent),
                       "HERMES_HOME": str(self.home), "HERMES_RUNTIME_DIR": str(self.home / "tools"),
                       "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "vendor"),
                       "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        # The independently installed native source is root-owned and read-only.
        # Trust only this explicit path; do not inherit a global Git exception.
        environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory",
                           GIT_CONFIG_VALUE_0=str(self.source))
        process = await asyncio.create_subprocess_exec(*command, cwd=self.source, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=8192)
        try:
            async with asyncio.timeout(10 if category else WORKER_TIMEOUT):
                await write_frame(process.stdin, prepared)
                process.stdin.close()
                response = await read_frame(process.stdout)
                if len(json.dumps(response)) > 8192:
                    raise ValueError("language_output_size")
                await process.wait()
                if process.returncode != 0:
                    raise ValueError("language_worker_failed")
                return response
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


class InterpreterServer(PeerServer):
    def __init__(self, path, *, client_uid, backend, socket_gid=None):
        super().__init__(path, ingress_uid=client_uid, socket_gid=socket_gid, deadline=20, max_connections=1)
        self.backend = backend

    async def respond(self, request):
        validate_input(request)
        response = await self.backend.interpret(request)
        if response.get("code") == "ok":
            validate_decision(response["decision"], request)
        return response


class CategoryServer(PeerServer):
    def __init__(self, path, *, client_uid, backend, clock=time.monotonic):
        super().__init__(path, ingress_uid=client_uid, deadline=11, max_connections=1)
        self.backend = backend
        self.clock = clock
        self.next_attempt = 0

    async def respond(self, request):
        validate_category_request(request)
        now = self.clock()
        if now < self.next_attempt:
            return {"category": None}
        self.next_attempt = now + 5
        response = await self.backend.interpret(request, category=True)
        category = response.get("category")
        return {"category": category if category in PRODUCT_CATEGORIES else None}


async def serve(config, *, category_client_uid=None):
    keys = {"schema_version", "socket", "socket_gid", "client_uid", "source", "home", "python"}
    if set(config) != keys or type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("language_config")
    if config["client_uid"] == os.geteuid():
        raise ValueError("language_identity_must_be_separate")
    for key in ("source", "home", "python"):
        path = Path(config[key])
        if not path.is_absolute():
            raise ValueError("language_path")
    server = InterpreterServer(config["socket"], client_uid=config["client_uid"], socket_gid=config["socket_gid"],
                               backend=NativeWorker(source=config["source"], home=config["home"], python=config["python"]))
    category = None
    if category_client_uid is not None:
        if category_client_uid in (config["client_uid"], os.geteuid()):
            raise ValueError("category_identity_must_be_separate")
        directory = Path(config["socket"]).parent / "category"
        directory.mkdir(mode=0o750, exist_ok=True)
        private_directory(directory, uid=os.geteuid(), socket_directory=True)
        directory.chmod(0o750)
        category = CategoryServer(directory / "request.sock", client_uid=category_client_uid, backend=server.backend)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await server.start()
        if category:
            await category.start()
        print('{"result":"ready","service":"wishlist-language"}', flush=True)
        await stopped.wait()
    finally:
        if category:
            await category.close()
        await server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--category-client-uid", type=int)
    args = parser.parse_args(argv)
    try:
        from scripts.wishlist_protocol import decode
        os.umask(0o077)
        with args.config.open("rb") as stream:
            config = decode(stream.read(65537))
        asyncio.run(serve(config, category_client_uid=args.category_client_uid))
    except Exception:
        print('{"result":"failed","code":"wishlist_language_failed"}')
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main())
