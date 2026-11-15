"""W3 no-model HTTPS worker. Run in a killable process with a whole-job deadline."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import ipaddress
import io
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
from urllib.parse import urljoin, urlsplit

from scripts.wishlist_collection import ADAPTERS, validate_result
from scripts.wishlist_domain import source_host
from scripts.wishlist_extract import MAX_BYTES, extract, identity
from scripts.wishlist_protocol import MAX_FRAME, decode

TIMEOUT = 10
REQUIRED_EVIDENCE = {"momenti-microdata-v1": "microdata", "fabini-jsonld-v1": "jsonld",
                     "ifantazie-microdata-v1": "microdata", "alza-public-price-v1": "jsonld",
                     "intimissimi-public-price-v1": "jsonld"}


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
    if host == "www.fabini.cz":
        return CurlConnection(host, endpoint)
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


class CurlConnection:
    """Fabini's HTTP/2 path; the caller has validated the entire DNS answer."""

    def __init__(self, host, endpoint):
        if host != "www.fabini.cz" or endpoint[1] != 443 or not public_address(endpoint[0]):
            raise FetchError("destination_denied")
        self.host, self.address = host, endpoint[0]
        self.response = None

    def request(self, method, path, *, headers):
        if (method != "GET" or not path.startswith("/") or "\\" in path or "#" in path
                or any(ord(c) <= 32 or ord(c) == 127 for c in path)):
            raise FetchError("destination_denied")
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        try:
            version = subprocess.run(["/usr/bin/curl", "--disable", "--version"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=environment, timeout=3, check=False)
            match = re.match(rb"curl (\d+)\.(\d+)\.(\d+) ", version.stdout)
            if (version.returncode or not match or tuple(map(int, match.groups())) < (8, 4, 0)
                    or not re.search(rb"^Features:.*\bHTTP2\b", version.stdout, re.MULTILINE)):
                raise FetchError("network_failed")
            address = "[" + self.address + "]" if ":" in self.address else self.address
            with tempfile.TemporaryDirectory(prefix="haven-fabini-") as temporary:
                header_path = Path(temporary) / "headers"
                argv = ["/usr/bin/curl", "--disable", "--silent", "--fail", "--http2", "--globoff",
                        "--path-as-is", "--no-netrc", "--disallow-username-in-url",
                        "--proxy", "", "--noproxy", "*", "--proto", "=https",
                        "--max-redirs", "0", "--retry", "0", "--connect-timeout", "10",
                        "--max-time", "30", "--speed-limit", "1", "--speed-time", "10",
                        "--max-filesize", str(MAX_BYTES), "--resolve", self.host + ":443:" + address,
                        "--dump-header", str(header_path)]
                for key, value in headers.items():
                    if any(c in key + value for c in "\r\n\x00"):
                        raise FetchError("destination_denied")
                    argv.extend(["--header", key + ": " + value])
                # Source URLs stay out of argv/diagnostics; disable config, netrc,
                # proxies, globbing, retries and automatic redirects explicitly.
                url = ("https://" + self.host + path).replace('"', '\\"')
                argv.extend(["--config", "-"])
                completed = subprocess.run(argv, input=('url = "' + url + '"\n').encode(),
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=environment, timeout=32, check=False)
                if completed.returncode == 63 or len(completed.stdout) > MAX_BYTES:
                    raise FetchError("document_limit")
                if completed.returncode not in (0, 22):
                    raise FetchError("fetch_timeout" if completed.returncode == 28 else "network_failed")
                with header_path.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise FetchError("document_limit")
                blocks = raw.split(b"\r\n\r\n")
                if blocks[-1] or not 2 <= len(blocks) <= 7:
                    raise FetchError("invalid_document")
                status = None
                for index, block in enumerate(blocks[:-1]):
                    line, _, fields = block.partition(b"\r\n")
                    match = re.fullmatch(rb"HTTP/(?:1\.[01]|2) ([1-5][0-9]{2})(?: [^\r\n]*)?", line)
                    if not match:
                        raise FetchError("invalid_document")
                    status = int(match[1])
                    if index < len(blocks) - 2 and not 100 <= status < 200:
                        raise FetchError("invalid_document")
                    message = http.client.parse_headers(io.BytesIO(fields + b"\r\n\r\n"))
                    if message.defects:
                        raise FetchError("invalid_document")
                if (status < 200 or (completed.returncode == 22) != (status >= 400)):
                    raise FetchError("invalid_document")
                self.response = CurlResponse(status, message, completed.stdout)
        except subprocess.TimeoutExpired:
            raise FetchError("fetch_timeout") from None

    def getresponse(self):
        if self.response is None:
            raise FetchError("invalid_document")
        return self.response

    def close(self):
        if self.response is not None:
            self.response.body.close()


class CurlResponse:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, io.BytesIO(body)

    def getheader(self, name, default=None):
        values = self.headers.get_all(name)
        return default if values is None else ", ".join(values)

    def getheaders(self):
        return list(self.headers.items())

    def read(self, size):
        return self.body.read(size)


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
                if adapter == "alza-public-price-v1":
                    from scripts.wishlist_alza import public_offer
                    candidate = {"status": "extracted", "evidence": ["jsonld"], "offer": public_offer(html, url)}
                elif adapter == "intimissimi-public-price-v1":
                    from scripts.wishlist_intimissimi import public_offer
                    candidate = {"status": "extracted", "evidence": ["jsonld"], "offer": public_offer(html, url)}
                else:
                    candidate = extract(html, url)
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
