#!/usr/bin/env python3
"""Strategy lab: every major, publicly documented strategy family, tested the same way on ~10 years of daily data.
Read-only, no orders.

  ../box-scanner/.venv/bin/python lab.py

Rules fixed BEFORE any result was seen (2026-11-02). Every strategy below was published before 2016, so 2017-2026 is
data none of them were designed on.

  Test     2017-01-03 to the latest close (2016 is warm-up for the 12-month lookbacks). $1 start, signals on the
           close, trades at the NEXT day's open. Costs per side: 0.03% for ETFs, 0.10% for single stocks, 0.35% for
           crypto (0.25% fee + 0.10% slippage). Idle money sits in BIL (T-bills); in crypto tests it sits in cash.
  Halves   2017-2021 and 2022-now (the second includes the 2022 bear market).
  PASS     higher yearly return than the S&P 500 (SPY) over the whole test AND in both halves, with a worst drop no
           deeper than SPY's. Stock-picking strategies must also beat an equal-weight hold of the same stocks,
           as a check on the data.
  SAFER    lower return than SPY, but a better return per unit of risk (Sharpe) and a worst drop at least a third
           smaller.

Strategies
  Hold      SPY; QQQ (Nasdaq-100); 60/40 SPY/AGG rebalanced monthly
  Trend     Faber: SPY above its 10-month average at month end, else T-bills
            SPY above its 200-day average (checked daily), else T-bills
            2x S&P (SSO) while SPY is above its 200-day average, else T-bills (leveraged)
            QQQ above its 200-day average, else T-bills
            2x Nasdaq (QLD) while QQQ is above its 200-day average, else T-bills (leveraged)
  Rotation  Dual momentum (Antonacci): SPY or EFA, whichever rose more over 12 months, if that beat T-bills; else AGG
            Sector momentum: the 3 of 9 sector SPDRs that rose most over 6 months, monthly
            Global trend (Faber "Ivy 5"): SPY, EFA, IEF, GLD, VNQ at 20% each while above their 10-month average
  Timing    RSI(2) dip-buying (Connors): SPY above its 200-day average and RSI(2) under 10; sell on a close above
            the 5-day average
            Turn of the month: SPY for the last trading day of the month and the first 3
            Overnight: buy SPY at the close, sell at the next open, every day
  Stocks    (S&P 500 members as of each date) Momentum: 20 that rose most from 12 months to 1 month ago, monthly
            Momentum with a market filter: the same, but T-bills while SPY is below its 10-month average
            52-week high: 20 closest to their 52-week high, monthly
            Low volatility: 20 with the calmest daily moves over a year, monthly
            Short-term reversal: 20 that fell most over the past week, weekly
            Control: all of them, equal weight, monthly
  Options   (fund proxies, held) XYLD S&P covered calls; QYLD Nasdaq covered calls
  Crypto    (from 2021) BTC hold; BTC above its 200-day / 50-day average, else cash; ETH hold; ETH above its 50-day
"""
from __future__ import annotations

import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BOX = HERE.parent / "box-scanner"
sys.path.insert(0, str(BOX))

START, SPLIT, RECENT = "2017-01-03", "2022-01-01", "2024-04-29"
ETF_COST, STOCK_COST, CRYPTO_COST = 0.0003, 0.0010, 0.0035
TOP_N = 20
SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
ETFS = ["SPY", "QQQ", "EFA", "AGG", "IEF", "GLD", "VNQ", "BIL", "SSO", "QLD", "XYLD", "QYLD"] + SECTORS
CRYPTO = ["BTC/USD", "ETH/USD"]


# ---------- data ----------

