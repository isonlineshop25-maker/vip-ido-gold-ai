"""Connection evidence and isolation checks; no outside provider requests."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch

from connections import bridge_check, bridge_identity, bridge_record, connection_report, deriv_configuration
from engine import connect, initialize
from market import SIM, Market


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        state = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(state) / "test.sqlite3"
        initialize(self.path)
        self.db = self.enterContext(closing(connect(self.path)))
        with patch("market.os.getenv", return_value=""):
            self.market = Market()
        self.now = 1_800_000_000

    def test_unconfigured_connections_report_no_evidence_and_no_execution(self):
        with patch("connections.os.getenv", return_value=""):
            report = connection_report(self.db, self.market, SIM, None, self.now)
        self.assertEqual(report["mode"], "demo_only")
        self.assertEqual(report["provider"], SIM)
        self.assertEqual(report["market"], dict(status="not_configured", lastCheckAt=None, lastCheckOk=None, lastCandleAt=None,
                                                lastQuoteAt=None, error=None, historyCheckedAt=None, historyError=None))
        self.assertEqual(report["spot"]["status"], "waiting")
        self.assertEqual(report["mt5Connection"], dict(status="not_configured", lastHeartbeatAt=None))
        self.assertEqual(report["deriv"], "not_configured")
        self.assertIs(report["liveExecutionAllowed"], False)

    def test_deriv_configuration_is_never_a_verified_connection(self):
        with patch("connections.os.getenv", side_effect=[token_urlsafe(12), ""]):
            self.assertEqual(deriv_configuration(), "partial")
        with patch("connections.os.getenv", return_value=token_urlsafe(32)):
            self.assertEqual(deriv_configuration(), "configured_untested")

    def test_provider_status_distinguishes_pending_failed_fresh_and_stale(self):
        with patch.object(self.market, "key", token_urlsafe(24)):
            self.assertEqual(self.market.connection_check(self.now)["status"], "waiting")
            self.market.checked[60] = self.now, False, "Geen candles"
            self.assertEqual(self.market.connection_check(self.now)["status"], "unavailable")
            self.market.cache[60] = self.now, [dict(time=self.now - 60, close=2700)]
            self.market.checked[60] = self.now, True, None
            self.assertEqual(self.market.connection_check(self.now)["status"], "connected")
            self.assertEqual(self.market.connection_check(self.now + 200)["status"], "stale")

    def test_demo_evidence_requires_matching_current_authentication(self):
        secret, proxy = token_urlsafe(32), token_urlsafe(32)
        identity = bridge_identity(secret, "demo-fixture", proxy, {"127.0.0.1"})
        self.assertIsNotNone(identity)
        self.db.execute("INSERT INTO bridge VALUES(1,?,?,?,?)", (self.now, 50000, "USD", identity))
        row = bridge_record(self.db, identity)
        self.assertEqual(bridge_check(row, identity, self.now)["status"], "verified_demo")
        self.assertEqual(bridge_check(row, identity, self.now + 91)["status"], "stale")
        self.assertEqual(bridge_check(row, identity, self.now - 1)["status"], "stale")
        for changed in [
            bridge_identity(token_urlsafe(32), "demo-fixture", proxy, {"127.0.0.1"}),
            bridge_identity(secret, "other-demo", proxy, {"127.0.0.1"}),
            bridge_identity(secret, "demo-fixture", token_urlsafe(32), {"127.0.0.1"}),
            bridge_identity(secret, "demo-fixture", proxy, {"127.0.0.2"}),
        ]:
            self.assertIsNone(bridge_record(self.db, changed))
            self.assertEqual(bridge_check(None, changed, self.now)["status"], "waiting")
        self.assertIsNone(bridge_identity(secret, "demo-fixture", secret, {"127.0.0.1"}))

    def test_existing_heartbeat_without_its_authentication_context_is_not_promoted(self):
        legacy = self.path.with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE bridge (id INTEGER PRIMARY KEY, seen INTEGER, balance REAL, currency TEXT)")
            db.execute("INSERT INTO bridge VALUES(1,?,?,?)", (self.now, 50000, "USD"))
        initialize(legacy)
        identity = bridge_identity(token_urlsafe(32), "demo-fixture", token_urlsafe(32), {"127.0.0.1"})
        with closing(connect(legacy)) as db:
            self.assertIsNone(bridge_record(db, identity))


if __name__ == "__main__":
    unittest.main()
