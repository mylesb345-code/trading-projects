#!/usr/bin/env python3
"""Day-trading lab: the two published intraday strategies with the strongest evidence, on 5-minute bars since 2016.
Every position is closed by 4 PM. Read-only, no orders.

  ../box-scanner/.venv/bin/python daytrade.py

Rules fixed BEFORE any result was seen (2026-11-02), as published:
  ORB   Zarattini & Aziz (2023), "Can Day Trading Really Be Profitable?". QQQ. If the 9:30-9:35 candle closes up, buy
        at 9:35; if down, short. Stop at that candle's low (long) or high (short); target 10x the risk; otherwise
        out at the close. Risk 1% of equity per trade, at most 4x leverage.
  NOISE Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday Momentum Strategy for SPY". SPY.
        Each half hour from 10:00, buy above the "noise area" (max(open, yesterday's close) x (1 + the average
        move from the open at that time of day over the last 14 days)), short below its mirror image. Exit when
        price crosses back through the boundary or VWAP, whichever is closer; out at the close. Size: leverage
        = 2% / SPY's 14-day daily volatility, at most 4x.
  Both are also shown at 1x (no leverage) to separate the edge from the leverage.
  Costs: $0.0035 commission + $0.001 slippage per share, per side (the papers' own assumption or worse).
  PASS: beats holding SPY per year overall and in both halves (2016-2020, 2021-now), with a worst drop no deeper
        than SPY's. The papers used data up to 2023/2024, so the months since publication are the only truly fresh
        test and are shown separately.
"""
from __future__ import annotations

import pickle
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BOX = HERE.parent / "box-scanner"
sys.path.insert(0, str(BOX))

COST_PER_SHARE = 0.0035 + 0.001
SPLIT = "2021-01-01"
PUBLISHED = {"ORB": "2023-05-01", "NOISE": "2024-05-01"}


def download() -> Dict[str, pd.DataFrame]:
    cache = HERE / "cache" / "bars5m.pkl"
    if cache.exists() and datetime.now().timestamp() - cache.stat().st_mtime < 86400:
        return pickle.loads(cache.read_bytes())
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from data import with_retries
    from settings import load_credentials
    creds = load_credentials(BOX / ".env")
    client = StockHistoricalDataClient(creds.api_key, creds.secret_key)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    out = {}
    for sym in ("SPY", "QQQ"):
        frames = []
        for year in range(2016, end.year + 1):
            req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                                   start=datetime(year, 1, 1, tzinfo=timezone.utc),
                                   end=min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), end),
                                   adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
            df = with_retries(client.get_stock_bars, req, what=f"{sym} 5-minute bars").df.droplevel(0)
            df.index = df.index.tz_convert("America/New_York")
            frames.append(df[["open", "high", "low", "close", "volume"]].astype(float))
            print(f"  {sym} {year}: {len(df)} bars", flush=True)
        df = pd.concat(frames)
        t = df.index.time
        out[sym] = df[(t >= time(9, 30)) & (t < time(16, 0))]  # regular hours only
    cache.write_bytes(pickle.dumps(out))
    return out


def days_of(df: pd.DataFrame) -> List[pd.DataFrame]:
    return [d for _, d in df.groupby(df.index.date) if len(d) >= 60 and d.index[0].time() == time(9, 30)]


def orb(days: List[pd.DataFrame], max_lev: float) -> pd.Series:
    eq, curve = 1.0, {}
    for d in days:
        o, h, l, c = (d[k].values for k in ("open", "high", "low", "close"))
        direction = np.sign(c[0] - o[0])
        entry = o[1]
        stop = l[0] if direction > 0 else h[0]
        risk = (entry - stop) * direction
        if direction != 0 and risk > 0:
            shares = min(eq * 0.01 / risk, max_lev * eq / entry)
            target = entry + direction * 10 * risk
            exit_ = c[-1]
            for k in range(1, len(d)):
                if (l[k] <= stop) if direction > 0 else (h[k] >= stop):
                    exit_ = min(o[k], stop) if direction > 0 else max(o[k], stop)
                    break
                if (h[k] >= target) if direction > 0 else (l[k] <= target):
                    exit_ = target
                    break
            eq += shares * direction * (exit_ - entry) - 2 * shares * COST_PER_SHARE
        curve[pd.Timestamp(d.index[0].date())] = eq
    return pd.Series(curve)


