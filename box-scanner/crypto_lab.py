#!/usr/bin/env python3
"""Crypto lab: test faster (4-hour) box rules on crypto with crypto-specific limits, as a real account would trade
them. Read-only, no orders.

  python crypto_lab.py

Fixed before looking at any result (no tuning afterwards):
  A  range_reversion 4h, 20 pairs, max 3 open, buy only when BTC is above its 200-bar average
  B  darvas_breakout 4h, 20 pairs, max 3 open, same BTC filter
  C  range_reversion 4h, BTC/ETH/SOL only, max 2 open, same BTC filter
  (plus the unlimited versions of A and B for comparison)

Simulation: $100,000 start; 1% of equity at risk per trade; no position over 10% of equity; enter at the next bar's
open; exit at the stop, the target (stop first if a bar hits both) or the next open after an exit signal; 0.1%
slippage + 0.25% fee per side. Positions are sized off current equity, so wins and losses compound.

PASS = profit factor >= 1.3 overall AND in both the first 60% and the last 40% of the period, max drawdown < 15%,
30+ trades. A passing setup is written to backtest_results/crypto_lab.json for the live bot to use.
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from backtest import CRYPTO
from data import BarCache, MarketData
from settings import ROOT, load_config, load_credentials, path, setup_logging
from strategies.box_theory import Bars, evaluate, params_for

START_EQUITY = 100_000.0
PASS_PF, PASS_DD, PASS_TRADES = 1.3, 0.15, 30


@dataclass
class Setup:
    name: str
    strategy: str
    symbols: List[str]
    max_open: int
    btc_filter: bool


@dataclass
class Position:
    symbol: str
    units: float
    entry: float
    stop: float
    target: float
    opened: int


@dataclass
class Result:
    setup: Setup
    trades: List[Dict] = field(default_factory=list)
    curve: List[float] = field(default_factory=list)


def precompute(df, symbol: str, strategy: str, cfg: Dict, asset_class: str = "crypto", timeframe: str = "4Hour"):
    """Entry signal (or None) and exit flag for every bar of one symbol."""
    box_p, _ = params_for(cfg, asset_class)
    bars = Bars(df, box_p.volume_avg_bars)
    entries: Dict[int, object] = {}
    exits = set()
    for i in range(len(bars)):
        for s in evaluate(df, strategy, symbol, asset_class, timeframe, cfg, i=i, bars=bars):
            if s.direction == "long":
                entries[i] = s
            elif s.direction == "exit":
                exits.add(i)
    return bars, entries, exits


def simulate(setup: Setup, data: Dict, pre: Dict, btc_up: Dict, times: List, cost: float, start_t) -> Result:
    res = Result(setup)
    cash = START_EQUITY
    open_: Dict[str, Position] = {}
    pending: List = []  # (symbol, signal) to fill at the next bar's open
    index = {s: {t: i for i, t in enumerate(pre[(s, setup.strategy)][0].times)} for s in setup.symbols if (s, setup.strategy) in pre}

    def equity_at(t) -> float:
        eq = cash
        for p in open_.values():
            i = index[p.symbol].get(t)
            px = pre[(p.symbol, setup.strategy)][0].c[i] if i is not None else p.entry
            eq += p.units * px
        return eq

    for t in times:
        if t < start_t:
            continue
        # 1. fill yesterday's signals at this bar's open
        for sym, s in pending:
            i = index[sym].get(t)
            if i is None or sym in open_ or len(open_) >= setup.max_open:
                continue
            bars = pre[(sym, setup.strategy)][0]
            entry = bars.o[i] * (1 + cost)
            if entry <= s.stop:
                continue
            eq = equity_at(t)
            units = min(eq * 0.01 / (entry - s.stop), eq * 0.10 / entry)
            if units <= 0 or units * entry > cash:
                continue
            cash -= units * entry
            open_[sym] = Position(sym, units, entry, s.stop, s.target, i)
        pending = []
        # 2. exits
        for sym in list(open_):
            bars, _, exits = pre[(sym, setup.strategy)]
            i = index[sym].get(t)
            if i is None:
                continue
            p = open_[sym]
            px = None
            if bars.l[i] <= p.stop:
                px, why = min(bars.o[i], p.stop), "stop"
            elif bars.h[i] >= p.target:
                px, why = max(bars.o[i], p.target), "target"
            elif i in exits and i > p.opened and i + 1 < len(bars):
                px, why = bars.o[i + 1], "exit signal"
            if px is not None:
                out = px * (1 - cost)
                cash += p.units * out
                res.trades.append({"symbol": sym, "time": str(t), "r": (out - p.entry) / (p.entry - p.stop),
                                   "pnl": p.units * (out - p.entry), "why": why})
                del open_[sym]
        # 3. new signals on this closed bar, best volume first
        cands = []
        for sym in setup.symbols:
            if (sym, setup.strategy) not in pre or sym in open_:
                continue
            i = index[sym].get(t)
            s = pre[(sym, setup.strategy)][1].get(i) if i is not None else None
            if s and (not setup.btc_filter or btc_up.get(t, False)):
                cands.append((sym, s))
        cands.sort(key=lambda x: -x[1].volume_ratio)
        pending = cands[: max(0, setup.max_open - len(open_))]
        res.curve.append(equity_at(t))
    return res


def summarize(trades: List[Dict], curve: List[float], start: float) -> Dict:
    pnl = [x["pnl"] for x in trades]
    won, lost = sum(v for v in pnl if v > 0), -sum(v for v in pnl if v <= 0)
    peak, dd = start, 0.0
    for e in curve:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    return {"trades": len(trades), "win_rate": sum(v > 0 for v in pnl) / len(pnl) if pnl else 0.0,
            "pf": won / lost if lost else (math.inf if won else 0.0), "return": (curve[-1] / start - 1) if curve else 0.0,
            "max_dd": dd}


def main() -> int:
    cfg = load_config()
    creds = load_credentials()
    log = setup_logging(cfg, creds)
    md = MarketData(creds, cfg, BarCache(path(cfg, "cache_db")))
    years = cfg["backtest"]["years"]
    warm = md.bars_needed("crypto")
    data = md.closed_bars(CRYPTO, "crypto", "4Hour", bars=int(years * 365 * 6) + warm)
    log.info("crypto lab: %d pairs with 4-hour data", len(data))
    setups = [Setup("A range 3-max BTC-filter", "range_reversion", CRYPTO, 3, True),
              Setup("B darvas 3-max BTC-filter", "darvas_breakout", CRYPTO, 3, True),
              Setup("C range BTC/ETH/SOL 2-max", "range_reversion", ["BTC/USD", "ETH/USD", "SOL/USD"], 2, True),
              Setup("(A without limits)", "range_reversion", CRYPTO, 20, False),
              Setup("(B without limits)", "darvas_breakout", CRYPTO, 20, False)]
    pre = {}
    for strategy in {s.strategy for s in setups}:
        for sym, df in data.items():
            pre[(sym, strategy)] = precompute(df, sym, strategy, cfg)
        log.info("crypto lab: signals computed for %s", strategy)
    btc = data["BTC/USD"]
    sma = btc["close"].rolling(200).mean()
    btc_up = {t: bool(c > m) for t, c, m in zip(btc.index, btc["close"], sma) if m == m}
    times = sorted({t for df in data.values() for t in df.index})
    start_t = times[max(0, len(times) - int(years * 365 * 6))]
    split_t = times[len(times) - int(years * 365 * 6 * 0.4)]
    cost = cfg["backtest"]["slippage"] + cfg["backtest"]["crypto_fee"]

    lines = [f"CRYPTO LAB: 4-hour bars, {years:g} years from {str(start_t)[:10]}, $100k start, 1% risk, 10% max "
             f"position, {cost:.2%} cost per side. PASS = PF >= {PASS_PF} overall and in both halves, "
             f"max drawdown < {PASS_DD:.0%}, {PASS_TRADES}+ trades.", "",
             f"{'setup':<28} {'trades':>6} {'win%':>5} {'PF':>5} {'return':>8} {'max DD':>7} {'PF 1st 60%':>10} "
             f"{'PF last 40%':>11}  verdict"]
    passed = []
    for setup in setups:
        r = simulate(setup, data, pre, btc_up, times, cost, start_t)
        full = summarize(r.trades, r.curve, START_EQUITY)
        first = [x for x in r.trades if x["time"] < str(split_t)]
        last = [x for x in r.trades if x["time"] >= str(split_t)]
        pf1, pf2 = summarize(first, [START_EQUITY], START_EQUITY)["pf"], summarize(last, [START_EQUITY], START_EQUITY)["pf"]
        ok = (full["trades"] >= PASS_TRADES and full["pf"] >= PASS_PF and pf1 >= PASS_PF and pf2 >= PASS_PF
              and full["max_dd"] > -PASS_DD and not setup.name.startswith("("))
        if ok:
            passed.append((full["return"] / max(1e-9, -full["max_dd"]), setup, full))
        fmt = lambda v: "inf" if v == math.inf else f"{v:.2f}"
        lines.append(f"{setup.name:<28} {full['trades']:>6} {full['win_rate']:>5.0%} {fmt(full['pf']):>5} "
                     f"{full['return']:>+8.1%} {full['max_dd']:>+7.1%} {fmt(pf1):>10} {fmt(pf2):>11}  "
                     f"{'PASS' if ok else ('(comparison)' if setup.name.startswith('(') else 'fail')}")
    lines.append("")
    out = ROOT / "backtest_results"
    out.mkdir(exist_ok=True)
    if passed:
        passed.sort(key=lambda x: -x[0])
        _, best, stats = passed[0]
        lines.append(f"BEST PASSING SETUP: {best.name}. The live bot can switch crypto on with it.")
        (out / "crypto_lab.json").write_text(json.dumps({"strategy": best.strategy, "symbols": best.symbols,
                                                         "max_open": best.max_open, "btc_filter": best.btc_filter,
                                                         "stats": stats}, indent=1))
    else:
        lines.append("NOTHING PASSED: crypto stays off.")
        (out / "crypto_lab.json").write_text(json.dumps({"passed": False}))
    lines.append("Past results with simulated fills; not a promise and not financial advice.")
    report = "\n".join(lines)
    (out / "crypto_lab.txt").write_text(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
