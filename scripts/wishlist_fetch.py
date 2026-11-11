"""W3 no-model HTTPS worker. Run in a killable process with a whole-job deadline."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import sys
from urllib.parse import urljoin, urlsplit

from scripts.wishlist_collection import ADAPTERS, validate_result
from scripts.wishlist_domain import source_host
from scripts.wishlist_extract import MAX_BYTES, extract, identity
from scripts.wishlist_protocol import MAX_FRAME, decode

TIMEOUT = 10
REQUIRED_EVIDENCE = {"momenti-microdata-v1": "microdata", "fabini-jsonld-v1": "jsonld",
                     "ifantazie-microdata-v1": "microdata", "alza-jsonld-v1": "jsonld"}


class FetchError(ValueError):
    """Only fixed diagnostic codes leave the worker."""


def failure(reason, *, status="failed", http_status=None, retry_after=0):
    return {"status": status, "reason": reason, "http_status": http_status, "retry_after": retry_after}


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
        if (not address.is_global or address.is_multicast or address.is_reserved
                or address.is_unspecified or address.is_loopback or "%" in value):
            return False
        if address.version == 6:
            if (address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None
                    or address in ipaddress.ip_network("64:ff9b::/96")
                    or address in ipaddress.ip_network("64:ff9b:1::/48")):
                return False
        return True
    except ValueError:
        return False


def resolve(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if (not addresses or len(addresses) > 32
            or any(family not in (socket.AF_INET, socket.AF_INET6) or not public_address(endpoint[0])
                   or endpoint[1] != 443 for family, _, _, _, endpoint in addresses)):
        raise FetchError("destination_denied")
    return addresses[0]


def connection(host):
    family, kind, protocol, _, endpoint = resolve(host)
    raw = socket.socket(family, kind, protocol)
    raw.settimeout(TIMEOUT)
    try:
        # Connect the exact checked sockaddr. Never resolve the host a second time.
        raw.connect(endpoint)
        if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(endpoint[0]):
            raise FetchError("destination_denied")
        context = ssl.create_default_context()
        secure = context.wrap_socket(raw, server_hostname=host)
        client = http.client.HTTPSConnection(host, timeout=TIMEOUT, context=context)
        client.sock = secure
        return client
    except BaseException:
        raw.close()
        raise


def retry_delay(value, now=None):
    if not isinstance(value, str) or len(value) > 100:
        return 0
    try:
        if value.isascii() and value.isdecimal():
            return min(int(value), 31536000)
        instant = parsedate_to_datetime(value)
        if instant.utcoffset() is None:
            return 0
        return min(max(0, int((instant - (now or datetime.now(UTC))).total_seconds()) + 1), 31536000)
    except (ValueError, TypeError, OverflowError):
        return 0


def fetch(url, adapter):
    try:
        host = source_host(url)
        if adapter == "unsupported":
            return failure("unsupported_shop", status="unsupported")
        if ADAPTERS.get(host) != adapter:
            raise FetchError("destination_denied")
        original = identity(url)
        current = url
        for hop in range(4):
            if source_host(current) != host or identity(current) != original:
                raise FetchError("redirect_denied")
            parsed = urlsplit(current)
            path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            client = connection(host)
            try:
                client.request("GET", path, headers={
                    "User-Agent": "HavenWishlistCollector/1.0", "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity", "Connection": "close",
                })
                response = client.getresponse()
                status = response.status
                retry = retry_delay(response.getheader("Retry-After"))
                if status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if hop == 3 or not location or len(location) > 4096:
                        raise FetchError("redirect_denied")
                    current = urljoin(current, location)
                    # The next iteration validates syntax, identity and newly resolved addresses.
                    continue
                if status in (401, 403, 429):
                    return failure("source_http_" + str(status), status="blocked", http_status=status, retry_after=retry)
                if status != 200:
                    return failure("source_http_error", http_status=status, retry_after=retry)
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    return failure("unsupported_encoding", http_status=status)
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in ("text/html", "application/xhtml+xml"):
                    return failure("unsupported_content_type", http_status=status)
                sizes = [value for key, value in response.getheaders() if key.lower() == "content-length"]
                if sizes and (len(sizes) != 1 or not sizes[0].isascii() or not sizes[0].isdecimal()
                              or len(sizes[0]) > 10 or int(sizes[0]) > MAX_BYTES):
                    return failure("document_limit", http_status=status)
                html = response.read(MAX_BYTES + 1)
                if len(html) > MAX_BYTES:
                    return failure("document_limit", http_status=status)
                if sizes and len(html) != int(sizes[0]):
                    return failure("invalid_document", http_status=status)
                candidate = extract(html, url, required_condition="NewCondition" if adapter == "alza-jsonld-v1" else None)
                required = REQUIRED_EVIDENCE[adapter]
                if candidate["status"] != "extracted" or required not in candidate.get("evidence", []):
                    return failure("offer_unverified", http_status=status)
                result = {"status": "ok", "reason": "structured_offer_candidate", "http_status": 200,
                          "retry_after": retry, "offer": candidate["offer"]}
                validate_result(result)
                return result
            finally:
                client.close()
        raise FetchError("redirect_denied")
    except FetchError as error:
        return failure(str(error))
    except (OSError, http.client.HTTPException):
        return failure("network_failed")
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return failure("offer_unverified")


def main():
    try:
        request = decode(sys.stdin.buffer.read(MAX_FRAME + 1))
        if (set(request) != {"url", "job"} or not isinstance(request["url"], str)
                or hashlib.sha256(request["url"].encode()).hexdigest() != request["job"]["url_sha256"]):
            raise ValueError("fetch_job")
        result = fetch(request["url"], request["job"]["adapter"])
    except Exception:
        result = failure("invalid_document")
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
