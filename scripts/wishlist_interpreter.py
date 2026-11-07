"""Separate peer-authenticated interpretation service; never owns domain state."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import sys

from scripts.wishlist_language_contract import validate_decision, validate_input
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

    async def interpret(self, prepared):
        command = [str(self.python), "-B", "-m", "scripts.wishlist_model",
                   "--source", str(self.source), "--home", str(self.home)]
        environment = {"PATH": str(self.python.parent) + ":/usr/bin:/bin", "HOME": str(self.home.parent),
                       "HERMES_HOME": str(self.home), "HERMES_RUNTIME_DIR": str(self.home / "tools"),
                       "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        process = await asyncio.create_subprocess_exec(*command, cwd=self.source, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=8192)
        try:
            async with asyncio.timeout(WORKER_TIMEOUT):
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


async def serve(config):
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
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await server.start()
        print('{"result":"ready","service":"wishlist-language"}', flush=True)
        await stopped.wait()
    finally:
        await server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from scripts.wishlist_protocol import decode
        os.umask(0o077)
        with args.config.open("rb") as stream:
            config = decode(stream.read(65537))
        asyncio.run(serve(config))
    except Exception:
        print('{"result":"failed","code":"wishlist_language_failed"}')
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main())
