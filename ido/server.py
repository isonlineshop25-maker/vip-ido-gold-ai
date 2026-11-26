"""IDO GOLD AI — standalone, same-origin-first Python API (stdlib + SQLite)."""

import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import advisor_scores as scores
import agent_coordinator as coordinator
import readiness
import forward_learning as learning
from advisor_journal import journal, latest_round
from books import SCALP_HORIZONS, prefs
from connections import bridge_identity, bridge_record, connection_report
from engine import (Rejected, apply_action, audit, chart_trades, connect, dashboard, evaluate,
                    get_positions, initial, initialize, load, save_settings, save_strategies, store,
                    validation_history_rows)
from market import Market, PROVIDERS, RANGES
from strategy import analysis
from validation import history as validation_history, record_history, snapshot
from operator_auth import ACCOUNT, COOKIE_SECONDS, COOKIE as OPERATOR_COOKIE, OperatorAuth
from runtime_status import WorkerStatus, sqlite_failure


LOG = logging.getLogger("ido")
DB = Path(os.getenv("IDO_STATE_PATH", str(Path(__file__).parent / "state" / "ido.sqlite3")))
ANDROID_APK = Path(__file__).resolve().parents[3] / "generated-artifacts" / "ido-gold-ai-debug.apk"
ANDROID_WEBVIEW_ORIGIN = "https://android.ido.invalid"
MARKET = Market()
AGENT_TOKEN = os.getenv("IDO_AGENT_WORKER_TOKEN", "")
MARKET.agent_remote = len(AGENT_TOKEN) >= 32
BRIDGE_TOKEN = os.getenv("IDO_MT5_BRIDGE_TOKEN", "")
DEMO_ACCOUNT = os.getenv("IDO_MT5_DEMO_ACCOUNT", "")
PROXY_TOKEN = os.getenv("IDO_DASHBOARD_PROXY_TOKEN", "")
TRUSTED_PROXIES = set(x.strip() for x in os.getenv("IDO_TRUSTED_TLS_PROXIES", "").split(",") if x.strip())
BRIDGE_IDENTITY = bridge_identity(BRIDGE_TOKEN, DEMO_ACCOUNT, PROXY_TOKEN, TRUSTED_PROXIES)
CORS = set(x.strip().rstrip("/") for x in os.getenv("IDO_ALLOWED_ORIGINS", "").split(",")
           if x.strip().startswith("https://"))
REPLIT_HOSTS = set(x.strip().lower() for x in os.getenv("REPLIT_DOMAINS", "").split(",") if x.strip())
REPLIT_HOSTS.add(os.getenv("REPLIT_DEV_DOMAIN", "").lower())
COOKIE = "ido_paper_session"
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
OPERATOR = OperatorAuth(os.getenv("IDO_OPERATOR_PASSWORD", ""), store=DB.with_name("logins.json"))
WORKERS = WorkerStatus()
WEB = Path(__file__).parent / "web"
# Only these files are served; nothing else on disk is reachable through the web page.
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/vip-ido-192.png": ("vip-ido-192.png", "image/png"), "/vip-ido-512.png": ("vip-ido-512.png", "image/png"),
          "/vip-ido-maskable.png": ("vip-ido-maskable.png", "image/png")}
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def secret_matches(actual, expected):
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


