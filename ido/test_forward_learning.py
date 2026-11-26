"""Causality, paired validation and promotion using isolated fixtures."""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import engine
import forward_learning as study
from advisors import Bar, Risk, causal_bars
from candle_features import describe
from market import GOLD, SIM, Market


class ForwardLearningTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_038
        self.db_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "learning.db"
        engine.initialize(self.db_file)
        self.db = self.enterContext(closing(engine.connect(self.db_file)))
        self.settings = engine.DEFAULT.copy()
        self.key = study.policy(self.settings, False)
        self.risk = Risk(True, True, True, True, True, .14, 3.5)
        last = self.now//60*60-60
        self.bars = tuple(Bar(last-60*(20-i), 1000, 1001, 999, 1000+(.1 if i%3 else -.1)) for i in range(21))

    def record(self):
        return study.save_call(self.db, "owner", self.key, GOLD, self.bars,
                               dict(fresh=True, time=self.now-1, price=1000), self.now, self.risk, self.settings, "basis")

    def test_inputs_are_frozen_before_target_opens_and_only_exact_later_candle_counts(self):
        target = self.record()
        self.assertGreater(target, self.now)
        before = self.db.execute("SELECT input,forecasts,outcome FROM forward_calls").fetchone()
        self.assertIsNone(before["outcome"])
        self.assertLessEqual(json.loads(before["input"])["features"]["candleTime"]+60, self.now-1)
        bar = dict(time=target, open=1000, high=1005, low=999, close=1004)
        self.record()  # rerunning cannot rewrite a published call
        study.settle(self.db, "owner", GOLD, dict(fresh=True, time=target+59, bars=[bar]), target+59)
        self.assertTrue(study.pending(self.db, "owner"))
        study.settle(self.db, "owner", GOLD, dict(fresh=True, time=target+60, bars=[bar]), target+60)
        after = self.db.execute("SELECT input,forecasts,outcome FROM forward_calls").fetchone()
        self.assertEqual(before["forecasts"], after["forecasts"])
        self.assertEqual(before["input"], after["input"])
        self.assertEqual(json.loads(after["outcome"])["candle"]["time"], target)
        self.assertEqual(study.report(self.db,"owner",self.settings,target+60,False)["forecasts"][0]["market"], "up")
        self.assertEqual(study.report(self.db,"other",self.settings,target+60,False)["forecasts"], [])

    def test_gap_expires_and_synthetic_bars_do_not_enter_learning(self):
        target = self.record()
        other = dict(time=target+60, open=1000, high=1010, low=1000, close=1005)
        study.settle(self.db, "owner", GOLD, dict(fresh=True, time=target+400, bars=[other]), target+400)
        r = study.report(self.db, "owner", self.settings, target+400, False)
        self.assertEqual(r["activeMetrics"]["evaluatedSignals"], 0)
        self.assertEqual(r["activeMetrics"]["missing"], 1)
        self.assertEqual(r["forecasts"][0]["market"], "missing")
        self.assertIsNone(study.save_call(self.db, "owner", self.key, SIM, self.bars,
                     dict(fresh=True,time=self.now,price=1000), self.now, self.risk, self.settings, "basis"))

    def test_features_exclude_future_and_match_patterns(self):
        ts = self.now//60*60
        candles = [dict(time=ts-120,open=1002,high=1003,low=998,close=1000),
                   dict(time=ts-60,open=999,high=1004,low=998,close=1003),
                   dict(time=ts,open=1003,high=1200,low=800,close=900)]
        f = describe(causal_bars(candles,self.now,self.now,True))
        self.assertEqual((f["engulfing"],f["body"],f["upperWick"],f["lowerWick"],f["resistance"]),("up",4,1,1,1003))
        pin = describe((Bar(ts,100.9,101.2,98.9,101.1),))
        self.assertEqual(pin["pinBar"], "up")
        self.assertTrue(pin["doji"])

    def test_spot_chart_never_mixes_ohlc_with_the_research_series(self):
        m = Market()
        ts = self.now//3600*3600-3600
        m.spot_bars = [dict(time=ts,open=3650,high=3652,low=3649,close=3651)]
        m.research_bars = [dict(time=ts,open=2001,high=2002,low=1999,close=2000)]
        chart = m.chart(GOLD, "30d", self.now)
        self.assertEqual(chart["candles"][0]["close"], 3651)
        self.assertEqual(m.analysis_bars(GOLD, self.now)[0]["close"], 2000)

    def sample(self, start, weeks=3, matched=False, risky=False):
        out = []
        for day in range(7*weeks):
            for minute in range(8):
                target = start + day*study.DAY + 60*(minute+10)
                change = 2 if minute < 3 else -2
                candidate = ("BUY" if change > 0 else "SELL") if minute != 0 else "SELL"
                if matched:
                    candidate = "BUY"
                forecasts = {key:dict(direction=direction,shadow=direction) for key,direction in
                             [("basis","BUY"),("pullback",candidate),("structuur","WAIT")]}
                b = dict(time=target,open=1000,close=1000+change, high=1004 if risky else max(1000,1000+change)+.1,
                         low=996 if risky else min(1000,1000+change)-.1)
                context = dict(cost=.2, distance=3.5, riskPct=.5, dailyLossPct=2, current="basis")
                out.append((target-40,target,context,forecasts,b))
        return out

    def save_settled(self, rows):
        for made,target,inputs,calls,candle in rows:
            self.db.execute("INSERT INTO forward_calls VALUES(?,?,?,?,?,?,?,?,?)",
                            ("owner",self.key,GOLD,target,made,json.dumps(inputs),json.dumps(calls),target+70,json.dumps({"candle":candle})))

    def test_gate_requires_unseen_sample_block_consistency_and_downside_control(self):
        start = self.now//study.WEEK*study.WEEK
        test = lambda rows: study.assess_gate(rows,"basis","pullback",start,self.settings)[0]
        sample = self.sample(start)
        self.assertEqual(test(sample[:100]), "insufficient")
        self.assertEqual(test(self.sample(start, matched=True)), "overfit")
        self.assertNotEqual(test(self.sample(start, risky=True)), "promoted")
        self.assertEqual(test(sample), "promoted")

    def test_candidate_is_frozen_and_old_data_cannot_promote_it(self):
        boundary = self.now//study.WEEK*study.WEEK
        self.save_settled(self.sample(boundary-study.WEEK,1))
        before = study.advance(self.db,"owner",self.key,self.settings,boundary+361,False)
        self.assertEqual((before["active"],before["candidate"],before["cycleFrom"]),("basis","pullback",boundary+study.WEEK))
        self.save_settled(self.sample(before["cycleFrom"]))
        middle = study.advance(self.db,"owner",self.key,self.settings,boundary+2*study.WEEK+361,False)
        self.assertEqual(middle["active"],"basis")
        after = study.advance(self.db,"owner",self.key,self.settings,boundary+4*study.WEEK+361,False)
        self.assertEqual((after["active"],after["state"]),("pullback","promoted"))
        self.assertIsNone(after["candidate"])
