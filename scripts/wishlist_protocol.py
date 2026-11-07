"""Bounded, strict JSON frames shared by the W2 Unix socket service and client."""

import asyncio
import json
import math
import socket
import struct

from scripts.wishlist_domain import canonical

MAX_FRAME = 65536
MAX_DEPTH = 32
DEADLINE = 5


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite_number")


def _values(value, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("json_depth")
    if isinstance(value, str):
        value.encode("utf-8")  # Reject escaped lone surrogates, too.
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite_number")
    elif isinstance(value, dict):
        for key, item in value.items():
            _values(key, depth + 1)
            _values(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _values(item, depth + 1)


def decode(raw):
    if not 0 < len(raw) <= MAX_FRAME:
        raise ValueError("frame_size")
    text = raw.decode("utf-8")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("json_depth")
        elif char in "]}":
            depth -= 1
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    _values(value)
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def encode(value):
    _values(value)
    raw = canonical(value).encode("utf-8")
    if not isinstance(value, dict) or not 0 < len(raw) <= MAX_FRAME:
        raise ValueError("frame_size")
    return struct.pack("!I", len(raw)) + raw


async def read_frame(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("frame_size")
    return decode(await reader.readexactly(size))


async def write_frame(writer, value):
    writer.write(encode(value))
    await writer.drain()


def peer_uid(sock):
    if not hasattr(socket, "SO_PEERCRED"):
        raise ValueError("linux_peer_credentials_required")
    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid


async def close_writer(writer):
    writer.close()
    try:
        async with asyncio.timeout(1):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        pass
