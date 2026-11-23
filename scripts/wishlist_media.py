"""Validation shared by the exact-offer extractor and the isolated photo worker."""

import base64
import re
import struct
from urllib.parse import unquote, urljoin, urlsplit

# Add a CDN only for its corresponding merchant. Unlisted sources use a placeholder.
IMAGE_HOSTS = {
    "www.momenti.cz": {"www.momenti.cz", "cdn.myshoptet.com"},
    "www.fabini.cz": {"www.fabini.cz", "cdn.shopify.com"},
    "www.ifantazie.cz": {"www.ifantazie.cz", "cdn.myshoptet.com"},
    "www.intimissimi.com": {"www.intimissimi.com", "static.intimissimi.com", "static.calzedonia.com"},
}
MAX_IMAGE_BYTES = 512 * 1024
IMAGE_FRAME_LIMIT = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def image_url(shop, value):
    if (not isinstance(value, str) or len(value) > 1024
            or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or "\\" in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS.get(shop, ())
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or not re.search(r"\.(?:jpe?g|png|webp)\Z", parsed.path, re.IGNORECASE)):
            return None
        if shop == "www.ifantazie.cz" and parsed.hostname == "cdn.myshoptet.com":
            if not re.fullmatch(r"/usr/www\.ifantazie\.cz/user/shop/(?:big|detail|orig)/[\w .~()+-]+",
                                unquote(parsed.path, errors="strict")):
                return None
    except ValueError:
        return None
    return value


def product_image(product, source_url):
    images = product.get("image", [])
    images = images if isinstance(images, list) else [images]
    shop = urlsplit(source_url).hostname
    for value in images[:16]:
        if isinstance(value, dict):
            value = value.get("contentUrl") or value.get("url")
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str) and len(value) <= 1024 and value and value == value.strip():
            try:
                approved = image_url(shop, urljoin(source_url, value))
            except ValueError:
                continue
            if approved:
                return approved
    return None


def jpeg_dimensions(data):
    offset = 2
    while offset + 4 < len(data) and data[offset] == 0xFF:
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        if length < 2 or offset + 2 + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2) and length >= 8:
            return struct.unpack_from(">HH", data, offset + 5)
        if marker in (0xDA, 0xD9):
            break
        offset += 2 + length
    return 0, 0


def dimensions(mime, data):
    if mime == "image/jpeg" and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        return jpeg_dimensions(data)
    if mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"):
        return struct.unpack_from(">II", data, 16)
    if (mime == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") + 8 == len(data)):
        if data[12:16] == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return tuple(value & 0x3FFF for value in struct.unpack_from("<HH", data, 26))
        if data[12:16] == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if data[12:16] == b"VP8X" and not data[20] & 0x02:  # No animated WebP.
            return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return 0, 0


def valid_image(mime, data):
    if mime not in IMAGE_TYPES or not 32 <= len(data) <= MAX_IMAGE_BYTES:
        return False
    width, height = dimensions(mime, data)
    return 1 <= width <= 2048 and 1 <= height <= 2048


def image_content(payload):
    """Only return the declared raster type when it agrees with the bytes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("mime"), str):
        return None
    encoded = payload.get("body")
    if not isinstance(encoded, str) or len(encoded) > IMAGE_FRAME_LIMIT - 1024:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return (data, payload["mime"]) if valid_image(payload["mime"], data) else None
