"""Offline tests for the strategy research. Run: python test_research.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research  # noqa: E402

DAY = research.DAY


def bars(closes):
    return [(i * DAY, c, c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]


class ResearchTests(unittest.TestCase):
    def test_fills_at_next_open_with_trench_costs(self):
        c = [(0, 100, 101, 99, 100), (DAY, 100, 111, 99, 110), (2 * DAY, 110, 121, 109, 120), (3 * DAY, 120, 121, 119, 120)]
        trades, curve = research.simulate(c, [1, 1, 0, 0], 0)
        self.assertEqual(len(trades), 1)
        entry, exit_ = 100 * 1.001, 120 * 0.999
        self.assertAlmostEqual(trades[0][3], (exit_ / entry - 1) * 0.92)  # 8% fee on the win
        self.assertEqual((trades[0][0], trades[0][1]), (DAY, 3 * DAY))

    def test_losses_pay_no_win_fee_and_shorts_mirror(self):
        c = [(0, 100, 101, 99, 100), (DAY, 100, 101, 99, 100), (2 * DAY, 110, 111, 109, 110), (3 * DAY, 110, 111, 109, 110)]
        trades, _ = research.simulate(c, [-1, -1, 0, 0], 0)
        self.assertAlmostEqual(trades[0][3], -(110 * 1.001 / (100 * 0.999) - 1))

    def test_rules_follow_the_trend(self):
        up = bars([100 * 1.01 ** i for i in range(300)])
        down = bars([100 * 0.99 ** i for i in range(300)])
        self.assertEqual(research.rule_ema_trend(up)[-1], 1)
        self.assertEqual(research.rule_ema_trend(down)[-1], -1)
        self.assertEqual(research.rule_momentum(up)[-1], 1)
        self.assertEqual(research.rule_donchian(up)[-1], 1)
        self.assertEqual(research.rule_rsi2(down)[-1], 0)  # never buys dips in a downtrend

    def test_evaluate_needs_both_halves_to_pass(self):
        # a steady uptrend: the trend rule just holds one long trade, far too few trades to pass
        r = research.evaluate(bars([100 * 1.005 ** i for i in range(800)]), research.rule_ema_trend)
        self.assertFalse(r["passed"])
        self.assertEqual(r["position"], 1)


class SelectionTests(unittest.TestCase):
    def rows(self, passing, symbols=("A", "B", "C", "D")):
        return [{"rule": "trend", "tf": "1d", "symbol": s, "passed": s in passing} for s in symbols]

    def prev(self, symbols):
        return [{"rule": "trend", "tf": "1d", "symbol": s} for s in symbols]

    def test_first_run_takes_todays_passes(self):
        got = research.confirm_selection(self.rows("ABC"), [], {}, first_run=True)
        self.assertEqual(got, {"trend|1d|A", "trend|1d|B", "trend|1d|C"})

    def test_new_market_joins_only_after_five_passes_in_a_row(self):
        streaks, prev = {}, self.prev("ABC")
        for day in range(1, 6):
            got = research.confirm_selection(self.rows("ABCD"), prev, streaks, first_run=False)
            self.assertEqual("trend|1d|D" in got, day == 5)

    def test_market_leaves_only_after_five_fails_and_rule_needs_three(self):
        streaks, prev = {}, self.prev("ABCD")
        for day in range(1, 6):
            got = research.confirm_selection(self.rows("ABC"), prev, streaks, first_run=False)
            self.assertEqual("trend|1d|D" in got, day < 5)
        streaks = {}
        for day in range(1, 6):
            got = research.confirm_selection(self.rows("AB", "ABC"), self.prev("ABC"), streaks, first_run=False)
        self.assertEqual(got, set())  # C dropped, leaving 2 markets: the whole rule stops

    def test_missing_data_keeps_place(self):
        got = research.confirm_selection(self.rows("AB", "AB"), self.prev("ABC"), {}, first_run=False)
        self.assertEqual(got, {"trend|1d|A", "trend|1d|B", "trend|1d|C"})


if __name__ == "__main__":
    unittest.main()
