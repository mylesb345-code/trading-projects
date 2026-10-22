"""Offline tests for the paper-trading bot team. No internet or keys needed. Run: python test_bot_team.py"""
import contextlib
import io
import math
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_team  # noqa: E402
import roles  # noqa: E402

CFG = bot_team.load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), None)
SLOT = roles.CANDLE_SECONDS
BASE = 1_792_540_800  # 2026-10-20 00:00 UTC, on a 15-minute boundary


def cfg(**over):
    return dict(CFG, **over)


def make_candles(n, end_slot):
    """A slide followed by a recovery, so EMA9 crosses back above EMA21 at some point."""
    out = []
    for i in range(n):
        close = 100 * (1 - 0.004 * min(i, 60) + 0.006 * max(0, i - 60)) * (1 + 0.004 * math.sin(i))
        prev = out[-1][4] if out else close
        out.append((end_slot - (n - 1 - i) * SLOT, prev, max(prev, close) * 1.002, min(prev, close) * 0.998, close))
    return out


def buy_setup(end_slot):
    """Shortest history ending on a candle where the Analyst says BUY."""
    full = make_candles(160, end_slot + 159 * SLOT)
    analyst = roles.Analyst(CFG)
    for i in range(30, len(full)):
        if analyst.evaluate("BTC", full[:i])["action"] == "BUY":
            shift = end_slot - full[i - 1][0]
            return [(t + shift, o, h, l, c) for t, o, h, l, c in full[:i]]
    raise AssertionError("test data never produced a BUY")


class FakeScanner:
    def __init__(self, candles, price):
        self.candles, self.prices = candles, price

    def scan(self, now):
        return dict(self.candles), {}

    def price(self, coin):
        return self.prices[coin]


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class IndicatorAndAnalystTests(unittest.TestCase):
    def test_rsi_extremes(self):
        self.assertEqual(roles.rsi(list(range(1, 30)))[-1], 100.0)
        self.assertLess(roles.rsi(list(range(30, 1, -1)))[-1], 1.0)

    def test_buy_needs_fresh_crossover_and_rsi_in_range(self):
        candles = buy_setup(BASE)
        setup = roles.Analyst(CFG).evaluate("BTC", candles)
        self.assertEqual(setup["action"], "BUY")
        self.assertLess(setup["stop"], setup["price"])
        self.assertAlmostEqual(setup["take_profit"] - setup["price"], 2 * (setup["price"] - setup["stop"]))
        self.assertEqual(roles.Analyst(CFG).evaluate("BTC", candles[:-1])["action"], "WAIT")
        strict = roles.Analyst(cfg(rsi_min=99, rsi_max=100)).evaluate("BTC", candles)
        self.assertIn("RSI", strict["reason"])

    def test_waits_without_enough_history(self):
        self.assertEqual(roles.Analyst(CFG).evaluate("BTC", make_candles(10, BASE))["action"], "WAIT")


class RiskManagerTests(unittest.TestCase):
    setup = {"coin": "BTC", "price": 100.0, "stop": 95.0, "take_profit": 110.0}

    def test_risk_is_at_most_two_percent_including_costs(self):
        review = roles.RiskManager(CFG).review(self.setup, 10000, 10000, {}, False)
        self.assertTrue(review["approved"])
        self.assertLessEqual(review["risk"], 200.0 + 1e-9)
        self.assertGreater(review["risk"], 199.0)

    def test_no_leverage(self):
        review = roles.RiskManager(CFG).review(dict(self.setup, stop=99.99), 10000, 10000, {}, False)
        self.assertLessEqual(review["notional"] * (1 + CFG["fee_bps"] / 10000), 10000 + 1e-6)

    def test_rejections(self):
        rm = roles.RiskManager(CFG)
        cases = [
            (dict(self.setup, stop=None), {}, False, "stop-loss"),
            (dict(self.setup, take_profit=None), {}, False, "take-profit"),
            (self.setup, {}, True, "daily loss limit"),
            (self.setup, {"BTC": {}}, False, "already holding"),
            (self.setup, {"ETH": {}, "SOL": {}, "XRP": {}}, False, "already 3 open"),
        ]
        for setup, positions, halted, words in cases:
            review = rm.review(setup, 10000, 10000, positions, halted)
            self.assertFalse(review["approved"])
            self.assertIn(words, review["reason"])
        self.assertFalse(roles.RiskManager(cfg(paper_only=False)).review(self.setup, 1e4, 1e4, {}, False)["approved"])


