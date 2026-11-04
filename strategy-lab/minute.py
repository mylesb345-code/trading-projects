#!/usr/bin/env python3
"""1-minute day-trading lab: published intraday patterns on SPY and QQQ 1-minute bars since 2016, each run three
ways: every day, calm days only, and calm days without scheduled big news. Flat every night. Read-only, no orders.

  ../box-scanner/.venv/bin/python minute.py

Why SPY/QQQ: the user wants to avoid highly volatile trades. These are the two most liquid, least volatile things a
day trader can trade (tight spreads, no single-company earnings shocks).

Rules fixed BEFORE any result was seen (2026-11-03):
  ORB-N   opening-range breakout with the first N minutes (N = 1, 5, 15, 30): buy if the range candle closed up,
          short if down, at the next minute's open; stop at the range's other end; target 10x the risk; out at 15:59.
          (Zarattini & Aziz 2023 used N = 5; the other N are the usual variants.) Sized to risk 1% of equity.
  VWAP    Zarattini & Aziz (2023), "VWAP: The Holy Grail for Day Trading Systems": each minute from 9:31, long while
          the last close is above the day's VWAP, short while below; trade at the next minute's open; out at 15:59.
  MOM     Gao, Han, Li & Zhou (2018), "Market Intraday Momentum": if the return from yesterday's close to 10:00 is
          positive, buy at 15:30 and sell at 15:59; if negative, short.
  GAPFADE the classic gap fill: if today opens 0.15%-0.60% away from yesterday's close, fade it at 9:31; target
          yesterday's close; stop the same distance beyond the open; out at 15:59.
  Filters (the "no gambling" versions):
    CALM     trade only when yesterday's VIX close was below 20 (normal, quiet markets).
    NO-NEWS  CALM, and also skip Fed decision days and jobs-report days (first Friday of the month).
  Leverage: shown at 1x (no borrowing). ORB uses risk-based size capped at 1x; nothing here uses more than 1x.
  Costs: $0.0045 per share per side (commission + slippage).
  PASS: beats holding SPY per year overall and in both halves (2016-2020, 2021-now), worst drop no deeper than
        SPY's, AND at least +1 basis point per trade after costs (so it isn't a rounding error).
"""
from __future__ import annotations

import json
import pickle
import sys
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BOX = HERE.parent / "box-scanner"
sys.path.insert(0, str(BOX))
sys.path.insert(0, str(HERE))
CACHE = HERE / "cache"
COST = 0.0045
SPLIT = "2021-01-01"


def bars(symbol: str) -> pd.DataFrame:
    path = CACHE / f"{symbol.lower()}1m.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from data import with_retries
    from settings import load_credentials
    creds = load_credentials(BOX / ".env")
    client = StockHistoricalDataClient(creds.api_key, creds.secret_key)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    frames = []
    for year in range(2016, end.year + 1):
        part = CACHE / f"{symbol.lower()}1m_{year}.pkl"  # per-year cache so a network drop doesn't restart the download
        if part.exists() and year < end.year:
            frames.append(pickle.loads(part.read_bytes()))
            continue
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                               start=datetime(year, 1, 1, tzinfo=timezone.utc),
                               end=min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), end),
                               adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
        df = with_retries(client.get_stock_bars, req, what=f"{symbol} 1-minute").df.droplevel(0)
        df.index = df.index.tz_convert("America/New_York")
        t = df.index.time
        df = df[(t >= time(9, 30)) & (t < time(16))][["open", "high", "low", "close", "volume", "vwap"]].astype(float)
        frames.append(df.astype(np.float32))
        part.write_bytes(pickle.dumps(frames[-1]))
        print(f"  {symbol} {year}: {len(df)} bars", flush=True)
    df = pd.concat(frames)
    path.write_bytes(pickle.dumps(df))
    return df


