#!/usr/bin/env python3
"""Whale consensus matrix: what Polymarket's proven winners hold right now, and where they agree.
READ-ONLY: public on-chain data, no account, no orders.

  python consensus.py                 15 whales, positions worth $1,000+
  python consensus.py --whales 25 --min 5000
  python consensus.py --alert         also push new strong agreements to your phone (trading-bot's ntfy channel)

Polymarket runs on a public blockchain, so every trader's open positions are public (Kalshi hides them).
"Proven" means a wallet is in BOTH Polymarket's all-time top 200 by profit AND the last 30 days' top 200, so it
has a long record and is still winning now. For every market two or more of them hold, the matrix shows who holds
which side, how strongly they agree, what they paid, and what it costs now.

The same games and events usually trade on Kalshi too, so the matrix can guide a Kalshi bet.
Writes consensus.html (the matrix) and consensus.csv next to this file.
"""
import argparse
import csv
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://data-api.polymarket.com"
HERE = os.path.dirname(os.path.abspath(__file__))


def get(path, **params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "whale-consensus/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def leaderboard(period, depth=200):
    rows = []
    for offset in range(0, depth, 50):
        page = get("/v1/leaderboard", timePeriod=period, orderBy="PNL", limit=50, offset=offset)
        rows += page
        if len(page) < 50:
            break
    return {r["proxyWallet"].lower(): r for r in rows}


def proven_whales(n):
    month, alltime = leaderboard("MONTH"), leaderboard("ALL")
    both = [dict(m, pnl_all=alltime[w]["pnl"], rank_all=int(alltime[w]["rank"]))
            for w, m in month.items() if w in alltime and m["pnl"] > 0 and alltime[w]["pnl"] > 0]
    both.sort(key=lambda r: -r["pnl"])
    return both[:n]


def open_positions(wallet, min_usd, today):
    rows = get("/positions", user=wallet, sizeThreshold=1, limit=500, sortBy="CURRENT", sortDirection="DESC")
    keep = []
    for p in rows:
        price = float(p.get("curPrice") or 0)
        end = (p.get("endDate") or "9999")[:10]
        if p.get("redeemable") or float(p.get("currentValue") or 0) < min_usd or not 0.02 < price < 0.98 or end < today:
            continue  # settled, dust, or already decided
        keep.append(p)
    return keep


def build(whales, positions):
    markets = {}
    for w in whales:
        for p in positions[w["proxyWallet"]]:
            m = markets.setdefault(p["conditionId"], {"title": p["title"], "slug": p.get("eventSlug") or p.get("slug"),
                                                      "end": (p.get("endDate") or "")[:10], "holders": {},
                                                      "prices": {}})
            m["holders"][w["proxyWallet"]] = {"outcome": p["outcome"], "usd": float(p["currentValue"]),
                                              "avg": float(p["avgPrice"]), "pnl": float(p.get("cashPnl") or 0)}
            m["prices"][p["outcome"]] = float(p["curPrice"])
    rows = []
    for cid, m in markets.items():
        sides = {}
        for h in m["holders"].values():
            s = sides.setdefault(h["outcome"], {"n": 0, "usd": 0.0, "cost": 0.0})
            s["n"] += 1
            s["usd"] += h["usd"]
            s["cost"] += h["usd"] / (m["prices"].get(h["outcome"]) or 1) * h["avg"]  # shares x average price paid
        top = max(sides, key=lambda o: (sides[o]["n"], sides[o]["usd"]))
        n_all = len(m["holders"])
        shares = sides[top]["usd"] / (m["prices"][top] or 1)
        rows.append(dict(m, cid=cid, pick=top, whales=n_all, agree=sides[top]["n"], consensus=sides[top]["n"] / n_all,
                         usd=sum(s["usd"] for s in sides.values()), pick_usd=sides[top]["usd"],
                         avg_entry=sides[top]["cost"] / shares if shares else None, price=m["prices"][top],
                         sides={o: (s["n"], s["usd"]) for o, s in sides.items()}))
    rows.sort(key=lambda r: (-r["agree"], -r["consensus"], -r["pick_usd"]))
    return rows


ALERT_MIN_WHALES, ALERT_MIN_CONSENSUS, ALERT_MAX_RUN = 3, 0.75, 0.05
ALERTED_PATH = os.path.join(HERE, "alerted.json")


def worth_alerting(r):
    """3+ proven whales on the same side, 75%+ agreement, and the price still within 5 cents of what they paid
    (a bigger move usually means the game has started or the edge is gone)."""
    return (r["agree"] >= ALERT_MIN_WHALES and r["consensus"] >= ALERT_MIN_CONSENSUS and r["avg_entry"] is not None
            and abs(r["price"] - r["avg_entry"]) <= ALERT_MAX_RUN)


def send_alerts(rows, notify, now, path=ALERTED_PATH, limit=5):
    """One push per market and side, the first time it qualifies. Returns the alerts sent."""
    try:
        with open(path) as f:
            sent = json.load(f)
    except FileNotFoundError:
        sent = {}
    sent = {k: t for k, t in sent.items() if now - t < 14 * 86400}  # forget after two weeks
    fresh = [r for r in rows if worth_alerting(r) and f"{r['cid']}|{r['pick']}" not in sent][:limit]
    for r in fresh:
        others = ", ".join(f"{o} {n}" for o, (n, _) in r["sides"].items() if o != r["pick"]) or "none"
        notify(f"Whale consensus: {r['pick']} ({r['agree']}/{r['whales']})",
               f"{r['title']} (ends {r['end']})\n"
               f"{r['agree']} of {r['whales']} proven Polymarket whales hold {r['pick']} ({money(r['pick_usd'])}). "
               f"Other side: {others}.\n"
               f"They paid {r['avg_entry'] * 100:.0f}c on average; it's {r['price'] * 100:.0f}c now.\n"
               "On Kalshi: find the same game and only consider it near that price. Whales lose too. "
               "Not financial advice.", "default")
        sent[f"{r['cid']}|{r['pick']}"] = now
    with open(path, "w") as f:
        json.dump(sent, f, indent=1, sort_keys=True)
    return fresh


def notifier():
    sys.path.insert(0, os.path.join(HERE, "..", "trading-bot"))
    import bot
    return bot.make_notifier(bot.load_config())


def short(name, n=12):
    return name if len(name) <= n else name[:n - 1] + "…"


def money(x):
    return f"${x:,.0f}" if x < 1e6 else f"${x / 1e6:,.1f}M"


def render(whales, rows, made, min_usd):
    head = "".join(f"<th class=w title='{html.escape(w['proxyWallet'])}'>{html.escape(short(w['userName'] or w['proxyWallet'][:8]))}"
                   f"<br><span class=m>{money(w['pnl'])} 30d</span></th>" for w in whales)
    body = []
    for r in rows:
        cells = []
        for w in whales:
            h = r["holders"].get(w["proxyWallet"])
            if not h:
                cells.append("<td class=e></td>")
                continue
            cls = "agree" if h["outcome"] == r["pick"] else "against"
            cells.append(f"<td class={cls} title='{html.escape(h['outcome'])} · {money(h['usd'])} · paid "
                         f"{h['avg'] * 100:.0f}¢'>{html.escape(short(h['outcome'], 10))}<br><span>{money(h['usd'])}</span></td>")
        level = "strong" if r["consensus"] >= 0.8 else "moderate" if r["consensus"] >= 0.5 else "weak"
        chase = r["avg_entry"] is not None and r["price"] - r["avg_entry"] > 0.10
        entry = f"{r['avg_entry'] * 100:.0f}¢" if r["avg_entry"] is not None else "-"
        body.append(
            f"<tr><td class=mk><a href='https://polymarket.com/event/{html.escape(r['slug'] or '')}'>{html.escape(r['title'])}</a>"
            f"<br><span class=m>ends {html.escape(r['end'] or '?')}</span></td>{''.join(cells)}"
            f"<td class='score {level}'>{html.escape(short(r['pick'], 14))}<br>{r['agree']}/{r['whales']} · {r['consensus']:.0%}</td>"
            f"<td>{entry} → {r['price'] * 100:.0f}¢{' <b class=warn>price ran</b>' if chase else ''}</td></tr>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Whale Consensus Matrix</title><style>
:root{{--bg:#0f1115;--panel:#161a21;--fg:#e8eaed;--muted:#8b93a1;--line:#262b35;--yes:#1f7a4d;--yesbg:#12311f;
--no:#b4443c;--nobg:#3a1715;--mod:#c9932b}}
@media (prefers-color-scheme:light){{:root{{--bg:#f7f7f5;--panel:#fff;--fg:#1b1b19;--muted:#6b6b66;--line:#e2e2dc;
--yes:#1f7a4d;--yesbg:#dcf1e4;--no:#b4443c;--nobg:#f8dfdc;--mod:#a8760f}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 system-ui,sans-serif}}
main{{max-width:1300px;margin:0 auto;padding:20px 16px 48px}} h1{{font-size:20px;margin:0 0 4px;letter-spacing:.02em}}
p{{color:var(--muted);margin:0 0 12px;max-width:900px}} .wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;background:var(--panel);font-variant-numeric:tabular-nums}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:center;white-space:nowrap}}
th{{color:var(--muted);font-weight:500;position:sticky;top:0;background:var(--panel)}} th.w{{min-width:86px}}
td.mk{{text-align:left;white-space:normal;min-width:240px;max-width:320px}} td.mk a{{color:var(--fg);text-decoration:none}}
.m{{color:var(--muted);font-size:11px}} td span{{font-size:11px;opacity:.8}}
td.agree{{background:var(--yesbg);color:var(--yes);font-weight:600}} td.against{{background:var(--nobg);color:var(--no);font-weight:600}}
td.score{{font-weight:700;min-width:110px}} .strong{{color:var(--yes)}} .moderate{{color:var(--mod)}} .weak{{color:var(--no)}}
.warn{{color:var(--mod);font-weight:600}}
</style></head><body><main>
<h1>CONSENSUS MATRIX · PROVEN POLYMARKET WHALES</h1>
<p>{len(whales)} wallets in both Polymarket's all-time top 200 and last-30-days top 200 by profit. Open positions
worth {money(min_usd)}+ in markets 2 or more of them hold. Green = sides with the majority, red = against it.
"Paid → now" is the majority's average entry vs today's price; "price ran" means you'd pay 10¢+ more than they did.
Made {made}.</p>
<p>Whales hedge, scale out and hold positions for reasons you can't see; agreement isn't a guarantee and some of these
lose. Polymarket isn't open to US residents; look for the same game or event on Kalshi. Not financial advice.</p>
<div class=wrap><table><thead><tr><th style="text-align:left">Market</th>{head}<th>Consensus</th><th>Paid → now</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div></main></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Consensus matrix of proven Polymarket whales' open positions.")
    ap.add_argument("--whales", type=int, default=15)
    ap.add_argument("--min", type=float, default=1000, help="smallest position to count, in dollars")
    ap.add_argument("--rows", type=int, default=30)
    ap.add_argument("--alert", action="store_true", help="push new strong agreements to your phone")
    a = ap.parse_args(argv)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    whales = proven_whales(a.whales)
    positions = {}
    for w in whales:
        try:
            positions[w["proxyWallet"]] = open_positions(w["proxyWallet"], a.min, today)
        except Exception as e:
            print(f"{w['userName']}: skipped ({e})", file=sys.stderr)
            positions[w["proxyWallet"]] = []
    all_rows = [r for r in build(whales, positions) if r["whales"] >= 2]
    rows = all_rows[:a.rows]
    made = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(os.path.join(HERE, "consensus.html"), "w") as f:
        f.write(render(whales, rows, made, a.min))
    with open(os.path.join(HERE, "consensus.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["market", "ends", "consensus_pick", "whales_agree", "whales_holding", "consensus", "pick_usd",
                     "avg_entry", "price_now", "all_sides"])
        for r in rows:
            wr.writerow([r["title"], r["end"], r["pick"], r["agree"], r["whales"], f"{r['consensus']:.2f}",
                         round(r["pick_usd"]), None if r["avg_entry"] is None else round(r["avg_entry"], 3),
                         round(r["price"], 3), "; ".join(f"{o}: {n} whales {money(u)}" for o, (n, u) in r["sides"].items())])
    print(f"Proven whales ({len(whales)}): " + ", ".join(f"{w['userName'] or w['proxyWallet'][:8]} ({money(w['pnl'])} 30d)"
                                                       for w in whales))
    print(f"Open positions counted: {sum(len(v) for v in positions.values())}; markets held by 2+ whales: {len(rows)}")
    for r in rows:
        entry = "-" if r["avg_entry"] is None else f"{r['avg_entry'] * 100:.0f}c"
        print(f"{r['agree']}/{r['whales']} {r['consensus']:>4.0%}  {r['pick'][:18]:<18} paid {entry:>4} now {r['price'] * 100:>3.0f}c  "
              f"{money(r['pick_usd']):>8}  {r['title'][:70]}  (ends {r['end']})")
    if a.alert:
        fresh = send_alerts(all_rows, notifier(), time.time())
        print(f"Phone alerts sent: {len(fresh)}" + "".join(f"\n  {r['pick']} ({r['agree']}/{r['whales']}) {r['title']}"
                                                         for r in fresh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
