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


class RankTests(unittest.TestCase):
    def test_hidden_losers_change_the_verdict(self):
        import rank
        now = 100 * rank.DAY
        wins = [{"t": now - i * rank.DAY // 2, "pnl": 100, "cost": 500, "entry": 0.5, "title": "A vs B"} for i in range(60)]
        losers = [{"t": now - i * rank.DAY, "pnl": -500, "cost": 500, "entry": 0.5, "title": "A vs B"} for i in range(40)]
        self.assertEqual(rank.verdict(rank.score(wins, now)), "PASS")
        s = rank.score(wins + losers, now)
        self.assertAlmostEqual(s["win_rate"], 0.6)
        self.assertIn("profit factor 0.30", rank.verdict(s))

    def test_favourite_buyers_are_flagged(self):
        import rank
        now = 100 * rank.DAY
        bets = [{"t": now - i * rank.DAY // 2, "pnl": 30 if i % 20 else -300, "cost": 900, "entry": 0.95, "title": "x"}
                for i in range(60)]
        self.assertIn("buys favourites", rank.verdict(rank.score(bets, now)))


class CopyAlertTests(unittest.TestCase):
    def test_first_run_starts_fresh_then_alerts_big_new_bets_only(self):
        import copy_alerts
        trader = {"name": "pro", "wallet": "0xabc", "avg_bet": 2000, "bets": 300, "win_rate": 0.7, "profit_factor": 2.0}
        buys = [{"conditionId": "c1", "outcome": "Heat", "title": "Heat vs. Hornets", "eventSlug": "x", "usdcSize": 700,
                 "size": 1250, "timestamp": 150, "side": "BUY"},
                {"conditionId": "c1", "outcome": "Heat", "title": "Heat vs. Hornets", "eventSlug": "x", "usdcSize": 500,
                 "size": 900, "timestamp": 160, "side": "BUY"},
                {"conditionId": "c2", "outcome": "Over", "title": "O/U", "eventSlug": "y", "usdcSize": 300,
                 "size": 600, "timestamp": 170, "side": "BUY"}]
        sent, state = [], {}
        copy_alerts.check([trader], state, lambda *a: sent.append(a), 100, fetch=lambda w, s: buys)
        self.assertEqual((sent, state), ([], {"0xabc": 100}))
        copy_alerts.check([trader], state, lambda *a: sent.append(a), 200, fetch=lambda w, s: buys)
        self.assertEqual([s[0] for s in sent], ["Copy alert: pro bought Heat"])  # $1,200 >= half of $2,000; $300 isn't
        self.assertIn("$1,200 on Heat at 56c", sent[0][1])
        self.assertEqual(state["0xabc"], 170)


if __name__ == "__main__":
    unittest.main()
