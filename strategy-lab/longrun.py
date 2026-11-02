#!/usr/bin/env python3
"""Out-of-sample check of the trend rules that lab.py ranked best, on 1999-2016 data lab.py never used (dot-com crash,
2008), plus the whole 2000-now span. Read-only, no orders.

  python3 longrun.py      (needs cache/yahoo_QQQ.json, yahoo_SPY.json, yahoo_IRX.json from Yahoo's chart API)

Same mechanics as lab.py: signal on the close, trade at the next open, 0.03% per side, idle money earns the 13-week
T-bill rate. The 2x rows are simulated (twice the daily move, minus the cost of borrowing the extra 1x at the T-bill rate and 1%
a year in fund fees), because the real 2x
funds only started in 2006. Lookbacks of 150 and 250 days are shown next to 200 only to check that 200 isn't a lucky
setting; the rule itself stays at 200.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(__file__).resolve().parent / "cache"
COST = 0.0003


def load(sym: str) -> pd.DataFrame:
    d = json.loads((CACHE / f"yahoo_{sym}.json").read_text())["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    df = pd.DataFrame({"open": q["open"], "close": q["close"]},
                      index=pd.to_datetime(d["timestamp"], unit="s").normalize())
    if "adjclose" in d["indicators"]:
        adj = np.array(d["indicators"]["adjclose"][0]["adjclose"], dtype=float) / df["close"].values
        df["open"], df["close"] = df["open"] * adj, df["close"] * adj
    return df[~df.index.duplicated()].dropna()


def run(px: pd.DataFrame, signal: pd.Series, cash_daily: pd.Series, lev: float = 1.0) -> pd.Series:
    """Daily equity. In the market from the open after a True close to the open after a False close."""
    o, c = px["open"].values, px["close"].values
    sig = signal.values
    eq, hold, out = 1.0, False, []
    for k in range(1, len(c)):
        want = bool(sig[k - 1])
        if hold:  # overnight move into today's open belongs to the holder
            eq *= 1 + lev * (o[k] / c[k - 1] - 1)
        else:
            eq *= 1 + cash_daily.iloc[k] / 2
        if want != hold:
            eq *= 1 - COST * (lev if want or hold else 1)
            hold = want
        if hold:
            # a 2x fund also pays to borrow the extra 1x (about the T-bill rate) plus ~1% a year in fees
            eq *= 1 + lev * (c[k] / o[k] - 1) - ((lev - 1) * cash_daily.iloc[k] + 0.01 / 252 if lev > 1 else 0)
        else:
            eq *= 1 + cash_daily.iloc[k] / 2
        out.append(eq)
    return pd.Series(out, index=px.index[1:])


def stats(cv: pd.Series) -> str:
    yrs = (cv.index[-1] - cv.index[0]).days / 365.25
    cagr = (cv.iloc[-1] / cv.iloc[0]) ** (1 / yrs) - 1
    dd = (cv / cv.cummax() - 1).min()
    r = cv.pct_change().dropna()
    return f"{cagr:>+8.1%} {dd:>+10.1%} {r.mean() / r.std() * np.sqrt(252):>6.2f}"


def main() -> None:
    qqq, spy, irx = load("QQQ"), load("SPY"), load("IRX")
    idx = qqq.index.intersection(spy.index)
    qqq, spy = qqq.loc[idx], spy.loc[idx]
    cash = (irx["close"].reindex(idx).ffill().bfill() / 100 / 252)
    windows = [("2000-01-03", "2017-01-01", "1999-2016: NEW DATA (dot-com crash, 2008)"),
               ("2017-01-01", None, "2017-now: the lab's period"),
               ("2000-01-03", None, "2000-now: everything")]
    always = pd.Series(True, index=idx)
    rows = [("Hold S&P 500 (SPY)", spy, always, 1.0), ("Hold Nasdaq-100 (QQQ)", qqq, always, 1.0)]
    for n in (150, 200, 250):
        rows.append((f"QQQ {n}-day trend" + ("  <- the rule" if n == 200 else ""), qqq,
                     qqq["close"] > qqq["close"].rolling(n).mean(), 1.0))
    rows.append(("SPY 200-day trend", spy, spy["close"] > spy["close"].rolling(200).mean(), 1.0))
    rows.append(("2x S&P, 200-day trend (simulated)", spy, spy["close"] > spy["close"].rolling(200).mean(), 2.0))
    rows.append(("2x Nasdaq, 200-day trend (simulated)", qqq, qqq["close"] > qqq["close"].rolling(200).mean(), 2.0))
    rows.append(("2x Nasdaq, always held (simulated)", qqq, always, 2.0))
    curves = {name: run(px, sig, cash, lev) for name, px, sig, lev in rows}
    lines = []
    for a, b, title in windows:
        lines += ["", title, f"{'strategy':<38} {'per year':>8} {'worst drop':>10} {'Sharpe':>6}"]
        for name, cv in curves.items():
            w = cv[cv.index >= a]
            w = w[w.index < b] if b else w
            lines.append(f"{name:<38} {stats(w)}")
    lines += ["", "Signals on the close, trades at the next open, 0.03% per side, idle money at the T-bill rate. "
              "Not financial advice."]
    report = "\n".join(lines).lstrip()
    (CACHE.parent / "longrun.txt").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