def download() -> Dict[str, pd.DataFrame]:
    """Daily bars from 2016 (split- and dividend-adjusted, full-market SIP feed), cached in cache/bars.pkl."""
    cache = HERE / "cache" / "bars.pkl"
    if cache.exists() and datetime.now().timestamp() - cache.stat().st_mtime < 86400:
        return pickle.loads(cache.read_bytes())
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from data import with_retries
    from settings import load_credentials
    creds = load_credentials(BOX / ".env")
    stocks = sorted(set().union(*membership().values()))
    client = StockHistoricalDataClient(creds.api_key, creds.secret_key)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)  # SIP history is free when older than 15 minutes
    out: Dict[str, pd.DataFrame] = {}
    names = ETFS + [s for s in stocks if s not in ETFS]
    for i in range(0, len(names), 100):
        req = StockBarsRequest(symbol_or_symbols=names[i:i + 100], timeframe=TimeFrame.Day,
                               start=datetime(2016, 1, 1, tzinfo=timezone.utc), end=end,
                               adjustment=Adjustment.ALL, feed=DataFeed.SIP)
        raw = with_retries(client.get_stock_bars, req, what="stock bars").df
        for sym, df in raw.groupby(level=0):
            df = df.droplevel(0)[["open", "close", "volume"]].astype(float)
            df.index = pd.DatetimeIndex([t.tz_convert("America/New_York").date() for t in df.index])
            out[sym] = df
        print(f"  downloaded {min(i + 100, len(names))}/{len(names)} symbols", flush=True)
    crypto = CryptoHistoricalDataClient(creds.api_key, creds.secret_key)
    raw = crypto.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=CRYPTO, timeframe=TimeFrame.Day,
                                                   start=datetime(2021, 1, 1, tzinfo=timezone.utc))).df
    for sym, df in raw.groupby(level=0):
        df = df.droplevel(0)[["open", "close", "volume"]].astype(float)
        df.index = pd.DatetimeIndex([t.date() for t in df.index])
        out[sym] = df
    out["_stocks"] = [s for s in stocks if s in out]
    cache.write_bytes(pickle.dumps(out))
    return out


def membership() -> Dict[pd.Timestamp, set]:
    """S&P 500 members on each change date since 2016 (cache/sp500_history.csv, from github.com/fja05680/sp500),
    so each month only that month's members can be picked, including companies later removed."""
    df = pd.read_csv(HERE / "cache" / "sp500_history.csv")
    df = df[df["date"] >= "2015-01-01"]
    return {pd.Timestamp(d): set(t.split(",")) for d, t in zip(df["date"], df["tickers"])}


def member_mask(columns: List[str], calendar: pd.DatetimeIndex) -> np.ndarray:
    m = membership()
    dates = sorted(m)
    pos = np.searchsorted(np.array(dates, dtype="datetime64[ns]"), calendar.values, side="right") - 1
    rows = {i: np.array([s in m[dates[i]] for s in columns]) for i in set(pos) if i >= 0}
    return np.array([rows.get(i, np.zeros(len(columns), bool)) for i in pos])


def matrices(bars: Dict, symbols: List[str], calendar: pd.DatetimeIndex):
    """Opens (NaN where a symbol didn't trade), closes (carried forward) and dollar volume, dates x symbols."""
    o = pd.DataFrame({s: bars[s]["open"] for s in symbols}).reindex(calendar)
    c = pd.DataFrame({s: bars[s]["close"] for s in symbols}).reindex(calendar)
    dv = (c * pd.DataFrame({s: bars[s]["volume"] for s in symbols}).reindex(calendar))
    return o, c.ffill(), dv


# ---------- the account ----------

def simulate(target_at: Callable[[int], Optional[Dict[str, float]]], o: pd.DataFrame, c: pd.DataFrame, start: int,
             cost: float) -> pd.Series:
    """Equity each close. target_at(k) is decided on day k's close and returns new weights (or None to keep the
    current holdings); the trades happen at day k+1's open."""
    col = {s: i for i, s in enumerate(c.columns)}
    O, C = o.values, c.values
    units = np.zeros(len(col))
    cash, pending, curve = 1.0, target_at(start - 1), []
    for k in range(start, len(C)):
        if pending is not None:
            px = np.where(np.isnan(O[k]), C[k - 1], O[k])
            px = np.nan_to_num(px)
            eq = cash + units @ px
            w = np.zeros(len(col))
            for s, v in pending.items():
                if px[col[s]] > 0:
                    w[col[s]] = v
            new = np.divide(w * eq, px, out=np.zeros_like(px), where=px > 0)
            fee = np.abs(new - units) @ px * cost
            cash = eq - new @ px - fee
            units, pending = new, None
        curve.append(cash + units @ np.nan_to_num(C[k]))
        t = target_at(k)
        if t is not None:
            pending = t
    return pd.Series(curve, index=c.index[start:])


