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


if __name__ == "__main__":
    unittest.main()