def vix() -> pd.Series:
    path = CACHE / "yahoo_VIX.json"
    if not path.exists():
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?period1=1420070400"
               f"&period2={int(datetime.now().timestamp())}&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        path.write_bytes(urllib.request.urlopen(req, timeout=30).read())
    r = json.loads(path.read_text())["chart"]["result"][0]
    s = pd.Series(r["indicators"]["quote"][0]["close"],
                  index=[datetime.fromtimestamp(t, timezone.utc).date() for t in r["timestamp"]]).dropna()
    return s


def news_days(days: List[date]) -> set:
    from liquidity import FOMC
    out = {date.fromisoformat(d) for d in FOMC}
    for d in days:
        if d.weekday() == 4 and d.day <= 7:  # jobs report: first Friday of the month
            out.add(d)
    return out


# ------------------------------------------------------------------ strategies: day arrays -> list of (return, price)
def orb(n: int) -> Callable:
    def run(o, h, l, c, v, w, prev_close):
        if len(c) < n + 30:
            return None
        hi, lo = h[:n].max(), l[:n].min()
        direction = np.sign(c[n - 1] - o[0])
        if direction == 0:
            return None
        entry = o[n]
        stop = lo if direction > 0 else hi
        risk = (entry - stop) * direction
        if risk <= 0:
            return None
        target = entry + direction * 10 * risk
        exit_ = c[-1]
        for k in range(n, len(c)):
            if (l[k] <= stop) if direction > 0 else (h[k] >= stop):
                exit_ = min(o[k], stop) if direction > 0 else max(o[k], stop)
                break
            if (h[k] >= target) if direction > 0 else (l[k] <= target):
                exit_ = target
                break
        size = min(0.01 / (risk / entry), 1.0)  # fraction of equity, risk 1%, never above 1x
        return size * (direction * (exit_ - entry) / entry - 2 * COST / entry)
    return run


def vwap_trend(o, h, l, c, v, w, prev_close):
    if len(c) < 60:
        return None
    pv = np.cumsum(w * v)
    cv = np.cumsum(v)
    day_vwap = np.where(cv > 0, pv / np.maximum(cv, 1), c)
    pos = np.sign(c[:-1] - day_vwap[:-1])  # decided at close of minute k, held from open of k+1
    ret = np.append(o[2:], c[-1]) / o[1:] - 1  # open k+1 -> open k+2 (last: -> final close)
    changes = np.abs(np.diff(np.concatenate([[0], pos, [0]])))
    return float(np.sum(pos * ret) - np.sum(changes) * COST / o[0])


def momentum(o, h, l, c, v, w, prev_close, idx_10=29, idx_1530=360):
    if len(c) < 389 or prev_close is None:
        return None
    first = c[idx_10] / prev_close - 1
    if first == 0:
        return None
    d = np.sign(first)
    return float(d * (c[-1] / o[idx_1530] - 1) - 2 * COST / o[idx_1530])


def gap_fade(o, h, l, c, v, w, prev_close):
    if len(c) < 60 or prev_close is None:
        return None
    gap = o[0] / prev_close - 1
    if not 0.0015 <= abs(gap) <= 0.006:
        return None
    d = -np.sign(gap)
    entry = o[1]
    target, stop = prev_close, entry - d * abs(entry - prev_close)
    exit_ = c[-1]
    for k in range(1, len(c)):
        if (l[k] <= stop) if d > 0 else (h[k] >= stop):
            exit_ = min(o[k], stop) if d > 0 else max(o[k], stop)
            break
        if (h[k] >= target) if d > 0 else (l[k] <= target):
            exit_ = target
            break
    return float(d * (exit_ - entry) / entry - 2 * COST / entry)


STRATS = {"ORB-1": orb(1), "ORB-5": orb(5), "ORB-15": orb(15), "ORB-30": orb(30), "VWAP trend": vwap_trend,
          "Intraday momentum (last 30 min)": momentum, "Gap fade": gap_fade}