def stats(curve: pd.Series, per_year: int = 252) -> Dict[str, float]:
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    r = curve.pct_change().dropna()
    return {"cagr": (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0,
            "dd": (curve / curve.cummax() - 1).min(),
            "sharpe": r.mean() / r.std() * np.sqrt(per_year) if r.std() > 0 else 0.0,
            "total": curve.iloc[-1] / curve.iloc[0] - 1}


def window(curve: pd.Series, a: str, b: Optional[str] = None) -> pd.Series:
    s = curve[curve.index >= pd.Timestamp(a)]
    return s[s.index < pd.Timestamp(b)] if b else s


# ---------- strategies ----------

def month_ends(idx: pd.DatetimeIndex) -> np.ndarray:
    m = idx.month.values
    return np.append(m[:-1] != m[1:], False)


def week_ends(idx: pd.DatetimeIndex) -> np.ndarray:
    w = idx.isocalendar().week.values
    return np.append(w[:-1] != w[1:], False)


def on_change(signal: Callable[[int], Dict[str, float]]) -> Callable[[int], Optional[Dict[str, float]]]:
    """Trade only when the daily signal changes."""
    last = {"t": None}

    def f(k: int):
        t = signal(k)
        if t != last["t"]:
            last["t"] = t
            return t
        return None
    return f


def monthly(pick: Callable[[int], Dict[str, float]], ends: np.ndarray, start: int):
    """Rebalance to pick(k) at each month end (and once at the start)."""
    return lambda k: pick(k) if (ends[k] or k == start - 1) else None


def rsi2(close: pd.Series) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def etf_strategies(o, c, start) -> Dict[str, Callable]:
    ends = month_ends(c.index)
    C = c.values
    i = {s: n for n, s in enumerate(c.columns)}
    sma200 = c.rolling(200).mean().values
    sma5 = c.rolling(5).mean().values
    end_idx = np.flatnonzero(ends)

    def sma10m(k: int, s: str) -> float:  # average of the last 10 month-end closes, including today if a month end
        prev = end_idx[end_idx <= k][-10:]
        return C[prev, i[s]].mean() if len(prev) == 10 else np.nan

    def ret(k: int, s: str, n: int) -> float:
        return C[k, i[s]] / C[k - n, i[s]] - 1

    above = lambda k, s: C[k, i[s]] > sma200[k, i[s]]
    rsi = rsi2(c["SPY"]).values
    td_in_month = c.groupby([c.index.year, c.index.month]).cumcount().values + 1
    td_left = c.iloc[::-1].groupby([c.index[::-1].year, c.index[::-1].month]).cumcount().values[::-1] + 1
    state = {"rsi": False}

    def rsi_signal(k: int):
        if not state["rsi"] and above(k, "SPY") and rsi[k] < 10:
            state["rsi"] = True
        elif state["rsi"] and C[k, i["SPY"]] > sma5[k, i["SPY"]]:
            state["rsi"] = False
        return {"SPY": 1.0} if state["rsi"] else {"BIL": 1.0}

    def tom_signal(k: int):
        # buy at the open of the last trading day (signal on the day before it), sell at the open of day 4
        if td_left[k] == 2 or (td_in_month[k] < 3 and td_left[k] > 2):
            return {"SPY": 1.0}
        return {"BIL": 1.0}

    def dual(k: int):
        spy, efa, bil = ret(k, "SPY", 252), ret(k, "EFA", 252), ret(k, "BIL", 252)
        if max(spy, efa) <= bil:
            return {"AGG": 1.0}
        return {"SPY": 1.0} if spy >= efa else {"EFA": 1.0}

    def sectors(k: int):
        top = sorted(SECTORS, key=lambda s: -ret(k, s, 126))[:3]
        return {s: 1 / 3 for s in top}

    def ivy(k: int):
        w: Dict[str, float] = {}
        for s in ("SPY", "EFA", "IEF", "GLD", "VNQ"):
            key = s if C[k, i[s]] > sma10m(k, s) else "BIL"
            w[key] = w.get(key, 0) + 0.2
        return w

    return {
        "Hold S&P 500 (SPY)": lambda k: {"SPY": 1.0} if k == start - 1 else None,
        "Hold Nasdaq-100 (QQQ)": lambda k: {"QQQ": 1.0} if k == start - 1 else None,
        "60/40 stocks/bonds": monthly(lambda k: {"SPY": 0.6, "AGG": 0.4}, ends, start),
        "Trend: SPY 10-month avg (Faber)": monthly(lambda k: {"SPY": 1.0} if C[k, i["SPY"]] > sma10m(k, "SPY")
                                                   else {"BIL": 1.0}, ends, start),
        "Trend: SPY 200-day avg": on_change(lambda k: {"SPY": 1.0} if above(k, "SPY") else {"BIL": 1.0}),
        "Trend: 2x S&P (SSO) 200-day": on_change(lambda k: {"SSO": 1.0} if above(k, "SPY") else {"BIL": 1.0}),
        "Trend: QQQ 200-day avg": on_change(lambda k: {"QQQ": 1.0} if above(k, "QQQ") else {"BIL": 1.0}),
        "Trend: 2x Nasdaq (QLD) 200-day": on_change(lambda k: {"QLD": 1.0} if above(k, "QQQ") else {"BIL": 1.0}),
        "Dual momentum (Antonacci)": monthly(dual, ends, start),
        "Sector momentum, top 3": monthly(sectors, ends, start),
        "Global trend (Faber Ivy 5)": monthly(ivy, ends, start),
        "RSI(2) dip-buying (Connors)": on_change(rsi_signal),
        "Turn of the month": on_change(tom_signal),
        "Covered calls (XYLD)": lambda k: {"XYLD": 1.0} if k == start - 1 else None,
        "Nasdaq covered calls (QYLD)": lambda k: {"QYLD": 1.0} if k == start - 1 else None,
    }


def overnight(o, c, start) -> pd.Series:
    """Buy SPY at every close, sell at the next open; T-bills are ignored (money is idle for the day)."""
    O, C = o["SPY"].values, c["SPY"].values
    r = O[start:] / C[start - 1:-1] * (1 - ETF_COST) / (1 + ETF_COST)
    return pd.Series(np.cumprod(r), index=c.index[start:])


def stock_strategies(o, c, dv, spy_c, start, member: np.ndarray) -> Dict[str, Callable]:
    ends, wends = month_ends(c.index), week_ends(c.index)
    C = c.values
    liquid = (dv.rolling(20).mean() >= 10_000_000).values & member
    names = np.array(c.columns)
    daily = np.log(c).diff()
    vol = daily.rolling(252, min_periods=240).std().values
    hi = c.rolling(252, min_periods=240).max().values
    spy = spy_c.values
    spy_end_idx = np.flatnonzero(ends)

    def spy_up(k: int) -> bool:
        prev = spy_end_idx[spy_end_idx <= k][-10:]
        return len(prev) < 10 or spy[k] > spy[prev].mean()

    def top(k: int, score: np.ndarray, highest: bool) -> Dict[str, float]:
        ok = liquid[k] & ~np.isnan(score)
        idx = np.flatnonzero(ok)
        idx = idx[np.argsort(-score[idx] if highest else score[idx])][:TOP_N]
        return {names[j]: 1 / len(idx) for j in idx}

    mom = lambda k: top(k, C[k - 21] / C[k - 252] - 1, True)
    return {
        "Stocks: momentum 12-1": monthly(mom, ends, start),
        "Stocks: momentum + market filter": monthly(lambda k: mom(k) if spy_up(k) else {"BIL": 1.0}, ends, start),
        "Stocks: 52-week high": monthly(lambda k: top(k, C[k] / hi[k], True), ends, start),
        "Stocks: low volatility": monthly(lambda k: top(k, vol[k], False), ends, start),
        "Stocks: short-term reversal": lambda k: top(k, C[k] / C[k - 5] - 1, False)
        if (wends[k] or k == start - 1) else None,
        "Stocks: all, equal weight (control)": monthly(
            lambda k: {names[j]: 1 / len(np.flatnonzero(liquid[k] & ~np.isnan(C[k])))
                       for j in np.flatnonzero(liquid[k] & ~np.isnan(C[k]))}, ends, start),
    }


def crypto_strategies(c) -> Dict[str, Callable]:
    C = c.values
    i = {s: n for n, s in enumerate(c.columns)}
    s200, s50 = c.rolling(200).mean().values, c.rolling(50).mean().values
    up = lambda k, s, m: C[k, i[s]] > m[k, i[s]]
    return {
        "Crypto: hold BTC": lambda k: {"BTC/USD": 1.0} if k == 199 else None,
        "Crypto: BTC 200-day avg": on_change(lambda k: {"BTC/USD": 1.0} if up(k, "BTC/USD", s200) else {}),
        "Crypto: BTC 50-day avg": on_change(lambda k: {"BTC/USD": 1.0} if up(k, "BTC/USD", s50) else {}),
        "Crypto: hold ETH": lambda k: {"ETH/USD": 1.0} if k == 199 else None,
        "Crypto: ETH 50-day avg": on_change(lambda k: {"ETH/USD": 1.0} if up(k, "ETH/USD", s50) else {}),
    }


# ---------- report ----------

def main() -> int:
    bars = download()
    stocks: List[str] = bars["_stocks"]
    calendar = bars["SPY"].index
    start = int(np.searchsorted(calendar, pd.Timestamp(START)))
    o, c, _ = matrices(bars, ETFS, calendar)
    curves: Dict[str, pd.Series] = {}
    for name, fn in etf_strategies(o, c, start).items():
        curves[name] = simulate(fn, o, c, start, ETF_COST)
    curves["Overnight SPY"] = overnight(o, c, start)
    so, sc, sdv = matrices(bars, stocks + ["BIL"], calendar)
    mask = member_mask(list(sc.columns), calendar)
    for name, fn in stock_strategies(so, sc, sdv, c["SPY"], start, mask).items():
        curves[name] = simulate(fn, so, sc, start, STOCK_COST)

    spy = curves["Hold S&P 500 (SPY)"]
    ctrl = curves["Stocks: all, equal weight (control)"]
    halves = lambda cv: (stats(window(cv, START, SPLIT))["cagr"], stats(window(cv, SPLIT))["cagr"])
    S, (S1, S2), C1 = stats(spy), halves(spy), stats(ctrl)["cagr"]

    def verdict(name: str, st: Dict, h) -> str:
        if name.startswith("Hold S&P") or "(control)" in name:
            return "benchmark"
        beats = st["cagr"] > S["cagr"] and h[0] > S1 and h[1] > S2 and st["dd"] >= S["dd"]
        if beats and (not name.startswith("Stocks") or st["cagr"] > C1):
            return "PASS"
        if st["sharpe"] > S["sharpe"] and st["dd"] > S["dd"] * 2 / 3:
            return "SAFER"
        return "fail"

    head = f"{'strategy':<38} {'per year':>8} {'worst drop':>10} {'Sharpe':>6} {'2017-21':>8} {'2022-now':>8} " \
           f"{'since Apr 24':>12}  verdict"
    rows = []
    for name, cv in curves.items():
        st, h = stats(cv), halves(cv)
        rows.append((st["cagr"], f"{name:<38} {st['cagr']:>+8.1%} {st['dd']:>+10.1%} {st['sharpe']:>6.2f} "
                                 f"{h[0]:>+8.1%} {h[1]:>+8.1%} {stats(window(cv, RECENT))['total']:>+12.1%}  "
                                 f"{verdict(name, st, h)}"))
    rows.sort(key=lambda r: -r[0])

    ccal = bars["BTC/USD"].index
    co, cc, _ = matrices(bars, CRYPTO, ccal)
    crows = []
    cstart = 200
    for name, fn in crypto_strategies(cc).items():
        cv = simulate(fn, co, cc, cstart, CRYPTO_COST)
        st = stats(cv, 365)
        crows.append(f"{name:<38} {st['cagr']:>+8.1%} {st['dd']:>+10.1%} {st['sharpe']:>6.2f}")
    spy_c = stats(window(spy, str(ccal[cstart].date())))

    lines = [f"STRATEGY LAB: {START} to {spy.index[-1].date()} (~{(spy.index[-1] - spy.index[0]).days / 365.25:.1f} "
             f"years), signals on the close, trades at the next open, costs included.",
             f"PASS = beats SPY per year overall and in both halves, worst drop no deeper than SPY's "
             f"({S['dd']:.1%}); stock strategies must also beat the equal-weight control.",
             "SAFER = less return than SPY but better Sharpe and a worst drop at least a third smaller.", "", head]
    lines += [r[1] for r in rows]
    lines += ["", f"CRYPTO, {ccal[cstart].date()} to {ccal[-1].date()} (0.35% cost per side, idle money in cash)",
              f"{'strategy':<38} {'per year':>8} {'worst drop':>10} {'Sharpe':>6}"] + crows
    lines += [f"{'(SPY over the same dates)':<38} {spy_c['cagr']:>+8.1%} {spy_c['dd']:>+10.1%} "
              f"{spy_c['sharpe']:>6.2f}", "",
              f"Stock strategies pick only from that date's S&P 500 members ({len(stocks)} stocks with price data); "
              "a few removed companies have no data, so stock results may still be slightly high.",
              "Past results with simulated fills; not a promise and not financial advice."]
    report = "\n".join(lines)
    (HERE / "results.txt").write_text(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
