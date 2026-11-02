"""Closed-candle rule, position sizing, entry checks, de-duplication and the paper-only safety checks."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data import bar_close_time, closed_only  # noqa: E402
from risk import AccountState, can_open, position_size  # noqa: E402
from settings import load_credentials  # noqa: E402
from store import Store  # noqa: E402
from tests.test_box_theory import META, darvas_series  # noqa: E402
from strategies.box_theory import Bars, BoxParams, darvas_signals  # noqa: E402

RISK = {"daily_loss_limit": 0.03, "max_open_positions": 10}


class ClosedCandleTests(unittest.TestCase):
    def test_stock_daily_bar_closes_at_4pm_new_york(self):
        start = pd.Timestamp("2026-10-30 04:00", tz="UTC")  # Alpaca stamps daily bars at midnight New York
        self.assertEqual(bar_close_time(start, "1Day", "stocks"), pd.Timestamp("2026-10-30 20:00", tz="UTC"))
        winter = pd.Timestamp("2026-11-03 05:00", tz="UTC")  # after the clocks change: 4 PM EST = 21:00 UTC
        self.assertEqual(bar_close_time(winter, "1Day", "stocks"), pd.Timestamp("2026-11-03 21:00", tz="UTC"))

    def test_crypto_bars_close_one_bar_later(self):
        self.assertEqual(bar_close_time(pd.Timestamp("2026-11-01 08:00", tz="UTC"), "4Hour", "crypto"),
                         pd.Timestamp("2026-11-01 12:00", tz="UTC"))
        self.assertEqual(bar_close_time(pd.Timestamp("2026-11-01", tz="UTC"), "1Day", "crypto"),
                         pd.Timestamp("2026-11-02", tz="UTC"))

    def test_forming_bar_is_dropped(self):
        idx = pd.DatetimeIndex(["2026-11-01 08:00", "2026-11-01 12:00", "2026-11-01 16:00"], tz="UTC")
        df = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
        now = datetime(2026, 11, 1, 13, 30, tzinfo=timezone.utc)  # the 16:00 bar is still forming
        self.assertEqual(list(closed_only(df, now)["close"]), [1, 2])
        self.assertEqual(list(closed_only(df, datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc))["close"]), [1, 2, 3])


class SizingTests(unittest.TestCase):
    def test_one_percent_risk(self):
        # $100k equity, 1% risk = $1,000; entry 100, stop 90 = $10 a share -> 100 shares ($10k = 10% cap, exactly)
        self.assertEqual(position_size(100_000, 100, 90, 0.01, 0.10, fractional=False), 100)

    def test_capped_at_max_position(self):
        # stop only $2 away would mean 500 shares ($50k); the 10% cap allows $10k = 100 shares
        self.assertEqual(position_size(100_000, 100, 98, 0.01, 0.10, fractional=False), 100)

    def test_whole_shares_round_down(self):
        self.assertEqual(position_size(100_000, 100, 70, 0.01, 0.10, fractional=False), 33)  # 33.33 -> 33

    def test_crypto_is_fractional(self):
        qty = position_size(100_000, 60_000, 54_000, 0.01, 0.10, fractional=True, step=0.0001)
        self.assertAlmostEqual(qty, 0.1666, places=4)  # $1,000 / $6,000 a coin

    def test_bad_stop_means_no_trade(self):
        self.assertEqual(position_size(100_000, 100, 100, 0.01, 0.10, fractional=False), 0)


class EntryCheckTests(unittest.TestCase):
    def test_rules(self):
        ok = AccountState(100_000, 100_000, {"MSFT"}, {"ETH/USD"})
        self.assertTrue(can_open(ok, "AAPL", RISK)[0])
        self.assertFalse(can_open(ok, "MSFT", RISK)[0])                    # already held
        self.assertFalse(can_open(ok, "ETHUSD", RISK)[0])                  # open order (BTCUSD vs BTC/USD spelling)
        down = AccountState(96_900, 100_000)                               # -3.1% today
        self.assertIn("daily loss", can_open(down, "AAPL", RISK)[1])
        full = AccountState(100_000, 100_000, {f"S{k}" for k in range(10)})
        self.assertIn("max 10", can_open(full, "AAPL", RISK)[1])


class StoreTests(unittest.TestCase):
    def test_same_box_never_alerts_twice(self):
        bars = Bars(darvas_series(last=(104, 112, 104, 111, 2000.0)))
        sig = darvas_signals(bars, len(bars) - 1, BoxParams(), META)[0]
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.sqlite")
            self.assertTrue(store.add_signal(sig))
            self.assertFalse(store.add_signal(sig))
            self.assertEqual(store.open_long_signal("TEST", "1Day", "darvas_breakout"), sig.key)


class PaperOnlyTests(unittest.TestCase):
    base = {"ALPACA_API_KEY": "PKTEST1234", "ALPACA_SECRET_KEY": "s3cret-value-xyz"}

    def test_paper_key_loads_and_hides_secret(self):
        c = load_credentials(environ=dict(self.base))
        self.assertNotIn("s3cret", repr(c))
        self.assertNotIn("PKTEST1234", repr(c))

    def test_refuses_live(self):
        for extra in ({"ALPACA_API_KEY": "AKLIVE1234"}, {"ALPACA_BASE_URL": "https://api.alpaca.markets"},
                      {"ALPACA_PAPER": "false"}, {"ALPACA_API_KEY": ""}):
            with self.assertRaises(SystemExit):
                load_credentials(environ=dict(self.base, **extra))


class BacktestTests(unittest.TestCase):
    def test_breakout_that_runs_to_target(self):
        import backtest
        from settings import load_config
        cfg = load_config()
        df = darvas_series(last=(104, 112, 104, 111, 2000.0))
        rows = [(112.0, 125.0, 111.0, 124.0, 1500.0), (124.0, 131.0, 123.0, 130.0, 1500.0)]  # target 130 hit
        more = pd.DataFrame(rows, columns=df.columns,
                            index=pd.date_range(df.index[-1] + pd.Timedelta(days=1), periods=2, freq="D"))
        df = pd.concat([df, more])
        trades = backtest.simulate(df, "TEST", "stocks", "1Day", "darvas_breakout", cfg, 0.0, len(df) - 3)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t.entry, t.exit, t.reason), (112.0, 130.0, "target"))
        self.assertAlmostEqual(t.r, (130 - 112) / (112 - 99.5))


if __name__ == "__main__":
    unittest.main()


class AutonomyRuleTests(unittest.TestCase):
    def test_leveraged_inverse_volatility_funds_are_skipped(self):
        from data import is_leveraged_inverse_or_volatility as bad
        for name in ("iPath Series B S&P 500 VIX Short-Term Futures ETNs", "ProShares UltraPro Short QQQ",
                     "Direxion Daily Semiconductor Bear 3X Shares", "ProShares Short S&P500",
                     "Direxion Daily S&P 500 Bear 1X Shares", "ProShares UltraShort QQQ"):
            self.assertTrue(bad(name), name)
        for name in ("Apple Inc. Common Stock", "Fidelity National Information Services, Inc.",
                     "Sprouts Farmers Market, Inc.", "iShares China Large-Cap ETF"):
            self.assertFalse(bad(name), name)

    def test_too_tall_ranges_are_ignored(self):
        from strategies.box_theory import RangeParams, range_signals
        from tests.test_box_theory import RangeTests
        bars = RangeTests().series((115, 121, 110, 120, 1500.0))  # a 100-200 range is exactly +100%
        meta = dict(META, strategy="range_reversion")
        self.assertEqual(len(range_signals(bars, len(bars) - 1, RangeParams(252, 0.25, 1.0), meta)), 1)
        self.assertEqual(range_signals(bars, len(bars) - 1, RangeParams(252, 0.25, 0.8), meta), [])


class EntryCheckAtOpenTests(unittest.TestCase):
    def test_live_price_rules(self):
        from risk import entry_check
        args = dict(direction="long", signal_close=100.0, stop=90.0, target=130.0, max_chase=0.03, min_reward_risk=1.5)
        self.assertTrue(entry_check(101.0, **args)[0])                     # normal open
        self.assertIn("below the stop", entry_check(89.0, **args)[1])     # gapped through the stop
        self.assertIn("past the target", entry_check(131.0, **args)[1])   # gapped through the target
        self.assertIn("above the signal close", entry_check(104.0, **args)[1])  # chased 4%
        ok, why, rr = entry_check(102.5, **dict(args, target=110.0))      # (110-102.5)/(102.5-90) = 0.6
        self.assertFalse(ok)
        self.assertIn("reward:risk", why)


class ReportTests(unittest.TestCase):
    def test_round_trips_from_fills(self):
        from report import round_trips
        fills = [
            {"symbol": "FIS", "side": "buy", "qty": "148", "price": "39.62", "time": "2026-11-02T15:00"},
            {"symbol": "VXX", "side": "buy", "qty": "355", "price": "18.36", "time": "2026-11-02T15:01"},
            {"symbol": "VXX", "side": "sell", "qty": "355", "price": "18.30", "time": "2026-11-02T17:14"},
            {"symbol": "AAPL", "side": "buy", "qty": "2", "price": "100", "time": "2026-11-03T15:00"},
            {"symbol": "AAPL", "side": "sell", "qty": "1", "price": "110", "time": "2026-11-04T15:00"},
            {"symbol": "AAPL", "side": "sell", "qty": "1", "price": "120", "time": "2026-11-05T15:00"},
        ]
        trips = {t.symbol: t for t in round_trips(fills)}
        self.assertAlmostEqual(trips["VXX"].pnl, 355 * (18.30 - 18.36))    # a small loss
        self.assertEqual(trips["VXX"].closed, "2026-11-02T17:14")
        self.assertAlmostEqual(trips["AAPL"].pnl, 30.0)                    # +10 and +20 on two partial sells
        self.assertIsNone(trips["FIS"].closed)                             # still open
