"""Verify paper execution, quote freshness and chronological model validation."""

import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import engine
import agent_coordinator as coordinator
import strategy
from advisor_journal import journal
from books import HOUR, SWING_MINUTES, rollup
from market import GOLD, Market
from spot import FeedError, decode_history, decode_spot, save_spot, saved_bars


class PaperMarketTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_039
        state_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "test.db"
        engine.initialize(state_path)
        self.db = self.enterContext(closing(engine.connect(state_path)))
        self.market = Market()
        self.state = engine.initial(self.now)
        self.positions = []

    def quote(self, minute, price, direction=None, received=True):
        ts = self.now + minute * 60
        self.market.spot = (ts, price)
        self.market.checked[GOLD] = ts, received, None if received else "Verzoeklimiet bereikt"
        if direction:
            moves = [0.5, 0.5, -0.3, -0.2] * 5
            p = price
            bars = []
            for i, delta in enumerate(moves):
                p += delta if direction == "long" else -delta
                bt = ts // 60 * 60 - (20 - i) * 60
                bars.append(dict(time=bt, open=p, high=p, low=p, close=p))
            self.market.spot_bars = bars
        return ts

    def run_engine(self, now):
        self.positions = engine.evaluate(self.db, "session", self.state, self.positions, self.market, now)

    def hours(self, now, side, gap=False):
        end = now // HOUR * HOUR
        candles = []
        for i in range(28):
            p = 3500 + (i - 27) * (2 if side == "long" else -2)
            candles.append(dict(time=end - (28 - i) * HOUR, open=p, high=p + 1.5, low=p - 1.5, close=p))
        if gap:
            candles.pop(-2)
        return candles

    def test_swing_long_and_short_are_properly_tagged_wider_and_close_at_target(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                sid = f"owner_{side}"
                ts = self.quote(0, 3500, side)
                state = engine.initial(ts)
                engine.save_strategies(self.db, sid, state, ts, dict(scalpEnabled=False, swingEnabled=True, scalpMinutes=15))
                with patch.object(self.market, "strategy_hours", return_value=self.hours(ts, side)):
                    positions = engine.apply_action(self.db, sid, state, [], self.market, ts, "start")
                self.assertEqual(len(positions), 1)
                trade = positions[0]
                self.assertEqual((trade["strategy"], trade["side"], trade["holdMinutes"]), ("swing", side, SWING_MINUTES))
                self.assertGreaterEqual(abs(trade["entry"] - trade["stop"]), 3500 * .004)
                self.assertTrue((trade["target"] > trade["entry"]) == (side == "long"))
                call = self.db.execute("SELECT made,target,hit FROM swing_calls WHERE trade=?", (trade["id"],)).fetchone()
                self.assertGreater(call["target"], call["made"])
                self.assertIsNone(call["hit"])
                # Turning the strategy off leaves its risk-managed position open.
                engine.save_strategies(self.db, sid, state, ts, dict(scalpEnabled=False, swingEnabled=False, scalpMinutes=60))
                future = call["target"] + HOUR + 1
                self.market.spot = (future, trade["target"])
                self.market.checked[GOLD] = (future, True, None)
                bar = dict(time=call["target"], open=3500, close=3501 if side == "long" else 3499)
                with patch.object(self.market, "strategy_hours", return_value=[bar]):
                    self.assertEqual(engine.evaluate(self.db, sid, state, positions, self.market, future), [])
                self.assertEqual(trade["reason"], "Koersdoel")
                self.assertGreater(trade["pnl"], 0)
                result = engine.dashboard(self.db, sid, state, self.market, future, None)
                self.assertEqual(result["strategyBooks"]["swing"]["validation"]["directionalSamples"], 1)
                self.assertEqual(result["strategyBooks"]["scalp"]["metrics"]["trades"], 0)
                self.assertEqual(result["strategyBooks"]["swing"]["metrics"]["trades"], 1)
                self.assertEqual(result["strategyBooks"]["combined"]["score"], 0)

    def test_books_share_capacity_and_cannot_hedge_against_each_other(self):
        ts = self.quote(0, 3500, "long")
        engine.save_strategies(self.db, "session", self.state, ts, dict(scalpEnabled=True, swingEnabled=True, scalpMinutes=5))
        self.state["settings"]["maxPositions"] = 2
        with patch.object(self.market, "strategy_hours", return_value=self.hours(ts, "short")):
            self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual([(t["strategy"], t["side"]) for t in self.positions], [("swing", "short")])
        self.assertIn("tegengestelde", self.state["bookDecisions"]["scalp"]["reason"])
        qty, error = engine.entry_room(self.state, self.positions, 3500, "short", 3.5, "scalp")
        self.assertGreater(qty, 0)
        self.assertIsNone(error)
        self.state["settings"]["maxPositions"] = 1
        self.assertEqual(engine.entry_room(self.state, self.positions, 3500, "short", 3.5, "scalp")[0], 0)

    def test_swing_missing_or_incomplete_candles_do_not_open_trades(self):
        ts = self.quote(0, 3500, "long")
        h1 = self.hours(ts, "long", gap=True)
        live = dict(time=ts // HOUR * HOUR, open=3500, high=5000, low=3500, close=5000)
        self.assertNotIn(live["time"], [b["time"] for b in rollup([*h1, live], HOUR, 4 * HOUR, ts)])
        engine.save_strategies(self.db, "session", self.state, ts, dict(scalpEnabled=False, swingEnabled=True, scalpMinutes=15))
        with patch.object(self.market, "strategy_hours", return_value=[*h1, live]):
            self.assertEqual(engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start"), [])
        self.assertEqual(self.state["bookDecisions"]["swing"]["action"], "WAIT")

    def test_pref_changes_do_not_rewrite_the_open_scalp_horizon(self):
        ts = self.quote(0, 3500, "long")
        engine.save_strategies(self.db, "session", self.state, ts, dict(scalpEnabled=True, swingEnabled=False, scalpMinutes=5))
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        trade = self.positions[0]
        self.assertEqual(trade["holdMinutes"], 5)
        engine.save_strategies(self.db, "session", self.state, ts, dict(scalpEnabled=False, swingEnabled=False, scalpMinutes=60))
        self.run_engine(self.quote(4, trade["entry"]))
        self.assertEqual(len(self.positions), 1)
        self.run_engine(self.quote(5, trade["entry"]))
        self.assertEqual(self.positions, [])
        self.assertEqual(trade["reason"], "Houdtijd verstreken")

    def start_external(self):
        self.market.agent_remote = True
        ts = self.quote(0, 3500, "long")
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual(self.positions, [])
        self.assertEqual(coordinator.activity(self.db, "session", GOLD, None)["phase"], "analysis")
        self.assertEqual(coordinator.activity(self.db, "another", GOLD, None)["phase"], "idle")
        return ts

    def post_agent(self, job, now, position="WAIT", target=None):
        return coordinator.submit(self.db, dict(id=job["id"], snapshotId=job["snapshot"]["id"], phase=job["phase"],
                                  position=position, target=target, text="Gecontroleerd marktargument."), now)

    def test_remote_mailbox_is_blind_until_review_and_trades_only_after_confirmation(self):
        ts = self.start_external()
        job = coordinator.task(self.db, ts+1)
        self.assertEqual(job["peers"], [])
        self.assertEqual(set(job["snapshot"]), {"id","symbol","provider","asOf","quoteTime","price","candles"})
        self.assertTrue(all(b["time"]+60 <= ts for b in job["snapshot"]["candles"]))
        self.assertTrue(self.post_agent(job, ts+1, "BUY"))
        self.assertFalse(self.post_agent(job, ts+1, "SELL"))
        review = coordinator.task(self.db, ts+2)
        self.assertEqual(job["snapshot"], review["snapshot"])
        self.assertEqual({p["agent"] for p in review["peers"]}, set(coordinator.DIRECTIONAL))
        self.run_engine(ts+3)
        self.assertEqual(self.positions, [])
        self.assertTrue(self.post_agent(review, ts+4, "REVIEW", "trend"))
        self.assertEqual(self.positions, [])
        self.run_engine(ts+5)
        self.assertEqual(len(self.positions), 1)
        r = journal(self.db, "session", GOLD, ts+5)["rounds"][0]["round"]
        self.assertEqual(r["snapshotId"], job["snapshot"]["id"])
        self.assertTrue(r["discussionComplete"])
        self.assertEqual(r["events"][-1]["position"], "BUY")
        ts = self.quote(1, 3509)
        self.run_engine(ts)
        settled = journal(self.db, "session", GOLD, ts)["rounds"][0]
        self.assertEqual(settled["outcome"]["reason"], "Koersdoel")
        self.assertEqual(settled["round"]["snapshotId"], job["snapshot"]["id"])

    def test_remote_timeout_never_fills_a_missing_vote(self):
        ts = self.start_external()
        job = coordinator.task(self.db, ts+1)
        self.assertFalse(self.post_agent(job, ts+coordinator.WINDOW))
        self.run_engine(ts+coordinator.WINDOW)
        self.assertEqual(self.positions, [])
        r = journal(self.db, "session", GOLD, ts+13)["rounds"][0]["round"]
        self.assertFalse(r["discussionComplete"])
        self.assertEqual(r["recommendation"], "WAIT")
        self.assertEqual([m["kind"] for m in r["events"] if m["agent"] == "remote"], ["analyzing", "unavailable"])

    def test_remote_objection_and_stop_are_binding(self):
        ts = self.start_external()
        self.assertTrue(self.post_agent(coordinator.task(self.db, ts+1), ts+1))
        job = coordinator.task(self.db, ts+2)
        self.assertTrue(self.post_agent(job, ts+2, "CHALLENGE", "trend"))
        self.run_engine(ts+4)
        self.assertEqual(self.positions, [])
        r = journal(self.db, "session", GOLD, ts+4)["rounds"][0]["round"]
        self.assertEqual(r["recommendation"], "WAIT")
        self.assertTrue(any(m["agent"] == "remote" and m["kind"] == "objection" for m in r["events"]))
        ts = self.quote(1, 3500, "long")
        self.run_engine(ts)
        old = coordinator.task(self.db, ts)
        engine.apply_action(self.db, "session", self.state, [], self.market, ts+1, "stop")
        self.assertFalse(self.post_agent(old, ts+2))
        canceled = journal(self.db, "session", GOLD, ts+2)["rounds"][0]["round"]
        self.assertIn("stopte", canceled["reason"])

    def test_provider_outage_or_snapshot_change_invalidates_remote_reports(self):
        ts = self.start_external()
        job = coordinator.task(self.db, ts+1)
        self.assertTrue(self.post_agent(job, ts+1, "BUY"))
        self.assertTrue(self.post_agent(coordinator.task(self.db, ts+2), ts+2, "REVIEW", "trend"))
        ts = self.quote(1, 3500, "long")  # The accepted review is not an order or permission to fill a different snapshot.
        self.run_engine(ts)
        self.assertEqual(self.positions, [])
        self.assertFalse(journal(self.db, "session", GOLD, ts)["rounds"][0]["round"]["executed"])
        ts = self.quote(2, 3500, "long")
        self.run_engine(ts)
        self.market.checked[GOLD] = (ts+1, False, "Netwerk")
        self.run_engine(ts+1)
        self.assertIsNone(coordinator.pending(self.db, "session", GOLD))
        self.assertIn("niet actueel", journal(self.db, "session", GOLD, ts+1)["rounds"][0]["round"]["reason"])

    def test_start_wait_long_close_and_costs_with_real_adapter_shape(self):
        ts = self.quote(0, 3500)
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual(self.state["decision"]["action"], "WAIT")
        ts = self.quote(1, 3501, "long")
        self.run_engine(ts)
        self.assertEqual(self.state["decision"]["action"], "BUY")
        self.assertEqual(len(self.positions), 1)
        trade = self.positions[0]
        paper_log = journal(self.db, "session", GOLD, ts)
        self.assertEqual(paper_log["rounds"][0]["round"]["id"], trade["advisorRoundId"])
        scored = self.db.execute("SELECT count(*) FROM advisor_votes WHERE account='session' AND side='BUY'").fetchone()[0]
        self.assertGreaterEqual(scored, 2)  # every supporting bot's vote is stored for its 5-minute score
        self.assertIsNone(paper_log["rounds"][0]["outcome"])
        self.assertEqual(trade["entryQuoteAt"], ts)
        self.assertLess(engine.pnl(trade, 3501), 0)
        self.run_engine(ts + 12)
        self.assertEqual(len(self.positions), 1)
        ts = self.quote(2, 3509)
        self.run_engine(ts)
        self.assertEqual(self.state["decision"]["action"], "CLOSE")
        self.assertEqual(self.positions, [])
        self.assertAlmostEqual(self.state["balance"] - strategy.START_BALANCE, engine.pnl(trade, 3509))
        paper_log = journal(self.db, "session", GOLD, ts)
        settled = next(r for r in paper_log["rounds"] if r["round"]["id"] == trade["advisorRoundId"])
        self.assertEqual(settled["outcome"]["pnl"], trade["pnl"])
        self.assertEqual(settled["outcome"]["exitQuoteAt"], ts)
        self.assertTrue(settled["outcome"]["comparable"])
        self.assertIn("momentum", {s["agent"] for s in paper_log["stats"]})
        self.assertEqual(journal(self.db, "other", GOLD, ts, trade["advisorRoundId"])["rounds"], [])
        self.assertEqual(engine.chart_trades(self.db, "session", GOLD, trade["openedAt"], ts)[0]["id"], trade["id"])
        self.assertEqual(engine.chart_trades(self.db, "other", GOLD, trade["openedAt"], ts), [])
        self.assertEqual(engine.snapshot(self.db, "session", self.state["settings"], ts)["sampleSize"], 1)
        self.assertEqual(engine.snapshot(self.db, "session", self.state["settings"], ts)["score"], 0)

    def test_short_stop_during_outage_waits_for_fresh_quote(self):
        ts = self.quote(0, 3500, "short")
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual(self.state["decision"]["action"], "SELL")
        ts = self.quote(1, 3490, received=False)
        self.run_engine(ts)
        self.assertEqual(self.state["decision"]["action"], "WAIT")
        self.positions = engine.apply_action(self.db, "session", self.state, self.positions, self.market, ts, "stop")
        self.assertEqual(len(self.positions), 1)
        self.assertEqual(self.state["engine"], "stopped")
        ts = self.quote(2, 3495)
        self.run_engine(ts)
        self.assertEqual(self.positions, [])
        self.assertEqual(self.state["decision"]["action"], "CLOSE")

    def test_bearish_short_profits_after_slippage_and_loses_on_adverse_move(self):
        ts = self.quote(0, 3500, "short")
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual(self.state["decision"]["action"], "SELL")
        trade = self.positions[0]
        self.assertEqual((trade["side"], trade["regime"]), ("short", "bearish"))
        self.assertGreater(trade["stop"], trade["entry"])
        self.assertLess(trade["target"], trade["entry"])
        self.assertEqual(engine.pnl(trade, 3500), -trade["costUsd"])
        self.assertLess(engine.pnl(trade, 3500.60), 0)
        self.assertLess(engine.pnl(trade, 3499.40), 0)  # favorable move still smaller than costs
        ts = self.quote(1, 3490)
        self.run_engine(ts)
        self.assertEqual(self.state["decision"]["action"], "CLOSE")
        self.assertGreater(trade["pnl"], 0)
        self.assertAlmostEqual(trade["pnl"], round((10 - sum(engine.COSTS)) * trade["quantity"], 2))
        dashboard = engine.dashboard(self.db, "session", self.state, self.market, ts, None)
        self.assertEqual(dashboard["performance"]["long"]["trades"], 0)
        self.assertEqual(dashboard["performance"]["bearish"]["netPnl"], trade["pnl"])
        self.assertEqual(dashboard["performance"]["short"]["netPnl"], trade["pnl"])
        self.assertFalse(dashboard["connections"]["liveTrading"])
        ts = self.quote(5, 3500, "short")
        self.run_engine(ts)
        losing = self.positions[0]
        self.run_engine(self.quote(6, 3510))
        self.assertEqual(losing["reason"], "Stop-loss")
        self.assertLess(losing["pnl"], 0)

    def test_sideways_market_waits_with_no_paper_position(self):
        ts = self.quote(0, 3500, "long")
        for i, bar in enumerate(self.market.spot_bars):
            mid = 3500 + [.25, 0, -.25][i % 3]
            bar.update(open=mid, high=mid, low=mid, close=mid)
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual(self.positions, [])
        self.assertEqual(self.state["decision"]["action"], "WAIT")
        self.assertIn("Zijwaartse", self.state["decision"]["reason"])

    def test_ingestion_never_invents_gaps_and_closed_history_is_filtered(self):
        base = self.now // 3600 * 3600
        for ts, price in [(base, 3500), (base + 30, 3501), (base + 120, 3499)]:
            save_spot(self.db, ts, price, self.now)
        bars = saved_bars(self.db, base)
        self.assertEqual([(b["time"], b["close"]) for b in bars], [(base, 3501), (base + 120, 3499)])
        self.assertEqual((bars[0]["open"], bars[0]["high"]), (3500, 3501))
        points = [dict(t=t, o=3501, h=3503, l=3500, c=3502) for t in (base - 3600, base - 123, base, base + 3600)]
        self.assertEqual([b["time"] for b in decode_history(dict(symbol="xau", currency="USD", interval="1h", points=points), self.now)],
                         [base - 3600])
        spot = dict(symbol="XAU", currency="USD", price=3501,
                    updatedAt=datetime.fromtimestamp(self.now + 10, timezone.utc).isoformat())
        with self.assertRaises(FeedError):
            decode_spot(spot, self.now)

    def test_learning_selection_never_fits_on_held_out_blocks(self):
        end = strategy.WARMUP + strategy.FIT + 2 * strategy.HOLDOUT
        values = tuple((i * 60, 3500, 3510) for i in range(end))
        self.assertEqual(strategy.learned(values[:-1])[1]["state"], "baseline")
        strategy.learned.cache_clear()
        self.addCleanup(strategy.learned.cache_clear)
        split = end - 2 * strategy.HOLDOUT
        with patch.object(strategy, "choose", return_value=(3, 10)) as fit, patch.object(
            strategy, "outcomes", side_effect=lambda _, start, *args: [1] * 20 if start == split else [-1] * 20
        ):
            model, report = strategy.learned(values)
            self.assertEqual(fit.call_args.args[2], split)
            self.assertEqual(model, strategy.BASELINE)
            self.assertEqual(report["state"], "tested")

    def test_veto_overrides_real_direction_without_preventing_close(self):
        ts = self.quote(0, 3500, "long")
        with patch.object(engine, "COSTS", (6, 0.10)):
            self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        round_ = journal(self.db, "session", GOLD, ts)["rounds"][0]["round"]
        self.assertEqual((round_["proposed"], round_["recommendation"], round_["executed"]), ("BUY", "WAIT", False))
        self.assertGreaterEqual(round_["buy"], 2)
        self.assertEqual(next(v for v in round_["votes"] if v["agent"] == "risk")["vote"], "VETO")
        self.assertEqual(self.positions, [])
        # A fresh quote inside the already processed candle cannot reopen the vetoed entry.
        self.market.spot = (ts + 15, 3501)
        self.run_engine(ts + 15)
        self.assertEqual(self.positions, [])

    def test_open_position_still_closes_when_new_entries_are_vetoed(self):
        ts = self.quote(0, 3500, "long")
        self.positions = engine.apply_action(self.db, "session", self.state, [], self.market, ts, "start")
        self.assertEqual(len(self.positions), 1)
        with patch.object(engine, "COSTS", (6, 0.10)):
            ts = self.quote(1, 3490)
            self.run_engine(ts)
        self.assertEqual(self.positions, [])
        self.assertEqual(self.state["decision"]["action"], "CLOSE")


if __name__ == "__main__":
    unittest.main()
