"""Synthetic offline checks; real products and captured pages never enter tests."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from scripts.wishlist_extract import MAX_BYTES, extract

URL = "https://shop.example/products/example?variant=two"


def product(*, url=URL, price="249.90", name="Synthetic product", **offer_extra):
    return {"@type": "Product", "name": name, "sku": "SYNTHETIC-2", "offers": {
        "@type": "Offer", "url": url, "price": price,
        "priceCurrency": "CZK", "availability": "https://schema.org/InStock", **offer_extra,
    }}


def page(records):
    return '<script type="application/ld+json">' + json.dumps(records) + '</script>'


class WishlistExtractTests(unittest.TestCase):
    def test_exact_variant_and_decimal_price_without_network_or_script_execution(self):
        html = page([product(url=URL.replace("two", "one"), price="1.00"), product()])
        html += '<script>throw new Error("this must not execute");</script>'
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = extract(html, URL)
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["offer"]["price"], "249.90")
        self.assertEqual(result["offer"]["conditions_verification"], "not_verified")
        self.assertEqual(result["model_calls"], 0)

    def test_scoped_microdata_excludes_breadcrumb_and_seller_names(self):
        html = '''<meta itemprop="name" content="Wrong global name">
        <div itemscope itemtype="https://schema.org/Product">
          <meta itemprop="name" content="Synthetic product">
          <meta itemprop="sku" content="SYNTHETIC-2">
          <div itemprop="brand" itemscope itemtype="https://schema.org/Brand">
            <meta itemprop="name" content="Wrong nested name">
          </div>
          <span itemprop="offers" itemscope itemtype="https://schema.org/Offer">
            <meta itemprop="url" content="https://shop.example/products/example?variant=two">
            <meta itemprop="price" content="249.90">
            <meta itemprop="priceCurrency" content="CZK">
            <link itemprop="availability" href="https://schema.org/InStock">
          </span>
        </div>'''
        result = extract(html, URL)
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["offer"]["name"], "Synthetic product")
        self.assertEqual(result["evidence"], ["microdata"])

    def test_recommended_product_is_not_used_as_the_primary_offer(self):
        result = extract(page([product(url="https://shop.example/other", price="1"), product()]), URL)
        self.assertEqual(result["offer"]["price"], "249.90")

    def test_graph_and_duplicate_identical_offers_are_supported(self):
        result = extract(page({"@graph": [product(), deepcopy(product())]}), URL)
        self.assertEqual(result["status"], "extracted")

    def test_missing_variant_identity_or_wrong_canonical_requires_review(self):
        cases = [page(product(url="https://shop.example/products/example")),
                 '<link rel="canonical" href="https://shop.example/other">' + page(product())]
        for html in cases:
            with self.subTest(html=html):
                self.assertEqual(extract(html, URL)["status"], "needs_review")

    def test_tracking_does_not_remove_variant_identity(self):
        self.assertEqual(extract(page(product()), URL + "&utm_source=synthetic")["status"], "extracted")
        self.assertEqual(extract(page(product()), URL + "&unknown=1")["status"], "needs_review")

    def test_conflicting_exact_offers_are_never_resolved_by_picking_cheapest(self):
        result = extract(page([product(), product(price="200")]), URL)
        self.assertEqual(result["reason"], "conflicting_offers")
        self.assertNotIn("offer", result)

    def test_member_and_quantity_conditions_need_a_shop_adapter(self):
        for condition in ({"validForMemberTier": "https://shop.example/member"},
                          {"eligibleQuantity": {"value": 3}},
                          {"priceSpecification": {"validForMemberTier": "club"}},
                          {"priceSpecification": {"eligibleQuantity": {"value": 2}}}):
            with self.subTest(condition=condition):
                self.assertEqual(extract(page(product(**condition)), URL)["reason"], "conditional_offer")

    def test_formatted_zero_negative_nonfinite_and_overprecise_prices_are_rejected(self):
        for price in ("1 000,00", "0", "-1", "NaN", "Infinity", "1.001", True, None):
            with self.subTest(price=price):
                result = extract(page(product(price=price)), URL)
                self.assertEqual(result["status"], "needs_review")
                self.assertNotIn("offer", result)

    def test_aggregate_price_is_not_an_exact_variant_price(self):
        record = product()
        record["offers"]["@type"] = "AggregateOffer"
        self.assertEqual(extract(page(record), URL)["status"], "needs_review")

    def test_missing_currency_or_availability_is_not_fabricated(self):
        for key in ("priceCurrency", "availability"):
            record = product()
            del record["offers"][key]
            result = extract(page(record), URL)
            self.assertEqual(result["status"], "needs_review")
            self.assertNotIn("offer", result)

    def test_blocked_and_failed_http_responses_are_not_parsed(self):
        for status in (401, 403, 429, 404, 500):
            result = extract(page(product()), URL, http_status=status)
            self.assertEqual(result["status"], "blocked" if status in (401, 403, 429) else "unavailable")
            self.assertNotIn("offer", result)

    def test_invalid_document_limits_and_unsafe_urls_have_fixed_diagnostics(self):
        cases = [(b"\xff", URL), (b"x" * (MAX_BYTES + 1), URL),
                 ('<script type="application/ld+json">PRIVATE_INPUT</script>', URL),
                 (page(product()), "file:///etc/passwd"),
                 (page(product()), "https://user:password@shop.example/products/example"),
                 ("<div>" * 130, URL),
                 ('<div itemscope itemtype="https://schema.org/Product"><meta itemprop="@type" content="x"></div>', URL)]
        for html, url in cases:
            with self.subTest(url=url):
                result = extract(html, url)
                self.assertEqual(result["status"], "needs_review")
                self.assertNotIn("PRIVATE_INPUT", json.dumps(result))
                self.assertNotIn("password", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
