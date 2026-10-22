"""Offline tests for the paper-only market scanner. Run: python test_sphere.py"""
import csv
import io
import math
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import marketdata  # noqa: E402
import paper  # noqa: E402
import scoring  # noqa: E402
import sphere  # noqa: E402

CFG = sphere.load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), None)


def make_candles(n, start="2024-01-01", base=100.0, drift=0.002, wave=0.03, volume=1000.0, step=1):
    d0 = date.fromisoformat(start)
    out, prev = [], base
    for i in range(n):
        close = base * math.exp(drift * i) * (1 + wave * math.sin(i / 7))
        out.append(((d0 + timedelta(days=i * step)).isoformat(), prev, max(prev, close) * 1.01,
                    min(prev, close) * 0.99, close, volume))
        prev = close
    return out


def cfg(**over):
    c = dict(CFG)
    c.update(over)
    return c


def score(symbol, value, direction="LONG", close=100.0, stop=90.0):
    return {"symbol": symbol, "score": value, "direction": direction, "close": close, "stop": stop, "why": "test"}


def candle(day, o, h, l, c):
    return (day, o, h, l, c, 1.0)


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, day, symbol, text):
        self.events.append((kind, day, symbol, text))

    def kinds(self):
        return [e[0] for e in self.events]


class DataTests(unittest.TestCase):
    def test_coinbase_columns(self):
        page = [[1792454400, 1, 4, 2, 3, 9]]
        rows = marketdata.fetch_coinbase("BTC-USD", 300, 1792540800, get=lambda url: page)
        self.assertEqual(rows, {"2026-10-20": (2.0, 4.0, 1.0, 3.0, 9.0)})

    def test_yahoo_dates_in_exchange_time_and_skips_gaps(self):
        payload = {"chart": {"error": None, "result": [{
            "meta": {"gmtoffset": -14400},
            "timestamp": [1792468800, 1792555200],  # 2026-10-20 and 2026-10-21 at 04:00 UTC (midnight New York)
            "indicators": {"quote": [{"open": [10, None], "high": [12, 13], "low": [9, 10], "close": [11, 12],
                                      "volume": [None, 5]}]}}]}}
        rows = marketdata.fetch_yahoo("GC=F", 730, 0, get=lambda url: payload)
        self.assertEqual(rows, {"2026-10-20": (10.0, 12.0, 9.0, 11.0, 0.0)})

    def test_get_candles_falls_back_and_stops_at_settled_date(self):
        def broken(*a):
            raise OSError("blocked")

        rows = {"2026-10-20": (1, 2, 0.5, 1.5, 1), "2026-10-21": (1.5, 2, 1, 1.8, 1)}
        candles, source = marketdata.get_candles({"symbol": "BTC-USD", "source": "coinbase"}, 10, 0, "2026-10-20",
                                                 {"coinbase": broken, "yahoo": lambda *a: rows})
        self.assertEqual((candles, source), ([("2026-10-20", 1, 2, 0.5, 1.5, 1)], "yahoo"))

    def test_orderflow_reads_maker_side_correctly(self):
        book = {"bids": [["100.0", "5", 1], ["99.8", "1", 1]], "asks": [["100.2", "1", 1], ["100.4", "1", 1]]}
        trades = [{"side": "sell", "size": "3"}, {"side": "buy", "size": "1"}]
        flow = marketdata.coinbase_orderflow("BTC-USD", 0.5, 2, get=lambda url: book if "book" in url else trades)
        self.assertAlmostEqual(flow["imbalance"], 0.5)
        self.assertAlmostEqual(flow["aggressive_buy_ratio"], 0.75)
        self.assertEqual((flow["score"], flow["bias"], flow["bid_wall"]), (75, "BULLISH", (100.0, 5.0)))

    def test_downloads_retry_dropped_connections(self):
        calls, pauses = [], []

        def flaky(req, timeout):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise marketdata.http.client.IncompleteRead(b"")
            return io.BytesIO(b"[1, 2]")

        with mock.patch.object(marketdata.urllib.request, "urlopen", flaky):
            self.assertEqual(marketdata.http_get_json("https://x", sleep=pauses.append), [1, 2])
            self.assertEqual((len(calls), pauses), (3, [2.0, 4.0]))
            calls.clear()
            with self.assertRaises(marketdata.http.client.IncompleteRead):
                marketdata.http_get_json("https://x", tries=2, sleep=pauses.append)

    def test_second_snapshot_spots_pulled_walls_and_thinning_liquidity(self):
        trades = [{"side": "sell", "size": "1"}]
        book1 = {"bids": [["100.0", "5", 1], ["99.8", "1", 1]], "asks": [["100.2", "4", 1]]}
        book2 = {"bids": [["100.0", "1", 1], ["99.8", "1", 1]], "asks": [["100.2", "4", 1]]}
        first = marketdata.coinbase_orderflow("BTC-USD", 0.5, 1, get=lambda url: book1 if "book" in url else trades)
        self.assertEqual(first["liquidity"], "single snapshot")
        second = marketdata.coinbase_orderflow("BTC-USD", 0.5, 1, first,
                                               get=lambda url: book2 if "book" in url else trades)
        self.assertEqual((second["bid_wall_held"], second["ask_wall_held"]), (False, True))
        self.assertTrue(second["liquidity"].startswith("disappearing"))
        self.assertIn("bid wall at 100 was pulled", second["main_risk"])
        self.assertIn("PULLED", sphere.describe_flow(second))

    def test_live_context_reads_order_flow_twice_and_waits_in_between(self):
        c = cfg(markets=[{"symbol": "BTC-USD", "name": "Bitcoin", "source": "coinbase"},
                         {"symbol": "GC=F", "name": "Gold", "source": "yahoo"}])
        calls, pauses = [], []

        def flow(sym, depth, n, earlier=None):
            calls.append((sym, earlier))
            return {"score": 70 if earlier else 50, "bias": "BULLISH" if earlier else "NEUTRAL"}

        context = sphere.live_context(c, flow, sleep=pauses.append)
        self.assertEqual(pauses, [15])
        self.assertEqual([sym for sym, _ in calls], ["BTC-USD", "BTC-USD"])
        self.assertEqual(context["BTC-USD"]["orderflow"], "70 BULLISH")
        self.assertNotIn("orderflow", context["GC=F"])
        self.assertEqual(context["GC=F"]["gamma"], sphere.NO_GAMMA)

    def test_settled_date_waits_for_new_york_midnight(self):
        self.assertEqual(sphere.settled_date(1792731540, 5), "2026-10-21")  # 2026-10-23 04:59 UTC
        self.assertEqual(sphere.settled_date(1792731600, 5), "2026-10-22")  # 2026-10-23 05:00 UTC


