#!/usr/bin/env python3
"""TJR-style "liquidity sweep -> market structure shift -> fair value gap" day trade, tested on QQQ and SPY.

  ../box-scanner/.venv/bin/python tjr.py      # writes tjr.txt

Rules fixed BEFORE looking at any result (1-minute regular-session bars, 2016-2026, New York time):
  Liquidity:   A  previous day's high and low (PDH/PDL)
               B  A + the opening range (9:30-9:45 high/low), which can only be swept after 9:45
               C  B, traded only with the daily bias: longs when yesterday's close > its 20-day average, shorts below
  Sweep:       between 9:30 and 11:30 a bar trades above a high level (bearish setup) or below a low level (bullish).
  Shift (MSS): within 30 bars of the sweep, a bar CLOSES beyond the structure point: the lowest low of the 15 bars
               before the sweep (bearish) / the highest high (bullish).
  FVG:         the newest fair value gap formed from the sweep bar to the MSS bar: bearish = bar i high < bar i-2 low,
               bullish = bar i low > bar i-2 high. No gap -> no trade.
  Entry:       limit order at the near edge of the gap (bearish: bar i high; bullish: bar i low), filled if price comes
               back to it within 30 bars after the MSS; otherwise no trade.
  Stop:        2 cents beyond the sweep's extreme (highest high / lowest low from the sweep to the MSS).
  Target:      2R. Otherwise out at 15:55. A bar touching both stop and target counts as the stop.
  One trade per symbol per day (the first setup that fills); longs and shorts.
  Sizing/costs: risk 1% of equity per trade, at most 4x leverage; $0.0045/share per side, stop exits 1 cent worse.
  PASS (same bar as every other lab test): beats holding SPY per year overall and in both halves (2016-20, 2021-now),
  worst drop no deeper than SPY's.
"""
from __future__ import annotations

import math
import pickle
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
COST, STOP_SLIP, RISK, LEV, TARGET_R = 0.0045, 0.01, 0.01, 4.0, 2.0
SPLIT = pd.Timestamp("2021-01-01").date()


def load(symbol):
    df = pickle.loads((CACHE / f"{symbol.lower()}1m.pkl").read_bytes())
    df = df[["open", "high", "low", "close"]].astype(float)
    return {d: g for d, g in df.groupby(df.index.date)}


def minutes(idx):
    return np.array([t.hour * 60 + t.minute for t in idx.time])


def setup_trade(o, h, l, c, mins, levels, bias):
    """First filled setup of the day -> (direction, entry, stop, exit_price) or None."""
    n = len(c)
    end_sweep = 11 * 60 + 30
    for s in range(1, n):
        if mins[s] > end_sweep:
            break
        for kind, level, earliest in levels:
            if mins[s] < earliest:
                continue
            if kind == "high" and h[s] > level and h[s - 1] <= level and bias <= 0:
                direction = -1
            elif kind == "low" and l[s] < level and l[s - 1] >= level and bias >= 0:
                direction = 1
            else:
                continue
            lo15 = l[max(0, s - 15):s].min() if s >= 1 else l[0]
            hi15 = h[max(0, s - 15):s].max() if s >= 1 else h[0]
            for m in range(s + 1, min(n, s + 31)):
                if (direction < 0 and c[m] < lo15) or (direction > 0 and c[m] > hi15):
                    break
            else:
                continue
            gap = None
            for i in range(m, max(s + 1, 2) - 1, -1):
                if i - 2 < s - 2:
                    break
                if direction < 0 and h[i] < l[i - 2]:
                    gap = h[i]
                    break
                if direction > 0 and l[i] > h[i - 2]:
                    gap = l[i]
                    break
            if gap is None:
                continue
            extreme = h[s:m + 1].max() if direction < 0 else l[s:m + 1].min()
            stop = extreme + 0.02 if direction < 0 else extreme - 0.02
            entry = gap
            risk = (stop - entry) * -direction
            if risk <= 0.01:
                continue
            target = entry + direction * TARGET_R * risk
            fill = None
            for k in range(m + 1, min(n, m + 31)):
                if (direction < 0 and h[k] >= entry) or (direction > 0 and l[k] <= entry):
                    fill = k
                    break
                if (direction < 0 and l[k] <= target) or (direction > 0 and h[k] >= target):
                    break  # ran to the target without filling: missed
            if fill is None:
                continue
            last = int(np.searchsorted(mins, 15 * 60 + 55))
            for k in range(fill, min(n, last + 1)):
                hit_stop = (direction < 0 and h[k] >= stop) or (direction > 0 and l[k] <= stop)
                hit_tgt = (direction < 0 and l[k] <= target) or (direction > 0 and h[k] >= target)
                if k == fill:  # on the fill bar only the stop can be known to come after the fill
                    hit_tgt = False
                if hit_stop:
                    worse = max(stop, o[k]) if direction < 0 and k > fill else min(stop, o[k]) if k > fill else stop
                    return direction, entry, stop, worse - direction * STOP_SLIP
                if hit_tgt:
                    return direction, entry, stop, target
            k = min(n - 1, last)
            return direction, entry, stop, c[k]
    return None


