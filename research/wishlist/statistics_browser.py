"""Opt-in local HTTPS acceptance with synthetic state and an isolated browser profile.

Run as a module after installing research/wishlist/browser-requirements.txt in a
separate environment. Never connects to the pilot or a product shop.
"""

import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
from threading import Event, Thread
from uuid import uuid4
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import expect, sync_playwright

from scripts.wishlist_collection import Collection
from scripts.wishlist_domain import Wishlist
from scripts.wishlist_product_preview import PreviewServer
from scripts.wishlist_web_domain import WebApplication
from scripts.wishlist_web_gateway import Application, ROOT

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOo7CsEAAL8AXl6GkipAAAAAElFTkSuQmCC"  # One synthetic pixel.
VIEWPORTS = [(3840, 2160), (1920, 1080), (1440, 900), (1280, 800), (1024, 768), (768, 900), (390, 844), (320, 760)]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_):
        pass


async def fixture_offer(payload, **_):
    if payload["url"].endswith("?dwvar_TOP100_Z_COL_INTU=123A"):
        return {"status": "failed", "http_status": 200, "retry_after": 0, "reason": "offer_unverified",
                "variants": [{"size": "M", "url": "https://www.intimissimi.com/cz/product/synthetic-TOP100+++123AM.html"}]}
    if payload["url"].endswith("/unavailable"):
        return {"status": "failed", "http_status": None, "retry_after": 0}
    name = "Rozpoznaný testovací produkt"
    if payload["url"].endswith("/synthetic-preview"):
        name = "Testovací pánev, ø 26 cm - vícevrstvá nerezová pánev s nepřilnavým keramickým povrchem"
    return {"status": "ok", "http_status": 200, "retry_after": 0,
            **({"image_url": "https://www.intimissimi.com/synthetic.png"} if payload["url"].endswith("/synthetic-preview") else {}),
            "offer": {"name": name, "price": None, "availability": "out_of_stock"}}


async def fixture_category(_):
    return "home"


async def fixture_image(payload, **_):
    return {"mime": "image/png", "body": PNG} if payload["url"].endswith("/synthetic.png") else {}



