"""Offline tests for the BTC volatility clock. Run: python test_volclock.py"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volclock  # noqa: E402

CFG = dict(volclock.DEFAULTS, vol_timezone="UTC")
START = int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp())  # a Monday, 00:00 UTC


def candles(days=30, busy_hour=14, busy_pct=2.0, normal_pct=0.5, last=None):
    """Flat BTC at 100 that swings busy_pct inside busy_hour each day and normal_pct in every other hour."""
    out = []
    for i in range(days * 24):
        t = START + i * 3600
        pct = busy_pct if i % 24 == busy_hour else normal_pct
        out.append((t, 100.0, 100 + pct / 2, 100 - pct / 2, 100.0))
    if last:
        out.append((START + days * 24 * 3600,) + last)
    return out


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, title, body, priority="default"):
        self.sent.append((title, body, priority))


class VolClockTests(unittest.TestCase):
    def test_profile_finds_the_busiest_hour(self):
        hours, days = volclock.profile(candles(), "UTC")
        self.assertEqual(max(hours, key=hours.get), 14)
        self.assertAlmostEqual(hours[14], 2.0)
        self.assertAlmostEqual(hours[3], 0.5)
        self.assertEqual(len(days), 7)

    def test_report_names_busiest_and_quietest_hours(self):
        text = volclock.report(candles(), "test", CFG)
        self.assertIn("Busiest hours: 2 PM (2.00%)", text)
        self.assertIn("cut both ways", text)

    def test_hour_labels(self):
        self.assertEqual([volclock.hour_label(h) for h in (0, 9, 12, 23)], ["12 AM", "9 AM", "12 PM", "11 PM"])

    def test_spike_needs_both_the_ratio_and_the_minimum(self):
        # 00:00 hour is normally 0.5%; 3% is 6x normal and above the 1% floor
        self.assertIsNotNone(volclock.spike(candles(last=(100.0, 101.5, 98.5, 99.0)), CFG))
        # 1.2% is above the floor but only 2.4x normal
        self.assertIsNone(volclock.spike(candles(last=(100.0, 100.6, 99.4, 100.0)), CFG))
        # 0.9% would be 1.8x a 0.5% hour, and also under the 1% floor
        self.assertIsNone(volclock.spike(candles(last=(100.0, 100.45, 99.55, 100.0)), CFG))

    def test_alerts_once_per_spike_and_gives_direction(self):
        data = candles(last=(100.0, 101.5, 98.5, 98.8))
        now = data[-1][0] + 3600 + 600
        notify, state = Recorder(), {}
        volclock.run_check(CFG, state, now, notify, fetch=lambda d, n: (data, "test"))
        volclock.run_check(CFG, state, now, notify, fetch=lambda d, n: (data, "test"))
        spikes = [s for s in notify.sent if s[0].startswith("BTC big move")]
        self.assertEqual(len(spikes), 1)
        self.assertIn("DOWN -1.2%", spikes[0][0])
        self.assertIn("not a trade signal", spikes[0][1])

    def test_weekly_report_once_on_sunday(self):
        data = candles()
        sunday = START + 6 * 86400 + 12 * 3600
        notify, state = Recorder(), {}
        for now in (sunday, sunday + 3600, sunday + 86400):
            volclock.run_check(CFG, state, now, notify, fetch=lambda d, n: (data, "test"))
        self.assertEqual([s[0] for s in notify.sent], ["BTC volatility clock"])

    def test_forced_report(self):
        notify = Recorder()
        volclock.run_check(CFG, {}, START + 3 * 86400, notify, fetch=lambda d, n: (candles(), "test"),
                           force_report=True)
        self.assertEqual(notify.sent[0][0], "BTC volatility clock")

    def test_falls_back_to_the_second_source(self):
        def broken(days, now):
            raise OSError("down")

        data = {c[0]: c[1:] for c in candles(days=20)}
        now = START + 20 * 86400
        got, name = volclock.get_hourly(20, now, (("A", broken), ("B", lambda d, n: data)))
        self.assertEqual(name, "B")
        self.assertEqual(len(got), 20 * 24)


if __name__ == "__main__":
    unittest.main()