def run(symbol, variant):
    days = load(symbol)
    order = sorted(days)
    closes = pd.Series({d: float(days[d]["close"].iloc[-1]) for d in order})
    sma20 = closes.rolling(20).mean()
    eq, curve, rs = 1.0, {}, []
    for j, d in enumerate(order):
        g = days[d]
        if j == 0 or len(g) < 120:
            curve[d] = eq
            continue
        prev = days[order[j - 1]]
        pdh, pdl = float(prev["high"].max()), float(prev["low"].min())
        o, h, l, c = (g[k].to_numpy() for k in ("open", "high", "low", "close"))
        mins = minutes(g.index)
        levels = [("high", pdh, 9 * 60 + 30), ("low", pdl, 9 * 60 + 30)]
        if variant in ("B", "C"):
            orng = mins < 9 * 60 + 45
            levels += [("high", float(h[orng].max()), 9 * 60 + 45), ("low", float(l[orng].min()), 9 * 60 + 45)]
        bias = 0
        if variant == "C":
            prev_close, avg = closes.iloc[j - 1], sma20.iloc[j - 1]
            if math.isnan(avg):
                curve[d] = eq
                continue
            bias = 1 if prev_close > avg else -1
        t = setup_trade(o, h, l, c, mins, levels, bias)
        if t:
            direction, entry, stop, exit_px = t
            risk = abs(entry - stop)
            shares = min(eq * RISK / risk, LEV * eq / entry)
            pnl = shares * (direction * (exit_px - entry) - 2 * COST)
            rs.append(direction * (exit_px - entry) / risk)
            eq += pnl
        curve[d] = eq
    return pd.Series(curve), np.array(rs)


def stats(curve):
    curve = curve[curve.index >= curve.index[0]]
    years = (pd.Timestamp(curve.index[-1]) - pd.Timestamp(curve.index[0])).days / 365.25
    cagr = curve.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    dd = (curve / curve.cummax() - 1).min()
    r = curve.pct_change().dropna()
    sharpe = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    return cagr, dd, sharpe


def halves(curve):
    a = curve[[d < SPLIT for d in curve.index]]
    b = curve[[d >= SPLIT for d in curve.index]]
    return stats(a / a.iloc[0])[0], stats(b / b.iloc[0])[0]


def main():
    spy_days = load("SPY")
    spy = pd.Series({d: float(g["close"].iloc[-1]) for d, g in spy_days.items()}).sort_index()
    spy = spy / spy.iloc[0]
    S, (S1, S2) = stats(spy), halves(spy)
    lines = ["TJR-STYLE LAB (rules in tjr.py, fixed before running). 1-minute regular-session bars, 2016-2026.",
             "Risk 1% per trade, max 4x leverage, $0.0045/share per side, stop exits 1 cent worse.", "",
             f"{'strategy':<44} {'trades':>6} {'won':>5} {'avg R':>6} {'t':>5} {'per year':>8} {'worst':>7} "
             f"{'Sharpe':>6} {'2016-20':>8} {'2021-':>7}  verdict",
             f"{'Hold SPY (price only)':<44} {'':>6} {'':>5} {'':>6} {'':>5} {S[0]:>+8.1%} {S[1]:>+7.1%} {S[2]:>6.2f} "
             f"{S1:>+8.1%} {S2:>+7.1%}  benchmark"]
    names = {"A": "sweep PDH/PDL -> MSS -> FVG", "B": "A + opening-range sweeps", "C": "B with daily bias (20-day)"}
    for sym in ("QQQ", "SPY"):
        for v in ("A", "B", "C"):
            curve, rs = run(sym, v)
            st, (h1, h2) = stats(curve), halves(curve)
            t = rs.mean() / rs.std() * math.sqrt(len(rs)) if len(rs) > 1 and rs.std() > 0 else 0
            ok = st[0] > S[0] and h1 > S1 and h2 > S2 and st[1] >= S[1]
            lines.append(f"{sym + ' ' + v + ': ' + names[v]:<44} {len(rs):>6} {np.mean(rs > 0) if len(rs) else 0:>5.0%} "
                         f"{rs.mean() if len(rs) else 0:>+6.2f} {t:>+5.1f} {st[0]:>+8.1%} {st[1]:>+7.1%} {st[2]:>6.2f} "
                         f"{h1:>+8.1%} {h2:>+7.1%}  {'PASS' if ok else 'fail'}")
            print(lines[-1], flush=True)
    lines += ["", "avg R = average result per trade in units of the risk taken (target is +2R, stop is -1R);",
              "t = how many standard errors the average is from zero (|t| < 2 is indistinguishable from luck).",
              "Past results with simulated fills; not a promise and not financial advice."]
    (HERE / "tjr.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:5]))


if __name__ == "__main__":
    main()
