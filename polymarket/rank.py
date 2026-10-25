#!/usr/bin/env python3
"""Rank Polymarket traders worth copying: big profits AND a low loss rate, judged on their actual closed bets.
READ-ONLY: public data, no account, no orders.

  python rank.py              score proven traders on their last 90 days of settled bets, save copy_list.json

Leaderboard "win %" alone misleads: someone who buys 95-cent favourites wins almost every bet and then loses
big on the rare upset. So every candidate is scored on what actually happened to their bets:

  Candidates  in Polymarket's all-time top 200 by profit (long record) AND this month's top 200 (still winning)
  Must have   (fixed up front, before looking at anyone)
              - 40+ real closed bets in 90 days (bets under $100 ignored)
              - win rate 55%+            (low loss rate)
              - profit factor 1.5+       (dollars won / dollars lost)
              - profitable in 60%+ of the weeks they bet
              - a bet in the last 7 days (still active, so there's something to copy)
              - average entry under 85c  (not just buying near-certain favourites)
  Ranked by   profit factor x weekly consistency, then profit

Writes rank.csv (everyone scored, pass or fail, with the reason) and copy_list.json (the ones that pass, best
first). consensus.py follows copy_list.json when it exists.
"""
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import consensus

HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86400
WINDOW_DAYS, MIN_BET_USD, MAX_POSITIONS = 90, 100, 3000
MIN_BETS, MIN_WIN_RATE, MIN_PF, MIN_WEEKS, ACTIVE_DAYS, MAX_AVG_ENTRY = 40, 0.55, 1.5, 0.60, 7, 0.85
SPORTS_WORDS = (" vs", "Spread", "O/U", "win on 20", "UFC", "NBA", "NFL", "NHL", "MLB", "Premier League")


def closed_bets(wallet, since):
    """Cashed-out bets since `since`, newest first. Returns (bets, covered_from): very active traders have more
    closed bets than we read, so covered_from is how far back the list really reaches."""
    bets, offset, oldest = [], 0, None
    while offset < MAX_POSITIONS:
        page = consensus.get("/closed-positions", user=wallet, limit=50, offset=offset, sortBy="TIMESTAMP",
                             sortDirection="DESC")
        if not page:
            break
        for p in page:
            if int(p["timestamp"]) < since:
                return bets, since
            oldest = int(p["timestamp"])
            cost = float(p["totalBought"]) * float(p["avgPrice"])
            if cost >= MIN_BET_USD:
                bets.append({"t": int(p["timestamp"]), "pnl": float(p["realizedPnl"]), "cost": cost,
                             "entry": float(p["avgPrice"]), "title": p.get("title") or ""})
        offset += 50
    return bets, (oldest if offset >= MAX_POSITIONS and oldest else since)


