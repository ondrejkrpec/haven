"""Synthetic CZ product/colour/size, price and stock evidence; no retailer data."""

from copy import deepcopy
import json
import unittest

from scripts import wishlist_fetch
from scripts.wishlist_extract import ExtractionError
from scripts.wishlist_intimissimi import public_offer
from tests import test_wishlist_fetch as fetch_tests

URL = "https://www.intimissimi.com/cz/product/synthetic_top-TOP100+++123AM.html"
PID = "TOP100   123AM"
ADAPTER = "intimissimi-public-price-v1"


def record():
    return {"@type": "Product", "sku": PID, "name": "Synthetic &amp; Shirt", "offers": {
        "@type": "Offer", "price": "249.90", "priceCurrency": "CZK", "itemCondition": "https://schema.org/NewCondition",
        "url": URL.replace("www.intimissimi.com", "production-eu02-calzedonia.demandware.net") + "?broken=undefined"}}


def page(product=None, *, stock="2.0", low=True):
    return f'''<link rel="canonical" href="https://www.intimissimi.com/cz/product/synthetic_top-TOP100.html">
    <script type="application/ld+json">{json.dumps(product or record())}</script>
    <div class="product-detail js-intProductDetail" data-pid="{PID}" data-variant-pid="{PID}">
      <h1 itemprop="name">Synthetic &amp; Shirt</h1>
      <span class="js-product-color-id">123a - synthetic colour</span>
      <div class="cc-intProductDetail-size-wrapper sizebay-js-desktop-size-buttons">
        <label class="js-intProductDetail-size-item button {'cc-intProductDetail-size-item_low' if low else ''}" for="M">
          <input type="radio" data-size-id="M" data-allocation="{stock}" data-iscomingsoon="false" {'disabled' if stock == '0' else ''}>
        </label>
        <label class="js-intProductDetail-size-item" for="L">
          <input type="radio" data-size-id="L" data-allocation="999" data-iscomingsoon="false">
        </label>
      </div>
      <input type="radio" data-size-id="M" checked>
      <div class="product-main-price">
        <div class="cc-intProductDetail-priceWrapper" itemprop="offers">
          <meta itemprop="priceCurrency" content="CZK">
          <span class="cc-intProductDetail-price-value" itemprop="price" content="249.90">249,90 Kč</span>
        </div>
        <span class="old-price">999,00 Kč</span>
        <span class="member-price hidden">1,00 Kč</span>
      </div>
    </div>'''


def unavailable_page():
    product = record()
    product['offers'].update(price='null', priceCurrency='null')
    return (page(product, stock='0', low=False).replace(' disabled', '')
            .replace('js-intProductDetail-size-item button ',
                     'js-intProductDetail-size-item button size-button--unavailable ')
            .replace('content="249.90"', 'content="null"').replace('249,90 Kč', '')
            .replace('content="CZK"', 'content="null"'))


