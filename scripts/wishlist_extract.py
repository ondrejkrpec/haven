"""Offline wishlist feasibility probe; never fetches URLs or executes page code."""

import argparse
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

MAX_BYTES = 4 * 1024 * 1024
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
AVAILABILITY = {
    "InStock": "in_stock", "OutOfStock": "out_of_stock", "SoldOut": "out_of_stock",
    "PreOrder": "preorder", "BackOrder": "backorder", "LimitedAvailability": "limited",
    "Discontinued": "discontinued",
}


class ExtractionError(ValueError):
    """Fixed diagnostics only; source content must not become an error message."""


def identity(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise ExtractionError("invalid_source_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError
    except ValueError:
        raise ExtractionError("invalid_source_url") from None
    # Unknown query parameters are identity-bearing. Only UTM tracking is ignored.
    query = tuple(sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                         if not k.lower().startswith("utm_")))
    return parsed.hostname.lower(), parsed.path.rstrip("/") or "/", query


def type_name(value):
    return value.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(value, str) else ""


def types(record):
    value = record.get("@type", [])
    return {type_name(v) for v in (value if isinstance(value, list) else [value])}


class Node:
    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return "".join(child.text() if isinstance(child, Node) else child for child in self.children)


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        if len(self.nodes) >= 50000 or len(self.stack) >= 128:
            raise ExtractionError("document_limit")
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def microdata(node):
    record = {"@type": node.attrs.get("itemtype", "")}
    pending = list(reversed(node.children))
    while pending:
        child = pending.pop()
        if not isinstance(child, Node):
            continue
        scoped = "itemscope" in child.attrs
        keys = child.attrs.get("itemprop", "").split()
        if keys:
            value = microdata(child) if scoped else next(
                (child.attrs[k] for k in ("content", "href", "src", "datetime") if k in child.attrs),
                child.text().strip(),
            )
            for key in keys:
                if key.startswith("@"):
                    raise ExtractionError("invalid_document")
                record.setdefault(key, []).append(value)
        if not scoped:
            pending.extend(reversed(child.children))
    return record


def json_products(value, depth=0):
    if depth > 64:
        raise ExtractionError("structured_data_limit")
    if isinstance(value, dict):
        if "Product" in types(value):
            yield value
        for child in value.values():
            yield from json_products(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from json_products(child, depth + 1)


def values(record, key):
    value = record.get(key, [])
    return value if isinstance(value, list) else [value]


def one(record, key, *, required=True):
    candidates = values(record, key)
    if not candidates and not required:
        return None
    if not candidates or not all(isinstance(v, (str, int, Decimal)) and not isinstance(v, bool)
                                 for v in candidates):
        raise ExtractionError("missing_or_ambiguous_" + key)
    normalized = {str(v).strip() for v in candidates}
    if len(normalized) != 1 or not next(iter(normalized)):
        raise ExtractionError("missing_or_ambiguous_" + key)
    return next(iter(normalized))


def amount(value):
    # Schema prices use decimal points, without display formatting or separators.
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", value):
        raise ExtractionError("invalid_price")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ExtractionError("invalid_price") from None
    if result <= 0:
        raise ExtractionError("invalid_price")
    return format(result.quantize(Decimal("0.01")), "f")


def invalid_constant(value):
    raise ExtractionError("invalid_document")


def extract(html, source_url, *, http_status=200, required_condition=None):
    """Return a candidate; a shop can additionally require an explicit item condition."""
    output = {"schema_version": 1, "http_status": http_status, "model_calls": 0}
    if http_status in (401, 403, 429):
        return {**output, "status": "blocked", "reason": "source_http_" + str(http_status)}
    if http_status != 200:
        return {**output, "status": "unavailable", "reason": "source_http_error"}
    try:
        requested = identity(source_url)
        if len(html) > MAX_BYTES:
            raise ExtractionError("document_limit")
        text = html.decode("utf-8-sig", errors="strict") if isinstance(html, bytes) else html
        document = Document()
        document.feed(text)
        document.close()
        canonical = [n.attrs.get("href") for n in document.nodes
                     if n.tag == "link" and n.attrs.get("rel") == "canonical"]
        if canonical and any(identity(url)[:2] != requested[:2] for url in canonical):
            raise ExtractionError("canonical_mismatch")
        products = []
        for node in document.nodes:
            if "itemscope" in node.attrs and type_name(node.attrs.get("itemtype", "")) == "Product":
                products.append((microdata(node), "microdata"))
            if node.tag == "script" and node.attrs.get("type", "").lower() == "application/ld+json":
                value = json.loads(node.text(), parse_float=Decimal, parse_constant=invalid_constant)
                products.extend((p, "jsonld") for p in json_products(value))
        if not products:
            raise ExtractionError("product_data_missing")
        candidates, sources = {}, set()
        for product, source in products:
            for offer in values(product, "offers"):
                if not isinstance(offer, dict) or "Offer" not in types(offer):
                    continue
                offer_url = one(offer, "url", required=False) or one(product, "url", required=False)
                if offer_url is None or identity(offer_url) != requested:
                    continue
                if required_condition is not None and type_name(one(offer, "itemCondition", required=False)) != required_condition:
                    raise ExtractionError("offer_condition_unverified")
                if (values(offer, "validForMemberTier") or values(offer, "eligibleQuantity")
                        or any(isinstance(p, dict) and (values(p, "validForMemberTier") or values(p, "eligibleQuantity"))
                               for p in values(offer, "priceSpecification"))):
                    raise ExtractionError("conditional_offer")
                currency = one(offer, "priceCurrency")
                if not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ExtractionError("invalid_currency")
                availability = AVAILABILITY.get(type_name(one(offer, "availability")))
                if availability is None:
                    raise ExtractionError("unknown_availability")
                result = {
                    "name": one(product, "name"), "sku": one(product, "sku", required=False),
                    "price": amount(one(offer, "price")), "currency": currency,
                    "availability": availability,
                    "conditions_verification": "not_verified",
                }
                signature = json.dumps(result, sort_keys=True)
                candidates[signature] = result
                sources.add(source)
        if not candidates:
            raise ExtractionError("exact_offer_missing")
        if len(candidates) != 1:
            raise ExtractionError("conflicting_offers")
        return {**output, "status": "extracted", "reason": "structured_offer_candidate",
                "evidence": sorted(sources), "offer": next(iter(candidates.values()))}
    except (ExtractionError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        reason = str(error) if isinstance(error, ExtractionError) else "invalid_document"
        return {**output, "status": "needs_review", "reason": reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--http-status", type=int, default=200)
    args = parser.parse_args()
    try:
        with args.html.open("rb") as source:
            html = source.read(MAX_BYTES + 1)
        result = extract(html, args.url, http_status=args.http_status)
    except OSError:
        result = {"schema_version": 1, "status": "unavailable", "reason": "input_unavailable", "model_calls": 0}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "extracted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
