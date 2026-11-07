"""Bounded Linux peer-authenticated server shared by wishlist and interpretation."""

import asyncio
import errno
import fcntl
import os
from pathlib import Path
import socket
import stat

from scripts.wishlist_protocol import DEADLINE, MAX_FRAME, close_writer, peer_uid, read_frame, write_frame


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


class PeerServer:
    def __init__(self, path, *, ingress_uid, socket_gid=None, deadline=DEADLINE, max_connections=8):
        if (type(ingress_uid) is not int or ingress_uid < 1 or not 0 < deadline <= 30
                or type(max_connections) is not int or not 1 <= max_connections <= 8):
            raise ValueError("server_limits")
        self.path = socket_path(str(path))
        self.ingress_uid, self.socket_gid = ingress_uid, socket_gid
        self.deadline, self.max_connections = deadline, max_connections
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

    @staticmethod
    def response(request_id, code):
        return {"protocol_version": 1, "request_id": request_id, "code": code, "committed": False}

    async def respond(self, request):
        raise NotImplementedError

    async def _serve(self, reader, writer):
        try:
            async with asyncio.timeout(self.deadline):
                if peer_uid(writer.get_extra_info("socket")) != self.ingress_uid:
                    await write_frame(writer, self.response(None, "denied"))
                    return
                request = await read_frame(reader)
                response = await self.respond(request)
                await write_frame(writer, response)
        except (ValueError, TypeError, OverflowError, RecursionError, asyncio.IncompleteReadError):
            try:
                async with asyncio.timeout(self.deadline):
                    await write_frame(writer, self.response(None, "invalid_request"))
            except (OSError, TimeoutError):
                pass
        except (OSError, TimeoutError):
            pass
        except Exception:
            try:
                async with asyncio.timeout(self.deadline):
                    await write_frame(writer, self.response(None, "unavailable"))
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
