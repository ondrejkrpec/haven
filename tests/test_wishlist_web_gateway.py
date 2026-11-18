"""HTTP authorization, CSRF, data separation and request forwarding boundaries."""

import io
import json
import unittest

from scripts.wishlist_web_gateway import Application, COOKIE


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.now = 10000
        config = {"origin": "https://haven.example.ts.net", "owner_login": "owner@example.com"}
        def transport(config, request):
            self.calls.append(request)
            return {"code": "ok", "committed": False, "data": {"entries": []}}
        self.app = Application(config, transport=transport, clock=lambda: self.now)
        self.headers = {"HTTP_HOST": "haven.example.ts.net", "HTTP_TAILSCALE_USER_LOGIN": "owner@example.com"}
        self.cookie = None
        self.csrf = None

    def call(self, path, *, method="GET", payload=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        env = {**self.headers, "REQUEST_METHOD": method, "PATH_INFO": path,
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if self.cookie:
            env["HTTP_COOKIE"] = self.cookie
        if headers:
            env.update(headers)
        result = {}
        def start(status, response_headers):
            result.update(status=status, headers=dict(response_headers))
        result["body"] = b"".join(self.app(env, start))
        return result

    def bootstrap(self):
        response = self.call("/api/bootstrap")
        self.assertEqual(response["status"], "200 OK")
        self.cookie = response["headers"]["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(response["body"])["csrf"]
        return response

    def post(self, **extra):
        return self.call("/api/command", method="POST", payload={"operation": "web.list"},
                         headers={"HTTP_ORIGIN": "https://haven.example.ts.net", "HTTP_X_CSRF_TOKEN": self.csrf or "", **extra})

    def test_identity_and_host_are_required_before_static_or_api_access(self):
        for headers in ({"HTTP_TAILSCALE_USER_LOGIN": ""}, {"HTTP_TAILSCALE_USER_LOGIN": "other@example.com"}, {"HTTP_HOST": "attacker.example.com"}):
            for path in ("/", "/api/bootstrap"):
                self.assertEqual(self.call(path, headers=headers)["status"], "403 Forbidden")
        self.assertEqual(self.calls, [])

    def test_cookie_is_secure_scoped_and_httponly_and_csrf_requires_origin(self):
        response = self.bootstrap()
        cookie = response["headers"]["Set-Cookie"]
        for text in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/wishlist/"):
            self.assertIn(text, cookie)
        self.assertIn(COOKIE, cookie)
        self.assertEqual(self.post(HTTP_ORIGIN="https://attacker.example.com")["status"], "403 Forbidden")
        self.assertEqual(self.post(HTTP_X_CSRF_TOKEN="x")["status"], "403 Forbidden")
        self.assertEqual(self.post(HTTP_SEC_FETCH_SITE="cross-site")["status"], "403 Forbidden")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.post()["status"], "200 OK")
        self.assertEqual(len(self.calls), 1)

    def test_expired_or_forged_session_and_another_process_secret_are_rejected(self):
        self.bootstrap()
        original = self.cookie
        self.cookie = original[:-1] + ("a" if original[-1] != "a" else "b")
        self.assertEqual(self.post()["status"], "403 Forbidden")
        self.cookie = original
        self.now += 43201
        self.assertEqual(self.post()["status"], "403 Forbidden")

    def test_malformed_or_oversized_json_and_nonweb_roles_never_reach_domain(self):
        self.bootstrap()
        headers = {"HTTP_ORIGIN": "https://haven.example.ts.net", "HTTP_X_CSRF_TOKEN": self.csrf}
        for raw in (b'{"operation":"web.list","operation":"web.add"}', b'{"x":NaN}', b'{}' + b' ' * 16384):
            self.assertEqual(self.call("/api/command", method="POST", raw=raw, headers=headers)["status"], "400 Bad Request")
        self.assertEqual(self.call("/api/command", method="POST", payload={"operation": "collection.claim"}, headers=headers)["status"], "403 Forbidden")
        self.assertEqual(self.calls, [])

    def test_production_assets_never_offer_demo_or_checkout_and_keep_csp(self):
        response = self.call("/")
        self.assertIn(b'name="haven-mode" content="production"', response["body"])
        self.assertIn("connect-src 'self'", response["headers"]["Content-Security-Policy"])
        for path in ("/demo.mjs", "/../config.json", "/.git/config", "/scripts/"):
            self.assertEqual(self.call(path)["status"], "404 Not Found")

    def test_domain_outage_returns_unknown_outcome_without_exposing_exception(self):
        self.bootstrap()
        def unavailable(*_):
            raise OSError("synthetic confidential path")
        self.app.transport = unavailable
        result = self.post()
        self.assertEqual(result["status"], "503 Service Unavailable")
        self.assertNotIn(b"confidential", result["body"])