def json_number(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def check_settings(data):
    if set(data) != {"provider", "maxPositions", "riskPct", "dailyLossPct"}:
        return False
    return (data["provider"] in PROVIDERS and type(data["maxPositions"]) is int
            and 1 <= data["maxPositions"] <= 3 and json_number(data["riskPct"], 0.1, 1)
            and json_number(data["dailyLossPct"], 0.5, 3))


def deployment_environment():
    return (os.getenv("REPLIT_DEPLOYMENT") == "1" or os.getenv("IDO_REQUIRE_PRIVATE_AUTH") == "1"
            or os.getenv("IDO_REQUIRE_DEPLOYMENT_ACCESS") == "1")


def private_operator_required():
    return (OPERATOR.configured or bool(os.getenv("IDO_OPERATOR_PASSWORD")) or os.getenv("IDO_REQUIRE_PRIVATE_AUTH") == "1"
            or os.getenv("REPLIT_DEPLOYMENT") == "1")


def deployment_access_configured():
    return not deployment_environment() or len(PROXY_TOKEN) >= 32 or (
        not PROXY_TOKEN and os.getenv("IDO_ENABLE_PUBLIC_DEMO") == "1")


def log_worker_error(worker, exc):
    code = sqlite_failure(exc)[1]
    LOG.error("Achtergrondcyclus mislukt: %s; type=%s; sqlite_code=%s.",
              worker, type(exc).__name__, code)


def worker_loop(worker, cycle, interval):
    while True:
        try:
            WORKERS.complete(worker, cycle() is not False)
        except Exception as exc:
            WORKERS.complete(worker, False)
            log_worker_error(worker, exc)
        time.sleep(interval)


def market_cycle():
    with closing(connect(DB)) as db:
        MARKET.refresh(db)


def engine_cycle():
    healthy = True
    with closing(connect(DB)) as db:
        active = list(db.execute("SELECT id FROM accounts WHERE active=1"))
        for row in active:
            try:
                sid, now = row["id"], int(time.time())
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    state = load(db, sid, now)
                    remaining = evaluate(db, sid, state, get_positions(db, sid), MARKET, now)
                    store(db, sid, state, remaining)
            except Exception as exc:
                healthy = False
                log_worker_error("engine", exc)
    return healthy


def validation_cycle():
    """Calculate outside the write lock; only append if the DB view is still current."""
    with closing(connect(DB)) as db:
        accounts = list(db.execute("SELECT id FROM accounts"))
        for account in accounts:
            sid, now = account["id"], int(time.time())
            with db:
                version = db.execute("PRAGMA data_version").fetchone()[0]
                db.execute("BEGIN")
                rows = validation_history_rows(db, sid, load(db, sid, now), MARKET, now)
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA data_version").fetchone()[0] != version:
                    continue
                for book, report, profile, strategy in rows:
                    record_history(db, sid, book, report, profile, strategy, now)


class Handler(BaseHTTPRequestHandler):
    server_version = "IDO"
    sys_version = ""

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_):
        # Paths or payloads may contain customer-provided data. Never log them.
        pass

    def host(self):
        return self.headers.get("Host", "").lower()

    def trusted_tls(self):
        return self.client_address[0] in TRUSTED_PROXIES and self.headers.get("X-Forwarded-Proto") == "https"

    def browser_https(self):
        host = self.host()
        return self.trusted_tls() or (host in REPLIT_HOSTS and bool(host)) or (
            deployment_environment() and host.endswith(".replit.app"))

    def allowed_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None
        same_host = origin == ("https://" if self.browser_https() else "http://") + self.host()
        if private_operator_required():
            return origin if self.operator_https() and (same_host or origin == ANDROID_WEBVIEW_ORIGIN) else None
        if origin == ANDROID_WEBVIEW_ORIGIN:
            # Reserved domain: only the packaged, locally served Android UI has this origin.
            # The public demo gets its own isolated cookie. Never bypass the operator proxy.
            return origin if self.browser_https() and not PROXY_TOKEN else None
        return origin if same_host or origin in CORS else None

    def operator_https(self):
        # Replit terminates TLS before the internal service. Match its HTTPS
        # scheme indicator as well as the routed hostname, never a bare Host.
        return self.trusted_tls() or (self.browser_https() and self.headers.get("X-Forwarded-Proto") == "https")

    def download_android(self):
        try:
            apk = ANDROID_APK.open("rb")
        except OSError:
            return self.error(404, "Er is hier geen Android-debugbuild beschikbaar.")
        with apk:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Disposition", 'attachment; filename="ido-gold-ai-debug.apk"')
            self.send_header("Content-Length", str(os.fstat(apk.fileno()).st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            while chunk := apk.read(65536):
                self.wfile.write(chunk)

    def send_static(self, path):
        name, kind = STATIC[path]
        try:
            body = (WEB / name).read_bytes()
        except OSError:
            return self.error(404, "Dashboardbestand ontbreekt.")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if name.endswith((".html", ".webmanifest")) else "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if name.endswith(".html"):
            self.send_header("Content-Security-Policy", PAGE_CSP)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, data, new_session=None, operator_cookie=None, retry_after=None):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Vary", "Origin")
        if origin := self.allowed_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        if new_session:
            attrs = "Secure; SameSite=None; Partitioned" if self.browser_https() else "SameSite=Lax"
            self.send_header("Set-Cookie", f"{COOKIE}={new_session}; Path=/api; HttpOnly; Max-Age=2592000; {attrs}")
        if operator_cookie is not None:
            policy = "None; Partitioned" if self.headers.get("Origin") == ANDROID_WEBVIEW_ORIGIN else "Lax"
            age = COOKIE_SECONDS if operator_cookie else 0
            self.send_header("Set-Cookie", f"{OPERATOR_COOKIE}={operator_cookie}; Path=/; HttpOnly; Secure; SameSite={policy}; Max-Age={age}")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(encoded)

    def error(self, code, message):
        self.send_json(code, {"error": message})

    def do_OPTIONS(self):
        if not self.allowed_origin():
            return self.error(403, "Herkomst niet toegestaan.")
        verb = self.headers.get("Access-Control-Request-Method", "")
        headers = {h.strip().lower() for h in self.headers.get("Access-Control-Request-Headers", "").split(",") if h.strip()}
        if verb not in {"GET", "POST", "PUT"} or not headers <= {"content-type", "x-ido-csrf", "x-ido-login"}:
            return self.error(403, "Voorcontrole niet toegestaan.")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.allowed_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-IDO-CSRF, X-IDO-Login")
        self.send_header("Vary", "Origin, Access-Control-Request-Method, Access-Control-Request-Headers")
        self.end_headers()

    def cookie(self, name):
        try:
            return SimpleCookie(self.headers.get("Cookie", ""))[name].value
        except (KeyError, ValueError, CookieError):
            return ""

    def reader_session(self):
        if len(PROXY_TOKEN) >= 32:
            # Everyone authorized by the private proxy shares this one operator account.
            return "private-operator" if self.trusted_tls() and secret_matches(
                self.headers.get("X-IDO-Operator", ""), PROXY_TOKEN) else None
        candidate = self.cookie(COOKIE)
        return candidate if SESSION_RE.fullmatch(candidate) else None

    def operator_status(self, csrf=None, **kwargs):
        self.send_json(200, {"required": True, "authenticated": csrf is not None, "csrfToken": csrf}, **kwargs)

    def operator_blocked(self, status):
        if status == 429:
            return self.send_json(429, {"error": "Te veel verzoeken. Probeer het straks opnieuw."}, retry_after=60)
        if status == 403:
            return self.error(403, "De sessiecontrole voor dit verzoek is mislukt.")
        return self.send_json(401, {"error": "Aanmelden bij IDO is vereist.", "code": "authentication_required"})

    def operator_login(self):
        if self.headers.get("X-IDO-Login") != "1":
            return self.error(403, "Aanmeldverzoek niet toegestaan.")
        data = self.body()
        if set(data) != {"password"}:
            raise ValueError
        status, token, wait = OPERATOR.sign_in(data["password"], self.cookie(OPERATOR_COOKIE))
        if status == 429:
            return self.send_json(429, {"error": "Te veel aanmeldpogingen. Probeer het later opnieuw."}, retry_after=wait)
        if status != 200:
            return self.error(401, "Aanmelden niet gelukt.")
        # Client must prove that its WebView accepted the cookie before use.
        return self.operator_status(operator_cookie=token)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get_content_type() != "application/json" or not 0 < length <= 4096:
            raise ValueError
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError
        return data

    def heartbeat(self):
        if not BRIDGE_IDENTITY:
            return self.error(503, "MT5-brug niet geconfigureerd.")
        if not self.trusted_tls() or not secret_matches(self.headers.get("Authorization", ""), "Bearer " + BRIDGE_TOKEN):
            return self.error(403, "MT5-brug niet geautoriseerd.")
        data = self.body()
        now = int(time.time())
        if (set(data) != {"mode", "accountRef", "balance", "currency", "timestamp", "nonce"}
                or data["mode"] != "demo" or not isinstance(data["accountRef"], str)
                or not secret_matches(data["accountRef"], DEMO_ACCOUNT)
                or type(data["timestamp"]) is not int or abs(now - data["timestamp"]) > 45
                or not isinstance(data["nonce"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,90}", data["nonce"])
                or not isinstance(data["currency"], str) or not re.fullmatch(r"[A-Z]{3}", data["currency"])
                or not json_number(data["balance"], 0, 1_000_000_000)):
            return self.error(403, "Ongeldige of niet-toegestane MT5-demostatus.")
        with closing(connect(DB)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM bridge_nonces WHERE seen<?", (now - 120,))
            if not db.execute("INSERT OR IGNORE INTO bridge_nonces VALUES(?,?)", (data["nonce"], now)).rowcount:
                return self.error(409, "Heartbeat reeds verwerkt.")
            old = bridge_record(db, BRIDGE_IDENTITY)
            db.execute("INSERT INTO bridge(id,seen,balance,currency,identity) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "seen=excluded.seen,balance=excluded.balance,currency=excluded.currency,identity=excluded.identity",
                       (now, round(data["balance"], 2), data["currency"], BRIDGE_IDENTITY))
            if not old or now - old["seen"] > 90:
                audit(db, "private-operator", now, "success", "MT5-demoheartbeat geverifieerd. Orderuitvoering blijft uit.")
        self.send_json(200, {"status": "verified_demo", "executionAllowed": False})

    def route(self):
        try:
            parsed = urlsplit(self.path)
            path, method, now = parsed.path, self.command, int(time.time())
            private = private_operator_required()
            if private and (path == "/api/bridge/v1/heartbeat" or path.startswith("/api/agents/v1/")):
                return self.error(404, "Deze koppeling is niet beschikbaar in de privé-paperdemo.")
            if method == "POST" and path == "/api/bridge/v1/heartbeat":
                return self.heartbeat()
            if (method, path) in (("GET", "/api/agents/v1/task"), ("POST", "/api/agents/v1/message")):
                # Scoped server-to-server capability. No browser session or shared broker token.
                if not MARKET.agent_remote:
                    return self.error(404, "Externe adviseur niet geconfigureerd.")
                if (self.headers.get("Origin") or self.headers.get("Cookie") or not (
                    self.trusted_tls() or self.browser_https() and self.headers.get("X-Forwarded-Proto") == "https"
                ) or not secret_matches(self.headers.get("Authorization", ""), "Bearer " + AGENT_TOKEN)):
                    return self.error(403, "Een geauthenticeerde HTTPS-worker is vereist.")
                with closing(connect(DB)) as db, db:
                    if method == "GET":
                        return self.send_json(200, {"task": coordinator.task(db, now)})
                    data = self.body()
                    db.execute("BEGIN IMMEDIATE")
                    if not coordinator.submit(db, data, int(time.time())):
                        return self.error(409, "Ronde, snapshot of fase is niet meer geldig.")
                return self.send_json(200, {"accepted": True})
            # Reject credential-bearing requests from other sites and any browser POST without an Origin.
            if (self.headers.get("Origin") or method != "GET") and not self.allowed_origin():
                return self.error(403, "Herkomst niet toegestaan.")
            if method == "GET" and path == "/api/healthz":
                return self.send_json(200, {"status": "ok"})
            if method == "GET" and path in STATIC:
                return self.send_static(path)
            if private:
                if not self.operator_https():
                    return self.error(403, "Voor IDO-privétoegang is HTTPS vereist.")
                if not OPERATOR.configured:
                    return self.send_json(503, {"error": "Privétoegang is nog niet ingesteld.",
                                                "code": "deployment_access_not_configured"})
                cookie = self.cookie(OPERATOR_COOKIE)
                if method == "POST" and path == "/api/auth/login":
                    return self.operator_login()
                status, csrf = OPERATOR.check(cookie, self.headers.get("X-IDO-CSRF", "") if method != "GET" else None)
                if method == "GET" and path == "/api/auth/session" and status != 429:
                    # Renew the cookie on every visit, so the phone never has to log in again.
                    return self.operator_status(csrf, operator_cookie=cookie if status == 200 else None)
                if status != 200:
                    return self.operator_blocked(status)
                if method == "POST" and path == "/api/auth/logout":
                    OPERATOR.revoke(cookie)
                    return self.operator_status(operator_cookie="")
                sid = ACCOUNT
            elif not deployment_access_configured():
                return self.send_json(503, {"error": "Toegang tot de gepubliceerde demo is nog niet ingesteld.",
                                            "code": "deployment_access_not_configured"})
            elif method == "GET" and path == "/api/auth/session":
                return self.send_json(200, {"required": False, "authenticated": False, "csrfToken": None})
            else:
                sid = self.reader_session()
            if method == "GET" and path == "/api/android-apk" and (private or not PROXY_TOKEN):
                return self.download_android()
            if not private and PROXY_TOKEN and (len(PROXY_TOKEN) < 32 or not sid):
                return self.error(403, "Deze API vereist privétoegang via de ingestelde HTTPS-proxy.")
            bridge = None if private else BRIDGE_IDENTITY
            if method == "GET":
                new_session = None
                if path == "/api/dashboard" and not sid:
                    sid = new_session = secrets.token_urlsafe(32)
                with closing(connect(DB)) as db:
                    # A WAL reader needs no reservation on the only writer.
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    state = load(db, sid, now) if sid else initial(now)
                    if path == "/api/dashboard":
                        result = dashboard(db, sid, state, MARKET, now, bridge)
                        db.commit()
                        return self.send_json(200, result, new_session)
                    if path == "/api/validation-history":
                        book = parse_qs(parsed.query).get("book", [None])[0]
                        if book not in ("combined", "scalp", "swing"):
                            return self.error(400, "Kies combined, scalp of swing.")
                        return self.send_json(200, validation_history(db, sid, book) if sid
                                              else dict(book=book, points=[]))
                    if path == "/api/connection-status":
                        result = connection_report(db, MARKET, state["settings"]["provider"], bridge, now)
                        result["sessionEstablished"] = sid is not None
                        result["workers"] = WORKERS.report()
                        return self.send_json(200, result)
                    if path == "/api/advisor-log":
                        raw_round = parse_qs(parsed.query).get("round", [None])[0]
                        requested = None if raw_round is None else int(raw_round)
                        if requested is not None and not 1 <= requested <= 9007199254740991:
                            return self.error(400, "Ongeldig adviesnummer.")
                        return self.send_json(200, journal(db, sid, state["settings"]["provider"], now, requested))
                    if path == "/api/readiness":
                        return self.send_json(200, readiness.report(db, sid, state["settings"]["provider"], now))
                    if path == "/api/bots":
                        provider = state["settings"]["provider"]
                        return self.send_json(200, scores.report(db, sid, provider, now, latest_round(db, sid, provider)))
                    if path == "/api/advisor-activity":
                        provider = state["settings"]["provider"]
                        data = coordinator.activity(db, sid, provider, latest_round(db, sid, provider))
                        return self.send_json(200, dict(data, externalEnabled=MARKET.agent_remote))
                    if path == "/api/learning":
                        remote = MARKET.agent_remote
                        data = learning.report(db, sid, state["settings"], now, remote)
                        data["paper"] = snapshot(db, sid, state["settings"], now, remote, data["active"],
                                                 forecast_profile=learning.policy(state["settings"], remote),
                                                 horizon=prefs(state)["scalpMinutes"])
                        return self.send_json(200, data)
                    provider = state["settings"]["provider"]
                    if path == "/api/candles":
                        range_key = parse_qs(parsed.query).get("range", ["1d"])[0]
                        if range_key not in RANGES:
                            return self.error(400, "Kies 1h, 1d, 7d of 30d.")
                        report = MARKET.chart(provider, range_key, now)
                        report["trades"] = chart_trades(db, sid, provider, report["windowStart"], now) if sid else []
                        return self.send_json(200, report)
                    if path == "/api/analysis":
                        arm = learning.load_model(db, sid, learning.policy(state["settings"], MARKET.agent_remote))["active"]
                        return self.send_json(200, analysis(MARKET, provider, now, lambda q: learning.preview(arm, q, now)))
            if (method, path) in (("POST", "/api/engine"), ("PUT", "/api/settings"), ("PUT", "/api/strategies")):
                if not sid:
                    return self.error(409, "Geen demosessie. Open het dashboard in je browser en sta sessiecookies toe.")
                data = self.body()
                with closing(connect(DB)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    state = load(db, sid, now)
                    positions = get_positions(db, sid)
                    if path == "/api/engine":
                        if set(data) != {"action"} or data["action"] not in ("start", "stop", "emergency_stop", "reset_emergency"):
                            raise ValueError
                        positions = apply_action(db, sid, state, positions, MARKET, now, data["action"])
                    elif path == "/api/strategies":
                        if (set(data) != {"scalpEnabled", "swingEnabled", "scalpMinutes"} or
                            any(type(data[key]) is not bool for key in ("scalpEnabled", "swingEnabled")) or
                            type(data["scalpMinutes"]) is not int or data["scalpMinutes"] not in SCALP_HORIZONS):
                            raise ValueError
                        save_strategies(db, sid, state, now, data)
                    else:
                        if not check_settings(data):
                            raise ValueError
                        save_settings(db, sid, state, positions, now, data, bool(MARKET.key))
                    store(db, sid, state, positions)
                    result = dashboard(db, sid, state, MARKET, now, bridge)
                return self.send_json(200, result)
            return self.error(404, "Niet gevonden. Deze API heeft geen route voor brokerorders.")
        except Rejected as exc:
            return self.error(409, str(exc))
        except (ValueError, TypeError, RecursionError, TimeoutError):
            return self.error(400, "Ongeldige aanvraag of waarde buiten de harde limieten.")
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.Error as exc:
            busy, code = sqlite_failure(exc)
            LOG.warning("Opslagverzoek mislukt; sqlite_code=%s.", code)
            return self.send_json(503, {"code": "storage_busy" if busy else "storage_unavailable",
                                       "error": "Opslag is bezig. Probeer zo opnieuw." if busy
                                       else "Opslag tijdelijk niet beschikbaar."}, retry_after=2 if busy else 5)

    do_GET = do_POST = do_PUT = route


class Server(ThreadingHTTPServer):
    slots = threading.BoundedSemaphore(48)

    def process_request(self, request, client_address):
        if self.slots.acquire(blocking=False):
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        LOG.warning("Aanvraag niet voltooid.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    if private_operator_required():
        MARKET.agent_remote = False
        if not OPERATOR.configured:
            reason = ("IDO_OPERATOR_PASSWORD voldoet niet aan de wachtwoordeisen"
                      if "IDO_OPERATOR_PASSWORD" in os.environ
                      else "IDO_OPERATOR_PASSWORD is niet beschikbaar in deze serveromgeving")
            LOG.warning("Privétoegang is geblokkeerd: %s.", reason)
    DB.parent.mkdir(parents=True, exist_ok=True)
    initialize(DB)
    with closing(connect(DB)) as db:
        MARKET.restore(db, int(time.time()))
    for worker, cycle, interval in (("market", market_cycle, 2), ("engine", engine_cycle, 5),
                                    ("validation", validation_cycle, 20)):
        threading.Thread(target=worker_loop, args=(worker, cycle, interval), daemon=True, name=worker).start()
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("IDO_BIND_HOST", "127.0.0.1")
    with Server((host, port), Handler) as server:
        LOG.info("IDO demo-API luistert op poort %d; echte orders zijn niet mogelijk.", port)
        server.serve_forever()
