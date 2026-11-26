import json
import sqlite3
import unittest

import readiness

T0 = 1_800_000_000


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE trades (id TEXT PRIMARY KEY, account TEXT, opened INTEGER, closed INTEGER, pnl REAL, data TEXT)")

    def tearDown(self):
        self.db.close()

    def add(self, i, t, pnl, feed="gold"):
        self.db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?)", (str(i), "a", t, t + 600, pnl, json.dumps({"feed": feed})))

    def test_empty_is_zero_and_not_ready(self):
        r = readiness.report(self.db, "a", "gold", T0)
        self.assertEqual(r["score"], 0)
        self.assertFalse(r["ready"])
        self.assertEqual(len(r["checks"]), 6)

    def test_few_lucky_trades_never_reach_100(self):
        for i in range(10):
            self.add(i, T0 + i * 3600, 50.0)
        r = readiness.report(self.db, "a", "gold", T0 + 2 * 86400)
        self.assertFalse(r["ready"])
        self.assertLessEqual(r["score"], 99)

    def test_steady_edge_over_weeks_reaches_100(self):
        for i in range(300):
            self.add(i, T0 + i * 3 * 3600, 12.0 if i % 3 else -10.0)   # 2 wins, 1 loss, steady
        now = T0 + 300 * 3 * 3600 + 8 * 86400
        r = readiness.report(self.db, "a", "gold", now)
        self.assertTrue(r["ready"], [c for c in r["checks"] if not c["met"]])
        self.assertEqual(r["score"], 100)

    def test_losing_bot_stays_low_and_other_feed_ignored(self):
        for i in range(300):
            self.add(i, T0 + i * 3 * 3600, -5.0 if i % 3 else 4.0)
            self.add(f"s{i}", T0 + i * 3 * 3600, 100.0, feed="simulation")
        r = readiness.report(self.db, "a", "gold", T0 + 300 * 3 * 3600 + 8 * 86400)
        self.assertFalse(r["ready"])
        self.assertFalse(next(c for c in r["checks"] if c["key"] == "profit")["met"])

    def test_announce_logs_once(self):
        logs, state = [], {}
        for i in range(300):
            self.add(i, T0 + i * 3 * 3600, 12.0 if i % 3 else -10.0)
        now = T0 + 300 * 3 * 3600 + 8 * 86400
        audit = lambda db, sid, t, level, msg: logs.append(msg)
        readiness.announce(self.db, "a", "gold", state, now, audit)
        readiness.announce(self.db, "a", "gold", state, now, audit)
        self.assertEqual(len(logs), 1)


if __name__ == "__main__":
    unittest.main()