def noise(days: List[pd.DataFrame], max_lev: float, target_vol: float = 0.02) -> pd.Series:
    """Checks at the close of the 5-minute bars ending on each half hour (10:00 ... 15:30)."""
    checks = [k for k in range(len(days[0])) if (days[0].index[k] + timedelta(minutes=5)).minute in (0, 30)
              and (days[0].index[k] + timedelta(minutes=5)).time() <= time(15, 30)]
    moves = [np.abs(d["close"].values[checks] / d["open"].values[0] - 1) if len(d) > max(checks) else None
             for d in days]
    closes = np.array([d["close"].values[-1] for d in days])
    daily_ret = np.diff(closes) / closes[:-1]
    eq, curve = 1.0, {}
    for i, d in enumerate(days):
        hist = [m for m in moves[max(0, i - 14):i] if m is not None]
        if i < 15 or len(hist) < 14 or moves[i] is None:
            curve[pd.Timestamp(d.index[0].date())] = eq
            continue
        sigma = np.mean(hist, axis=0)
        o, c, h, l, v = (d[k].values for k in ("open", "close", "high", "low", "volume"))
        day_open, prev_close = o[0], closes[i - 1]
        ub = max(day_open, prev_close) * (1 + sigma)
        lb = min(day_open, prev_close) * (1 - sigma)
        vwap = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1)
        lev = min(max_lev, target_vol / max(np.std(daily_ret[i - 14:i]), 1e-9))
        shares = eq * lev / day_open
        pos, entry, pnl = 0, 0.0, 0.0
        for j, k in enumerate(checks):
            px = c[k]
            if pos > 0 and px < max(ub[j], vwap[k]) or pos < 0 and px > min(lb[j], vwap[k]):
                pnl += shares * pos * (px - entry) - shares * COST_PER_SHARE
                pos = 0
            if pos == 0:
                new = 1 if px > ub[j] else -1 if px < lb[j] else 0
                if new:
                    pos, entry = new, px
                    pnl -= shares * COST_PER_SHARE
        if pos:
            pnl += shares * pos * (c[-1] - entry) - shares * COST_PER_SHARE
        eq += pnl
        curve[pd.Timestamp(d.index[0].date())] = eq
    return pd.Series(curve)


def stats(cv: pd.Series) -> Dict[str, float]:
    cv = cv[cv > 0]
    yrs = max((cv.index[-1] - cv.index[0]).days / 365.25, 1e-9)
    r = cv.pct_change().dropna()
    return {"cagr": (cv.iloc[-1] / cv.iloc[0]) ** (1 / yrs) - 1, "dd": (cv / cv.cummax() - 1).min(),
            "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0, "total": cv.iloc[-1] / cv.iloc[0] - 1}


def main() -> int:
    bars = download()
    spy_days, qqq_days = days_of(bars["SPY"]), days_of(bars["QQQ"])
    hold = lambda days: pd.Series({pd.Timestamp(d.index[0].date()): d["close"].values[-1] for d in days})
    curves = {"Hold SPY": hold(spy_days), "Hold QQQ": hold(qqq_days),
              "ORB on QQQ, up to 4x (as published)": orb(qqq_days, 4.0), "ORB on QQQ, 1x": orb(qqq_days, 1.0),
              "Noise-area on SPY, up to 4x (as published)": noise(spy_days, 4.0),
              "Noise-area on SPY, 1x": noise(spy_days, 1.0)}
    start = max(cv.index[0] for cv in curves.values()) + timedelta(days=30)
    curves = {k: v[v.index >= start] for k, v in curves.items()}
    S = stats(curves["Hold SPY"])
    S1 = stats(curves["Hold SPY"][curves["Hold SPY"].index < SPLIT])["cagr"]
    S2 = stats(curves["Hold SPY"][curves["Hold SPY"].index >= SPLIT])["cagr"]
    lines = [f"DAY-TRADING LAB: {start.date()} to {curves['Hold SPY'].index[-1].date()}, 5-minute bars, flat every "
             f"night, ${COST_PER_SHARE:.4f}/share per side.", "",
             f"{'strategy':<44} {'per year':>8} {'worst drop':>10} {'Sharpe':>6} {'2016-20':>8} {'2021-now':>8} "
             f"{'since published':>15}  verdict"]
    for name, cv in curves.items():
        st = stats(cv)
        a, b = stats(cv[cv.index < SPLIT])["cagr"], stats(cv[cv.index >= SPLIT])["cagr"]
        key = "ORB" if name.startswith("ORB") else "NOISE" if name.startswith("Noise") else None
        pub = stats(cv[cv.index >= PUBLISHED[key]]) if key else None
        spy_pub = stats(curves["Hold SPY"][curves["Hold SPY"].index >= PUBLISHED[key]]) if key else None
        since = f"{pub['cagr']:+.1%} vs {spy_pub['cagr']:+.1%}" if key else ""
        ok = st["cagr"] > S["cagr"] and a > S1 and b > S2 and st["dd"] >= S["dd"]
        verdict = "benchmark" if name.startswith("Hold") else "PASS" if ok else "fail"
        lines.append(f"{name:<44} {st['cagr']:>+8.1%} {st['dd']:>+10.1%} {st['sharpe']:>6.2f} {a:>+8.1%} "
                     f"{b:>+8.1%} {since:>15}  {verdict}")
    lines += ["", "'since published' = per year after the paper came out (ORB May 2023, noise-area May 2024), "
              "vs holding SPY over the same months.",
              "Hold rows are price only (no dividends, about 1.3%/yr for SPY). Past results with simulated fills; "
              "not a promise and not financial advice."]
    report = "\n".join(lines)
    (HERE / "daytrade.txt").write_text(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
