#!/usr/bin/env python3
"""Backtest both box-theory strategies on 50 liquid stocks and 20 crypto pairs, with costs. Read-only, no orders.

  python backtest.py              ~2.5 years (config.yaml backtest.years)
  python backtest.py --years 3

Uses the exact signal functions the live scanner uses. For every signal on a closed bar it buys at the NEXT bar's
open (that's when the bot's order would fill), then exits at the stop or target (stop first if a bar hits both),
or at the next open after an exit signal. Slippage 0.1% per side, plus Alpaca's crypto fee per side.
One position per symbol at a time; the portfolio limits in config.yaml are not modelled.
Results in R: 1R = the money at risk between entry and stop. Results are also saved to backtest_results/.
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from data import BarCache, MarketData
from settings import ROOT, load_config, load_credentials, path, setup_logging
from strategies.box_theory import Bars, evaluate, params_for

STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA", "UNH", "XOM", "LLY",
          "JNJ", "PG", "HD", "COST", "ABBV", "MRK", "PEP", "KO", "BAC", "WMT", "CVX", "CRM", "NFLX", "AMD", "ADBE",
          "ORCL", "TMO", "ACN", "MCD", "CSCO", "ABT", "LIN", "DIS", "WFC", "INTU", "QCOM", "TXN", "CAT", "IBM", "GE",
          "AMAT", "NOW", "UBER", "GS", "PFE", "BA"]
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "AVAX/USD", "LINK/USD", "LTC/USD", "BCH/USD",
          "DOT/USD", "UNI/USD", "AAVE/USD", "SHIB/USD", "CRV/USD", "GRT/USD", "SUSHI/USD", "XTZ/USD", "BAT/USD",
          "YFI/USD", "MKR/USD"]
log = logging.getLogger("box.backtest")


@dataclass
class Trade:
    symbol: str
    asset_class: str
    timeframe: str
    strategy: str
    direction: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    stop: float
    r: float
    reason: str


def simulate(df, symbol: str, asset_class: str, timeframe: str, strategy: str, cfg: Dict, cost: float,
             start_index: int) -> List[Trade]:
    box_p, _ = params_for(cfg, asset_class)
    bars = Bars(df, box_p.volume_avg_bars)
    o, h, l, c = bars.o, bars.h, bars.l, bars.c
    trades: List[Trade] = []
    i, n = start_index, len(bars)
    while i < n - 1:
        sigs = [s for s in evaluate(df, strategy, symbol, asset_class, timeframe, cfg, i=i, bars=bars)
                if s.direction in ("long", "short")]
        if not sigs:
            i += 1
            continue
        s = sigs[0]
        side = 1 if s.direction == "long" else -1
        e = i + 1
        entry = o[e] * (1 + side * cost)
        risk = (entry - s.stop) * side
        if risk <= 0:  # opened beyond the stop: the order would be pointless
            i += 1
            continue
        exit_px, reason, j = None, "", e
        while j < n:
            hit_stop = l[j] <= s.stop if side == 1 else h[j] >= s.stop
            hit_target = h[j] >= s.target if side == 1 else l[j] <= s.target
            if hit_stop:  # stop first when a bar touches both (the cautious assumption)
                exit_px, reason = (min(o[j], s.stop) if side == 1 else max(o[j], s.stop)), "stop"
            elif hit_target:
                exit_px, reason = (max(o[j], s.target) if side == 1 else min(o[j], s.target)), "target"
            elif j + 1 < n and any(x.direction == "exit" for x in
                                   evaluate(df, strategy, symbol, asset_class, timeframe, cfg, i=j, bars=bars)) \
                    and side == 1:
                j += 1
                exit_px, reason = o[j], "exit signal"
            if exit_px is not None:
                break
            j += 1
        if exit_px is None:
            exit_px, reason, j = c[n - 1], "end of test", n - 1
        exit_px *= (1 - side * cost)
        trades.append(Trade(symbol, asset_class, timeframe, strategy, s.direction, str(bars.times[e])[:16],
                            str(bars.times[j])[:16], entry, exit_px, s.stop, (exit_px - entry) * side / risk, reason))
        i = j + 1
    return trades


def stats(trades: List[Trade]) -> Dict:
    rs = [t.r for t in sorted(trades, key=lambda t: t.exit_time)]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    peak = dd = run = 0.0
    for r in rs:
        run += r
        peak = max(peak, run)
        dd = min(dd, run - peak)
    return {"trades": len(rs), "win_rate": len(wins) / len(rs) if rs else 0.0, "avg_r": sum(rs) / len(rs) if rs else 0.0,
            "total_r": sum(rs), "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else math.inf,
            "max_dd_r": dd}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the box-theory strategies (read-only).")
    ap.add_argument("--years", type=float)
    a = ap.parse_args(argv)
    cfg = load_config()
    creds = load_credentials()
    setup_logging(cfg, creds)
    years = a.years or cfg["backtest"]["years"]
    md = MarketData(creds, cfg, BarCache(path(cfg, "cache_db")))
    slip, fee = cfg["backtest"]["slippage"], cfg["backtest"]["crypto_fee"]
    plan = [("stocks", "1Day", STOCKS, slip)] + [("crypto", tf, CRYPTO, slip + fee)
                                                 for tf in cfg["timeframes"]["crypto"]]
    all_trades: List[Trade] = []
    for asset_class, tf, universe, cost in plan:
        per_day = 1 if tf == "1Day" else 6
        days = years * (252 if asset_class == "stocks" else 365)
        warmup = md.bars_needed(asset_class)
        bars_wanted = int(days * per_day) + warmup
        data = md.closed_bars(universe, asset_class, tf, bars=bars_wanted)
        missing = sorted(set(universe) - set(data))
        if missing:
            log.warning("%s %s: no data for %s", asset_class, tf, ", ".join(missing))
        for strategy in ("darvas_breakout", "range_reversion"):
            for sym, df in data.items():
                start = max(warmup, len(df) - int(days * per_day))
                if len(df) <= start + 2:
                    continue
                try:
                    all_trades += simulate(df, sym, asset_class, tf, strategy, cfg, cost, start)
                except Exception:
                    log.exception("%s %s %s %s failed", sym, asset_class, tf, strategy)
            log.info("%s %s %s: done", asset_class, tf, strategy)

    out_dir = ROOT / "backtest_results"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / "trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(Trade.__dataclass_fields__))
        for t in all_trades:
            w.writerow([getattr(t, k) for k in Trade.__dataclass_fields__])

    lines = [f"BACKTEST: {years:g} years · slippage {slip:.1%}/side · crypto fee {fee:.2%}/side · "
             "entry at next open · stop first if a bar hits both · not financial advice", "",
             f"{'strategy':<17} {'market':<14} {'trades':>6} {'win%':>6} {'avg R':>7} {'total R':>8} {'PF':>6} "
             f"{'max DD R':>9}"]
    groups: Dict[tuple, List[Trade]] = {}
    for t in all_trades:
        groups.setdefault((t.strategy, f"{t.asset_class} {t.timeframe}"), []).append(t)
        groups.setdefault((t.strategy, f"{t.asset_class} (all)"), []).append(t)
    warnings = []
    for (strategy, market), ts in sorted(groups.items()):
        s = stats(ts)
        pf = "inf" if s["profit_factor"] == math.inf else f"{s['profit_factor']:.2f}"
        lines.append(f"{strategy:<17} {market:<14} {s['trades']:>6} {s['win_rate']:>6.0%} {s['avg_r']:>+7.2f} "
                     f"{s['total_r']:>+8.1f} {pf:>6} {s['max_dd_r']:>9.1f}")
        if s["trades"] < 30:
            warnings.append(f"WARNING: {strategy} on {market} has only {s['trades']} trades: too few to judge.")
    lines += [""] + warnings + ["", "At 1% risk per trade, a max drawdown of -10 R is roughly a 10% account drawdown.",
                                f"Every trade: {out_dir / 'trades.csv'}"]
    report = "\n".join(lines)
    (out_dir / "summary.txt").write_text(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
