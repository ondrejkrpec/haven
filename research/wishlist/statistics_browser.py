"""Opt-in local HTTPS acceptance with synthetic state and an isolated browser profile.

Run as a module after installing research/wishlist/browser-requirements.txt in a
separate environment. Never connects to the pilot or a product shop.
"""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
from threading import Event, Thread
from uuid import uuid4
from wsgiref.simple_server import WSGIRequestHandler, make_server
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

from scripts.wishlist_domain import Wishlist
from scripts.wishlist_web_domain import WebApplication
from scripts.wishlist_web_gateway import Application


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
                    page.goto(shared["origin"] + "/wishlist/")
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    expect(page.locator("#statistics-savings")).to_have_text("400 Kč", use_inner_text=True)
                    expect(page.locator("#statistics-results")).to_contain_text("Nákupy s neznámou cenou: 1")
                    expect(page.locator("#statistics-results")).to_contain_text("Nákupy bez referenčního průměru: 1")
                    page.locator("#filter-category").select_option(label="Domácnost")
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    expect(page.locator("#visible-count")).to_have_text("0")
                    page.locator("#filter-category").select_option(label="Dárky")
                    expect(page.locator("#statistics-spending")).to_have_text("0 Kč", use_inner_text=True)
                    page.locator("#reset-filters").click()
                    page.locator("#statistics-previous").click()
                    expect(page.locator("#statistics-spending")).to_have_text("100 Kč", use_inner_text=True)
                    page.locator("#statistics-next").click()
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    expect(page.locator("#statistics-next")).to_be_disabled()
                    page.locator("#statistics-period").select_option("all")
                    expect(page.locator("#statistics-spending")).to_have_text("1 800 Kč", use_inner_text=True)
                    expect(page.locator("#period-navigation")).to_be_hidden()
                    page.locator("#statistics-period").select_option("year")
                    local_now = datetime.now(ZoneInfo("Europe/Prague"))
                    year = local_now.year
                    expected = "1 700 Kč" if local_now.month == 1 else "1 800 Kč"
                    expect(page.locator("#statistics-spending")).to_have_text(expected, use_inner_text=True)
                    expect(page.locator("#statistics-date")).to_have_text(str(year))
                    page.locator("#statistics-period").select_option("month")
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    page.screenshot(path=str(args.output / "statistics-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / "statistics-mobile.png"), full_page=True)
                    page.locator("#search").fill("nenalezeno")
                    expect(page.locator("#statistics-spending")).to_have_text("0 Kč", use_inner_text=True)
                    page.locator("#search").fill("")
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)

                    delayed = []
                    def reordered(route):
                        request = route.request.post_data_json
                        if request.get("operation") == "web.statistics" and request["arguments"]["filters"]["search"] == "delayed":
                            delayed.append(route)
                        else:
                            route.continue_()

                    page.route("**/api/command", reordered)
                    with page.expect_request(lambda request: request.method == "POST" and request.post_data_json.get("arguments", {}).get("filters", {}).get("search") == "delayed"):
                        page.locator("#search").fill("delayed")
                    page.locator("#search").fill("Testovací")
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    assert len(delayed) == 1
                    delayed[0].fulfill(status=200, content_type="application/json", body=json.dumps({
                        "code": "ok", "data": {"total": {"count": 0, "spending": "0", "savings": "0",
                                                             "unknown_price": 0, "unknown_reference": 0}, "categories": []}}))
                    page.wait_for_timeout(100)
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    page.unroute("**/api/command", reordered)

                    def outage(route):
                        if route.request.post_data_json.get("operation") == "web.statistics":
                            route.fulfill(status=503, content_type="application/json", body='{"code":"unavailable"}')
                        else:
                            route.continue_()

                    page.route("**/api/command", outage)
                    page.locator("#search").fill("lampa")
                    expect(page.locator("#statistics-retry")).to_be_visible()
                    expect(page.locator("#statistics-spending")).to_have_count(0)
                    page.unroute("**/api/command", outage)
                    page.locator("#statistics-retry").click()
                    expect(page.locator("#statistics-spending")).to_have_text("1 700 Kč", use_inner_text=True)
                    page.get_by_role("button", name="Označit jako koupené: Testovací lampa").click()
                    page.locator('#editor [name="price"]').fill("300")
                    page.locator("#editor-submit").click()
                    expect(page.locator("#statistics-spending")).to_have_text("2 000 Kč", use_inner_text=True)
                    page.locator("#filter-category").select_option(label="Dárky")
                    expect(page.locator("#statistics-spending")).to_have_text("300 Kč", use_inner_text=True)
                    assert not errors, errors
                    (args.output / "statistics-browser.json").write_text(json.dumps({
                        "result": "passed", "scope": "synthetic local HTTPS; no pilot or shop access",
                        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
                        "browser": browser.version, "viewports": [1440, 390],
                        "checks": ["known and unknown money", "saved category filter", "current filters", "period navigation",
                                   "empty result", "outage hides stale totals", "retry", "purchase refreshes totals",
                                   "late response cannot replace current totals", "no mobile overflow", "no page errors"]}, indent=2) + "\n")
                    context.close()
                finally:
                    browser.close()
        finally:
            shared["server"].shutdown()
            shared["server"].server_close()
            thread.join(timeout=5)
    print("Synthetic desktop/mobile statistics acceptance passed.")


if __name__ == "__main__":
    main()
