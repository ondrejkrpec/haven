"""Alza public gross price: exact structured identity plus the primary price box."""

from decimal import Decimal, ROUND_HALF_UP
import re

from scripts.wishlist_extract import Document, ExtractionError, extract


def descendants(node):
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(child for child in reversed(current.children) if hasattr(child, "attrs"))


def public_offer(html, url):
    result = extract(html, url, required_condition="NewCondition")
    if result["status"] != "extracted" or "jsonld" not in result.get("evidence", []):
        raise ExtractionError("offer_unverified")
    if result["offer"]["currency"] != "CZK":
        raise ExtractionError("offer_unverified")
    document = Document()
    document.feed(html.decode("utf-8-sig") if isinstance(html, bytes) else html)
    boxes = [node for node in document.nodes if node.attrs.get("data-testid") == "price-primary"]
    if len(boxes) != 1:
        raise ExtractionError("offer_unverified")
    wrappers = [node for node in descendants(boxes[0]) if node.attrs.get("data-slot") == "pb-wrapper"]
    if len(wrappers) != 1 or "ads-pb--basic" not in wrappers[0].attrs.get("class", "").split():
        raise ExtractionError("offer_unverified")
    text = " ".join(wrappers[0].text().split()).casefold()
    if any(marker in text for marker in ("alzaplus", "s kódem", "s kodem", "člen", "přihlášen", "na splátky")):
        raise ExtractionError("offer_unverified")
    prices = [node for node in descendants(wrappers[0]) if node.attrs.get("data-slot") == "pb-price"]
    if len(prices) != 1:
        raise ExtractionError("offer_unverified")
    value = " ".join(prices[0].text().split())
    match = re.fullmatch(r"([0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3})+),(-|[0-9]{2})(?: Kč)?", value)
    if match is None:
        raise ExtractionError("offer_unverified")
    visible = Decimal(match[1].replace(" ", "") + "." + ("00" if match[2] == "-" else match[2]))
    structured = Decimal(result["offer"]["price"])
    expected = structured.quantize(Decimal("1"), rounding=ROUND_HALF_UP) if match[2] == "-" else structured
    if visible <= 0 or visible != expected:
        raise ExtractionError("offer_unverified")
    return {**result["offer"], "price": format(visible, ".2f")}