class ExecutorSafetyTests(unittest.TestCase):
    def test_alpaca_refuses_anything_not_clearly_paper(self):
        with self.assertRaises(roles.RefuseToTrade):
            roles.AlpacaPaperExecutor(CFG, "", "", env={})
        with self.assertRaises(roles.RefuseToTrade):
            roles.AlpacaPaperExecutor(CFG, "AKLIVEKEY", "secret", env={})
        with self.assertRaises(roles.RefuseToTrade):
            roles.AlpacaPaperExecutor(CFG, "PKPAPER", "secret", env={"APCA_API_BASE_URL": "https://api.alpaca.markets"})

    def test_alpaca_only_ever_calls_the_paper_api(self):
        urls = []

        def http(method, url, headers=None, body=None):
            urls.append(url)
            if url.endswith("/v2/account"):
                return {"status": "ACTIVE", "account_number": "PA123", "equity": "100000"}
            if "/v2/assets" in url:
                return [{"symbol": "BTC/USD", "tradable": True}]
            if method == "POST" or method == "DELETE":
                return {"id": "o1"}
            return {"status": "filled", "filled_avg_price": "100", "filled_qty": "0.5", "id": "o1"}

        ex = roles.AlpacaPaperExecutor(CFG, "PKPAPER", "secret", http=http, env={})
        self.assertIn("paper-api.alpaca.markets", ex.check())
        self.assertTrue(ex.supports("BTC"))
        self.assertFalse(ex.supports("ADA"))
        ex.buy("BTC", 0.5, 100)
        ex.sell("BTC", 0.5, 100)
        self.assertTrue(all(u.startswith(roles.ALPACA_PAPER_URL) for u in urls))

    def test_unknown_executor_is_refused(self):
        with self.assertRaises(roles.RefuseToTrade):
            bot_team.build(cfg(executor="live"), {})

    def test_config_must_stay_paper_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            bot_team.save_json(path, cfg(paper_only=False))
            with self.assertRaises(SystemExit):
                bot_team.load_config(path)

    def test_simulated_fills_include_slippage_and_fees(self):
        state = {}
        ex = roles.SimulatedExecutor(CFG, state)
        fill = ex.buy("BTC", 1.0, 100.0)
        self.assertAlmostEqual(fill["price"], 100.05)
        self.assertAlmostEqual(state["cash"], 10000 - 100.05 - 100.05 * 0.0025)


