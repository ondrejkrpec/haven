"""Opt-in local HTTPS acceptance with synthetic state and an isolated browser profile.

Run as a module after installing research/wishlist/browser-requirements.txt in a
separate environment. Never connects to the pilot or a product shop.
"""

import argparse
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
from scripts.wishlist_web_domain import WebApplication
from scripts.wishlist_web_gateway import Application, ROOT

VIEWPORTS = [(1920, 1080), (1440, 900), (1280, 800), (1024, 768), (768, 900), (390, 844), (320, 760)]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_):
        pass


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

    def observe(price, availability="in_stock"):
        claim = collection.handle({"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
        assert claim["code"] == "ok", claim
        offer = {"name": "Syntetický produkt", "sku": "EXAMPLE-1", "price": price, "currency": "CZK" if price else None,
                 "availability": availability, "conditions_verification": "not_verified"}
        result = {"status": "ok", "reason": "structured_offer_candidate" if price else "source_out_of_stock",
                  "http_status": 200, "retry_after": 0, "offer": offer}
        completed = collection.handle({"protocol_version": 1, "operation": "collection.complete",
                                       "job": claim["job"], "result": result})
        assert completed["code"] == "ok", completed

    for price, availability in (("1000.00", "in_stock"), ("750.00", "in_stock"), (None, "out_of_stock"), ("750.00", "in_stock")):
        observe(price, availability)
        check_at += timedelta(days=1)
    check_at = now - timedelta(hours=1)
    call("add", {"url": "https://www.fabini.cz/synthetic-fixture/", "details": {
        "label": "Testovací pánev", "category": "home", "purchase_mode": "once"}})
    observe("1250.00")
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
                with patch("scripts.wishlist_web_gateway.ROOT", assets):
                    gateway = Application({"origin": origin, "owner_login": "owner@example.com"},
                                          transport=lambda config, request: application.handle(request))

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
                    page.clock.install()
                    page.goto(shared["origin"] + "/wishlist/")
                    expect(page.locator("#visible-count")).to_have_text("3")
                    expect(page.locator("#purchase-statistics")).to_have_count(0)
                    expect(page.locator(".summary-card")).to_have_count(0)
                    expect(page.get_by_text("Znovu skladem")).to_be_visible()
                    expect(page.get_by_text("Zlevnilo o")).to_be_visible()
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
                    page.locator("#daily-summary").click()
                    expect(page.locator(".daily-table")).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(kettle).to_be_focused()
                    page.get_by_role("button", name="Testovací pánev", exact=True).click()
                    expect(page.locator(".chart-empty")).to_contain_text("První cena zaznamenána")
                    expect(page.locator("svg.chart")).to_have_count(0)
                    expect(page.locator(".detail-stat-grid")).to_have_count(0)
                    expect(page.locator(".detail-history")).to_contain_text("Rozsah")
                    page.screenshot(path=str(args.output / "wishlist-detail-mobile.png"), animations="disabled")
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Testovací lampa", exact=True).click()
                    expect(page.locator(".purchase-card")).to_have_count(4)
                    page.get_by_role("button", name="Zavřít detail", exact=True).click()
                    page.get_by_role("button", name="Označit jako koupené: Testovací lampa").click()
                    page.locator('#editor [name="price"]').fill("300")
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
                    page.screenshot(path=str(args.output / "wishlist-add-mobile.png"), animations="disabled")
                    if custom_select:
                        disclosure = page.locator(".extra-fields summary")
                        disclosure.click()
                        category = page.get_by_role("combobox", name="Kategorie").last
                        category.focus()
                        page.keyboard.press("Space")
                        assert category.evaluate("(el) => el.matches(':open')")
                        page.keyboard.press("Escape")
                        assert not category.evaluate("(el) => el.matches(':open')")
                        expect(category).to_be_focused()
                        expect(page.locator("#editor")).to_be_visible()
                        disclosure.click()
                    url.fill("http://shop.example.com/invalid")
                    page.locator("#editor-submit").click()
                    expect(url).to_have_attribute("aria-invalid", "true")
                    expect(url).to_be_focused()
                    url.fill("https://shop.example.com/new-synthetic-item")
                    page.locator("#editor-submit").click()
                    expect(page.locator("#editor")).not_to_be_visible()
                    expect(page.locator("#detail")).to_be_visible()
                    expect(page.locator("#detail-content")).to_contain_text("Odkaz je uložený")
                    page.keyboard.press("Escape")
                    expect(page.locator("#visible-count")).to_have_text("4")
                    assert "web.statistics" not in operations
                    assert not errors, errors
                    (args.output / "wishlist-browser.json").write_text(json.dumps({
                        "result": "passed", "scope": "synthetic local HTTPS; no pilot or shop access",
                        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
                        "browser": browser.version, "viewports": [width for width, _ in VIEWPORTS],
                        "same_origin_fonts": bool(args.fonts),
                        "checks": ["purchase summaries absent", "no automatic aggregate requests", "list filters",
                                   "search and empty result", "purchase history retained", "purchase recording",
                                   "price and stock changes", "sorting", "responsive rows", "keyboard focus",
                                   "informative and single-price histories", "URL-first add", "no page errors"]}, indent=2) + "\n")
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
