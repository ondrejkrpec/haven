"""Read-only socket readiness: verify server UID and rejection of unauthorized root."""

from pathlib import Path
import socket
import stat
import struct


def check_socket(path: Path, uid: int):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o660:
        raise ValueError("wishlist_socket_metadata")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path))
        if struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != uid:
            raise ValueError("wishlist_socket_peer")
        # Root is deliberately not an enrolled application peer. No request is sent.
        def read(size):
            result = bytearray()
            while len(result) < size:
                block = client.recv(size - len(result))
                if not block:
                    raise ValueError("wishlist_health_frame")
                result.extend(block)
            return bytes(result)
        size = struct.unpack("!I", read(4))[0]
        if not 0 < size <= 1024:
            raise ValueError("wishlist_health_frame")
        import json
        if json.loads(read(size)) != {"protocol_version": 1, "request_id": None, "code": "denied", "committed": False}:
            raise ValueError("wishlist_health_response")