class ScoringTests(unittest.TestCase):
    def test_indicator_basics(self):
        self.assertEqual(scoring.ema([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])
        self.assertEqual(scoring.rolling_mean([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(scoring.rolling_pct_rank([3, 1, 2], 3, 2), [None, 0.5, 2 / 3])

    def test_uptrend_scores_long_and_downtrend_scores_short(self):
        for drift, direction in ((0.003, "LONG"), (-0.003, "SHORT")):
            s = scoring.build_series(make_candles(400, drift=drift))
            result = scoring.score_at(s, 399, CFG["weights"], 2.0, 2.0)
            self.assertEqual(result["direction"], direction)
            self.assertGreater(result["parts"]["trend"], 0.6)
            self.assertTrue(0 <= result["score"] <= 1)
            sign = 1 if direction == "LONG" else -1
            self.assertAlmostEqual(result["stop"], result["close"] - sign * 2 * result["atr"])
            self.assertAlmostEqual(result["target"], result["close"] + sign * 4 * result["atr"])

    def test_not_enough_history(self):
        s = scoring.build_series(make_candles(150))
        self.assertIsNone(scoring.score_at(s, 149, CFG["weights"], 2.0, 2.0))

    def test_missing_volume_counts_as_zero(self):
        with_volume = scoring.score_at(scoring.build_series(make_candles(400)), 399, CFG["weights"], 2.0, 2.0)
        without = scoring.score_at(scoring.build_series(make_candles(400, volume=0.0)), 399, CFG["weights"], 2.0, 2.0)
        self.assertEqual(without["missing"], ["volume"])
        self.assertIn("missing volume (scored 0)", without["why"])
        self.assertAlmostEqual(with_volume["score"] - without["score"],
                               CFG["weights"]["volume"] * with_volume["parts"]["volume"], places=3)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.log = Recorder()
        self.engine = paper.PaperEngine(cfg(), log=self.log)

    def test_only_the_top_market_above_threshold_gets_a_setup(self):
        self.engine.step("2026-01-01", {}, {"A": score("A", 0.9), "B": score("B", 0.95, stop=80)})
        self.assertEqual(self.engine.state["pending"]["symbol"], "B")
        low = paper.PaperEngine(cfg(), log=self.log)
        low.step("2026-01-01", {}, {"A": score("A", 0.69)})
        self.assertIsNone(low.state["pending"])
        self.assertIn("below the 0.70 threshold", self.log.events[-1][3])

    def test_fill_next_open_with_one_percent_risk_including_costs(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 101, 99.5, 100.5)}, {})
        pos = e.state["position"]
        self.assertAlmostEqual(pos["entry"], 100.05)
        self.assertAlmostEqual(pos["risk"], 100.0)
        self.assertAlmostEqual(pos["target"], 100.05 + 2 * 10.05)
        self.assertEqual(self.log.kinds()[:2], ["SETUP", "FILL"])

    def test_no_leverage_cap(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9, stop=99.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 100.2, 99.95, 100.1)}, {})
        pos = e.state["position"]
        self.assertLessEqual(pos["units"] * pos["entry"], 10000 + 1e-6)
        self.assertLess(pos["risk"], 100.0)

    def test_stop_wins_when_stop_and_target_hit_same_candle(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 130, 85, 100)}, {})
        trade = e.state["closed"][0]
        self.assertEqual(trade["reason"], "stop")
        self.assertAlmostEqual(trade["r"], -1.0, places=6)
        self.assertAlmostEqual(e.state["equity"], 9900.0, places=6)

    def test_target_pays_about_two_r_after_costs(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 101, 99, 100)}, {})
        e.step("2026-01-03", {"A": candle("2026-01-03", 101, 125, 100, 121)}, {})
        trade = e.state["closed"][0]
        self.assertEqual((trade["reason"], trade["result"]), ("target", "WIN"))
        self.assertTrue(1.85 < trade["r"] < 2.0)

    def test_short_side_mirrors(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9, direction="SHORT", stop=110)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 101, 99, 100)}, {})
        e.step("2026-01-03", {"A": candle("2026-01-03", 99, 99.5, 75, 80)}, {})
        trade = e.state["closed"][0]
        self.assertEqual((trade["direction"], trade["reason"]), ("SHORT", "target"))
        self.assertGreater(trade["pnl"], 0)

    def test_gap_past_stop_blocks_the_fill(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 89, 95, 88, 94)}, {})
        self.assertIsNone(e.state["position"])
        self.assertIn("BLOCKED", self.log.kinds())

    def test_one_position_at_a_time(self):
        e = self.engine
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 101, 99, 100)}, {"B": score("B", 0.99)})
        self.assertEqual(e.state["position"]["symbol"], "A")
        self.assertIsNone(e.state["pending"])

    def test_daily_loss_limit_blocks_new_entries_that_day(self):
        e = paper.PaperEngine(cfg(daily_loss_limit=0.005), log=self.log)
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        e.step("2026-01-02", {"A": candle("2026-01-02", 100, 101, 85, 90)}, {"B": score("B", 0.95)})
        self.assertIn("HALT", self.log.kinds())
        self.assertIsNone(e.state["pending"])
        e.step("2026-01-03", {}, {"B": score("B", 0.95)})
        self.assertEqual(e.state["pending"]["symbol"], "B")

    def test_time_limit(self):
        e = paper.PaperEngine(cfg(max_hold_days=3), log=self.log)
        e.step("2026-01-01", {}, {"A": score("A", 0.9)})
        for d in range(2, 7):
            day = f"2026-01-0{d}"
            e.step(day, {"A": candle(day, 100, 101, 99, 100)}, {})
        self.assertEqual(e.state["closed"][0]["reason"], "time limit")
        self.assertEqual(e.state["closed"][0]["exit_date"], "2026-01-05")

    def test_summary_numbers(self):
        trades = [{"pnl": 200, "r": 2.0}, {"pnl": -100, "r": -1.0}, {"pnl": -100, "r": -1.0}]
        curve = [["d1", 10000], ["d2", 10200], ["d3", 10100], ["d4", 10000]]
        m = paper.summarize(trades, curve, 10000)
        self.assertEqual(m["trades"], 3)
        self.assertAlmostEqual(m["win_rate"], 1 / 3)
        self.assertAlmostEqual(m["profit_factor"], 1.0)
        self.assertAlmostEqual(m["max_drawdown"], 10000 / 10200 - 1)
        self.assertAlmostEqual(m["avg_r"], 0.0)


