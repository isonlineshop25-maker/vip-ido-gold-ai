"""Exercise the Android-origin API contract through a real HTTP server."""

import http.client
import json
import secrets
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import server
from engine import close_trade, open_trade
from market import Market
from operator_auth import ABSOLUTE_SECONDS, ACCOUNT, API_LIMIT, ATTEMPTS, ATTEMPT_WINDOW, IDLE_SECONDS, OperatorAuth
from runtime_status import WorkerStatus
from spot import GOLD, save_spot


class TransportHarness:
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "ido.sqlite3"
        server.initialize(path)
        self.enterContext(patch.object(server, "DB", path))
        market = Market()
        market.agent_remote = False  # Match the managed service without an optional worker.
        self.enterContext(patch.object(server, "MARKET", market))
        self.enterContext(patch.object(server, "REPLIT_HOSTS", set()))
        self.enterContext(patch.object(server, "PROXY_TOKEN", ""))
        self.enterContext(patch.object(server, "OPERATOR", OperatorAuth()))
        self.enterContext(patch.dict(server.os.environ, IDO_REQUIRE_DEPLOYMENT_ACCESS="1", IDO_ENABLE_PUBLIC_DEMO="1",
                                     IDO_OPERATOR_PASSWORD="", IDO_REQUIRE_PRIVATE_AUTH="", REPLIT_DEPLOYMENT=""))
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.origin = server.ANDROID_WEBVIEW_ORIGIN

    def request(self, method, path, cookie=None, body=None, origin=None, extra=None):
        headers = {"Host": "ido-transport-check.replit.app", "Origin": origin or self.origin}
        if cookie:
            headers["Cookie"] = cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=4)
        try:
            conn.request(method, "/api/" + path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(data) if data else None
        finally:
            conn.close()


class AndroidTransportTests(TransportHarness, unittest.TestCase):
    def test_unconfigured_production_fails_closed_but_health_is_available(self):
        with patch.dict(server.os.environ, IDO_ENABLE_PUBLIC_DEMO=""):
            self.assertEqual(self.request("GET", "healthz")[0], 200)
            status, headers, data = self.request("GET", "dashboard")
            self.assertEqual((status, data["code"]), (503, "deployment_access_not_configured"))
            self.assertNotIn("set-cookie", headers)
            self.assertEqual(self.request("GET", "android-apk")[0], 503)
            self.assertEqual(self.request("POST", "engine", body={"action": "start"})[0], 503)

    def test_all_android_routes_and_cookie_round_trip(self):
        self.assertFalse(self.request("GET", "connection-status")[2]["sessionEstablished"])
        status, headers, dashboard = self.request("GET", "dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], self.origin)
        self.assertEqual(headers["access-control-allow-credentials"], "true")
        self.assertIn("Origin", headers["vary"])
        attributes = headers["set-cookie"]
        for key in ("Secure", "SameSite=None", "Partitioned", "HttpOnly", "Path=/api"):
            self.assertIn(key, attributes)
        cookie = attributes.split(";", 1)[0]
        self.assertEqual(dashboard["mode"], "paper")
        self.assertFalse(dashboard["connections"]["liveTrading"])
        self.assertTrue(self.request("GET", "connection-status", cookie)[2]["sessionEstablished"])
        for route in ("healthz", "candles?range=1h", "analysis", "learning", "advisor-log", "advisor-activity"):
            status, h, _ = self.request("GET", route, cookie)
            self.assertEqual(status, 200, route)
            self.assertEqual(h["access-control-allow-credentials"], "true")
        for path, method, body in (
            ("settings", "PUT", dashboard["settings"]),
            ("strategies", "PUT", dashboard["strategyBooks"]["prefs"]),
            ("engine", "POST", {"action": "stop"}),
        ):
            status, h, _ = self.request("OPTIONS", path, extra={
                "Access-Control-Request-Method": method, "Access-Control-Request-Headers": "content-type",
            })
            self.assertEqual(status, 204)
            self.assertIn(method, h["access-control-allow-methods"])
            self.assertEqual(self.request(method, path, body=body)[0], 409)
            self.assertEqual(self.request(method, path, cookie, body)[0], 200)
        self.assertEqual(self.request("GET", "dashboard", origin="https://other.example")[0], 403)
        self.assertEqual(self.request("POST", "engine", cookie, {"action": "activate_live"})[0], 400)

    def test_native_does_not_bypass_private_proxy_or_http(self):
        with patch.object(server, "PROXY_TOKEN", "test-proxy-secret" * 4):
            self.assertEqual(self.request("GET", "dashboard")[0], 403)
            self.assertEqual(self.request("OPTIONS", "settings", extra={"Access-Control-Request-Method": "PUT"})[0], 403)
        self.assertEqual(self.request("GET", "dashboard", extra={"Host": "untrusted.example"})[0], 403)


class OperatorTransportTests(TransportHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Start each case in the old transport-test mode; private cases opt in explicitly.
        self.now = 1000
        self.password = secrets.token_urlsafe(24)

    def private(self, configured=True):
        self.enterContext(patch.dict(server.os.environ, IDO_REQUIRE_PRIVATE_AUTH="1"))
        self.auth = OperatorAuth(self.password if configured else "", clock=lambda: self.now)
        self.enterContext(patch.object(server, "OPERATOR", self.auth))

    def secure(self, method, route, cookie=None, body=None, origin=None, extra=None):
        return self.request(method, route, cookie, body, origin, {"X-Forwarded-Proto": "https", **(extra or {})})

    def login(self, origin=None, password=None, cookie=None):
        return self.secure("POST", "auth/login", cookie, {"password": self.password if password is None else password},
                           origin, {"X-IDO-Login": "1"})

    def start_session(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        cookie = headers["set-cookie"].split(";", 1)[0]
        _, _, session = self.secure("GET", "auth/session", cookie)
        self.assertTrue(session["authenticated"])
        return cookie, {"X-IDO-CSRF": session["csrfToken"]}

    def test_data_routes_remain_readable_during_wal_write_and_writes_recover(self):
        self.private()
        cookie, csrf = self.start_session()
        with closing(server.connect(server.DB)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            for route in ("dashboard", "candles?range=1h", "learning", "advisor-log",
                          "advisor-activity", "connection-status", "validation-history?book=scalp"):
                self.assertEqual(self.secure("GET", route, cookie)[0], 200, route)
            status, headers, body = self.secure("POST", "engine", cookie, {"action": "stop"}, extra=csrf)
            self.assertEqual((status, body["code"], headers["retry-after"]), (503, "storage_busy", "2"))
            self.assertEqual(writer.execute("SELECT count(*) FROM validation_history").fetchone()[0], 0)
            writer.rollback()
        self.assertEqual(self.secure("POST", "engine", cookie, {"action": "stop"}, extra=csrf)[0], 200)
        self.assertEqual(self.secure("GET", "auth/session", cookie)[2]["authenticated"], True)

    def test_workers_and_existing_paper_data_survive_reopen(self):
        self.private()
        cookie, _ = self.start_session()
        now = int(time.time())
        minute = now // 60 * 60
        with closing(server.connect(server.DB)) as db, db:
            for offset in range(24, -1, -1):
                save_spot(db, minute - offset * 60, 3200 + offset / 10, now)
            server.MARKET.restore(db, now)
            server.MARKET.spot = (now, 3200)
            server.MARKET.checked[GOLD] = (now, True, None)
            state = server.load(db, ACCOUNT, now)
            trade = open_trade(db, ACCOUNT, state, "long", dict(price=3200, time=now - 20, fresh=True, bars=[]),
                               0.5, 5, now - 20, "Basis", reason="Fixture")
            close_trade(db, ACCOUNT, state, trade, dict(price=3203, time=now - 5, fresh=True, bars=[]),
                        now - 5, "Fixture-exit")
            state["engine"] = "running"
            server.store(db, ACCOUNT, state, [])

        self.assertTrue(server.engine_cycle())
        server.validation_cycle()
        server.validation_cycle()
        status, _, d = self.secure("GET", "dashboard", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(d["mode"], "paper")
        self.assertFalse(d["connections"]["liveTrading"])
        self.assertEqual(d["history"][0]["id"], trade["id"])
        self.assertTrue(d["logs"])
        self.assertGreaterEqual(len(self.secure("GET", "candles?range=1h", cookie)[2]["candles"]), 24)
        self.assertTrue(self.secure("GET", "learning", cookie)[2]["forecasts"])
        self.assertTrue(self.secure("GET", "advisor-activity", cookie)[2]["events"])
        self.assertEqual(self.secure("GET", "advisor-log", cookie)[0], 200)
        self.assertEqual(len(self.secure("GET", "validation-history?book=scalp", cookie)[2]["points"]), 1)
        with closing(server.connect(server.DB)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM spot_quotes").fetchone()[0], 25)
            self.assertEqual(db.execute("SELECT count(*) FROM trades").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM forward_calls").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM validation_history").fetchone()[0], 3)

    def test_connection_status_reports_worker_failure_and_recovery(self):
        self.private()
        cookie, _ = self.start_session()
        with patch.object(server, "WORKERS", WorkerStatus(lambda: self.now, lambda: self.now)) as workers:
            def fetch():
                return self.secure("GET", "connection-status", cookie)[2]["workers"]
            self.assertEqual(fetch()["engine"]["status"], "starting")
            workers.complete("engine", False)
            self.assertEqual(fetch()["engine"]["status"], "degraded")
            workers.complete("engine", True)
            self.assertEqual(fetch()["engine"], {"status": "ok", "lastSuccessAt": self.now})
            self.now += 26
            self.assertEqual(fetch()["engine"]["status"], "degraded")
            workers.complete("engine", True)
            self.assertEqual(fetch()["engine"]["status"], "ok")

    def test_private_fails_closed_even_when_public_demo_opt_in_is_left_behind(self):
        self.private(configured=False)
        self.assertEqual(self.secure("GET", "healthz")[2], {"status": "ok"})
        for route in ("auth/session", "dashboard", "connection-status", "android-apk"):
            result = self.secure("GET", route)
            self.assertEqual(result[0], 503, route)
            self.assertEqual(result[2]["code"], "deployment_access_not_configured")
        self.assertEqual(self.login()[0], 503)

    def test_published_runtime_enforces_private_access_without_explicit_flag(self):
        with patch.dict(server.os.environ, REPLIT_DEPLOYMENT="1"):
            self.assertEqual(self.secure("GET", "healthz")[0], 200)
            self.assertEqual(self.secure("GET", "dashboard")[0], 503)
        self.assertEqual(self.secure("GET", "dashboard")[0], 200)

    def test_all_native_routes_require_private_session_and_writes_require_csrf(self):
        self.private()
        self.assertEqual(self.secure("GET", "auth/session")[2],
                         {"required": True, "authenticated": False, "csrfToken": None})
        anonymous = "ido_paper_session=" + secrets.token_urlsafe(32)
        for route in ("dashboard", "connection-status", "candles?range=1h", "analysis", "learning",
                      "advisor-log", "advisor-activity", "android-apk"):
            self.assertEqual(self.secure("GET", route, anonymous)[0], 401, route)
        self.assertEqual(self.secure("POST", "engine", anonymous, {"action": "stop"})[0], 401)
        self.assertEqual(self.secure("POST", "bridge/v1/heartbeat")[0], 404)
        self.assertEqual(self.secure("GET", "agents/v1/task")[0], 404)
        cookie, csrf = self.start_session()
        status, headers, dash = self.secure("GET", "dashboard", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], self.origin)
        self.assertEqual(headers["access-control-allow-credentials"], "true")
        self.assertNotIn("set-cookie", headers)
        self.assertEqual(dash["settings"]["provider"], "gold_api")
        for route in ("candles?range=1h", "analysis", "learning", "advisor-log", "advisor-activity"):
            self.assertEqual(self.secure("GET", route, cookie)[0], 200, route)
        state = self.secure("GET", "connection-status", cookie)[2]
        self.assertTrue(state["sessionEstablished"])
        self.assertFalse(state["liveExecutionAllowed"])
        for method, path, body in (("POST", "engine", {"action": "stop"}),
                                   ("PUT", "settings", dash["settings"]),
                                   ("PUT", "strategies", dash["strategyBooks"]["prefs"])):
            self.assertEqual(self.secure(method, path, cookie, body)[0], 403)
            self.assertEqual(self.secure(method, path, cookie, body, extra=csrf)[0], 200)
        self.assertEqual(self.secure("POST", "engine", cookie, {"action": "activate_live"}, extra=csrf)[0], 400)

    def test_tls_origin_csrf_cookies_and_logout(self):
        self.private()
        self.assertEqual(self.request("GET", "auth/session")[0], 403)
        self.assertEqual(self.login("https://untrusted.example")[0], 403)
        self.assertEqual(self.secure("POST", "auth/login", body={"password": self.password})[0], 403)
        check = self.secure("OPTIONS", "auth/login", extra={"Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "content-type,x-ido-login"})
        self.assertEqual(check[0], 204)
        self.assertIn("X-IDO-CSRF", check[1]["access-control-allow-headers"])
        self.assertEqual(self.secure("OPTIONS", "auth/login", extra={"Access-Control-Request-Method": "POST",
                             "Access-Control-Request-Headers": "authorization"})[0], 403)
        native = self.login()[1]["set-cookie"]
        for item in ("__Host-ido_operator=", "Path=/", "Secure", "HttpOnly", "SameSite=None", "Partitioned"):
            self.assertIn(item, native)
        origin = "https://ido-transport-check.replit.app"
        browser = self.login(origin)[1]["set-cookie"]
        self.assertIn("SameSite=Lax", browser)
        self.assertNotIn("Partitioned", browser)
        cookie, csrf = self.start_session()
        self.assertEqual(self.secure("POST", "auth/logout", cookie)[0], 403)
        result = self.secure("POST", "auth/logout", cookie, extra=csrf)
        self.assertEqual(result[0], 200)
        self.assertIn("Max-Age=0", result[1]["set-cookie"])
        self.assertEqual(self.secure("GET", "dashboard", cookie)[0], 401)
        self.assertFalse(self.secure("GET", "auth/session", cookie)[2]["authenticated"])

    def test_time_limits_login_and_api_throttling(self):
        self.private()
        for _ in range(ATTEMPTS):
            self.assertEqual(self.login(password="invalid")[0], 401)
        result = self.login()
        self.assertEqual(result[0], 429)
        self.assertGreater(int(result[1]["retry-after"]), 0)
        self.now += ATTEMPT_WINDOW + 1
        cookie, _ = self.start_session()
        token = cookie.split("=", 1)[1]
        for _ in range(API_LIMIT - 1):
            self.assertEqual(self.auth.check(token)[0], 200)
        self.assertEqual(self.auth.check(token)[0], 429)
        self.now += IDLE_SECONDS
        self.assertEqual(self.auth.check(token)[0], 401)
        cookie, _ = self.start_session()
        token = cookie.split("=", 1)[1]
        self.now += ABSOLUTE_SECONDS - 1
        self.assertEqual(self.auth.check(token)[0], 200)
        self.now += 30
        self.assertEqual(self.auth.check(token)[0], 401)
