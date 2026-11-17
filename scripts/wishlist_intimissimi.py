"""CZ Intimissimi: bind a full model/colour/size PID to public price and stock.

Canonical links identify the base model; JSON-LD offer URLs can name an internal
storefront. Neither is a fetch target. Identity comes from the requested full
PID, primary DOM, selected colour/size and matching Product SKU instead.
"""

from decimal import Decimal
from html import unescape
import json
import re
from urllib.parse import unquote, urlsplit

from scripts.wishlist_extract import (
    Document, ExtractionError, MAX_BYTES, amount, identity, invalid_constant, json_products, types,
)


def require(condition):
    if not condition:
        raise ExtractionError("offer_unverified")


def walk(root):
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(child for child in reversed(node.children) if hasattr(child, "attrs"))


def classes(node):
    return set((node.attrs.get("class") or "").split())


def only(nodes):
    require(len(nodes) == 1)
    return nodes[0]


def text(node):
    return " ".join(node.text().split())


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def requested(url):
    host, _, query = identity(url)
    parsed = urlsplit(url)
    require(host == "www.intimissimi.com" and not query and not parsed.fragment)
    path = unquote(parsed.path, errors="strict").replace("+", " ")
    match = re.fullmatch(r"(/cz/product/[a-z0-9_-]+-)([A-Z0-9]{2,24}) {3}([A-Z0-9]{2,24})\.html", path)
    require(match is not None)
    return match[2] + "   " + match[3], match[1] + match[2] + ".html", match[3]