class IntimissimiTests(unittest.TestCase):
    responses = fetch_tests.FetchTests.responses

    def test_primary_gallery_falls_back_to_a_photo_of_the_verified_colour(self):
        prefix = ("https://www.intimissimi.com/dw/image/v2/BHHR_PRD/on/demandware.static/"
                  "-/Sites-INT_EC_COM/default/dw1234abcd/images/")
        photo = prefix + "TOP100123A-M.jpg?sw=600&sfrm=jpeg"
        wrong_colour = prefix + "TOP100123B-M.jpg?sw=600&sfrm=jpeg"
        product = record() | {"image": "not_available"}
        html = page(product).replace('<div class="product-main-price">',
                                     f'''<div class="cc-intProductDetail-images-wrapper">
          <img class="productImageCarouselItem" itemprop="image" src="{wrong_colour}">
          <img class="productImageCarouselItem" itemprop="image" srcset="{photo} 600w">
        </div><div class="product-main-price">''')
        self.assertNotIn("image_url", public_offer(html, URL))
        self.assertEqual(public_offer(html, URL, include_image=True)["image_url"], photo)
        self.assertNotIn("image_url", public_offer(html.replace(photo, wrong_colour), URL, include_image=True))

    def test_exact_unavailable_variant_can_report_stock_without_inventing_a_price(self):
        result = public_offer(unavailable_page(), URL)
        self.assertEqual(result['availability'], 'out_of_stock')
        self.assertIsNone(result['price'])
        self.assertIsNone(result['currency'])
        self.assertEqual(result['sku'], PID)
        with self.responses((200, {'Content-Type': 'text/html'}, unavailable_page().encode())):
            result = wishlist_fetch.fetch(URL, ADAPTER)
            self.assertEqual((result['status'], result['reason']), ('ok', 'source_out_of_stock'))
            self.assertIsNone(result['offer']['price'])

    def test_unavailable_without_price_still_requires_exact_identity_zero_stock_and_absence(self):
        html = unavailable_page()
        for invalid in (html.replace('data-allocation="0"', 'data-allocation="2"'),
                        html.replace('size-button--unavailable', 'size-button--unavailable size-button--low-in-stock'),
                        html.replace('"sku": "' + PID, '"sku": "OTHER'),
                        html.replace('data-iscomingsoon="false"', 'data-iscomingsoon="true"'),
                        html.replace('"priceCurrency": "null"', '"priceCurrency": "EUR"'),
                        html.replace('itemprop="price" content="null">', 'itemprop="price" content="null">249,90 Kč'),
                        html.replace('itemprop="price" content="null"', 'itemprop="price" content="1.00"'),
                        html.replace('data-size-id="M" checked', 'data-size-id="L" checked'),
                        html.replace('size-button--unavailable', 'unknown-stock-class')):
            with self.subTest(html=invalid[:100]), self.assertRaises(ExtractionError):
                public_offer(invalid, URL)

    def test_unavailable_with_a_verified_price_remains_a_normal_price_observation(self):
        html = (page(stock='0', low=False).replace(' disabled', '')
                .replace('js-intProductDetail-size-item button ',
                         'js-intProductDetail-size-item button size-button--unavailable '))
        self.assertEqual(public_offer(html, URL)['price'], '249.90')

    def test_full_pid_binds_selected_colour_size_visible_price_and_limited_stock(self):
        result = public_offer(page().encode(), URL)
        self.assertEqual(result, {"name": "Synthetic & Shirt", "sku": PID, "price": "249.90", "currency": "CZK",
                                  "availability": "limited", "conditions_verification": "not_verified"})
        self.assertEqual(public_offer(page(stock="20", low=False), URL)["availability"], "in_stock")
        self.assertEqual(public_offer(page(stock="0"), URL)["availability"], "out_of_stock")

    def test_url_space_encoding_and_tracking_do_not_change_the_requested_variant(self):
        for source in (URL, URL.replace("+++", "%20%20%20"), URL + "?utm_source=synthetic"):
            with self.subTest(source=source):
                self.assertEqual(public_offer(page(), source)["sku"], PID)

    def test_ambiguous_or_changed_model_colour_size_cannot_supply_a_price(self):
        for source in (URL.replace("123AM", "123AL"), URL.replace("TOP100", "TOP200"),
                       URL.replace("+++123AM", ""), URL + "?size=L", URL + "#other",
                       URL.replace("/cz/", "/de/"), URL.replace("www.intimissimi.com", "other.example")):
            with self.subTest(source=source), self.assertRaises(ExtractionError):
                public_offer(page(), source)
        for change in (page().replace('data-variant-pid="' + PID, 'data-variant-pid="WRONG'),
                       page().replace('123a - synthetic colour', '999a - other colour'),
                       page().replace('data-size-id="M" checked', 'data-size-id="L" checked'),
                       page().replace(' checked', ''),
                       page().replace('data-size-id="M" checked>',
                                      'data-size-id="M" checked><input type="radio" data-size-id="L" checked>')):
            with self.subTest(change=change[:80]), self.assertRaises(ExtractionError):
                public_offer(change, URL)

    def test_missing_or_conflicting_stock_does_not_borrow_another_sizes_availability(self):
        for stock in ('', '-1', 'NaN', '1e2', 'unknown'):
            with self.subTest(stock=stock), self.assertRaises(ExtractionError):
                public_offer(page(stock=stock), URL)
        for html in (page().replace('data-iscomingsoon="false"', 'data-iscomingsoon="true"'),
                     page().replace('data-allocation="2.0"', 'disabled data-allocation="2.0"'),
                     page(stock='0').replace(' disabled', ''),
                     page().replace('js-intProductDetail-size-item button', 'js-intProductDetail-size-item button unselectable')):
            with self.assertRaises(ExtractionError):
                public_offer(html, URL)
        # The sibling L remains available while the exact linked M is out of stock.
        self.assertEqual(public_offer(page(stock='0'), URL)['availability'], 'out_of_stock')

    def test_schema_visible_amount_and_currency_must_agree(self):
        for change in ({'price': '0'}, {'price': 'NaN'}, {'price': '100.00'}, {'priceCurrency': 'EUR'},
                       {'validForMemberTier': 'club'}, {'eligibleQuantity': 2}, {'@type': 'AggregateOffer'},
                       {'itemCondition': 'https://schema.org/UsedCondition'}, {'availability': 'https://schema.org/OutOfStock'},
                       {'url': URL.replace('123AM', '123AL')}, {'url': 'https://other.example/product'}):
            product = record()
            product['offers'].update(change)
            with self.subTest(change=change), self.assertRaises(ExtractionError):
                public_offer(page(product), URL)
        for html in (page().replace('249,90 Kč', '199,90 Kč'), page().replace('content="249.90"', 'content="199.90"'),
                     page().replace('content="CZK"', 'content="EUR"')):
            with self.assertRaises(ExtractionError):
                public_offer(html, URL)

    def test_recommendations_old_prices_and_hidden_member_prices_do_not_override_primary_price(self):
        html = page().replace('<div class="product-main-price">', '''
          <div class="recommendation"><span itemprop="price" content="1.00">1,00 Kč</span></div>
          <div class="product-main-price">''')
        self.assertEqual(public_offer(html, URL)['price'], '249.90')
        for replacement in ('product-main-price hidden', 'product-main-price" aria-hidden="true',
                            'product-main-price" style="display: none', 'product-main-price member-price'):
            with self.subTest(replacement=replacement), self.assertRaises(ExtractionError):
                public_offer(page().replace('product-main-price', replacement), URL)

    def test_wrong_or_duplicate_structured_product_and_duplicate_json_keys_are_rejected(self):
        product = record()
        product['sku'] = 'OTHER'
        with self.assertRaises(ExtractionError):
            public_offer(page(product), URL)
        conflict = deepcopy(record())
        conflict['offers']['price'] = '1.00'
        with self.assertRaises(ExtractionError):
            public_offer(page([record(), conflict]), URL)
        with self.assertRaises(ExtractionError):
            public_offer(page().replace('"price": "249.90"', '"price": "1.00", "price": "249.90"'), URL)

    def test_canonical_model_and_primary_product_must_be_unique(self):
        for html in (page().replace('synthetic_top-TOP100.html', 'synthetic_top-TOP200.html'),
                     page() + '<link rel="canonical" href="https://www.intimissimi.com/cz/product/other-TOP100.html">',
                     page() + '<div class="product-detail js-intProductDetail"></div>'):
            with self.subTest(html=html[-80:]), self.assertRaises(ExtractionError):
                public_offer(html, URL)

    def test_malformed_stock_and_incomplete_metadata_fail_in_the_bounded_worker(self):
        for html in (page().replace('data-allocation="2.0"', 'data-allocation'),
                     page().replace('class="product-detail js-intProductDetail"', 'class'),
                     page().replace('type="application/ld+json"', 'type'),
                     page().replace('"priceCurrency": "CZK"', '"priceCurrency": null')):
            with self.subTest(html=html[:70]), self.responses((200, {'Content-Type': 'text/html'}, html.encode())):
                result = wishlist_fetch.fetch(URL, ADAPTER)
                self.assertEqual(result['status'], 'failed')
                self.assertNotIn('offer', result)

    def test_worker_fetches_only_the_original_public_host_and_preserves_network_guards(self):
        with self.responses((200, {'Content-Type': 'text/html'}, page().encode())) as (clients, connect):
            result = wishlist_fetch.fetch(URL, ADAPTER)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['offer']['sku'], PID)
            connect.assert_called_once_with('www.intimissimi.com')
            clients[0].request.assert_called_once()
        for status in (403, 429):
            with self.responses((status, {'Retry-After': '60'}, b'challenge')) as (clients, _):
                result = wishlist_fetch.fetch(URL, ADAPTER)
                self.assertEqual(result['status'], 'blocked')
                self.assertNotIn('offer', result)
                clients[0].getresponse.return_value.read.assert_not_called()
        with self.responses((302, {'Location': 'https://127.0.0.1/'}, b'')) as (_, connect):
            self.assertEqual(wishlist_fetch.fetch(URL, ADAPTER)['status'], 'failed')
            self.assertEqual(connect.call_count, 1)


if __name__ == '__main__':
    unittest.main()
