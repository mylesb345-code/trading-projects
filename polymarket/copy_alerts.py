#!/usr/bin/env python3
"""Copy alerts: push to your phone when a trader on copy_list.json (built by rank.py) places a new bet.
READ-ONLY: public on-chain data, no account, no orders. You decide whether to copy.

  python copy_alerts.py           check for new bets since the last run and push them
  python copy_alerts.py --dry     show what would be pushed, send nothing, remember nothing

To skip noise, a bet only alerts when the trader put at least half their usual bet size into one market and side
since the last check (their small hedges and top-ups stay quiet), at most 5 alerts per trader per run.
The first run for a trader only records where to start, so you don't get a flood of old bets.
"""
import argparse
import json
import os
import sys
import time

import consensus

HERE = os.path.dirname(os.path.abspath(__file__))
LIST_PATH = os.path.join(HERE, "copy_list.json")
STATE_PATH = os.path.join(HERE, "copy_state.json")
MIN_SHARE_OF_AVG_BET, MIN_USD, PER_TRADER = 0.5, 250, 5


def new_buys(wallet, since):
    rows = consensus.get("/activity", user=wallet, limit=500, type="TRADE")
    return [r for r in rows if r.get("side") == "BUY" and int(r["timestamp"]) > since]


def group(buys):
    """One entry per market and side: total dollars, shares, average price, latest time."""
    out = {}
    for b in buys:
        key = (b["conditionId"], b.get("outcome", ""))
        g = out.setdefault(key, {"title": b.get("title", ""), "outcome": b.get("outcome", ""),
                                 "slug": b.get("eventSlug") or b.get("slug") or "", "usd": 0.0, "shares": 0.0, "t": 0})
        g["usd"] += float(b.get("usdcSize") or 0)
        g["shares"] += float(b.get("size") or 0)
        g["t"] = max(g["t"], int(b["timestamp"]))
    for g in out.values():
        g["price"] = g["usd"] / g["shares"] if g["shares"] else 0.0
    return sorted(out.values(), key=lambda g: -g["usd"])


def check(traders, state, notify, now, fetch=new_buys):
    sent = []
    for tr in traders:
        wallet = tr["wallet"]
        if wallet not in state:
            state[wallet] = now  # first run: start from here, don't replay history
            continue
        buys = fetch(wallet, state[wallet])
        if buys:
            state[wallet] = max(int(b["timestamp"]) for b in buys)
        floor = max(MIN_USD, MIN_SHARE_OF_AVG_BET * tr.get("avg_bet", 0))
        for g in [g for g in group(buys) if g["usd"] >= floor][:PER_TRADER]:
            notify(f"Copy alert: {tr['name']} bought {g['outcome']}",
                   f"{g['title']}\n{tr['name']} put {consensus.money(g['usd'])} on {g['outcome']} at "
                   f"{g['price'] * 100:.0f}c (their usual bet {consensus.money(tr.get('avg_bet', 0))}).\n"
                   f"Record (last 90 days): {tr['bets']} bets, {tr['win_rate']:.0%} won, profit factor "
                   f"{tr['profit_factor']:.2f}.\n"
                   "Only copy near their price; if it has moved a lot, the value has gone. They lose too. "
                   "Not financial advice.\n"
                   f"https://polymarket.com/event/{g['slug']}", "high")
            sent.append((tr["name"], g))
    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(description="Phone alerts when traders on the copy list place a new bet.")
    ap.add_argument("--dry", action="store_true", help="print instead of pushing; don't save progress")
    a = ap.parse_args(argv)
    try:
        traders = json.load(open(LIST_PATH))["traders"]
    except FileNotFoundError:
        sys.exit("No copy_list.json yet: run rank.py first.")
    state = {} if not os.path.exists(STATE_PATH) else json.load(open(STATE_PATH))
    notify = (lambda title, body, *_: print(f"== {title}\n{body}\n")) if a.dry else consensus.notifier()
    sent = check(traders, state, notify, int(time.time()))
    print(f"Watching {len(traders)} traders: {', '.join(t['name'] for t in traders)}. Alerts: {len(sent)}")
    if not a.dry:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
