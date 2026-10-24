"""Offline tests for the whale consensus matrix. Run: python test_consensus.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consensus  # noqa: E402


def pos(cid, outcome, usd, avg, price, title="Heat vs. Hornets"):
    return {"conditionId": cid, "title": title, "eventSlug": "heat-hornets", "endDate": "2026-10-24",
            "outcome": outcome, "currentValue": usd, "avgPrice": avg, "curPrice": price, "cashPnl": 0}


WHALES = [{"proxyWallet": f"0x{i}", "userName": f"w{i}", "pnl": 1e6 - i} for i in range(4)]


class ConsensusTests(unittest.TestCase):
    def rows(self, holdings):
        return consensus.build(WHALES, {w["proxyWallet"]: holdings.get(i, []) for i, w in enumerate(WHALES)})

    def test_majority_side_and_weighted_entry(self):
        rows = self.rows({0: [pos("c1", "Heat", 580, 0.50, 0.58)], 1: [pos("c1", "Heat", 580, 0.60, 0.58)],
                          2: [pos("c1", "Heat", 580, 0.55, 0.58)], 3: [pos("c1", "Hornets", 420, 0.40, 0.42)]})
        r = rows[0]
        self.assertEqual((r["pick"], r["agree"], r["whales"]), ("Heat", 3, 4))
        self.assertAlmostEqual(r["consensus"], 0.75)
        self.assertAlmostEqual(r["avg_entry"], 0.55)  # equal share counts, so the plain average
        self.assertTrue(consensus.worth_alerting(r))

    def test_no_alert_when_price_ran_or_too_few_agree(self):
        ran = self.rows({i: [pos("c1", "Heat", 700, 0.50, 0.70)] for i in range(3)})[0]
        self.assertFalse(consensus.worth_alerting(ran))
        two = self.rows({i: [pos("c1", "Heat", 500, 0.50, 0.50)] for i in range(2)})[0]
        self.assertFalse(consensus.worth_alerting(two))

    def test_each_agreement_alerts_once(self):
        rows = self.rows({i: [pos("c1", "Heat", 500, 0.50, 0.51)] for i in range(3)})
        sent = []
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "alerted.json")
            consensus.send_alerts(rows, lambda *a: sent.append(a), 1000, path)
            consensus.send_alerts(rows, lambda *a: sent.append(a), 2000, path)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "Whale consensus: Heat (3/3)")
        self.assertIn("Not financial advice", sent[0][1])

    def test_open_positions_drops_settled_and_dust(self):
        rows = [pos("a", "Yes", 5000, 0.5, 0.5), pos("b", "Yes", 5000, 0.5, 0.99), pos("c", "Yes", 50, 0.5, 0.5),
                dict(pos("d", "Yes", 5000, 0.5, 0.5), endDate="2026-01-01"), dict(pos("e", "Yes", 5000, 0.5, 0.5), redeemable=True)]
        original = consensus.get
        consensus.get = lambda *a, **k: rows
        try:
            kept = consensus.open_positions("0x0", 1000, "2026-10-24")
        finally:
            consensus.get = original
        self.assertEqual([p["conditionId"] for p in kept], ["a"])


if __name__ == "__main__":
    unittest.main()