def fixture(path):
    domain = Wishlist(path, bot_id="200001", owner_id="100001", web_enabled=True)
    application = WebApplication(domain)
    today = domain.clock().astimezone(domain.zone).date()

    def call(operation, arguments):
        result = application.handle({"protocol_version": 1, "operation": "web." + operation,
                                     "request_id": uuid4().hex, "arguments": arguments})
        assert result["code"] == "ok", result
        return result

    now = domain.clock()
    check_at = now - timedelta(days=4)
    domain.clock = lambda: check_at
    call("add", {"url": "https://www.momenti.cz/synthetic-fixture/", "details": {
        "label": "Testovací konvice", "category": "home", "purchase_mode": "once"}})
    collection = Collection(domain)

    def observe(price, availability="in_stock", image_url=None):
        claim = collection.handle({"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
        assert claim["code"] == "ok", claim
        offer = {"name": "Syntetický produkt", "sku": "EXAMPLE-1", "price": price, "currency": "CZK" if price else None,
                 "availability": availability, "conditions_verification": "not_verified"}
        result = {"status": "ok", "reason": "structured_offer_candidate" if price else "source_out_of_stock",
                  "http_status": 200, "retry_after": 0, "offer": offer}
        if image_url:
            result["image_url"] = image_url
        completed = collection.handle({"protocol_version": 1, "operation": "collection.complete",
                                       "job": claim["job"], "result": result})
        assert completed["code"] == "ok", completed

    for price, availability in (("1000.00", "in_stock"), ("750.00", "in_stock"), (None, "out_of_stock"), ("750.00", "in_stock")):
        observe(price, availability)
        check_at += timedelta(days=1)
    check_at = now - timedelta(hours=1)
    call("add", {"url": "https://www.fabini.cz/synthetic-fixture/", "details": {
        "label": "Testovací pánev", "category": "home", "purchase_mode": "once"}})
    observe("1250.00", image_url="https://www.fabini.cz/synthetic.png")
    check_at = now
    entry = call("add", {"url": "https://shop.example.com/item", "details": {
        "label": "Testovací lampa", "category": "home", "purchase_mode": "recurring"}})["data"]["entry"]
    for price, quantity, date in (("800", 2, today.isoformat()), ("100", 1, today.isoformat()),
                                  ("100", 1, today.isoformat()), ("100", 1, (today.replace(day=1)-timedelta(days=1)).isoformat())):
        result = call("purchase", {"entry_id": entry["entry_id"], "expected_version": entry["version"],
                                  "unit_price": price, "quantity": quantity, "purchase_date": date})
        entry = result["data"]["entry"]
    db = domain.store.db
    db.execute("UPDATE purchases SET reference_price=100000 WHERE sequence=1")
    db.execute("UPDATE purchases SET unit_price=NULL WHERE sequence=3")
    call("update", {"entry_id": entry["entry_id"], "expected_version": entry["version"], "patch": {"category": "gifts"}})
    return domain, application


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--axe", type=Path, help="Optionally run a local axe-core browser bundle")
    parser.add_argument("--fonts", type=Path, help="Optionally verify separately obtained owner webfonts")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="haven-statistics-browser-") as temporary:
        root = Path(temporary)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem"), "-subj", "/CN=localhost"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready = Event()
        errors = []
        shared = {}

        def serve():
            domain = None
            try:
                domain, application = fixture(root / "synthetic.sqlite3")
                server = make_server("127.0.0.1", 0, lambda *_: [], handler_class=QuietHandler)
                origin = f"https://127.0.0.1:{server.server_port}"
                assets = ROOT
                if args.fonts:
                    assets = root / "wishlist"
                    shutil.copytree(ROOT, assets)
                    for name in ("gambarino.woff2", "general-sans.woff2"):
                        shutil.copyfile(args.fonts / name, assets / name)
                preview = PreviewServer(root / "preview.sock", ingress_uid=1000, fetcher=fixture_offer,
                                        category_endpoint={}, image_fetcher=fixture_image)
                preview.category_for = fixture_category
                with patch("scripts.wishlist_web_gateway.ROOT", assets):
                    gateway = Application({"origin": origin, "owner_login": "owner@example.com", "preview": {}},
                                          transport=lambda config, request: application.handle(request),
                                          preview_transport=lambda config, request, **_: asyncio.run(preview.respond(request)))

                def mounted(environ, start):
                    environ["HTTP_TAILSCALE_USER_LOGIN"] = "owner@example.com"
                    environ["PATH_INFO"] = environ["PATH_INFO"].removeprefix("/wishlist")
                    return gateway(environ, start)

                server.set_app(mounted)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(root / "cert.pem", root / "key.pem")
                server.socket = context.wrap_socket(server.socket, server_side=True)
                shared.update(server=server, origin=origin)
                ready.set()
                server.serve_forever()
            except BaseException as error:
                errors.append(error)
                ready.set()
            finally:
                if domain:
                    domain.close()

        thread = Thread(target=serve, daemon=True)
        thread.start()
        assert ready.wait(15)
        if errors:
            raise errors[0]
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(args.chromium), headless=True,
                                                     chromium_sandbox=True)
                try:
                    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(shared["origin"] + "/") else route.abort())
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    operations = []
                    def record(request):
                        if request.method == "POST" and request.url.endswith("/api/command"):
                            operations.append(request.post_data_json.get("operation"))
                    page.on("request", record)
                    a11y = {}
                    def audit(name):
                        if not args.axe:
                            return
                        page.evaluate(args.axe.read_text())
                        report = page.evaluate("""async () => (await axe.run({
                          runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21aa','wcag22aa']}
                        })).violations.map(({id, nodes}) => ({id, targets:nodes.map(node => node.target)}))""")
                        a11y[name] = report

                    page.clock.install()
                    page.add_init_script("""(() => {
                        const request = window.fetch;
                        let held = 0;
                        window.fetch = async (url, options) => {
                            const response = await request(url, options);
                            const body = options?.body && JSON.parse(options.body);
                            if (body?.operation === 'web.list' && body.arguments.state === 'removed' && held++ < 2) {
                                return new Promise((resolve, reject) => {
                                    window.settleList = success => {
                                        delete window.settleList;
                                        if (success) resolve(response);
                                        else reject(new Error('Synthetic list failure'));
                                    };
                                });
                            }
                            return response;
                        };
                    })()""")
                    pending_images = []
                    page.route("**/api/image/i/**", lambda route: pending_images.append(route))
                    page.goto(shared["origin"] + "/wishlist/")
                    page.wait_for_function("() => Boolean(window.settleList)")
                    pending = page.locator("#list-pending")
                    expect(pending).to_be_visible()
                    expect(page.locator("#list-content")).to_have_attribute("aria-busy", "true")
                    expect(page.locator("#list-status")).to_have_text("Načítám wishlist…")
                    expect(page.locator("#rows .item-title")).to_have_count(0)
                    audit("list-loading")
                    skeleton_style = """node => {
                        const shape = getComputedStyle(node.querySelector('.skeleton-fill'));
                        const motion = getComputedStyle(node);
                        return [shape.backgroundColor, shape.borderRadius, motion.animationName, motion.animationDuration];
                    }"""
                    list_style = pending.locator(".skeleton").evaluate(skeleton_style)
                    page.screenshot(path=str(args.output / "wishlist-loading-desktop.png"), animations="disabled")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / "wishlist-loading-mobile.png"), animations="disabled")
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    page.evaluate("window.settleList(false)")
                    expect(pending).not_to_be_visible()
                    expect(page.locator("#list-content")).to_have_attribute("aria-busy", "false")
                    expect(page.locator("#empty-title")).to_have_text("Přehled se nepodařilo načíst")
                    retry = page.get_by_role("button", name="Zkusit znovu", exact=True)
                    retry.focus()
                    page.keyboard.press("Enter")
                    expect(page.locator("#list-title")).to_be_focused()
                    expect(pending).to_be_visible()
                    page.wait_for_function("() => Boolean(window.settleList)")
                    with page.expect_request(lambda request: "/api/image/i/" in request.url):
                        page.evaluate("window.settleList(true)")
                    expect(pending).to_be_visible()
                    expect(page.locator("#list-content")).to_have_attribute("aria-busy", "true")
                    expect(page.locator("#rows .item-title").first).not_to_be_visible()
                    expect(page.locator("#rows img")).to_have_attribute("loading", "eager")
                    pending_images.pop().continue_()
                    page.unroute("**/api/image/i/**")
                    expect(pending).not_to_be_visible()
                    expect(page.locator("#list-content")).to_have_attribute("aria-busy", "false")
                    expect(page.locator("#visible-count")).to_have_text("3")
                    expect(page.locator("#purchase-statistics")).to_have_count(0)
                    expect(page.locator(".summary-card")).to_have_count(0)
                    expect(page.get_by_text("Znovu skladem")).to_be_visible()
                    expect(page.get_by_text("Zlevnilo o")).to_be_visible()
                    expect(page.locator("#rows img")).to_have_count(1)
                    expect(page.locator("#rows .thumbnail.is-loading")).to_have_count(0)
                    assert page.locator("#rows img").evaluate("(img) => img.decode().then(() => img.naturalWidth)") == 1
                    for scenario, viewport in (
                        ("broken", {"width": 390, "height": 844}),
                        ("timeout", {"width": 390, "height": 844}),
                        ("below-fold", {"width": 390, "height": 300}),
                    ):
                        check = context.new_page()
                        check.set_viewport_size(viewport)
                        check.clock.install()
                        held_photos = []
                        if scenario == "below-fold":
                            check.add_init_script("HTMLImageElement.prototype.decode = () => new Promise(() => {});")
                        elif scenario == "broken":
                            check.route("**/api/image/i/**", lambda route: route.abort())
                        else:
                            check.route("**/api/image/i/**", lambda route: held_photos.append(route))
                        try:
                            if scenario == "below-fold":
                                check.goto(shared["origin"] + "/wishlist/", wait_until="domcontentloaded")
                            else:
                                with check.expect_request(lambda request: "/api/image/i/" in request.url):
                                    check.goto(shared["origin"] + "/wishlist/", wait_until="domcontentloaded")
                            photo = check.locator("#rows img")
                            skeleton = check.locator("#list-pending")
                            if scenario == "below-fold":
                                expect(photo).to_have_attribute("loading", "lazy")
                            if scenario == "timeout":
                                expect(skeleton).to_be_visible()
                                check.emulate_media(reduced_motion="reduce")
                                expect(skeleton.locator(".skeleton")).to_have_css("animation-name", "none")
                                check.clock.run_for(5001)
                            expect(skeleton).not_to_be_visible(timeout=1000)
                            expect(check.locator("#list-content")).to_have_attribute("aria-busy", "false")
                            if scenario == "broken":
                                expect(photo).to_have_count(0)
                                expect(check.locator("#rows .thumbnail:not(.is-loading)")).to_have_count(3)
                            if scenario == "timeout":
                                expect(check.locator("#rows .thumbnail.is-loading")).to_have_count(1)
                                held_photos.pop().continue_()
                                expect(check.locator("#rows .thumbnail.is-loading")).to_have_count(0)
                        finally:
                            check.unroute_all(behavior="ignoreErrors")
                            check.close()
                    sort = page.locator("#sort")
                    page.locator("#filter-category").focus()
                    page.keyboard.press("Tab")
                    expect(sort).to_be_focused()
                    assert sort.evaluate("(el) => getComputedStyle(el).outlineStyle") == "solid"
                    custom_select = page.evaluate(
                        "CSS.supports('appearance', 'base-select') && CSS.supports('selector(::picker(select))')"
                    )
                    if custom_select:
                        assert sort.evaluate("(el) => getComputedStyle(el).appearance") == "base-select"
                        page.keyboard.press("Space")
                        expect(sort).to_have_css("appearance", "base-select")
                        assert sort.evaluate("(el) => el.matches(':open')")
                        page.keyboard.press("Escape")
                        expect(sort).to_be_focused()
                        assert not sort.evaluate("(el) => el.matches(':open')")
                        page.keyboard.press("Space")
                        page.keyboard.press("End")
                        page.keyboard.press("Enter")
                        expect(sort).to_have_value("newest")
                        expect(page.locator(".item-title").first).to_have_text("Testovací pánev")
                        sort.select_option("recent")
                    page.emulate_media(reduced_motion="reduce")
                    assert page.locator("#add-button").evaluate("(el) => getComputedStyle(el).transitionDuration") == "0s, 0s, 0s"
                    page.emulate_media(reduced_motion="no-preference")
                    page.get_by_role("button", name="Zlevnění · 1").click()
                    expect(page.locator("#visible-count")).to_have_text("1")
                    page.locator("#reset-filters").click()
                    page.locator("#more-filters summary").click()
                    if page.evaluate("CSS.supports('selector(details::details-content)')"):
                        assert page.locator("#more-filters").evaluate(
                            "(el) => parseFloat(getComputedStyle(el, '::details-content').transitionDuration) > 0"
                        )
                    page.locator("#filter-stock").select_option("in_stock")
                    expect(page.locator("#visible-count")).to_have_text("2")
                    page.locator("#reset-filters").click()
                    page.locator("#more-filters summary").click()
                    page.locator("#sort").select_option("name")
                    expect(page.locator(".item-title").first).to_have_text("Testovací konvice")
                    page.locator("#sort").select_option("newest")
                    expect(page.locator(".item-title").first).to_have_text("Testovací pánev")
                    page.locator("#sort").select_option("recent")
                    page.locator("#filter-category").select_option(label="Domácnost")
                    expect(page.locator("#visible-count")).to_have_text("2")
                    page.locator("#filter-category").select_option(label="Dárky")
                    expect(page.locator("#visible-count")).to_have_text("1")
                    page.locator("#search").fill("nenalezeno")
                    expect(page.locator("#visible-count")).to_have_text("0")
                    page.locator("#reset-filters").click()
                    expect(page.locator("#visible-count")).to_have_text("3")
                    page.evaluate("document.activeElement.blur()")
                    if args.fonts:
                        assert page.evaluate("""async () => {
                            await document.fonts.ready;
                            const fonts = [...document.fonts].filter(font => font.status === 'loaded').map(font => font.family);
                            return fonts.includes('Gambarino') && fonts.some(name => name.includes('General Sans'));
                        }""")
                    for width, height in VIEWPORTS:
                        page.set_viewport_size({"width": width, "height": height})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        expect(page.locator("#purchase-statistics")).to_have_count(0)
                        if width in (320, 390):
                            price = page.locator(".product-price").first.bounding_box()
                            assert price and price["y"] + price["height"] < height, (width, height, price)
                        if width in (1440, 390):
                            audit(f"list-{width}")
                            page.screenshot(path=str(args.output / f"wishlist-{width}.png"), full_page=True)
                    kettle = page.get_by_role("button", name="Testovací konvice", exact=True)
                    kettle.focus()
                    with page.expect_response(lambda response: response.request.method == "POST"
                                              and response.request.post_data_json.get("operation") == "web.list"
                                              and response.request.post_data_json["arguments"]["state"] == "removed"):
                        page.clock.fast_forward("00:31")
                    expect(kettle).to_be_focused()
                    page.keyboard.press("Enter")
                    expect(page.locator("svg.chart")).to_be_visible()
                    expect(page.locator("#detail-close")).to_be_focused()
                    expect(page.locator("#open-shop")).to_have_class("button primary")
                    expect(page.locator(".detail-history")).to_contain_text("Dní bez ceny")
                    page.get_by_role("button", name="Měsíc", exact=True).click()
                    expect(page.get_by_role("button", name="Měsíc", exact=True)).to_be_focused()
                    assert float(page.locator(".chart-axis span").first.evaluate("(el) => getComputedStyle(el).fontSize").removesuffix("px")) >= 12
                    page.locator("#daily-summary").click()
                    expect(page.locator(".daily-table")).to_be_visible()
                    audit("detail-with-history")
                    page.locator("#history-info").click()
                    expect(page.locator("#info-dialog")).to_be_visible()
                    audit("history-help")
                    page.keyboard.press("Escape")
                    expect(page.locator("#history-info")).to_be_focused()
                    page.keyboard.press("Escape")
                    expect(kettle).to_be_focused()
                    page.get_by_role("button", name="Testovací pánev", exact=True).click()
                    expect(page.locator(".chart-empty")).to_contain_text("První cena zaznamenána")
                    expect(page.locator("svg.chart")).to_have_count(0)
                    expect(page.locator(".detail-stat-grid")).to_have_count(0)
                    expect(page.locator(".chart-tabs")).to_have_count(0)
                    expect(page.locator(".detail-history")).to_contain_text("Rozsah")
                    page.screenshot(path=str(args.output / "wishlist-detail-mobile.png"), animations="disabled")
                    page.get_by_role("button", name="Upravit", exact=True).click()
                    page.locator('#editor textarea[name="note"]').fill("Syntetická poznámka.")
                    audit("edit")
                    page.locator("#editor-submit").click()
                    expect(page.locator("#editor")).not_to_be_visible()
                    expect(page.locator(".detail-note")).to_have_text("Syntetická poznámka.")
                    expect(page.locator("#detail #toast")).to_be_visible()
                    remove = page.get_by_role("button", name="Odstranit položku", exact=True)
                    remove.focus()
                    assert remove.evaluate("""el => {
                        const b = el.getBoundingClientRect(), toast = document.querySelector('#toast').getBoundingClientRect();
                        return b.bottom <= toast.top || b.top >= toast.bottom || b.right <= toast.left || b.left >= toast.right;
                    }""")
                    page.keyboard.press("Escape")
                    expect(page.locator("#detail")).not_to_be_visible()
                    page.get_by_role("button", name="Testovací lampa", exact=True).click()
                    expect(page.locator(".purchase-card")).to_have_count(4)
                    page.get_by_role("button", name="Zavřít detail", exact=True).click()
                    page.get_by_role("button", name="Zapsat nákup: Testovací konvice").click()
                    price = page.locator("#field-price")
                    expect(price).to_have_value("")
                    page.locator("#use-last-price").click()
                    expect(price).to_have_value("750,00")
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Zahodit", exact=True).click()
                    page.get_by_role("button", name="Zapsat nákup: Testovací lampa").click()
                    price.fill("nesmysl")
                    page.locator("#field-quantity").fill("0")
                    page.locator("#editor-submit").click()
                    expect(price).to_have_attribute("aria-invalid", "true")
                    expect(price).to_be_focused()
                    expect(page.locator("#field-error-quantity")).to_be_visible()
                    audit("purchase-invalid")
                    page.screenshot(path=str(args.output / "wishlist-purchase-error-mobile.png"), animations="disabled")
                    price.fill("300")
                    page.locator("#field-quantity").fill("1")
                    page.locator("#editor-submit").click()
                    expect(page.locator("#editor")).not_to_be_visible()
                    page.get_by_role("button", name="Testovací lampa", exact=True).click()
                    expect(page.locator(".purchase-card")).to_have_count(5)
                    last_purchase = page.locator(".purchase-card").first
                    last_purchase.get_by_role("button", name="Opravit", exact=True).click()
                    page.locator('#editor [name="price"]').fill("350")
                    page.locator("#editor-submit").click()
                    expect(page.locator("#editor")).not_to_be_visible()
                    expect(last_purchase).to_contain_text("350")
                    last_purchase.get_by_role("button", name="Stornovat nákup").click()
                    expect(page.get_by_role("button", name="Ponechat nákup")).to_be_focused()
                    audit("cancel-purchase")
                    page.keyboard.press("Escape")
                    expect(last_purchase).not_to_contain_text("Stornováno")
                    last_purchase.get_by_role("button", name="Stornovat nákup").click()
                    page.locator("#confirm-dialog").get_by_role("button", name="Stornovat nákup").click()
                    expect(last_purchase).to_contain_text("Stornováno")
                    page.keyboard.press("Escape")
                    kettle.click()
                    page.get_by_role("button", name="Odstranit položku", exact=True).click()
                    expect(page.locator("#toast-undo")).to_be_visible()
                    expect(page.locator("#visible-count")).to_have_text("2")
                    page.locator("#toast-undo").click()
                    expect(page.locator("#visible-count")).to_have_text("3")
                    page.locator("#add-button").click()
                    url = page.locator('#editor [name="url"]')
                    expect(url).to_be_focused()
                    expect(page.locator('#editor [name="name"]')).not_to_be_visible()
                    disclosure = page.locator(".extra-fields summary")
                    expect(disclosure).not_to_be_visible()
                    expect(page.locator("#editor-submit")).to_be_disabled()
                    expect(page.locator("#editor-submit")).not_to_be_visible()
                    audit("add-initial")
                    page.screenshot(path=str(args.output / "wishlist-add-mobile.png"), animations="disabled")
                    url.fill("http://shop.example.com/invalid")
                    page.clock.run_for(500)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "invalid")
                    expect(url).to_have_attribute("aria-invalid", "true")
                    expect(url).to_be_focused()
                    url.fill("https://www.fabini.cz/products/unavailable")
                    expect(page.locator("#editor-submit")).to_be_disabled()
                    page.locator("#editor-form").evaluate("(form) => form.requestSubmit()")
                    page.clock.run_for(500)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "unavailable")
                    expect(page.locator("#preview-action")).to_have_text("Zkusit náhled znovu")
                    if custom_select:
                        category = page.get_by_role("combobox", name="Kategorie").last
                        category.focus()
                        page.keyboard.press("Space")
                        assert category.evaluate("(el) => el.matches(':open')")
                        page.keyboard.press("Escape")
                        assert not category.evaluate("(el) => el.matches(':open')")
                        expect(category).to_be_focused()
                        expect(page.locator("#editor")).to_be_visible()
                    url.fill("https://www.intimissimi.com/cz/product/synthetic-preview")
                    page.clock.run_for(500)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "ready")
                    expect(page.locator("#preview-product strong")).to_have_text("Testovací pánev, ø 26 cm")
                    expect(page.locator("#preview-product")).to_contain_text("Cena nezjištěna")
                    expect(page.locator("#category-announcement")).to_have_text("Návrh kategorie: Domácnost.")
                    assert page.locator("#preview-product img").evaluate("(img) => img.decode().then(() => img.naturalWidth)") == 1
                    expect(page.locator("#editor-submit")).to_have_text("Přidat položku")
                    assert "web.add" not in operations
                    expect(page.locator("#visible-count")).to_have_text("3")
                    audit("add-ready")
                    page.screenshot(path=str(args.output / "wishlist-product-preview-mobile.png"), animations="disabled")
                    page.locator(".extra-fields summary").click()
                    if page.evaluate("CSS.supports('selector(details::details-content)')"):
                        assert page.locator(".extra-fields").evaluate(
                            "(el) => parseFloat(getComputedStyle(el, '::details-content').transitionDuration) > 0"
                        )
                    name = page.locator('#editor [name="name"]')
                    expect(name).to_have_value("Testovací pánev, ø 26 cm")
                    category = page.locator('#editor [name="category"]')
                    expect(category).to_have_value("Domácnost")
                    category.select_option("Dárky")
                    name.fill("Vlastní název položky")
                    page.mouse.click(4, 4)
                    expect(page.locator("#editor")).to_be_visible()
                    expect(page.locator("#discard-dialog")).not_to_be_visible()
                    expect(name).to_have_value("Vlastní název položky")
                    page.get_by_role("button", name="Zrušit", exact=True).click()
                    expect(page.get_by_role("button", name="Pokračovat v úpravách")).to_be_focused()
                    audit("discard-confirmation")
                    page.get_by_role("button", name="Pokračovat v úpravách").click()
                    expect(name).to_have_value("Vlastní název položky")
                    # Hold an older response open even after its request has finished
                    # to verify the UI guard independently of native fetch abortion.
                    page.evaluate("""() => {
                        const fetchNormally = window.fetch;
                        window.fetch = async (url, options) => {
                            const body = options?.body && JSON.parse(options.body);
                            if (body?.operation !== 'web.preview' || !body.arguments.url.endsWith('/stale-preview')) {
                                return fetchNormally(url, options);
                            }
                            const response = await fetchNormally(url, options);
                            return new Promise(resolve => {
                                window.releaseOldPreview = () => resolve(response);
                                document.documentElement.dataset.oldPreviewHeld = 'true';
                            });
                        };
                    }""")
                    url.fill("https://www.momenti.cz/stale-preview")
                    page.clock.run_for(500)
                    expect(page.locator("html")).to_have_attribute("data-old-preview-held", "true")
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "loading")
                    expect(page.locator("#editor-submit")).to_be_disabled()
                    page.keyboard.press("Enter")
                    page.locator("#editor-form").evaluate("(form) => form.requestSubmit()")
                    expect(page.locator("#editor")).to_be_visible()
                    assert "web.add" not in operations
                    url.fill("https://shop.example.com/new-synthetic-item")
                    page.clock.run_for(500)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "unsupported")
                    page.evaluate("window.releaseOldPreview()")
                    page.clock.run_for(100)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "unsupported")
                    expect(name).to_have_value("Vlastní název položky")
                    expect(category).to_have_value("Dárky")
                    page.locator("#editor-submit").click()
                    expect(page.locator("#editor")).not_to_be_visible()
                    expect(page.locator("#detail")).to_be_visible()
                    expect(page.locator("#detail-content")).to_contain_text("Odkaz je uložený")
                    expect(page.locator("#detail-title")).to_have_text("Vlastní název položky")
                    expect(page.locator("#detail .detail-top .context-label")).to_contain_text("Dárky")
                    page.keyboard.press("Escape")
                    expect(page.locator("#visible-count")).to_have_text("4")
                    page.locator("#add-button").click()
                    url.fill("https://www.ifantazie.cz/synthetic-confirm")
                    page.clock.run_for(500)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "ready")
                    assert operations.count("web.add") == 1
                    page.locator("#editor-submit").click()
                    expect(page.locator("#detail-title")).to_have_text("Rozpoznaný testovací produkt")
                    page.keyboard.press("Escape")
                    expect(page.locator("#visible-count")).to_have_text("5")
                    page.locator("#add-button").click()
                    page.wait_for_timeout(2100)
                    url.fill("https://www.intimissimi.com/cz/product/synthetic-TOP100.html?dwvar_TOP100_Z_COL_INTU=123A")
                    page.clock.run_for(500)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "choose_variant")
                    expect(page.locator("#preview-size")).to_be_visible()
                    expect(page.locator("#editor-submit")).to_be_disabled()
                    page.wait_for_timeout(2100)
                    page.locator("#preview-size").select_option(label="M")
                    expect(page.locator("#preview-skeleton")).to_be_visible()
                    page.clock.run_for(2100)
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "ready")
                    expect(url).to_have_value("https://www.intimissimi.com/cz/product/synthetic-TOP100+++123AM.html")
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Zahodit", exact=True).click()
                    page.locator("#add-button").click()
                    page.evaluate("""() => {
                        const request = window.fetch;
                        window.fetch = async (url, options) => {
                            const response = await request(url, options);
                            const action = options?.body && JSON.parse(options.body);
                            if (action?.operation === 'web.preview' && action.arguments.url.endsWith('/skip-focus')) {
                                return new Promise(resolve => { window.finishPreview = () => resolve(response); });
                            }
                            if (action?.operation === 'web.preview_category') {
                                return new Promise(resolve => { window.finishCategory = () => resolve(response); });
                            }
                            return response;
                        };
                    }""")
                    # Previous checks may have previewed this shop within the server's two-second throttle.
                    page.wait_for_timeout(2100)
                    url.fill("https://www.momenti.cz/skip-focus")
                    page.clock.run_for(500)
                    skip = page.get_by_role("button", name="Přeskočit náhled")
                    expect(skip).to_be_visible()
                    skip.focus()
                    page.wait_for_function("() => Boolean(window.finishPreview)")
                    page.evaluate("window.finishPreview()")
                    page.wait_for_function("() => Boolean(window.finishCategory)")
                    expect(page.locator("#preview-skeleton")).to_be_visible()
                    assert page.locator("#preview-skeleton").evaluate(skeleton_style) == list_style
                    page.screenshot(path=str(args.output / "wishlist-preview-loading-mobile.png"), animations="disabled")
                    expect(page.locator("#preview-product")).not_to_be_visible()
                    expect(category).not_to_be_visible()
                    expect(page.locator("#editor-submit")).to_be_disabled()
                    page.locator("#editor-form").evaluate("(form) => form.requestSubmit()")
                    assert operations.count("web.add") == 2
                    page.evaluate("window.finishCategory()")
                    expect(page.locator("#preview-skeleton")).not_to_be_visible()
                    expect(page.locator("#product-preview")).to_have_attribute("data-state", "ready")
                    expect(disclosure).to_be_focused()
                    expect(category).to_have_value("Domácnost")
                    expect(page.locator("#editor-submit")).to_be_enabled()
                    category.select_option("Dárky")
                    expect(category).to_have_value("Dárky")
                    expect(page.locator("#category-status")).not_to_be_visible()
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Zahodit", exact=True).click()
                    expect(page.locator("#editor")).not_to_be_visible()
                    kettle.click()
                    page.get_by_role("button", name="Zkontrolovat cenu", exact=True).click()
                    expect(page.get_by_text("Čeká na další ověření ceny a dostupnosti.")).to_be_visible()
                    expect(page.get_by_text("Poslední známá cena", exact=True)).to_be_visible()
                    expect(page.locator("svg.chart")).to_be_visible()
                    page.keyboard.press("Escape")
                    assert operations.count("web.add") == 2
                    assert "web.statistics" not in operations
                    assert not errors, errors
                    if a11y:
                        (args.output / "wishlist-axe.json").write_text(json.dumps(a11y, indent=2) + "\n")
                        assert not any(a11y.values()), a11y
                    (args.output / "wishlist-browser.json").write_text(json.dumps({
                        "result": "passed", "scope": "synthetic local HTTPS; no pilot or shop access",
                        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
                        "browser": browser.version, "viewports": [width for width, _ in VIEWPORTS],
                        "same_origin_fonts": bool(args.fonts),
                        "checks": ["list and visible photos reveal together", "failed, slow and offscreen photos",
                                   "shared skeleton styling and reduced motion",
                                   "purchase summaries absent", "no automatic aggregate requests", "list filters",
                                   "search and empty result", "purchase history retained", "purchase recording",
                                   "price and stock changes", "sorting", "responsive rows", "keyboard focus",
                                   "informative and single-price histories", "pre-save preview and manual fallback",
                                   "unavailable, partial and superseded previews", "no page errors"]}, indent=2) + "\n")
                    context.close()
                finally:
                    browser.close()
        finally:
            shared["server"].shutdown()
            shared["server"].server_close()
            thread.join(timeout=5)
    print("Synthetic desktop/mobile wishlist acceptance passed.")


if __name__ == "__main__":
    main()