class EndToEndTests(unittest.TestCase):
    def universe(self, n):
        return {
            "UP-USD": make_candles(n, drift=0.003),
            "DOWN-USD": make_candles(n, drift=-0.003, wave=0.05),
            "FLAT=X": make_candles(n, drift=0.0, volume=0.0),
        }

    def test_backtest_follows_the_risk_rules(self):
        state = sphere.replay(cfg(), self.universe(700), "2024-08-01")
        trades = state["closed"]
        self.assertGreater(len(trades), 3)
        for a, b in zip(trades, trades[1:]):
            self.assertLessEqual(a["exit_date"], b["entry_date"])  # never two positions at once
        for t in trades:
            self.assertGreater(t["entry_date"], t["signal_date"])  # fills at a later open, never the signal close
            self.assertGreaterEqual(t["r"], -1.0 - 1e-9)  # these candles never gap, so no loss beyond 1R
            if t["reason"] == "stop":
                self.assertAlmostEqual(t["r"], -1.0, places=6)

    def test_live_run_processes_each_day_once(self):
        candles = self.universe(400)
        last = candles["UP-USD"][-1][0]
        now = (date.fromisoformat(last) + timedelta(days=1)).toordinal()
        now_ts = (now - date(1970, 1, 1).toordinal()) * 86400 + 6 * 3600
        markets = [{"symbol": s, "name": s, "group": "test", "source": "fake"} for s in candles]
        c = cfg(markets=markets)

        def fetch(market, days, now, through):
            return [x for x in candles[market["symbol"]] if x[0] <= through], "fake"

        with tempfile.TemporaryDirectory() as d:
            state_path, log_dir = os.path.join(d, "state.json"), os.path.join(d, "logs")
            run = dict(fetch=fetch, flow=lambda *a: {"score": 50, "bias": "NEUTRAL"}, state_path=state_path,
                       log_dir=log_dir)
            state = sphere.run_live(c, now_ts, lambda *a: None, **run)
            self.assertEqual(state["last_date"], last)
            self.assertEqual(len(state["curve"]), 1)
            with open(os.path.join(log_dir, "scores.csv")) as f:
                first_rows = len(list(csv.reader(f)))
            state = sphere.run_live(c, now_ts, lambda *a: None, **run)
            self.assertEqual(len(state["curve"]), 1)
            with open(os.path.join(log_dir, "scores.csv")) as f:
                self.assertEqual(len(list(csv.reader(f))), first_rows)
            nxt = make_candles(401, drift=0.003)[-1]
            candles["UP-USD"].append(nxt)
            state = sphere.run_live(c, now_ts + 86400, lambda *a: None, **run)
            self.assertEqual(state["last_date"], nxt[0])
            with open(os.path.join(log_dir, "sphere.html")) as f:
                page = f.read()
            self.assertIn(nxt[0], page)
            self.assertIn('"sym": "UP-USD"', page)

    def test_config_must_stay_paper_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            sphere.save_json(path, dict(CFG, paper_only=False))
            with self.assertRaises(SystemExit):
                sphere.load_config(path)


if __name__ == "__main__":
    unittest.main()
