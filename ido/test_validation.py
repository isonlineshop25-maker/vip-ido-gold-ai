"""Fixtures exercise the scoring gates without seeding the application."""

import tempfile
import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import validation as v
import forward_learning as learning
from engine import DEFAULT, connect, initialize
from market import REAL, SIM


class ValidationTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        path = Path(state.name) / "sample.sqlite3"
        initialize(path)
        self.db = self.enterContext(closing(connect(path)))
        self.enterContext(patch.object(v, "COSTS", (0.35, 0.15, 0.10)))
        self.opened = 2800 * v.WEEK + 3600
        self.trade = dict(id="trial", feed=REAL, entry=2700.0, stop=2697.0, quantity=2.0,
                          side="long", openedAt=self.opened, closedAt=self.opened + 120, pnl=999)
        self.bars = [dict(time=self.opened - 960 + i * 60, close=2698 + i / 10) for i in range(16)]

    def create_trial(self, feed=REAL, fresh=True, newer=True):
        trade = self.trade | {"feed": feed}
        quote = dict(price=2700.0, time=self.opened, fresh=True)
        v.record_entry(self.db, "owner", DEFAULT, trade, self.bars, quote, 50000)
        v.record_exit(self.db, "owner", trade, dict(price=2701.5, time=self.opened + (60 if newer else 0), fresh=fresh))

    def test_empty_and_synthetic_are_zero(self):
        self.create_trial(feed=SIM)
        report = v.snapshot(self.db, "owner", DEFAULT, self.opened)
        self.assertEqual((report["score"], report["sampleSize"], report["status"]), (0, 0, "none"))
        self.assertIsNone(report["updatedAt"])

    def test_costs_are_independent_and_small_sample_cannot_advance(self):
        self.create_trial()
        report = v.snapshot(self.db, "owner", DEFAULT, self.opened + 120)
        self.assertEqual((report["score"], report["status"], report["sampleSize"]), (0, "insufficient", 1))
        self.assertEqual(report["expectancyUsd"], 1.8)  # $3 gross less spread, fees and slippage
        self.assertEqual(report["updatedAt"], self.trade["closedAt"])
        self.assertEqual(v.snapshot(self.db, "other", DEFAULT, self.opened)["sampleSize"], 0)
        self.assertEqual(v.snapshot(self.db, "owner", DEFAULT | {"riskPct": 1}, self.opened)["sampleSize"], 0)

    def test_stale_exit_is_excluded(self):
        self.create_trial(fresh=False)
        report = v.snapshot(self.db, "owner", DEFAULT, self.opened + 120)
        self.assertEqual((report["score"], report["sampleSize"], report["excluded"]), (0, 0, 1))

    def test_same_quote_exit_is_excluded(self):
        self.create_trial(newer=False)
        self.assertEqual(v.snapshot(self.db, "owner", DEFAULT, self.opened + 120)["excluded"], 1)

    def test_missing_cost_assumptions_are_not_zero_cost(self):
        with patch.object(v, "COSTS", None):
            self.create_trial()
            report = v.snapshot(self.db, "owner", DEFAULT, self.opened + 120)
            self.assertFalse(report["costModelConfigured"])
            self.assertEqual((report["sampleSize"], report["excluded"]), (0, 1))

    def sample(self, weeks, unstable=False, one_regime=False):
        evidence = []
        regimes = ("bullish_low", "bullish_high", "bearish_low", "bearish_high")
        for week in range(weeks):
            for index in range(16):
                opened = self.opened + week * v.WEEK + index * 2700
                r = -0.5 if index % 4 == 0 else 0.55
                if unstable and week % 2 == 0:
                    r = -0.95
                # Every condition receives winning and losing trades.
                regime = regimes[(index // 4 + week) % 4] if not one_regime else regimes[0]
                outcome = dict(r=r, net=r * 40, regime=regime,
                               side="long" if regime.startswith("bullish") else "short",
                               equity=50000, beyondStop=False)
                evidence.append((opened, opened + 600, outcome))
        return evidence

    def score(self, evidence, excluded=0, age=0, forecasts=None):
        if forecasts is None:
            forecasts = [i % 4 != 0 for i in range(len(evidence))]
        last = evidence[-1][1]
        observations = [(t[1], hit) for t, hit in zip(evidence, forecasts)]
        return v.score_trials(dict(score=0, excluded=excluded), evidence, DEFAULT, last + age,
                              observations, [last] * excluded)

    def test_uses_only_the_frozen_forecast_and_its_unseen_nonflat_candle(self):
        self.create_trial()
        target = self.opened + 60
        key = learning.policy(DEFAULT, False)
        learning.audit_decision(self.db, "owner", DEFAULT["provider"], self.opened, "BUY",
                                "Paperinstap", {}, self.trade["id"], target)
        learning.audit_decision(self.db, "owner", DEFAULT["provider"], self.opened, "BUY",
                                "Zelfde voorspelling", {}, self.trade["id"], target)
        self.db.execute("INSERT INTO forward_calls VALUES(?,?,?,?,?,?,?,?,?)",
                        ("owner", key, DEFAULT["provider"], target, self.opened - 10,
                         json.dumps({"current": "basis"}), json.dumps({"basis": {"direction": "BUY"}}),
                         target + 80, json.dumps({"candle": {"time": target, "open": 2701, "close": 2700}})))
        read = lambda now=self.opened + 180: v.snapshot(self.db, "owner", DEFAULT, now, forecast_profile=key)
        self.assertEqual(read(now=target + 60)["directionalSamples"], 0)
        self.assertEqual((read()["directionalSamples"], read()["directionalAccuracy"], read()["updatedAt"]),
                         (1, 0, target + 80))
        self.assertGreater(read()["expectancyUsd"], 0)  # a profitable fill does not make the forecast correct
        self.db.execute("UPDATE forward_calls SET outcome=?", (json.dumps({"candle": {"time": target, "open": 2701, "close": 2701}}),))
        self.assertIsNone(read()["directionalAccuracy"])
        self.db.execute("UPDATE forward_calls SET made=?,outcome=?",
                        (target + 1, json.dumps({"candle": {"time": target, "open": 2701, "close": 2702}})))
        self.assertEqual(read()["directionalSamples"], 0)

    def test_missing_or_weak_direction_evidence_cannot_get_a_high_score(self):
        evidence = self.sample(19)
        self.assertEqual(self.score(evidence, forecasts=[True] * 19)["score"], 0)
        weak = self.score(evidence, forecasts=[True, False, False] * 90)
        self.assertLessEqual(weak["score"], 49)
        self.assertLessEqual(self.score(evidence, forecasts=[True] * 75 + [False] * 75)["score"], 89)
        self.assertLess(self.score(evidence, forecasts=[True, False] * 100 + [True] * 24)["score"], 100)

    def test_strong_requires_long_and_consistent_evidence(self):
        self.assertLess(self.score(self.sample(10, unstable=True))["score"], 70)
        self.assertEqual(self.score(self.sample(10, one_regime=True))["score"], 0)
        self.assertLess(self.score(self.sample(10), age=31 * v.DAY)["score"], 70)
        early = self.score(self.sample(10))
        self.assertGreaterEqual(early["evidenceCeiling"], 90)
        self.assertLess(early["score"], 90)
        strong = self.score(self.sample(15))
        self.assertEqual(strong["status"], "strong")
        self.assertTrue(90 <= strong["score"] < 100)

    def test_one_profitable_day_cannot_advance(self):
        last_day = self.sample(1) * 20
        self.assertEqual(self.score(last_day)["score"], 0)

    def test_rigorous_threshold_and_exclusion_gate(self):
        evidence = self.sample(19)
        rigorous = self.score(evidence)
        self.assertEqual((rigorous["score"], rigorous["status"]), (100, "rigorous"))
        self.assertLess(self.score(evidence, excluded=1)["score"], 100)
        self.assertLess(self.score(evidence, excluded=50)["score"], 90)

    def test_each_profit_can_only_earn_one_point_and_only_while_supported(self):
        trades = self.sample(19)
        prev, progressed, fell = 0, False, False
        for n, (_, _, result) in enumerate(trades, 1):
            report = self.score(trades[:n])
            step = report["score"] - prev
            self.assertLessEqual(step, 1 if result["net"] > 0 else 0)
            self.assertLessEqual(report["score"], report["evidenceCeiling"])
            self.assertEqual(report["lastStep"], step)
            progressed |= step > 0
            fell |= step < 0 and result["net"] < 0
            prev = report["score"]
        self.assertTrue(progressed)
        self.assertTrue(fell)

    def test_late_forecast_alone_does_not_fill_the_bar_and_staleness_can_lower_it(self):
        trades = self.sample(19)
        last = trades[-1][1]
        scores = [(closed, idx % 4 != 0) for idx, (_, closed, _) in enumerate(trades)]
        read = lambda now, observations: v.score_trials(dict(excluded=0), trades, DEFAULT, now, observations)
        baseline = read(last, scores)
        later = read(last + 60, scores + [(last + 60, True)])
        self.assertLessEqual(later["score"], baseline["score"])
        self.assertEqual(later["score"], read(last + v.DAY, scores + [(last + 60, True)])["score"])
        old = read(last + 31 * v.DAY, scores)
        self.assertLess(old["score"], baseline["score"])
        self.assertLessEqual(old["score"], 69)

    def test_history_keeps_only_changed_calculations_and_marks_causes(self):
        report = dict(score=0, evidenceCeiling=0, sampleSize=0, directionalSamples=0,
                      excluded=0, status="none", updatedAt=None, periodTo=None)
        self.assertTrue(v.record_history(self.db, "owner", "scalp", report, "profile-a", "Kortetermijn · 15 min", self.opened))
        self.assertFalse(v.record_history(self.db, "owner", "scalp", report, "profile-a", "Kortetermijn · 15 min", self.opened + 5))

        added = report | dict(score=1, evidenceCeiling=3, sampleSize=1, directionalSamples=1,
                              status="insufficient", updatedAt=self.opened + 60, periodTo=self.opened + 60)
        self.assertTrue(v.record_history(self.db, "owner", "scalp", added, "profile-a", "Kortetermijn · 15 min", self.opened + 70))
        expired = report | dict(updatedAt=self.opened + 60, periodTo=self.opened + 60)
        self.assertTrue(v.record_history(self.db, "owner", "scalp", expired, "profile-a", "Kortetermijn · 15 min",
                                         self.opened + 31 * v.DAY))
        changed = report | dict(score=2, status="insufficient")
        self.assertTrue(v.record_history(self.db, "owner", "scalp", changed, "profile-b", "Kortetermijn · 30 min",
                                         self.opened + 32 * v.DAY))

        history = v.history(self.db, "owner", "scalp")
        self.assertEqual([point["cause"] for point in history["points"]],
                         ["initial", "new_evidence", "evidence_expired", "strategy_changed"])
        self.assertEqual([point["score"] for point in history["points"]], [0, 1, 0, 2])
        self.assertEqual([point["series"] for point in history["points"]], [1, 1, 1, 2])
        self.assertEqual(v.history(self.db, "other", "scalp")["points"], [])

    def test_large_long_wins_cannot_cover_an_unprofitable_short_side(self):
        trades = [(opened, closed, dict(result, net=-8, r=-.2) if result["side"] == "short" else result)
                  for opened, closed, result in self.sample(19)]
        report = self.score(trades)
        self.assertGreater(report["expectancyUsd"], 0)
        self.assertGreater(report["shortSampleSize"], 60)
        self.assertLess(report["shortExpectancyR"], 0)
        self.assertEqual(report["score"], 0)


if __name__ == "__main__":
    unittest.main()
