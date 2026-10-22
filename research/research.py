#!/usr/bin/env python3
"""Strategy research: test a fixed set of textbook rules on many markets, with Trench-style costs, and keep only
what holds up on data the rules never saw. PAPER RESEARCH ONLY: no orders, no keys.

  python research.py            download history, test every rule on every market, write REPORT.md and selected.json
  python research.py --notify   the same, then send the daily digest to your phone (trading-bot's ntfy channel)

The method is fixed up front, so the results can't be tuned to look good:
  - rules use standard settings from trading literature; nothing is optimized
  - each signal is acted on at the NEXT bar's open (no peeking at the close you trade on)
  - costs: 0.1% slippage per side, plus Trench's 8% fee on winning trades, at x1 (no multiplier)
  - history is split: the first 60% ("in-sample") and the last 40% ("out-of-sample")
  - a market/rule pair PASSES only if: 15+ trades, 5+ of them out-of-sample, profit factor > 1.3 in BOTH halves,
    and the out-of-sample return beats simply holding the market over the same stretch
  - a rule/timeframe is only SELECTED for signals if it passes on at least 3 markets, so one lucky fit can't get in
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "market-sphere"))
import marketdata  # noqa: E402

DAY = 86400
SLIP = 0.001
WIN_FEE = 0.08
IN_SAMPLE = 0.6
MIN_TRADES, MIN_OOS_TRADES, MIN_PF, MIN_MARKETS = 15, 5, 1.3, 3
CONFIRM_DAYS = 5
YEARS = 6

MARKETS = [
    {"symbol": s, "name": n, "source": "coinbase", "trench": t} for s, n, t in [
        ("BTC-USD", "Bitcoin", "BTC"), ("ETH-USD", "Ethereum", "ETH"), ("SOL-USD", "Solana", "SOL"),
        ("XRP-USD", "XRP", "XRP"), ("DOGE-USD", "Dogecoin", "DOGE"), ("ADA-USD", "Cardano", "ADA"),
        ("AVAX-USD", "Avalanche", "AVAX"), ("LINK-USD", "Chainlink", "LINK"), ("LTC-USD", "Litecoin", "LTC"),
        ("BCH-USD", "Bitcoin Cash", "BCH")]
] + [
    {"symbol": s, "name": n, "source": "yahoo", "trench": t} for s, n, t in [
        ("GC=F", "Gold", "GOLD"), ("SI=F", "Silver", "SILVER"), ("CL=F", "WTI crude oil", "CL"),
        ("ES=F", "S&P 500", "SP500"), ("NQ=F", "Nasdaq 100", "NAS100")]
]


# ---------- data ----------

def fetch_coinbase_6h(symbol, days, now, get=marketdata.http_get_json):
    step = 21600
    rows, end = {}, int(now // step) * step
    start_all = end - days * DAY
    while end > start_all:
        start = max(start_all, end - 299 * step)
        page = get(f"https://api.exchange.coinbase.com/products/{symbol}/candles"
                   f"?granularity={step}&start={marketdata.iso(start)}&end={marketdata.iso(end)}")
        if not page:
            break
        for t, low, high, open_, close, _ in page:
            if int(t) + step <= now:
                rows[int(t)] = (int(t), float(open_), float(high), float(low), float(close))
        end = start - step
        time.sleep(0.25)
    return [rows[t] for t in sorted(rows)]


def fetch_daily(market, days, now):
    through = datetime.fromtimestamp(now - DAY, timezone.utc).strftime("%Y-%m-%d")
    candles, _ = marketdata.get_candles(market, days, now, through)
    out = []
    for d, o, h, l, c, _v in candles:
        t = int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        out.append((t, o, h, l, c))
    return out


# ---------- indicators ----------

def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k, avg = 2 / (n + 1), sum(values[:n]) / n
    out[n - 1] = avg
    for i in range(n, len(values)):
        avg = values[i] * k + avg * (1 - k)
        out[i] = avg
    return out


def sma(values, n):
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(c, 0) for c in ch[:n]) / n
    loss = sum(max(-c, 0) for c in ch[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            c = ch[i - 1]
            gain = (gain * (n - 1) + max(c, 0)) / n
            loss = (loss * (n - 1) + max(-c, 0)) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


# ---------- rules: each returns the target position after every bar's close (+1 long, -1 short, 0 flat) ----------

def rule_ema_trend(c):
    """Trend: long when close > EMA50 > EMA200, short when close < EMA50 < EMA200; exit when close crosses EMA50."""
    closes = [x[4] for x in c]
    f, s = ema(closes, 50), ema(closes, 200)
    pos, out = 0, []
    for i, close in enumerate(closes):
        if f[i] is None or s[i] is None:
            out.append(0)
            continue
        if (pos == 1 and close < f[i]) or (pos == -1 and close > f[i]):
            pos = 0
        if pos == 0:
            pos = 1 if close > f[i] > s[i] else -1 if close < f[i] < s[i] else 0
        out.append(pos)
    return out


def rule_donchian(c, entry=20, exit_=10):
    """Breakout (Turtle style): long on a close above the 20-bar high, short below the 20-bar low;
    exit on a close beyond the opposite 10-bar extreme."""
    pos, out = 0, []
    for i in range(len(c)):
        if i < entry:
            out.append(0)
            continue
        close = c[i][4]
        hi_n, lo_n = max(x[2] for x in c[i - entry:i]), min(x[3] for x in c[i - entry:i])
        hi_x, lo_x = max(x[2] for x in c[i - exit_:i]), min(x[3] for x in c[i - exit_:i])
        if (pos == 1 and close < lo_x) or (pos == -1 and close > hi_x):
            pos = 0
        if pos == 0:
            pos = 1 if close > hi_n else -1 if close < lo_n else 0
        out.append(pos)
    return out


def rule_momentum(c, n=90):
    """Time-series momentum: long if the price is higher than 90 bars ago, short if lower."""
    closes = [x[4] for x in c]
    return [0 if i < n else (1 if closes[i] > closes[i - n] else -1) for i in range(len(closes))]


def rule_rsi2(c):
    """Mean reversion (Connors RSI-2), long only: buy when RSI(2) < 10 in an uptrend (close > SMA200);
    sell when the close gets back above the 5-bar average."""
    closes = [x[4] for x in c]
    r, s200, s5 = rsi(closes, 2), sma(closes, 200), sma(closes, 5)
    pos, out = 0, []
    for i, close in enumerate(closes):
        if None in (r[i], s200[i], s5[i]):
            out.append(0)
            continue
        if pos == 1 and close > s5[i]:
            pos = 0
        elif pos == 0 and r[i] < 10 and close > s200[i]:
            pos = 1
        out.append(pos)
    return out


RULES = {
    "trend_ema50_200": (rule_ema_trend, "Trend: close vs EMA50/EMA200, exit on EMA50 cross"),
    "breakout_20_10": (rule_donchian, "Breakout: 20-bar high/low entry, 10-bar exit"),
    "momentum_90": (rule_momentum, "Momentum: price vs 90 bars ago, always in"),
    "rsi2_dip": (rule_rsi2, "Dip-buy: RSI(2) < 10 above SMA200, exit above SMA5 (long only)"),
}


# ---------- simulation ----------

def simulate(c, target, start_i):
    """Trades from target positions, filled at the next bar's open, with Trench-style costs.
    Returns trades [(entry_t, exit_t, side, ret)] and a marked-to-market equity curve [(t, equity)]."""
    trades, curve = [], []
    eq, pos, entry_px, entry_t = 1.0, 0, None, None
    for i in range(start_i, len(c) - 1):
        want, nxt = target[i], c[i + 1]
        if want != pos:
            if pos:
                exit_px = nxt[1] * (1 - pos * SLIP)
                ret = pos * (exit_px / entry_px - 1)
                ret = ret * (1 - WIN_FEE) if ret > 0 else ret
                eq *= 1 + ret
                trades.append((entry_t, nxt[0], pos, ret))
            pos = want
            if pos:
                entry_px, entry_t = nxt[1] * (1 + pos * SLIP), nxt[0]
        mark = eq * (1 + pos * (nxt[4] / entry_px - 1)) if pos else eq
        curve.append((nxt[0], mark))
    return trades, curve


def stats(trades, curve, c, t0, t1):
    tr = [x for x in trades if t0 <= x[0] < t1]
    cv = [x for x in curve if t0 <= x[0] < t1]
    wins, losses = [x[3] for x in tr if x[3] > 0], [x[3] for x in tr if x[3] <= 0]
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else (math.inf if wins else None)
    ret = (cv[-1][1] / cv[0][1] - 1) if len(cv) > 1 else 0.0
    peak, dd = None, 0.0
    for _, e in cv:
        peak = e if peak is None else max(peak, e)
        dd = min(dd, e / peak - 1)
    inside = [x for x in c if t0 <= x[0] < t1]
    hold = inside[-1][4] / inside[0][1] - 1 if len(inside) > 1 else 0.0
    return {"trades": len(tr), "win_rate": len(wins) / len(tr) if tr else None, "pf": pf, "ret": ret, "dd": dd,
            "hold": hold}


def evaluate(c, rule):
    target = rule(c)
    start_i = 200  # same warm-up for every rule, so they're all scored on the same bars
    if len(c) < start_i + 100:
        return None
    trades, curve = simulate(c, target, start_i)
    t_start, t_end = c[start_i][0], c[-1][0] + 1
    t_split = c[start_i + int((len(c) - start_i) * IN_SAMPLE)][0]
    full, ins, oos = (stats(trades, curve, c, a, b) for a, b in
                      ((t_start, t_end), (t_start, t_split), (t_split, t_end)))
    passed = (full["trades"] >= MIN_TRADES and oos["trades"] >= MIN_OOS_TRADES
              and (ins["pf"] or 0) > MIN_PF and (oos["pf"] or 0) > MIN_PF and oos["ret"] > oos["hold"])
    return {"full": full, "in": ins, "oos": oos, "passed": passed, "split": t_split, "start": t_start,
            "position": target[-1]}


def fmt_pf(x):
    return "n/a" if x is None else ("inf" if x == math.inf else f"{x:.2f}")


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def pair_key(rule, tf, symbol):
    return f"{rule}|{tf}|{symbol}"


def confirm_selection(rows, previous, streaks, first_run):
    """Which pairs give live signals. A pair joins after passing CONFIRM_DAYS runs in a row and leaves after failing
    CONFIRM_DAYS runs in a row, so one day's wobble can't switch alerts on or off. A rule/timeframe still needs
    MIN_MARKETS confirmed markets. Pairs with no data today keep their streaks and their place."""
    for r in rows:
        st = streaks.setdefault(pair_key(r["rule"], r["tf"], r["symbol"]), {"pass": 0, "fail": 0})
        st["pass"], st["fail"] = (st["pass"] + 1, 0) if r["passed"] else (0, st["fail"] + 1)
    passing = {pair_key(r["rule"], r["tf"], r["symbol"]) for r in rows if r["passed"]}
    if first_run:
        confirmed = passing
    else:
        kept = {pair_key(p["rule"], p["tf"], p["symbol"]) for p in previous}
        confirmed = {k for k in kept if streaks.get(k, {}).get("fail", 0) < CONFIRM_DAYS}
        confirmed |= {k for k in passing if streaks[k]["pass"] >= CONFIRM_DAYS}
    groups = {}
    for k in confirmed:
        rule, tf, _ = k.split("|")
        groups.setdefault((rule, tf), set()).add(k)
    return {k for ks in groups.values() if len(ks) >= MIN_MARKETS for k in ks}


def paper_record(journal_path=os.path.join(HERE, "..", "trading-bot", "journal.csv")):
    """The live paper results so far for markets that send real alerts."""
    try:
        with open(journal_path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["tested"] == "yes"]
    except FileNotFoundError:
        rows = []
    wins = sum(float(r["after_fee_pct"]) > 0 for r in rows)
    total = sum(float(r["after_fee_pct"]) for r in rows)
    return (f"Live paper record: {len(rows)} closed trades, {wins} wins, {total:+.1f}% added up after fees. "
            f"Judge it after 30 trades, not before.")


def digest(previous, selected, streaks, rows):
    """The daily phone message: where signals come from, what changed, what's close to changing, live record."""
    def label(k):
        rule, tf, sym = k.split("|")
        trench = next((m["trench"] for m in MARKETS if m["symbol"] == sym), sym)
        return f"{trench} {rule} {tf}"

    old = {pair_key(p["rule"], p["tf"], p["symbol"]) for p in previous}
    new = {pair_key(p["rule"], p["tf"], p["symbol"]) for p in selected}
    lines = ["Signals come from: " + (", ".join(sorted(label(k) for k in new)) or "nothing (no rule passed)")]
    if new - old:
        lines.append("ADDED (passed 5 days running): " + ", ".join(sorted(label(k) for k in new - old)))
    if old - new:
        lines.append("DROPPED (alerts stop, paper only): " + ", ".join(sorted(label(k) for k in old - new)))
    rising = sorted(f"{label(k)} {v['pass']}/{CONFIRM_DAYS}" for k, v in streaks.items()
                    if k not in new and 0 < v["pass"] < CONFIRM_DAYS)
    slipping = sorted(f"{label(k)} {v['fail']}/{CONFIRM_DAYS}" for k, v in streaks.items()
                      if k in new and v["fail"] > 0)
    if rising:
        lines.append("Close to joining: " + ", ".join(rising))
    if slipping:
        lines.append("Failing lately: " + ", ".join(slipping))
    lines.append(f"Tested today: {len(rows)} rule/market pairs, {sum(r['passed'] for r in rows)} passed.")
    lines.append(paper_record())
    return "\n".join(lines)