def settled_unclaimed(wallet, since):
    """Bets whose market has settled but were never cashed out: almost always losers (worth $0), which
    Polymarket never lists as closed. Without these, every win rate looks far better than it is."""
    since_day = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d")
    out, offset = [], 0
    while offset < 2000:
        page = consensus.get("/positions", user=wallet, sizeThreshold=0, limit=500, offset=offset)
        for p in page:
            price, end = float(p.get("curPrice") or 0), (p.get("endDate") or "")[:10]
            if not (price <= 0.001 or price >= 0.999) or not end or end < since_day:
                continue  # still live, or settled before the window
            cost = float(p.get("initialValue") or 0)
            if cost >= MIN_BET_USD:
                t = int(datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
                out.append({"t": t, "pnl": float(p.get("cashPnl") or 0) + float(p.get("realizedPnl") or 0),
                            "cost": cost, "entry": float(p["avgPrice"]), "title": p.get("title") or ""})
        if len(page) < 500:
            break
        offset += 500
    return out


def score(bets, now):
    if not bets:
        return None
    wins = [b for b in bets if b["pnl"] > 0]
    losses = [b for b in bets if b["pnl"] <= 0]
    won, lost = sum(b["pnl"] for b in wins), -sum(b["pnl"] for b in losses)
    weeks = {}
    for b in bets:
        weeks[(now - b["t"]) // (7 * DAY)] = weeks.get((now - b["t"]) // (7 * DAY), 0) + b["pnl"]
    cost = sum(b["cost"] for b in bets)
    return {
        "bets": len(bets), "win_rate": len(wins) / len(bets), "profit_factor": won / lost if lost else 99.0,
        "pnl_90d": won - lost, "roi": (won - lost) / cost if cost else 0.0,
        "avg_win": won / len(wins) if wins else 0.0, "avg_loss": lost / len(losses) if losses else 0.0,
        "worst_loss": min(b["pnl"] for b in bets), "weeks_profitable": sum(v > 0 for v in weeks.values()) / len(weeks),
        "avg_entry": sum(b["entry"] * b["cost"] for b in bets) / cost if cost else 0.0,
        "days_since_bet": (now - max(b["t"] for b in bets)) / DAY, "avg_bet": cost / len(bets),
        "sports_share": sum(any(w in b["title"] for w in SPORTS_WORDS) for b in bets) / len(bets),
    }


def verdict(s):
    if s is None:
        return "no closed bets in 90 days"
    checks = [(s["bets"] >= MIN_BETS, f"only {s['bets']} bets"),
              (s["win_rate"] >= MIN_WIN_RATE, f"wins {s['win_rate']:.0%}"),
              (s["profit_factor"] >= MIN_PF, f"profit factor {s['profit_factor']:.2f}"),
              (s["weeks_profitable"] >= MIN_WEEKS, f"profitable in {s['weeks_profitable']:.0%} of weeks"),
              (s["days_since_bet"] <= ACTIVE_DAYS, f"last bet {s['days_since_bet']:.0f} days ago"),
              (s["avg_entry"] < MAX_AVG_ENTRY, f"buys favourites (avg {s['avg_entry'] * 100:.0f}c)")]
    failed = [why for ok, why in checks if not ok]
    return "PASS" if not failed else "; ".join(failed)


def main():
    now = int(time.time())
    month, alltime = consensus.leaderboard("MONTH"), consensus.leaderboard("ALL")
    cands = [dict(m, pnl_all=alltime[w]["pnl"]) for w, m in month.items()
             if w in alltime and m["pnl"] > 0 and alltime[w]["pnl"] > 0]
    print(f"Candidates (all-time top 200 AND this month's top 200): {len(cands)}", file=sys.stderr)

    def work(c):
        try:
            since = now - WINDOW_DAYS * DAY
            won, covered = closed_bets(c["proxyWallet"], since)
            # count unclaimed (mostly losing) bets over exactly the same stretch the cashed-out list covers
            bets = won + settled_unclaimed(c["proxyWallet"], covered)
            return c, score(bets, now)
        except Exception as e:
            print(f"  {c['userName']}: skipped ({e})", file=sys.stderr)
            return c, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, cands))
    rows = []
    for c, s in results:
        v = verdict(s)
        rows.append({"wallet": c["proxyWallet"], "name": c["userName"] or c["proxyWallet"][:10], "pnl_30d": c["pnl"],
                     "pnl_all": c["pnl_all"], "verdict": v, **(s or {})})
    passed = sorted([r for r in rows if r["verdict"] == "PASS"],
                    key=lambda r: (-r["profit_factor"] * r["weeks_profitable"], -r["pnl_90d"]))
    fields = ["name", "wallet", "verdict", "pnl_all", "pnl_30d", "pnl_90d", "bets", "win_rate", "profit_factor",
              "weeks_profitable", "roi", "avg_win", "avg_loss", "worst_loss", "avg_entry", "avg_bet", "sports_share",
              "days_since_bet"]
    with open(os.path.join(HERE, "rank.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in passed + [r for r in rows if r["verdict"] != "PASS"]:
            w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})
    with open(os.path.join(HERE, "copy_list.json"), "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "rules": {"min_bets": MIN_BETS, "min_win_rate": MIN_WIN_RATE, "min_profit_factor": MIN_PF,
                             "min_weeks_profitable": MIN_WEEKS, "active_days": ACTIVE_DAYS,
                             "max_avg_entry": MAX_AVG_ENTRY, "window_days": WINDOW_DAYS},
                   "traders": [{k: r[k] for k in ("name", "wallet", "pnl_all", "pnl_30d", "pnl_90d", "bets", "win_rate",
                                                  "profit_factor", "weeks_profitable", "avg_entry", "avg_bet",
                                                  "sports_share")} for r in passed]}, f, indent=1)
    # traders who dropped off the list stop alerting; ones who joined start fresh on the next copy_alerts run
    state_path = os.path.join(HERE, "copy_state.json")
    if os.path.exists(state_path):
        keep = {r["wallet"] for r in passed}
        state = {w: t for w, t in json.load(open(state_path)).items() if w in keep}
        with open(state_path, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
    print(f"{len(passed)} of {len(rows)} passed. Last 90 days of closed bets ($100+):")
    print(f"{'#':>2} {'trader':<22} {'bets':>4} {'win%':>5} {'PF':>5} {'weeks+':>6} {'90d P&L':>10} {'avg bet':>8} "
          f"{'entry':>5} {'sports':>6}")
    for i, r in enumerate(passed, 1):
        print(f"{i:>2} {r['name'][:22]:<22} {r['bets']:>4} {r['win_rate']:>5.0%} {r['profit_factor']:>5.2f} "
              f"{r['weeks_profitable']:>6.0%} {consensus.money(r['pnl_90d']):>10} {consensus.money(r['avg_bet']):>8} "
              f"{r['avg_entry'] * 100:>4.0f}c {r['sports_share']:>6.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
