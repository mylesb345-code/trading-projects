#!/usr/bin/env python3
"""Liquidity-video lab: the rules from the user's "@liquidityaddict free course part #3" video (Nasdaq day trading),
tested on QQQ 5-minute bars since 2016, including pre-market (4 AM) and after-hours (to 8 PM). Read-only, no orders.

  ../box-scanner/.venv/bin/python liquidity.py

QQQ stands in for the Nasdaq futures (MNQ) in the video: same index, and Alpaca has no futures. The 8 PM-4 AM Asia
session isn't in QQQ's data, so its levels are left out.

Rules fixed BEFORE any result was seen (2026-11-03), straight from the video:
  SWEEP   "Mark out the high/low" of yesterday's New York session (9:30-4), yesterday's New Day session (4-8 PM) and
          today's pre-market (4-9:30 AM). From 9:35 to 3:30, when a 5-minute candle trades above one of those highs
          and closes back below it (the sweep the video circles), short at the next candle's open; stop just above
          the sweep candle's high, target 2x the risk, out by 3:55 PM. Mirror image for the lows. First signal of
          the day only. 1% risk per trade, at most 4x leverage (as in the ORB test).
  SWEEP-NO-NEWS  the same, skipping days with 2 PM Fed decisions ("avoid red folder news").
  HTF     "there'll always be a retracement after a higher-timeframe candle closure": at each 1-hour close from
          11:00 to 3:00 (4-hour closes at 2:00 included), trade against that hour's candle for 15 minutes.
  :45     "manipulation before a 1-hour close": trade against the 10:45-11:00 ... 2:45-3:00 move for 15 minutes.
  The HTF and :45 rules are tested as plain statistics too (average move after the signal, and how often it wins).
  Costs: $0.01 per share per side (commission + slippage).
  PASS: beats holding SPY per year overall and in both halves (2016-2020, 2021-now), worst drop no deeper than SPY's.
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

COST = 0.01  # $ per share per side
SPLIT = "2021-01-01"
# Fed decision days (2 PM ET statements), 2016-2026
FOMC = {d for d in """
2016-01-27 2016-03-16 2016-04-27 2016-06-15 2016-07-27 2016-09-21 2016-11-02 2016-12-14 2017-02-01 2017-03-15
2017-05-03 2017-06-14 2017-07-26 2017-09-20 2017-11-01 2017-12-13 2018-01-31 2018-03-21 2018-05-02 2018-06-13
2018-08-01 2018-09-26 2018-11-08 2018-12-19 2019-01-30 2019-03-20 2019-05-01 2019-06-19 2019-07-31 2019-09-18
2019-10-30 2019-12-11 2020-01-29 2020-03-03 2020-03-15 2020-04-29 2020-06-10 2020-07-29 2020-09-16 2020-11-05
2020-12-16 2021-01-27 2021-03-17 2021-04-28 2021-06-16 2021-07-28 2021-09-22 2021-11-03 2021-12-15 2022-01-26
2022-03-16 2022-05-04 2022-06-15 2022-07-27 2022-09-21 2022-11-02 2022-12-14 2023-02-01 2023-03-22 2023-05-03
2023-06-14 2023-07-26 2023-09-20 2023-11-01 2023-12-13 2024-01-31 2024-03-20 2024-05-01 2024-06-12 2024-07-31
2024-09-18 2024-11-07 2024-12-18 2025-01-29 2025-03-19 2025-05-07 2025-06-18 2025-07-30 2025-09-17 2025-10-29
2025-12-10 2026-01-28 2026-03-18 2026-04-29 2026-06-17 2026-07-29 2026-09-16 2026-10-28""".split()}


def download() -> pd.DataFrame:
    cache = HERE / "cache" / "qqq5m_all.pkl"
    if cache.exists():
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
    frames = []
    for year in range(2016, end.year + 1):
        req = StockBarsRequest(symbol_or_symbols="QQQ", timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=datetime(year, 1, 1, tzinfo=timezone.utc),
                               end=min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), end),
                               adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
        df = with_retries(client.get_stock_bars, req, what="QQQ 5-minute bars").df.droplevel(0)
        df.index = df.index.tz_convert("America/New_York")
        frames.append(df[["open", "high", "low", "close", "volume"]].astype(float))
        print(f"  QQQ {year}: {len(df)} bars", flush=True)
    df = pd.concat(frames)
    cache.write_bytes(pickle.dumps(df))
    return df


def session(d: pd.DataFrame, a: time, b: time) -> pd.DataFrame:
    t = d.index.time
    return d[(t >= a) & (t < b)]


def sweep(days: Dict, order: List, skip_news: bool) -> tuple:
    """Equity curve and trade list for the sweep-and-reverse rule."""
    eq, curve, trades = 1.0, {}, []
    for i in range(1, len(order)):
        day, prev = order[i], order[i - 1]
        d, p = days[day], days[prev]
        ny_prev, nd_prev = session(p, time(9, 30), time(16)), session(p, time(16), time(20))
        pre, rth = session(d, time(4), time(9, 30)), session(d, time(9, 30), time(16))
        curve[pd.Timestamp(day)] = eq
        if len(rth) < 70 or len(ny_prev) < 70 or (skip_news and str(day) in FOMC):
            continue
        highs = [x["high"].max() for x in (ny_prev, nd_prev, pre) if len(x)]
        lows = [x["low"].min() for x in (ny_prev, nd_prev, pre) if len(x)]
        o, h, l, c = (rth[k].values for k in ("open", "high", "low", "close"))
        times = rth.index.time
        trade = None
        for k in range(1, len(rth) - 1):
            if not (time(9, 35) <= times[k] <= time(15, 30)):
                continue
            if any(h[k] > lv > c[k] for lv in highs):
                trade = (-1, k, h[k])
                break
            if any(l[k] < lv < c[k] for lv in lows):
                trade = (1, k, l[k])
                break
        if trade is None:
            continue
        direction, k, extreme = trade
        entry = o[k + 1]
        stop = extreme * (1 + 0.0002 * -direction)
        risk = (entry - stop) * direction
        if risk <= 0:
            continue
        shares = min(eq * 0.01 / risk, 4 * eq / entry)
        target = entry + direction * 2 * risk
        exit_ = c[-1]
        for j in range(k + 1, len(rth)):
            if (l[j] <= stop) if direction > 0 else (h[j] >= stop):
                exit_ = min(o[j], stop) if direction > 0 else max(o[j], stop)
                break
            if (h[j] >= target) if direction > 0 else (l[j] <= target):
                exit_ = target
                break
        pnl = shares * direction * (exit_ - entry) - 2 * shares * COST
        trades.append(pnl / (shares * risk))
        eq += pnl
        curve[pd.Timestamp(day)] = eq
    return pd.Series(curve), trades


def fade(days: Dict, order: List, kind: str) -> tuple:
    """Per-day P&L (1x, all equity) of fading each signal for 15 minutes, and the list of per-trade returns."""
    eq, curve, rets = 1.0, {}, []
    for day in order:
        rth = session(days[day], time(9, 30), time(16))
        curve[pd.Timestamp(day)] = eq
        if len(rth) < 70:
            continue
        idx = {t: n for n, t in enumerate(rth.index.time)}
        o, c = rth["open"].values, rth["close"].values
        for hour in (11, 12, 13, 14, 15):
            start = time(hour - 1, 0) if kind == "htf" else time(hour - 1, 45)
            if start not in idx or time(hour, 0) not in idx or time(hour, 10) not in idx:
                continue
            move = c[idx[time(hour, 0)] - 1] - o[idx[start]]
            if move == 0:
                continue
            entry, exit_ = o[idx[time(hour, 0)]], c[idx[time(hour, 10)]]
            r = -np.sign(move) * (exit_ - entry) / entry - 2 * COST / entry
            rets.append(r)
            eq *= 1 + r
        curve[pd.Timestamp(day)] = eq
    return pd.Series(curve), rets


def stats(cv: pd.Series) -> Dict[str, float]:
    yrs = (cv.index[-1] - cv.index[0]).days / 365.25
    r = cv.pct_change().dropna()
    return {"cagr": (cv.iloc[-1] / cv.iloc[0]) ** (1 / yrs) - 1 if cv.iloc[-1] > 0 else -1.0,
            "dd": (cv / cv.cummax() - 1).min(), "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0}


def main() -> int:
    df = download()
    days = {d: g for d, g in df.groupby(df.index.date)}
    order = sorted(days)
    spy_days = pickle.loads((HERE / "cache" / "bars5m.pkl").read_bytes())["SPY"]
    spy = spy_days.groupby(spy_days.index.date)["close"].last()
    spy.index = pd.to_datetime(spy.index)
    curves, notes = {}, {}
    for name, skip in (("Sweep & reverse (session highs/lows)", False), ("Sweep & reverse, no Fed days", True)):
        cv, trades = sweep(days, order, skip)
        curves[name] = cv
        notes[name] = f"{len(trades)} trades, won {np.mean(np.array(trades) > 0):.0%}, average {np.mean(trades):+.2f}R"
    for name, kind in (("Fade the 1-hour candle after it closes", "htf"), ("Fade the :45-:00 move", "45")):
        cv, rets = fade(days, order, kind)
        curves[name] = cv
        r = np.array(rets)
        notes[name] = (f"{len(r)} trades, won {np.mean(r > 0):.0%}, average {r.mean() * 1e4:+.1f} basis points "
                       f"(t = {r.mean() / r.std() * np.sqrt(len(r)):+.1f})")
    start = max(cv.index[0] for cv in curves.values())
    curves = {k: v[v.index >= start] for k, v in curves.items()}
    spy = spy[spy.index >= start]
    S = stats(spy)
    S1, S2 = stats(spy[spy.index < SPLIT])["cagr"], stats(spy[spy.index >= SPLIT])["cagr"]
    lines = [f"LIQUIDITY-VIDEO LAB: QQQ 5-minute bars incl. pre-market and after-hours, {start.date()} to "
             f"{spy.index[-1].date()}, ${COST}/share per side.", "",
             f"{'rule':<40} {'per year':>8} {'worst drop':>10} {'Sharpe':>6} {'2016-20':>8} {'2021-now':>8}  verdict",
             f"{'Hold SPY (price only)':<40} {S['cagr']:>+8.1%} {S['dd']:>+10.1%} {S['sharpe']:>6.2f} {S1:>+8.1%} "
             f"{S2:>+8.1%}  benchmark"]
    for name, cv in curves.items():
        st = stats(cv)
        a, b = stats(cv[cv.index < SPLIT])["cagr"], stats(cv[cv.index >= SPLIT])["cagr"]
        ok = st["cagr"] > S["cagr"] and a > S1 and b > S2 and st["dd"] >= S["dd"]
        lines.append(f"{name:<40} {st['cagr']:>+8.1%} {st['dd']:>+10.1%} {st['sharpe']:>6.2f} {a:>+8.1%} {b:>+8.1%}  "
                     f"{'PASS' if ok else 'fail'}")
        lines.append(f"{'':<40} {notes[name]}")
    lines += ["", "Past results with simulated fills; not a promise and not financial advice."]
    report = "\n".join(lines)
    (HERE / "liquidity.txt").write_text(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