def public_offer(html, url):
    require(isinstance(html, (str, bytes)) and len(html) <= MAX_BYTES)
    pid, master_path, variant = requested(url)
    document = Document()
    document.feed(html.decode("utf-8-sig") if isinstance(html, bytes) else html)
    document.close()
    canonical = only([node for node in document.nodes
                      if node.tag == "link" and node.attrs.get("rel") == "canonical"])
    require(identity(canonical.attrs.get("href")) == ("www.intimissimi.com", master_path, ()))
    root = only([node for node in document.nodes
                 if {"js-intProductDetail", "product-detail"} <= classes(node)])
    require(root.attrs.get("data-pid") == pid and root.attrs.get("data-variant-pid") == pid)
    nodes = list(walk(root))
    parents = {id(child): node for node in nodes for child in node.children if hasattr(child, "attrs")}

    def visible(node):
        while node is not None:
            style = re.sub(r"\s+", "", node.attrs.get("style") or "").lower()
            if ({"hidden", "hide", "d-none", "member-price", "loyalty-price"} & classes(node)
                    or "hidden" in node.attrs or node.attrs.get("aria-hidden") == "true"
                    or re.search(r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style)):
                return False
            node = parents.get(id(node))
        return True

    require(visible(root))
    colour = text(only([node for node in nodes if "js-product-color-id" in classes(node)]))
    colour_match = re.fullmatch(r"([A-Za-z0-9]{1,16}) - .+", colour)
    require(colour_match is not None)
    selected = {node.attrs.get("data-size-id") for node in nodes if node.tag == "input"
                and node.attrs.get("type") == "radio" and "checked" in node.attrs
                and node.attrs.get("data-size-id") is not None}
    require(len(selected) == 1)
    size = selected.pop()
    require(isinstance(size, str) and re.fullmatch(r"[A-Z0-9]{1,8}", size) is not None
            and colour_match[1].upper() + size == variant)

    wrapper = only([node for node in nodes if
                    {"cc-intProductDetail-size-wrapper", "sizebay-js-desktop-size-buttons"} <= classes(node)])
    size_label = only([node for node in walk(wrapper) if node.tag == "label"
                       and "js-intProductDetail-size-item" in classes(node) and node.attrs.get("for") == size])
    require("button" in classes(size_label) and not classes(size_label) - {
        "js-intProductDetail-size-item", "cc-intProductDetail-size-item", "size-list-item", "boolean-field", "button",
        "size-button--low-in-stock", "cc-intProductDetail-size-item_low", "size-button--unavailable", "disabled"})
    stock = only([node for node in walk(size_label) if node.tag == "input"
                  and node.attrs.get("type") == "radio" and node.attrs.get("data-size-id") == size])
    allocation = stock.attrs.get("data-allocation", "")
    require(isinstance(allocation, str) and re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,3})?", allocation) is not None
            and stock.attrs.get("data-iscomingsoon") == "false")
    available = Decimal(allocation) > 0
    unavailable = "size-button--unavailable" in classes(size_label)
    disabled = "disabled" in stock.attrs or "disabled" in classes(size_label) or unavailable
    require(available != disabled)
    low = bool({"size-button--low-in-stock", "cc-intProductDetail-size-item_low"} & classes(size_label))
    require(not unavailable or not low)
    availability = ("limited" if low else "in_stock") if available else "out_of_stock"

    records = []
    for node in document.nodes:
        if node.tag == "script" and (node.attrs.get("type") or "").lower() == "application/ld+json":
            value = json.loads(node.text(), parse_float=Decimal, parse_constant=invalid_constant, object_pairs_hook=pairs)
            records.extend(record for record in json_products(value) if record.get("sku") == pid)
    product = only(records)
    offer = product.get("offers")
    if isinstance(offer, list):
        offer = only(offer)
    require(isinstance(offer, dict) and types(offer) == {"Offer"}
            and not set(offer) - {"@type", "price", "priceCurrency", "url", "itemCondition", "availability"}
            and offer.get("itemCondition") in ("NewCondition", "https://schema.org/NewCondition"))
    offer_url = offer.get("url")
    offer_host, _, _ = identity(offer_url)
    offer_parsed = urlsplit(offer_url)
    require(offer_host in {"www.intimissimi.com", "production-eu02-calzedonia.demandware.net"}
            and not offer_parsed.fragment
            and unquote(offer_parsed.path, errors="strict").replace("+", " ")
            == unquote(urlsplit(url).path, errors="strict").replace("+", " "))
    if "availability" in offer:
        expected = {"in_stock": "InStock", "limited": "LimitedAvailability", "out_of_stock": "OutOfStock"}[availability]
        require(offer["availability"] in (expected, "https://schema.org/" + expected))
    title = text(only([node for node in nodes if node.tag == "h1" and node.attrs.get("itemprop") == "name"]))
    require(isinstance(product.get("name"), str) and title == " ".join(unescape(product["name"]).split()))

    primary = only([node for node in nodes if "product-main-price" in classes(node)])
    public = only([node for node in walk(primary) if "cc-intProductDetail-priceWrapper" in classes(node)])
    require(visible(public))
    prices = [node for node in walk(public) if node.attrs.get("itemprop") == "price"]
    visible_price = only(prices)
    require(visible(visible_price))
    currency = only([node for node in walk(public) if node.attrs.get("itemprop") == "priceCurrency"])
    # Absence is meaningful only after exact variant, SKU and zero stock are
    # verified. A present/conflicting price never falls back to availability.
    missing = (None, "", "null")
    if (not available and offer.get("price") in missing
            and visible_price.attrs.get("content") in missing and not text(visible_price)):
        require(offer.get("priceCurrency") in (*missing, "CZK")
                and currency.attrs.get("content") in (*missing, "CZK"))
        return {"name": title, "sku": pid, "price": None, "currency": None,
                "availability": "out_of_stock", "conditions_verification": "not_verified"}
    require(offer.get("priceCurrency") == "CZK")
    price = amount(str(offer.get("price", "")))
    require(currency.attrs.get("content") == "CZK" and amount(visible_price.attrs.get("content", "")) == price)
    display = re.fullmatch(r"([0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3})+),([0-9]{2}) Kč", text(visible_price))
    require(display is not None and amount(display[1].replace(" ", "") + "." + display[2]) == price)
    return {"name": title, "sku": pid, "price": price, "currency": "CZK", "availability": availability,
            "conditions_verification": "not_verified"}
