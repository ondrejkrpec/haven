"""W2 Linux local service. Explicit configuration; no network fetches or Telegram poller."""

import argparse
import asyncio
import errno
import fcntl
import os
from pathlib import Path
import re
import signal
import socket
import stat

from scripts.wishlist_domain import IngressContext, Wishlist
from scripts.wishlist_protocol import DEADLINE, MAX_FRAME, close_writer, decode, peer_uid, read_frame, write_frame
from scripts.wishlist_telegram import TelegramApplication


def socket_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or len(os.fsencode(value)) > 100
            or Path(value).resolve() != Path(value) or "\x00" in value):
        raise ValueError("socket_path")
    return Path(value)


def private_directory(path, *, uid, socket_directory=False):
    info = path.lstat()
    modes = (0o700, 0o750) if socket_directory else (0o700,)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) not in modes or path.resolve() != path):
        raise ValueError("private_directory_required")


def validate_settings(settings):
    if (not isinstance(settings, dict) or set(settings) != {"socket", "service_uid", "bot_id"}
            or type(settings["service_uid"]) is not int or not 1 <= settings["service_uid"] <= 2**31 - 1
            or not isinstance(settings["bot_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", settings["bot_id"])):
        raise ValueError("wishlist_settings")
    socket_path(settings["socket"])
    return dict(settings)


def load_service_config(path):
    with path.open("rb") as stream:
        config = decode(stream.read(MAX_FRAME + 1))
    keys = {"schema_version", "database", "socket", "socket_gid", "ingress_uid", "bot_id", "owner_id", "timezone"}
    if set(config) != keys or type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("service_config")
    validate_settings({"socket": config["socket"], "service_uid": config["ingress_uid"], "bot_id": config["bot_id"]})
    if type(config["socket_gid"]) is not int or not 1 <= config["socket_gid"] <= 2**31 - 1:
        raise ValueError("socket_group")
    database = Path(config["database"])
    if not database.is_absolute() or database.resolve() != database:
        raise ValueError("database_path")
    return config


def private_database(path):
    private_directory(path.parent, uid=os.geteuid())
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("private_database_required")
    finally:
        os.close(descriptor)


class Server:
    def __init__(self, domain, path, *, ingress_uid, socket_gid=None, deadline=DEADLINE, max_connections=8):
        if (type(ingress_uid) is not int or ingress_uid < 1 or not 0 < deadline <= DEADLINE
                or type(max_connections) is not int or not 1 <= max_connections <= 8):
            raise ValueError("server_limits")
        self.domain, self.path = domain, socket_path(str(path))
        self.ingress_uid, self.socket_gid = ingress_uid, socket_gid
        self.deadline, self.max_connections = deadline, max_connections
        self.application = TelegramApplication(domain)
        self.tasks, self.server, self.inode = set(), None, None
        self.lease = None

    def _claim_socket(self):
        self.lease = os.open(self.path.with_name(".ingress.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(self.lease)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("socket_lease_untrusted")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("socket_already_owned") from None
        try:
            previous = self.path.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid()
                or stat.S_IMODE(previous.st_mode) != 0o660 or previous.st_nlink != 1):
            raise ValueError("socket_already_exists")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(str(self.path)) != errno.ECONNREFUSED:
                raise ValueError("socket_already_exists")
        if self.path.lstat().st_ino != previous.st_ino:
            raise ValueError("socket_changed")
        self.path.unlink()

    async def start(self):
        if not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("linux_peer_credentials_required")
        private_directory(self.path.parent, uid=os.geteuid(), socket_directory=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._claim_socket()
            # Bind in a private directory. W4 must set its shared ingress group explicitly.
            listener.bind(str(self.path))
            self.inode = self.path.lstat().st_ino
            if self.socket_gid is not None:
                os.chown(self.path, -1, self.socket_gid)
            self.path.chmod(0o660)
            listener.listen(self.max_connections)
            listener.setblocking(False)
            self.server = await asyncio.start_unix_server(self._connected, sock=listener, limit=MAX_FRAME,
                                                         backlog=self.max_connections)
        except BaseException:
            listener.close()
            self._unlink_owned_socket()
            self._release_lease()
            raise
        return self

    def _unlink_owned_socket(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if self.inode is not None and stat.S_ISSOCK(info.st_mode) and info.st_ino == self.inode:
            self.path.unlink()

    def _release_lease(self):
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None

    def _connected(self, reader, writer):
        if len(self.tasks) >= self.max_connections:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _serve(self, reader, writer):
        try:
            async with asyncio.timeout(self.deadline):
                if peer_uid(writer.get_extra_info("socket")) != self.ingress_uid:
                    await write_frame(writer, self.domain.response(None, "denied"))
                    return
                request = await read_frame(reader)
                if set(request) == {"protocol_version", "operation", "event"}:
                    if type(request["protocol_version"]) is not int or request["protocol_version"] != 1:
                        response = self.domain.response(None, "invalid_request")
                    elif request["operation"] != "telegram.message":
                        response = self.domain.response(None, "denied")
                    else:
                        response = self.application.handle(request["event"])
                else:
                    source = request.get("source", {})
                    context = IngressContext(self.domain.bot_id, source.get("user_id"), source.get("chat_id"),
                                             source.get("message_id"), source.get("text_sha256"),
                                             source.get("platform")) if isinstance(source, dict) else None
                    response = self.domain.handle(request, context)
                await write_frame(writer, response)
        except (ValueError, TypeError, OverflowError, RecursionError, asyncio.IncompleteReadError):
            try:
                async with asyncio.timeout(self.deadline):
                    await write_frame(writer, self.domain.response(None, "invalid_request"))
            except (OSError, TimeoutError):
                pass
        except (OSError, TimeoutError):
            pass
        except Exception:
            try:
                async with asyncio.timeout(self.deadline):
                    await write_frame(writer, self.domain.response(None, "unavailable"))
            except (OSError, TimeoutError):
                pass
        finally:
            await close_writer(writer)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self._unlink_owned_socket()
        self._release_lease()


async def serve(config):
    path = Path(config["database"])
    private_database(path)
    domain = Wishlist(path, bot_id=config["bot_id"], owner_id=config["owner_id"], timezone=config["timezone"])
    server = Server(domain, config["socket"], ingress_uid=config["ingress_uid"], socket_gid=config["socket_gid"])
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await server.start()
        print('{"result":"ready","service":"wishlist"}', flush=True)
        await stopped.wait()
    finally:
        await server.close()
        domain.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        asyncio.run(serve(load_service_config(args.config)))
    except Exception:
        print('{"result":"failed","code":"wishlist_service_failed"}', flush=True)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
