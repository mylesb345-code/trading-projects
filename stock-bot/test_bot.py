"""Offline tests: no internet, no keys, no orders. Run: .venv/bin/python test_bot.py"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

CFG = {"symbol": "AAPL", "fast_ma": 20, "slow_ma": 50, "trade_qty": 1, "history_days": 100}


def series(values):
    start = date(2026, 6, 1)
    return pd.Series(values, index=[start + timedelta(days=i) for i in range(len(values))], dtype=float)


def crossing(direction):
    """99 flat closes, then a jump: the 20-day average moves through the 50-day exactly on the last day."""
    base = [100.0] * 99
    return series(base + [130.0 if direction == "up" else 70.0])


class FakeBroker:
    def __init__(self, held=0):
        self.held = held

    def shares_held(self, symbol):
        return self.held


class SignalTests(unittest.TestCase):
    def test_cross_up_is_buy(self):
        signal, info = bot.find_signal(crossing("up"), 20, 50)
        self.assertEqual(signal, "BUY")
        self.assertLessEqual(info["fast_prev"], info["slow_prev"])
        self.assertGreater(info["fast"], info["slow"])

    def test_cross_down_is_sell(self):
        self.assertEqual(bot.find_signal(crossing("down"), 20, 50)[0], "SELL")

    def test_already_above_is_not_a_crossover(self):
        rising = series([100 + i for i in range(100)])  # 20-day sits above the 50-day the whole time
        self.assertIsNone(bot.find_signal(rising, 20, 50)[0])

    def test_needs_enough_history(self):
        with self.assertRaises(RuntimeError):
            bot.find_signal(series([100.0] * 50), 20, 50)


class DecisionTests(unittest.TestCase):
    info = {"date": "2026-09-07"}

    def test_buy_uses_trade_size(self):
        self.assertEqual(bot.decide("BUY", self.info, CFG, {}, FakeBroker())[:2], ("BUY", 1))

    def test_sell_only_what_is_held(self):
        self.assertEqual(bot.decide("SELL", self.info, CFG, {}, FakeBroker(held=0))[0], "SKIP")
        self.assertEqual(bot.decide("SELL", self.info, CFG, {}, FakeBroker(held=3))[:2], ("SELL", 1))

    def test_same_crossover_is_acted_on_once(self):
        state = {"last_signal": ["2026-09-07", "BUY"]}
        self.assertEqual(bot.decide("BUY", self.info, CFG, state, FakeBroker())[0], "SKIP")

    def test_no_signal_holds(self):
        self.assertEqual(bot.decide(None, self.info, CFG, {}, FakeBroker())[0], "HOLD")


class SafetyTests(unittest.TestCase):
    def test_refuses_live_or_missing_keys(self):
        for key, secret in (("", ""), ("AKLIVE123", "secret")):
            with self.assertRaises(SystemExit):
                bot.AlpacaPaper(key, secret)

    def test_bad_config_is_refused(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(dict(CFG, fast_ma=50, slow_ma=20), f)
        with self.assertRaises(SystemExit):
            bot.load_config(f.name)
        os.unlink(f.name)


class FetchTests(unittest.TestCase):
    def test_drops_todays_unfinished_bar(self):
        import types
        now = datetime(2026, 11, 2, 11, 0, tzinfo=bot.NEW_YORK)
        idx = pd.DatetimeIndex([now - timedelta(days=i) for i in range(150, -1, -1)])
        frame = pd.DataFrame({"Close": range(151)}, index=idx)
        fake = types.SimpleNamespace(Ticker=lambda s: types.SimpleNamespace(history=lambda **k: frame))
        sys.modules["yfinance"] = fake
        try:
            closes = bot.fetch_closes("AAPL", 100, now)
        finally:
            del sys.modules["yfinance"]
        self.assertEqual(len(closes), 100)
        self.assertEqual(closes.index[-1], (now - timedelta(days=1)).date())


if __name__ == "__main__":
    unittest.main()
