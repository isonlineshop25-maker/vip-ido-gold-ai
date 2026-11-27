import sqlite3
import unittest

import advisor_scores as scores
from advisors import Bar, Risk, assess, bigtrend, breakout, candle, recommend, session, vote


def bar(t, o, h, l, c):
    return Bar(t, o, h, l, c)


class NewBotTests(unittest.TestCase):
    t = 1_800_000_000 // 86400 * 86400 + 14 * 3600   # 14:00 UTC: London + New York

    def flat(self, n=20, price=3500.0):
        return [bar(self.t - (n - i) * 60, price, price + .5, price - .5, price + (.1 if i % 2 else -.1)) for i in range(n)]

    def test_candle_reads_bullish_engulfing_at_support(self):
        bars = self.flat()
        bars[-2] = bar(bars[-2].time, 3500.0, 3500.1, 3499.3, 3499.4)
        bars[-1] = bar(bars[-1].time, 3499.3, 3500.9, 3499.2, 3500.8)
        self.assertEqual(candle(bars)["vote"], "BUY")

    def test_candle_doji_waits(self):
        bars = self.flat()
        bars[-1] = bar(bars[-1].time, 3500.0, 3501.0, 3499.0, 3500.05)
        self.assertEqual(candle(bars)["vote"], "WAIT")

    def test_breakout_above_channel(self):
        bars = self.flat()
        bars[-1] = bar(bars[-1].time, 3500.2, 3501.2, 3500.1, 3501.1)
        self.assertEqual(breakout(bars)["vote"], "BUY")

    def test_session_waits_in_asia(self):
        night = self.t - 12 * 3600
        bars = [bar(night - (20 - i) * 60, 3500 + i, 3500.5 + i, 3499.5 + i, 3500 + i) for i in range(20)]
        self.assertEqual(session(bars, night)["vote"], "WAIT")
        self.assertIn("Aziatische", session(bars, night)["evidence"])

    def test_bigtrend_needs_agreeing_hours(self):
        up = [dict(time=self.t - (13 - i) * 3600, open=3500 + 3 * i, high=3503 + 3 * i, low=3499 + 3 * i, close=3502 + 3 * i)
              for i in range(13)]
        self.assertEqual(bigtrend(up)["vote"], "BUY")
        self.assertEqual(bigtrend(up[:5])["vote"], "WAIT")
        self.assertEqual(bigtrend(None)["confidence"], "none")

    def test_weighted_votes_and_big_trend_block(self):
        base = [vote("risk", "PASS", "high", ""), vote("skeptic", "PASS", "high", "")]
        strong = [vote("trend", "BUY", "high", "", 1.5), vote("momentum", "BUY", "high", "", 1.0)]
        self.assertEqual(recommend("BUY", strong + base)[0], "BUY")
        weak = [vote("trend", "BUY", "high", "", 0.6), vote("momentum", "BUY", "high", "", 0.6)]
        self.assertEqual(recommend("BUY", weak + base)[0], "WAIT")
        self.assertEqual(recommend("BUY", weak + base, learning=True)[0], "BUY")   # paper learning: two bots are enough
        against = strong + [vote("bigtrend", "SELL", "medium", "", 0.2)]
        self.assertEqual(recommend("BUY", against + base)[0], "WAIT")

    def test_assess_applies_weights(self):
        bars = self.flat()
        report = assess(bars, "WAIT", Risk(True, True, True, True, True, .2), self.t, self.t,
                        dict(weights={"trend": 1.7}))
        self.assertEqual(next(v for v in report["votes"] if v["agent"] == "trend")["weight"], 1.7)
        self.assertNotIn("weight", {k for v in report["votes"] if v["agent"] == "risk" for k in v if v.get("weight") != 1.0})


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        scores.init_scores(self.db)
        self.t = 1_800_000_000 // 60 * 60

    def tearDown(self):
        self.db.close()

    def test_hits_must_beat_costs_and_shift_weight(self):
        for i in range(40):
            c = self.t + i * 60
            scores.record(self.db, "a", "gold", [vote("trend", "BUY", "high", ""), vote("momentum", "SELL", "high", ""),
                                                  vote("risk", "PASS", "high", "")], c, 3500.0, 0.7)
        bars = [dict(time=self.t + i * 60, open=3500, high=3502, low=3499, close=3501.0) for i in range(60)]
        scores.settle(self.db, "a", "gold", bars, self.t + 60 * 61, self.t + 60 * 61)
        w = scores.weights(self.db, "a", "gold", self.t + 60 * 61)
        self.assertGreater(w["trend"], 1.0)      # +1.00 move beats 0.70 costs
        self.assertLess(w["momentum"], 1.0)      # wrong direction
        self.assertEqual(w["candle"], 1.0)       # no votes yet: neutral
        self.assertEqual(self.db.execute("SELECT count(*) FROM advisor_votes WHERE agent='risk'").fetchone()[0], 0)

    def test_small_move_below_costs_is_a_miss(self):
        scores.record(self.db, "a", "gold", [vote("trend", "BUY", "high", "")], self.t, 3500.0, 0.7)
        bars = [dict(time=self.t + scores.HORIZON, open=3500, high=3501, low=3499, close=3500.5)]
        scores.settle(self.db, "a", "gold", bars, self.t + scores.HORIZON + 60, self.t + scores.HORIZON + 60)
        self.assertEqual(self.db.execute("SELECT hit FROM advisor_votes").fetchone()[0], 0)

    def test_missing_candle_is_skipped_not_guessed(self):
        scores.record(self.db, "a", "gold", [vote("trend", "BUY", "high", "")], self.t, 3500.0, 0.7)
        late = self.t + scores.HORIZON + 60 + scores.GRACE + 1
        scores.settle(self.db, "a", "gold", [], late, late)
        row = self.db.execute("SELECT evaluated, hit FROM advisor_votes").fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])

    def test_report_shape(self):
        r = scores.report(self.db, "a", "gold", self.t)
        self.assertEqual(len(r["bots"]), 7)
        self.assertEqual({g["agent"] for g in r["guards"]}, {"risk", "skeptic"})


if __name__ == "__main__":
    unittest.main()