def stats(cv: pd.Series) -> Dict[str, float]:
    yrs = max((cv.index[-1] - cv.index[0]).days / 365.25, 1e-9)
    r = cv.pct_change().dropna()
    return {"cagr": (cv.iloc[-1] / cv.iloc[0]) ** (1 / yrs) - 1 if cv.iloc[-1] > 0 else -1.0,
            "dd": (cv / cv.cummax() - 1).min(), "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0}


def main() -> int:
    vx = vix()
    lines = ["1-MINUTE DAY-TRADING LAB (rules in minute.py). No leverage; flat every night; $0.0045/share per side.", ""]
    spy_curve = None
    for symbol in ("SPY", "QQQ"):
        df = bars(symbol)
        groups = [(d, g) for d, g in df.groupby(df.index.date) if len(g) >= 300 and g.index[0].time() == time(9, 30)]
        order = [d for d, _ in groups]
        calm_ok = {d: (vx[vx.index < d].iloc[-1] < 20) if (vx.index < d).any() else False for d in order}
        news = news_days(order)
        closes = pd.Series({pd.Timestamp(d): float(g["close"].iloc[-1]) for d, g in groups})
        if symbol == "SPY":
            spy_curve = closes / closes.iloc[0]
        rets = {k: {} for k in STRATS}
        prev = None
        for d, g in groups:
            a = [g[k].values.astype(float) for k in ("open", "high", "low", "close", "volume", "vwap")]
            for name, fn in STRATS.items():
                r = fn(*a, prev)
                if r is not None:
                    rets[name][d] = r
            prev = float(a[3][-1])
        S = stats(spy_curve)
        S1, S2 = stats(spy_curve[spy_curve.index < SPLIT]), stats(spy_curve[spy_curve.index >= SPLIT])
        lines += [f"{symbol}: {order[0]} to {order[-1]}, {len(order)} days",
                  f"{'strategy':<44} {'days':>5} {'won':>4} {'bp/trade':>8} {'t':>5} {'per year':>8} {'worst':>7} "
                  f"{'Sharpe':>6} {'2016-20':>7} {'2021-':>7}  verdict",
                  f"{'Hold SPY (price only)':<44} {'':>5} {'':>4} {'':>8} {'':>5} {S['cagr']:>+8.1%} {S['dd']:>+7.1%} "
                  f"{S['sharpe']:>6.2f} {S1['cagr']:>+7.1%} {S2['cagr']:>+7.1%}  benchmark"]
        for name in STRATS:
            for fname, keep in (("", lambda d: True), (", calm days", lambda d: calm_ok[d]),
                                (", calm + no Fed/jobs days", lambda d: calm_ok[d] and d not in news)):
                r = pd.Series({pd.Timestamp(d): x for d, x in rets[name].items() if keep(d)})
                if len(r) < 50:
                    continue
                daily = r.reindex(closes.index, fill_value=0.0)
                cv = (1 + daily).cumprod()
                st, a, b = stats(cv), stats(cv[cv.index < SPLIT]), stats(cv[cv.index >= SPLIT])
                bp = r.mean() * 1e4
                t = r.mean() / r.std() * np.sqrt(len(r)) if r.std() else 0
                ok = (st["cagr"] > S["cagr"] and a["cagr"] > S1["cagr"] and b["cagr"] > S2["cagr"]
                      and st["dd"] >= S["dd"] and bp >= 1)
                lines.append(f"{(name + fname):<44} {len(r):>5} {np.mean(r > 0):>4.0%} {bp:>+8.1f} {t:>+5.1f} "
                             f"{st['cagr']:>+8.1%} {st['dd']:>+7.1%} {st['sharpe']:>6.2f} {a['cagr']:>+7.1%} "
                             f"{b['cagr']:>+7.1%}  {'PASS' if ok else 'fail'}")
        lines.append("")
    lines += ["bp/trade = average result per trade in basis points of equity (0.01%), after costs; t = how far that",
              "average is from zero in standard errors (|t| < 2 is indistinguishable from luck).",
              "Past results with simulated fills; not a promise and not financial advice."]
    text = "\n".join(lines)
    (HERE / "minute.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
