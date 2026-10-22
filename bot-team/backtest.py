#!/usr/bin/env python3
"""Backtest the bot team's rules on recent 15-minute history. PAPER ONLY: no orders, no keys.

  python backtest.py              last 30 days
  python backtest.py --days 60    a longer stretch (Coinbase keeps plenty of 15-minute history)

It uses the same Analyst and Risk Manager as the live bot, with the same 100-candle window, fees and slippage.
Differences from live, on purpose and on the cautious side:
  - a setup found on a candle's close fills at the NEXT candle's open (live fills a few seconds after the close)
  - stops and take-profits are checked against each candle's high and low; if both are touched in one candle,
    the stop is counted
Results go to logs/backtest_trades.csv and logs/backtest_summary.txt.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

import bot_team
import roles

WINDOW = 100


def fetch_history(coin, days, now, http=roles.http_json):
    """Closed 15-minute candles for the last `days` days, oldest first, paged 300 at a time."""
    slot = roles.CANDLE_SECONDS
    end = int(now // slot) * slot
    start_all = end - days * 86400 - WINDOW * slot
    rows = {}
    while end > start_all:
        start = max(start_all, end - 300 * slot)
        page = http("GET", f"{roles.COINBASE}/products/{coin}-USD/candles?granularity={slot}"
                           f"&start={roles.iso(start)}&end={roles.iso(end)}")
        for t, low, high, open_, close, _ in page:
            if int(t) + slot <= now:
                rows[int(t)] = (int(t), float(open_), float(high), float(low), float(close))
        end = start
        time.sleep(0.25)
    return [rows[t] for t in sorted(rows)]


def hour_of(t):
    return datetime.fromtimestamp(t, timezone.utc).hour


def run(cfg, history, start_ts):
    """Replay every 15-minute slot from start_ts. history: {coin: [(t, o, h, l, c), ...]}."""
    analyst, risk = roles.Analyst(cfg), roles.RiskManager(cfg)
    slip, fee = cfg["slippage_bps"] / 10000, cfg["fee_bps"] / 10000
    index = {coin: {c[0]: i for i, c in enumerate(rows)} for coin, rows in history.items()}
    slots = sorted({c[0] for rows in history.values() for c in rows if c[0] >= start_ts})
    cash, positions, pending, trades, curve = float(cfg["starting_equity"]), {}, {}, [], []
    day, day_start, halted_day = None, None, None
    last_close = {}

    def equity():
        return cash + sum(p["qty"] * last_close.get(c, p["entry"]) for c, p in positions.items())

    def close(coin, price, reason, t):
        nonlocal cash
        p = positions.pop(coin)
        fill = price * (1 - slip)
        exit_fee = p["qty"] * fill * fee
        cash += p["qty"] * fill - exit_fee
        pnl = (fill - p["entry"]) * p["qty"] - p["entry_fee"] - exit_fee
        trades.append({"coin": coin, "opened": p["opened"], "closed": t, "entry": p["entry"], "exit": fill,
                       "qty": p["qty"], "stop": p["stop"], "take_profit": p["take_profit"], "pnl": pnl,
                       "pnl_pct": pnl / (p["qty"] * p["entry"]) * 100, "reason": reason, "why": p["why"],
                       "fees": p["entry_fee"] + exit_fee})

    for t in slots:
        today = bot_team.stamp(t)[:10]
        if today != day:
            day, day_start = today, equity()
        # 1. fill setups from the previous candle at this candle's open
        for coin, setup in list(pending.items()):
            i = index[coin].get(t)
            if i is None:
                continue
            del pending[coin]
            candle = history[coin][i]
            if candle[1] <= setup["stop"]:
                continue  # opened below the stop already: the live bot would see the same thing as a loss
            review = risk.review(dict(setup, price=candle[1]), equity(), cash, positions, halted_day == day)
            if not review["approved"]:
                continue
            entry = candle[1] * (1 + slip)
            entry_fee = review["qty"] * entry * fee
            cash -= review["qty"] * entry + entry_fee
            positions[coin] = {"entry": entry, "qty": review["qty"], "stop": setup["stop"],
                               "take_profit": setup["take_profit"], "opened": t, "entry_fee": entry_fee,
                               "why": setup["reason"]}
        # 2. stops, take-profits and the time limit, using this candle's range
        for coin in list(positions):
            i = index[coin].get(t)
            if i is None:
                continue
            _, o, h, l, c = history[coin][i]
            p = positions[coin]
            if l <= p["stop"]:
                close(coin, min(o, p["stop"]), "stop-loss", t)
            elif h >= p["take_profit"]:
                close(coin, max(o, p["take_profit"]), "take-profit", t)
            elif t + roles.CANDLE_SECONDS - p["opened"] >= cfg["max_hold_hours"] * 3600:
                close(coin, c, "time limit", t)
        for coin, rows in history.items():
            i = index[coin].get(t)
            if i is not None:
                last_close[coin] = rows[i][4]
        # 3. daily loss limit, then look for new setups on this candle's close
        eq = equity()
        if halted_day != day and eq <= day_start * (1 - cfg["daily_loss_limit"]):
            halted_day = day
        curve.append((t, eq))
        for coin, rows in history.items():
            i = index[coin].get(t)
            if i is None or i < WINDOW - 1 or coin in positions or coin in pending:
                continue
            setup = analyst.evaluate(coin, rows[i - WINDOW + 1:i + 1])
            if setup["action"] == "BUY":
                pending[coin] = setup
    end = slots[-1] if slots else start_ts
    for coin in list(positions):
        close(coin, last_close[coin], "end of test", end)
    return trades, curve


def summarize(trades, curve, start_equity):
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    peak, max_dd = None, 0.0
    for _, eq in curve:
        peak = eq if peak is None else max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1)
    gross_loss = -sum(t["pnl"] for t in losses)
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0,
        "pnl": sum(t["pnl"] for t in trades),
        "fees": sum(t["fees"] for t in trades),
        "profit_factor": sum(t["pnl"] for t in wins) / gross_loss if gross_loss else None,
        "max_dd": max_dd,
        "end_equity": curve[-1][1] if curve else start_equity,
    }


def group(trades, key):
    out = {}
    for t in trades:
        g = out.setdefault(key(t), [0, 0, 0.0])
        g[0] += 1
        g[1] += t["pnl"] > 0
        g[2] += t["pnl"]
    return out


def report(cfg, history, trades, curve, start_ts, days):
    s = summarize(trades, curve, cfg["starting_equity"])
    split = curve[int(len(curve) * 0.7)][0] if curve else start_ts
    first = summarize([t for t in trades if t["opened"] < split], [p for p in curve if p[0] < split],
                      cfg["starting_equity"])
    lines = [f"===== BACKTEST: bot team, last {days:g} days of 15-minute candles (PAPER ONLY) =====",
             f"{bot_team.stamp(start_ts)} to {bot_team.stamp(curve[-1][0]) if curve else '-'} | "
             f"{len(history)} coins | fees {cfg['fee_bps']} bps + slippage {cfg['slippage_bps']} bps per side",
             f"Trades: {s['trades']} ({s['wins']} wins, {s['losses']} losses)"
             + (f", win rate {s['win_rate']:.0%}" if trades else ""),
             f"Average win ${s['avg_win']:,.2f} | average loss ${s['avg_loss']:,.2f} | "
             f"profit factor {s['profit_factor']:.2f}" if s["profit_factor"] else "Profit factor: n/a",
             f"Paper account ${cfg['starting_equity']:,.2f} -> ${s['end_equity']:,.2f} "
             f"({s['end_equity'] / cfg['starting_equity'] - 1:+.2%}) | max drawdown {s['max_dd']:.2%} | "
             f"fees and slippage paid ${s['fees']:,.2f}"]
    changes = []
    for rows in history.values():
        inside = [c for c in rows if c[0] >= start_ts]
        if inside:
            changes.append(inside[-1][4] / inside[0][1] - 1)
    if changes:
        lines.append(f"Buy and hold the same {len(changes)} coins in equal amounts: {sum(changes) / len(changes):+.2%}")
    second_trades = [t for t in trades if t["opened"] >= split]
    lines.append(f"First 70% of the period: {first['trades']} trades, P&L ${first['pnl']:,.2f} | "
                 f"last 30%: {len(second_trades)} trades, P&L ${sum(t['pnl'] for t in second_trades):,.2f}")
    lines.append("How trades ended:")
    for reason, (n, w, pnl) in sorted(group(trades, lambda t: t["reason"]).items(), key=lambda x: -x[1][0]):
        lines.append(f"  {reason:<12} {n:>4} trades  {w / n:>4.0%} won  P&L ${pnl:>10,.2f}")
    lines.append("By coin:")
    for coin, (n, w, pnl) in sorted(group(trades, lambda t: t["coin"]).items(), key=lambda x: x[1][2]):
        lines.append(f"  {coin:<5} {n:>4} trades  {w / n:>4.0%} won  P&L ${pnl:>10,.2f}")
    lines.append("By entry time (UTC, 4-hour blocks):")
    for block, (n, w, pnl) in sorted(group(trades, lambda t: hour_of(t["opened"]) // 4 * 4).items()):
        lines.append(f"  {block:02d}:00-{block + 3:02d}:59 {n:>4} trades  {w / n:>4.0%} won  P&L ${pnl:>10,.2f}")
    lines.append("Past results, made-up fills. Not a promise about the future and not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest the paper bot team on recent 15-minute candles.")
    parser.add_argument("--days", type=float, default=30)
    args = parser.parse_args(argv)
    cfg = bot_team.load_config()
    now = time.time()
    history, problems = {}, {}
    for coin in cfg["coins"]:
        try:
            history[coin] = fetch_history(coin, args.days, now)
            print(f"{coin}: {len(history[coin])} candles", flush=True)
        except Exception as e:
            problems[coin] = str(e)
            print(f"{coin}: left out ({e})", file=sys.stderr)
    if not history:
        sys.exit("No market data could be downloaded.")
    start_ts = int((now - args.days * 86400) // roles.CANDLE_SECONDS) * roles.CANDLE_SECONDS
    trades, curve = run(cfg, history, start_ts)
    text = report(cfg, history, trades, curve, start_ts, args.days)
    print(text)
    os.makedirs(bot_team.LOG_DIR, exist_ok=True)
    with open(os.path.join(bot_team.LOG_DIR, "backtest_summary.txt"), "w") as f:
        f.write(text + "\n")
    path = os.path.join(bot_team.LOG_DIR, "backtest_trades.csv")
    if os.path.exists(path):
        os.remove(path)
    for t in trades:
        bot_team.append_csv(path, ["opened", "closed", "coin", "entry", "exit", "qty", "stop", "take_profit", "fees",
                                   "pnl", "pnl_pct", "reason", "why"],
                            dict(t, opened=bot_team.stamp(t["opened"]), closed=bot_team.stamp(t["closed"]),
                                 pnl=round(t["pnl"], 2), pnl_pct=round(t["pnl_pct"], 3), fees=round(t["fees"], 2)))
    print(f"Trades: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