def notify(text):
    sys.path.insert(0, os.path.join(HERE, "..", "trading-bot"))
    import bot
    bot.make_notifier(bot.load_config())("Daily research", text, "default")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Test textbook rules on many markets; keep what holds up.")
    parser.add_argument("--notify", action="store_true", help="message your phone with what changed")
    args = parser.parse_args(argv)
    previous = (json.load(open(os.path.join(HERE, "selected.json"))) if os.path.exists(os.path.join(HERE, "selected.json"))
                else {}).get("selected", [])
    now = time.time()
    data = {}
    for m in MARKETS:
        try:
            data[(m["symbol"], "1d")] = fetch_daily(m, YEARS * 365, now)
            if m["source"] == "coinbase":
                data[(m["symbol"], "6h")] = fetch_coinbase_6h(m["symbol"], 3 * 365, now)
            print(f"{m['symbol']}: " + ", ".join(f"{tf} {len(data[(m['symbol'], tf)])} bars"
                                                  for tf in ("1d", "6h") if (m["symbol"], tf) in data), flush=True)
        except Exception as e:
            print(f"{m['symbol']}: left out ({e})", file=sys.stderr)

    names = {m["symbol"]: m for m in MARKETS}
    rows = []
    for (sym, tf), c in data.items():
        for key, (rule, _) in RULES.items():
            r = evaluate(c, rule)
            if r:
                rows.append({"symbol": sym, "trench": names[sym]["trench"], "tf": tf, "rule": key, **r})

    with open(os.path.join(HERE, "results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule", "timeframe", "market", "trench", "passed", "trades", "win_rate", "pf_all", "pf_in", "pf_out",
                    "return_all", "return_out", "hold_out", "max_dd_all", "position_now"])
        for r in rows:
            fu, i, o = r["full"], r["in"], r["oos"]
            w.writerow([r["rule"], r["tf"], r["symbol"], r["trench"], r["passed"], fu["trades"],
                        f"{fu['win_rate']:.2f}" if fu["win_rate"] is not None else "", fmt_pf(fu["pf"]),
                        fmt_pf(i["pf"]), fmt_pf(o["pf"]), f"{fu['ret']:.3f}", f"{o['ret']:.3f}", f"{o['hold']:.3f}",
                        f"{fu['dd']:.3f}", r["position"]])

    streaks_path = os.path.join(HERE, "streaks.json")
    first_run = not previous and not os.path.exists(streaks_path)
    streaks = json.load(open(streaks_path)) if os.path.exists(streaks_path) else {}
    confirmed = confirm_selection(rows, previous, streaks, first_run)
    by_key = {pair_key(r["rule"], r["tf"], r["symbol"]): r for r in rows}
    prev_by_key = {pair_key(p["rule"], p["tf"], p["symbol"]): p for p in previous}

    selected, lines = [], []
    lines.append("# Strategy research report\n")
    lines.append(f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. {len(MARKETS)} markets, {len(RULES)} rules, "
                 "daily candles (6 years) and 6-hour candles (crypto, 3 years). Costs: 0.1% slippage per side + "
                 "Trench's 8% fee on wins, x1. Fills at the next bar's open. Paper research, not financial advice.\n")
    lines.append(f"A pair passes with {MIN_TRADES}+ trades ({MIN_OOS_TRADES}+ out-of-sample), profit factor > {MIN_PF} "
                 "in both the first 60% and the last 40% of history, and an out-of-sample return above buy-and-hold. "
                 f"It joins the live signals after passing {CONFIRM_DAYS} daily runs in a row and leaves after failing "
                 f"{CONFIRM_DAYS} in a row. A rule needs {MIN_MARKETS}+ confirmed markets.\n")
    lines.append("| Rule | Timeframe | Markets tested | Passed today | Passing today |")
    lines.append("|---|---|---|---|---|")
    for key, (_, desc) in RULES.items():
        for tf in ("1d", "6h"):
            group = [r for r in rows if r["rule"] == key and r["tf"] == tf]
            if group:
                ok = [r for r in group if r["passed"]]
                lines.append(f"| {desc} | {tf} | {len(group)} | {len(ok)} | {', '.join(r['trench'] for r in ok) or '-'} |")
    for k in sorted(confirmed):
        r = by_key.get(k)
        if r is None:
            selected.append(prev_by_key[k])
            continue
        selected.append({"rule": r["rule"], "tf": r["tf"], "symbol": r["symbol"], "trench": r["trench"],
                         "source": names[r["symbol"]]["source"], "name": names[r["symbol"]]["name"],
                         "trades": r["full"]["trades"], "pf_in": r["in"]["pf"], "pf_out": r["oos"]["pf"],
                         "ret_out": r["oos"]["ret"], "hold_out": r["oos"]["hold"], "max_dd": r["full"]["dd"],
                         "win_rate": r["full"]["win_rate"], "out_of_sample_from": day(r["split"]),
                         "passed_today": r["passed"]})
    lines.append("")
    if selected:
        lines.append("## Selected for live signals\n")
        lines.append("| Market (Trench) | Rule | TF | Trades | Win rate | PF in / out | Out-of-sample return vs hold | Max drawdown | Passed today |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for s in selected:
            lines.append(f"| {s['trench']} | {s['rule']} | {s['tf']} | {s['trades']} | {s['win_rate']:.0%} | "
                         f"{fmt_pf(s['pf_in'])} / {fmt_pf(s['pf_out'])} | {s['ret_out']:+.0%} vs {s['hold_out']:+.0%} | "
                         f"{s['max_dd']:.0%} | {'yes' if s.get('passed_today', True) else 'no'} |")
    else:
        lines.append("## Nothing selected\n\nNo rule is confirmed on enough markets. No live signals until one is.")
    lines.append("\nThese are past results with simulated fills. Real fills, holding fees and gaps will differ. "
                 "Many rule/market pairs were tested, so some passes can still be luck; that's why a rule needs "
                 f"{MIN_MARKETS}+ markets and a {CONFIRM_DAYS}-day streak.")
    with open(os.path.join(HERE, "REPORT.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    for s in selected:
        for k in ("pf_in", "pf_out"):
            if s[k] == math.inf:
                s[k] = None
    with open(os.path.join(HERE, "selected.json"), "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "selected": selected}, f,
                  indent=1)
    with open(streaks_path, "w") as f:
        json.dump(streaks, f, indent=1, sort_keys=True)
    print("\n".join(lines))
    text = digest(previous, selected, streaks, rows)
    print("\n" + text)
    if args.notify:
        notify(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
