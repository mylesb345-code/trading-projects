"""Box detection, breakout/breakdown and range-reversion signals on synthetic prices with known answers."""
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.box_theory import BoxParams, Bars, RangeParams, find_box, darvas_signals, range_signals, rank  # noqa

META = {"symbol": "TEST", "asset_class": "stocks", "timeframe": "1Day", "strategy": "darvas_breakout"}


def frame(rows):
    """rows: (open, high, low, close, volume) -> DataFrame indexed by daily close times."""
    idx = pd.date_range("2025-01-01 21:00", periods=len(rows), freq="D", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def uptrend(n=260):
    """A steady climb from 50 to 100, so close > SMA50 > SMA200 holds."""
    out = []
    for k in range(n):
        c = 50 + 50 * k / (n - 1)
        out.append((c, c * 1.005, c * 0.995, c, 1000.0))
    return out


def darvas_series(last=None, box_bars=12):
    """Uptrend, then a top at 110 (bar 260), a bottom at 100 (bar 263) confirmed by 3 bars, a quiet box, and
    optionally a final bar `last`."""
    rows = uptrend()
    rows.append((100.0, 110.0, 101.0, 108.0, 1000.0))  # 260: the top
    rows += [(106, 107, 103, 104, 1000.0), (104, 106, 101, 102, 1000.0), (102, 105, 100, 103, 1000.0)]  # 261-263
    rows += [(103, 106, 101, 104, 1000.0)] * box_bars  # holds inside 100-110
    if last:
        rows.append(last)
    return frame(rows)


class DarvasBoxTests(unittest.TestCase):
    p = BoxParams()

    def test_finds_the_box(self):
        bars = Bars(darvas_series())
        box = find_box(bars, len(bars) - 1, self.p)
        self.assertIsNotNone(box)
        self.assertEqual((box.top, box.bottom), (110.0, 100.0))
        self.assertEqual((box.top_index, box.bottom_index), (260, 263))
        self.assertAlmostEqual(box.height_pct, 0.10)

    def test_bottom_must_be_confirmed(self):
        df = darvas_series(box_bars=0)  # only bars 261-263 after the top: bottom at 263 has no 3 bars after it
        self.assertIsNone(find_box(Bars(df), len(df) - 1, self.p))

    def test_box_too_short_or_wrong_height(self):
        bars = Bars(darvas_series())
        end = len(bars) - 1
        self.assertIsNone(find_box(bars, end, BoxParams(min_box_bars=40)))
        self.assertIsNone(find_box(bars, end, BoxParams(min_box_pct=0.12)))   # box is 10%
        self.assertIsNone(find_box(bars, end, BoxParams(max_box_pct=0.08)))

    def test_top_exceeded_means_new_top(self):
        df = darvas_series(last=(104, 111, 103, 105, 1000.0))  # a later high above 110: 110 is no longer the top
        box = find_box(Bars(df), len(df) - 1, self.p)
        self.assertTrue(box is None or box.top != 110.0)

    def test_bottom_undercut_breaks_the_box(self):
        df = darvas_series(last=(103, 104, 99, 101, 1000.0))  # low 99 undercuts the 100 bottom
        self.assertIsNone(find_box(Bars(df), len(df) - 1, self.p))


class BreakoutTests(unittest.TestCase):
    p = BoxParams()

    def signals(self, last, p=None, allow_short=False):
        df = darvas_series(last=last)
        bars = Bars(df)
        return darvas_signals(bars, len(bars) - 1, p or self.p, META, allow_short)

    def test_breakout_on_volume_is_long(self):
        s = self.signals((104, 112, 104, 111, 2000.0))
        self.assertEqual([x.direction for x in s], ["long"])
        sig = s[0]
        self.assertEqual((sig.box_top, sig.box_bottom), (110.0, 100.0))
        self.assertAlmostEqual(sig.volume_ratio, 2.0)
        self.assertAlmostEqual(sig.stop, 99.5)
        self.assertAlmostEqual(sig.target, 130.0)
        self.assertAlmostEqual(sig.reward_risk, (130 - 111) / (111 - 99.5))

    def test_needs_the_buffer(self):
        self.assertEqual(self.signals((104, 111, 104, 110.1, 2000.0)), [])  # 110.1 < 110 x 1.002

    def test_needs_volume(self):
        self.assertEqual(self.signals((104, 112, 104, 111, 1400.0)), [])  # 1.4x < 1.5x

    def test_trend_filter(self):
        df = darvas_series(last=(104, 112, 104, 111, 2000.0))
        df.iloc[:260, df.columns.get_loc("close")] = np.linspace(200, 100, 260)  # a downtrend before the box
        bars = Bars(df)
        self.assertEqual(darvas_signals(bars, len(bars) - 1, self.p, META), [])
        self.assertEqual(len(darvas_signals(bars, len(bars) - 1, BoxParams(trend_filter=False), META)), 1)

    def test_breakdown_is_exit_and_short_only_if_allowed(self):
        self.assertEqual([x.direction for x in self.signals((101, 101, 98, 98.5, 1000.0))], ["exit"])
        both = self.signals((101, 101, 98, 98.5, 1000.0), allow_short=True)
        self.assertEqual([x.direction for x in both], ["exit", "short"])
        crypto = dict(META, asset_class="crypto")
        bars = Bars(darvas_series(last=(101, 101, 98, 98.5, 1000.0)))
        self.assertEqual([x.direction for x in darvas_signals(bars, len(bars) - 1, self.p, crypto, True)], ["exit"])

    def test_signal_records_every_field(self):
        s = self.signals((104, 112, 104, 111, 2000.0))[0]
        d = s.as_dict()
        for k in ("symbol", "asset_class", "timeframe", "strategy", "direction", "box_top", "box_bottom",
                  "box_height_pct", "close", "volume_ratio", "stop", "target", "reward_risk", "bar_close_utc",
                  "bar_close_central"):
            self.assertIn(k, d)
        self.assertIn("UTC", d["bar_close_utc"])
        self.assertTrue(d["bar_close_central"].endswith(("CST", "CDT")))


class RangeTests(unittest.TestCase):
    p = RangeParams(range_lookback=252, zone_pct=0.25)

    def series(self, last):
        rows = []
        for k in range(300):  # oscillates between 100 and 200
            c = 150 + 50 * math.sin(k / 8)
            rows.append((c, min(200.0, c * 1.01), max(100.0, c * 0.99), c, 1000.0))
        rows[-1] = (118, 119, 116, 117, 1000.0)  # previous close 117
        rows.append(last)
        return Bars(frame(rows))

    def test_confirmed_bounce_in_bottom_zone(self):
        bars = self.series((115, 121, 110, 120, 1500.0))  # dips into the bottom zone, closes up on open and prior close
        s = range_signals(bars, len(bars) - 1, self.p, dict(META, strategy="range_reversion"))
        self.assertEqual([x.direction for x in s], ["long"])
        hi, lo = s[0].box_top, s[0].box_bottom
        self.assertAlmostEqual(s[0].stop, lo * 0.99)
        self.assertAlmostEqual(s[0].target, hi - (hi - lo) * 0.25)

    def test_no_bounce_no_signal(self):
        bars = self.series((119, 121, 110, 116, 1500.0))  # closed below its open and the prior close
        self.assertEqual(range_signals(bars, len(bars) - 1, self.p, META), [])

    def test_never_buys_the_middle(self):
        bars = self.series((120, 160, 110, 155, 1500.0))  # touched the bottom zone but closed mid-range
        self.assertEqual([x.direction for x in range_signals(bars, len(bars) - 1, self.p, META)], [])

    def test_top_zone_is_exit(self):
        bars = self.series((180, 195, 179, 190, 1000.0))
        self.assertIn("exit", [x.direction for x in range_signals(bars, len(bars) - 1, self.p, META)])


class RankTests(unittest.TestCase):
    def test_volume_then_tighter_box_then_reward_risk(self):
        bars = Bars(darvas_series(last=(104, 112, 104, 111, 2000.0)))
        base = darvas_signals(bars, len(bars) - 1, BoxParams(), META)[0]
        import copy
        a, b, c = (copy.copy(base) for _ in range(3))
        a.symbol, a.volume_ratio = "A", 2.0
        b.symbol, b.volume_ratio, b.box_height_pct = "B", 3.0, 0.12
        c.symbol, c.volume_ratio, c.box_height_pct = "C", 3.0, 0.05
        self.assertEqual([s.symbol for s in rank([a, b, c])], ["C", "B", "A"])


if __name__ == "__main__":
    unittest.main()