class TeamTests(unittest.TestCase):
    def make_team(self, d, candles, prices, **over):
        state = {}
        c = cfg(**over)
        team = bot_team.BotTeam(c, state, FakeScanner(candles, prices), roles.Analyst(c), roles.RiskManager(c),
                                roles.SimulatedExecutor(c, state), bot_team.Logs(os.path.join(d, "logs")),
                                stop_path=os.path.join(d, "STOP"))
        return team, state

    def test_full_trade_cycle_is_logged_with_every_field(self):
        now = BASE + 10 * SLOT + 60
        latest = BASE + 9 * SLOT
        candles = buy_setup(latest)
        price = candles[-1][4]
        with tempfile.TemporaryDirectory() as d, quiet() as out:
            team, state = self.make_team(d, {"BTC": candles}, {"BTC": price})
            self.assertTrue(team.tick(now))
            self.assertIn("BTC", state["positions"])
            self.assertIn("PAPER BUY BTC", out.getvalue())
            self.assertIn("[PAPER-SIMULATED]", out.getvalue())
            team.scanner.prices["BTC"] = state["positions"]["BTC"]["stop"]
            team.tick(now + 60)
            self.assertEqual(state["positions"], {})
            rows = bot_team.read_csv(os.path.join(d, "logs", "trades.csv"))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(list(row), bot_team.TRADE_FIELDS)
            self.assertEqual((row["mode"], row["exit_reason"]), ("PAPER-SIMULATED", "stop-loss"))
            self.assertEqual(row["approved_by"], bot_team.APPROVAL_CHAIN)
            self.assertLess(float(row["pnl"]), 0)
            self.assertGreaterEqual(float(row["pnl"]), -200.01)  # 2% of the account at the stop, costs included
            summary = team.summary(now + 120, prices={"BTC": price})
            self.assertIn("Trades: 1 (0 wins, 1 losses), win rate 0%", summary)
            self.assertIn("Buy and hold", summary)
            self.assertIn("max drawdown", summary)

    def test_stale_candles_are_not_traded(self):
        now = BASE + 10 * SLOT + 60
        candles = buy_setup(BASE + 8 * SLOT)  # one candle behind
        with tempfile.TemporaryDirectory() as d, quiet():
            team, state = self.make_team(d, {"BTC": candles}, {"BTC": candles[-1][4]})
            team.tick(now)
            self.assertEqual(state["positions"], {})
            rows = bot_team.read_csv(os.path.join(d, "logs", "decisions.csv"))
            self.assertEqual(rows[0]["decision"], "MISSED")
            self.assertIn("stale data", rows[0]["reason"])

    def test_waits_for_a_new_candle_before_scanning_again(self):
        now = BASE + 10 * SLOT + 60
        with tempfile.TemporaryDirectory() as d, quiet() as out:
            team, state = self.make_team(d, {"BTC": make_candles(30, BASE + 9 * SLOT)}, {"BTC": 100.0})
            team.tick(now)
            team.tick(now + 60)
            self.assertEqual(out.getvalue().count("Scan done"), 1)

    def test_kill_switch_closes_everything_and_stops(self):
        now = BASE + 10 * SLOT + 60
        candles = buy_setup(BASE + 9 * SLOT)
        with tempfile.TemporaryDirectory() as d, quiet():
            team, state = self.make_team(d, {"BTC": candles}, {"BTC": candles[-1][4]})
            team.tick(now)
            self.assertIn("BTC", state["positions"])
            open(os.path.join(d, "STOP"), "w").close()
            self.assertFalse(team.tick(now + 60))
            self.assertEqual(state["positions"], {})
            rows = bot_team.read_csv(os.path.join(d, "logs", "trades.csv"))
            self.assertEqual(rows[0]["exit_reason"], "kill switch")

    def test_daily_loss_limit_blocks_new_trades(self):
        now = BASE + 10 * SLOT + 60
        candles = buy_setup(BASE + 9 * SLOT)
        with tempfile.TemporaryDirectory() as d, quiet():
            team, state = self.make_team(d, {"BTC": candles}, {"BTC": candles[-1][4]})
            state.update(day=bot_team.stamp(now)[:10], day_start_equity=20000.0)
            team.tick(now)
            self.assertEqual(state["positions"], {})
            rows = bot_team.read_csv(os.path.join(d, "logs", "decisions.csv"))
            self.assertIn("daily loss limit", rows[-1]["reason"])

    def test_downtime_is_recorded(self):
        now = BASE + 10 * SLOT + 60
        with tempfile.TemporaryDirectory() as d, quiet():
            team, state = self.make_team(d, {"BTC": make_candles(30, BASE + 9 * SLOT)}, {"BTC": 100.0})
            team.tick(now)
            team.tick(now + 3600)
            rows = bot_team.read_csv(os.path.join(d, "logs", "downtime.csv"))
            self.assertEqual(rows[0]["minutes"], "60")
            self.assertIn("60 minutes of downtime", team.summary(now + 3600, prices={"BTC": 100.0}))


class BacktestTests(unittest.TestCase):
    def test_replay_uses_live_rules_and_fills_next_open(self):
        import backtest
        setup = buy_setup(BASE + 200 * SLOT)
        prev = setup[-1][4]
        crash = [(setup[-1][0] + k * SLOT, prev, prev * 1.001, prev * 0.9, prev * 0.9) for k in (1, 2)]
        candles = setup + crash
        history = {"BTC": candles}
        with unittest.mock.patch.object(backtest, "WINDOW", len(setup)):
            trades, curve = backtest.run(CFG, history, candles[0][0])
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["reason"], "stop-loss")
        self.assertEqual(t["opened"], crash[0][0])  # filled at the open after the signal candle
        self.assertGreaterEqual(t["pnl"], -200.01)  # never more than 2% of the account at the stop
        s = backtest.summarize(trades, curve, CFG["starting_equity"])
        self.assertEqual((s["trades"], s["losses"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
