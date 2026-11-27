import unittest

from advisors import Bar, Risk, assess, causal_bars, recommend, vote


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.t = 1_800_000_000
        self.risk = Risk(True, True, True, True, True, 0.2)

    def candles(self):
        # Exaggerated upward candles: trend sees rising structure, momentum rejects RSI=100.
        return [dict(time=self.t - (20-i)*60, open=3500+i, high=3501.05+i,
                     low=3499.95+i, close=3501+i) for i in range(20)]

    def test_disagreement_is_not_hidden(self):
        bars = causal_bars(self.candles(), self.t, self.t, True)
        report = assess(bars, "WAIT", self.risk, self.t, self.t)
        self.assertEqual(len(report["votes"]), 11)
        self.assertEqual(report["votes"][0]["vote"], "BUY")
        self.assertEqual(report["votes"][1]["vote"], "WAIT")
        self.assertEqual(report["recommendation"], "WAIT")
        self.assertTrue(all(v["time"] == self.t and v["version"] for v in report["votes"]))

    def test_future_and_unfinished_candles_never_change_advice(self):
        prior = self.candles()
        added = [dict(prior[-1], time=self.t, close=10000), dict(prior[-1], time=self.t + 60, close=100)]
        a = causal_bars(prior, self.t+5, self.t, True)
        b = causal_bars(prior+added, self.t+5, self.t, True)
        self.assertEqual(a, b)
        self.assertEqual(assess(a, "BUY", self.risk, self.t, self.t), assess(b, "BUY", self.risk, self.t, self.t))
        self.assertLessEqual(b[-1].time + 60, self.t)

    def test_gap_is_not_filled_and_exposes_missing_data(self):
        candles = self.candles()
        bars = causal_bars(candles[:-4] + candles[-3:], self.t, self.t, True)
        self.assertEqual(len(bars), 3)
        report = assess(bars, "WAIT", self.risk, self.t, self.t)
        self.assertEqual(report["votes"][0]["confidence"], "none")
        self.assertIn("3/12", report["votes"][0]["evidence"])

    def test_reviewer_and_opposed_direction_can_block_majority(self):
        block = [vote("trend","BUY","high",""), vote("momentum","BUY","medium",""),
                 vote("volatility","WAIT","low",""), vote("risk","PASS","high",""), vote("skeptic","FLAG","medium","")]
        self.assertEqual(recommend("BUY", block)[0], "WAIT")
        block[-1] = vote("skeptic","FLAG","low","")
        self.assertEqual(recommend("BUY", block)[0], "BUY")
        block[2] = vote("volatility","SELL","medium","")
        self.assertEqual(recommend("BUY", block)[0], "WAIT")


class NewBotTests(unittest.TestCase):
    def bars(self, closes, wide=0.3):
        return tuple(Bar(1_800_000_000 + i * 60, c, c + wide, c - wide, c) for i, c in enumerate(closes))

    def test_spike_follows_a_strong_close_only(self):
        from advisors import spike, pullback
        calm = self.bars([3500 + (i % 2) * 0.2 for i in range(20)])
        self.assertEqual(spike(calm)["vote"], "WAIT")
        jump = list(calm[:-3]) + [Bar(1, 3500, 3503, 3499.9, 3502.9), Bar(2, 3502.9, 3506, 3502.8, 3505.9), Bar(3, 3505.9, 3509, 3505.8, 3508.9)]
        self.assertEqual(spike(tuple(jump))["vote"], "BUY")
        self.assertEqual(spike(calm[:5])["confidence"], "none")

    def test_pullback_buys_a_recovering_dip(self):
        from advisors import pullback
        closes = [3500 + i * 0.5 for i in range(17)] + [3507.0, 3506.0, 3507.5]
        self.assertEqual(pullback(self.bars(closes))["vote"], "BUY")
        flat = [3500.0] * 20
        self.assertEqual(pullback(self.bars(flat))["vote"], "WAIT")


class BigTrendTimingTests(unittest.TestCase):
    def test_practice_mode_waits_for_the_minute_chart(self):
        from advisors import bigtrend
        hours = [dict(time=i * 3600, open=3000 + i * 5, high=3000 + i * 5 + 6, low=3000 + i * 5 - 1, close=3000 + i * 5 + 5) for i in range(13)]
        bar = lambda i, c: Bar(1_800_000_000 + i * 60, c, c + .3, c - .3, c)
        falling = tuple(bar(i, 3100 - i * .5) for i in range(30))
        rising = tuple(bar(i, 3100 + i * .5) for i in range(30))
        self.assertEqual(bigtrend(hours)["vote"], "BUY")                  # strict: unchanged
        self.assertEqual(bigtrend(hours, rising)["vote"], "BUY")
        self.assertEqual(bigtrend(hours, falling)["vote"], "WAIT")
