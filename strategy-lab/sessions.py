#!/usr/bin/env python3
"""Four-session opening-range lab: the opening-range breakout run at the start of EACH of the four sessions from the
user's liquidity video, not just once a day. Read-only, no orders.

  ../box-scanner/.venv/bin/python sessions.py qqq      # QQQ, cached 5-minute bars (minutes)
  ../box-scanner/.venv/bin/python sessions.py crypto   # BTC/ETH/SOL from Coinbase's public candles (~30 min download)
  ../box-scanner/.venv/bin/python sessions.py stocks   # S&P 500 "stocks in play" (~2 h download, resumable)
  ../box-scanner/.venv/bin/python sessions.py report   # write sessions.txt from whatever has finished

Sessions (New York time, from the video): pre-open/Asia 4:00-7:00, London 7:00-9:30, New York 9:30-16:00,
late 16:00-20:00. Each trade is closed by the end of its session.

Rules fixed BEFORE any result was seen (2026-11-03):
  QQQ / CRYPTO ORB  Zarattini & Aziz (2023) rules at each session open: the session's first 5-minute candle sets the
        direction (up = buy, down = short, flat = no trade); enter at the next candle's open; stop at the first
        candle's low (long) / high (short); target 10x the risk; otherwise out at the session's last close. Risk 1%
        of equity per trade, at most 4x leverage. Crypto also runs as Coinbase spot can actually trade it: long
        only, no leverage.
  STOCKS IN PLAY  Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy For The U.S. Equity Market",
        at each session open. Universe: S&P 500 members on that date (avoids survivorship bias; the paper used all
        US stocks), price > $5, 14-day average volume >= 1M shares, 14-day ATR > $0.50. Relative volume = the
        session's first 5-minute volume / its average over the previous 14 days. Trade the top 20 with relative
        volume >= 1 in the first candle's direction; stop 10% of ATR from entry; out at the session's end. Each trade
        risks 1% of equity, each position at most 4x/20 of equity (4x in total).
  Costs: QQQ and stocks $0.0045/share per side in regular hours, 0.10% of price per side in pre-market/after-hours
        (wider spreads). Crypto spot 0.60% per side (Coinbase's low-volume taker fee); crypto 4x as US perpetual
        futures, 0.05% per side (0.02% fee + $0.15/contract + slippage; funding ignored). Gross (no-cost) results are
        shown too, to separate "no edge" from "edge eaten by costs".
  PASS: beats holding SPY per year overall and in both halves (to 2020, 2021-now), worst drop no deeper than SPY's.
"""
from __future__ import annotations

import json
import pickle
import sys
import time as clock
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BOX = HERE.parent / "box-scanner"
sys.path.insert(0, str(BOX))
NY = ZoneInfo("America/New_York")
CACHE = HERE / "cache"
SESSIONS = [("Pre-open / Asia 4:00-7:00", time(4), time(7)), ("London 7:00-9:30", time(7), time(9, 30)),
            ("New York 9:30-16:00", time(9, 30), time(16)), ("Late 16:00-20:00", time(16), time(20))]
RTH_COST, EXT_COST = 0.0045, 0.0010  # $/share in regular hours; fraction of price outside them
SPLIT = "2021-01-01"


def stats(cv: pd.Series) -> Dict[str, float]:
    cv = cv[~cv.index.duplicated(keep="last")]
    yrs = max((cv.index[-1] - cv.index[0]).days / 365.25, 1e-9)
    r = cv.pct_change().dropna()
    return {"cagr": (cv.iloc[-1] / cv.iloc[0]) ** (1 / yrs) - 1 if cv.iloc[-1] > 0 else -1.0,
            "dd": (cv / cv.cummax() - 1).min(), "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0}


# ------------------------------------------------------------------ single-instrument ORB (QQQ, crypto)
def orb_session(d: pd.DataFrame, a: time, b: time) -> Tuple[float, float, float]:
    """(direction, entry, exit) for one session of one instrument; direction 0 = no trade."""
    t = d.index.time
    s = d[(t >= a) & (t < b)]
    if len(s) < 3 or s.index[0].time() != a:
        return 0.0, 0.0, 0.0
    o, h, l, c = (s[k].values for k in ("open", "high", "low", "close"))
    direction = float(np.sign(c[0] - o[0]))
    entry = o[1]
    stop = l[0] if direction > 0 else h[0]
    risk = (entry - stop) * direction
    if direction == 0 or risk <= 0:
        return 0.0, 0.0, 0.0
    target = entry + direction * 10 * risk
    for k in range(1, len(s)):
        if (l[k] <= stop) if direction > 0 else (h[k] >= stop):
            return direction, entry, (min(o[k], stop) if direction > 0 else max(o[k], stop))
        if (h[k] >= target) if direction > 0 else (l[k] <= target):
            return direction, entry, target
    return direction, entry, c[-1]


def orb_curve(days: Dict[date, pd.DataFrame], sessions, max_lev: float, long_only: bool, cost) -> Tuple[pd.Series, list]:
    """cost(price, session_start) -> $ per unit per side. Sessions run one after another, compounding."""
    eq, curve, trades = 1.0, {}, []
    for day in sorted(days):
        for _, a, b in sessions:
            direction, entry, exit_ = orb_session(days[day], a, b)
            if direction == 0 or (long_only and direction < 0):
                continue
            first = days[day][days[day].index.time >= a].iloc[0]
            risk = abs(entry - (first["low"] if direction > 0 else first["high"]))
            units = min(eq * 0.01 / risk, max_lev * eq / entry)
            pnl = units * (direction * (exit_ - entry) - cost(entry, a) - cost(exit_, a))
            trades.append(pnl / eq)
            eq += pnl
        curve[pd.Timestamp(day)] = eq
    return pd.Series(curve), trades


def stock_cost(price: float, a: time) -> float:
    return RTH_COST if a == time(9, 30) else EXT_COST * price


def run_qqq() -> Dict[str, Tuple[pd.Series, str]]:
    df = pickle.loads((CACHE / "qqq5m_all.pkl").read_bytes())
    days = {d: g for d, g in df.groupby(df.index.date)}
    out = {}
    for label, sess in [(s[0], [s]) for s in SESSIONS] + [("All 4 sessions", SESSIONS)]:
        for lev in (4.0,):
            for name, cost in (("", stock_cost), (" (no costs)", lambda p, a: 0.0)):
                cv, tr = orb_curve(days, sess, lev, False, cost)
                tr = np.array(tr)
                out[f"QQQ ORB, {label}{name}"] = (cv, f"{len(tr)} trades, won {np.mean(tr > 0):.0%}, "
                                                      f"average {tr.mean() * 1e4:+.1f} bp of equity")
    return out


# ------------------------------------------------------------------ crypto (Coinbase public candles)
def coinbase_5m(product: str) -> pd.DataFrame:
    path = CACHE / f"coinbase_{product}_5m.pkl"
    have = pickle.loads(path.read_bytes()) if path.exists() else pd.DataFrame()
    first = {"BTC-USD": datetime(2016, 1, 1), "ETH-USD": datetime(2016, 5, 19), "SOL-USD": datetime(2021, 6, 18)}
    start = (have.index[-1].tz_convert(timezone.utc).to_pydatetime() if len(have)
             else first[product].replace(tzinfo=timezone.utc))
    end = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows, t, n = [], start, 0
    while t < end:
        u = min(t + timedelta(minutes=5 * 300), end)
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=300"
               f"&start={t:%Y-%m-%dT%H:%M:%SZ}&end={u:%Y-%m-%dT%H:%M:%SZ}")
        for attempt in range(6):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "strategy-lab"})
                data = json.loads(urllib.request.urlopen(req, timeout=30).read())
                break
            except Exception as e:
                clock.sleep(2 ** attempt)
        else:
            raise SystemExit(f"Coinbase candles failed for {product} at {t}: {e}")
        if isinstance(data, list):
            rows += data
        t = u
        n += 1
        clock.sleep(0.12)
        if n % 500 == 0:
            print(f"  {product}: {t:%Y-%m-%d}", flush=True)
    new = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    new.index = pd.DatetimeIndex(pd.to_datetime(new.pop("t"), unit="s", utc=True)).tz_convert(NY)
    df = pd.concat([have, new.astype(float)]).sort_index()
    df = df[~df.index.duplicated()]
    path.write_bytes(pickle.dumps(df))
    return df


def run_crypto() -> Dict[str, Tuple[pd.Series, str]]:
    out = {}
    for product in ("BTC-USD", "ETH-USD", "SOL-USD"):
        df = coinbase_5m(product)
        print(f"  {product}: {len(df)} candles from {df.index[0].date()}", flush=True)
        days = {d: g for d, g in df.groupby(df.index.date)}
        coin = product.split("-")[0]
        hold = df["close"].groupby(df.index.date).last()
        hold.index = pd.to_datetime(hold.index)
        out[f"Hold {coin}"] = (hold / hold.iloc[0], "buy and hold, for comparison")
        variants = [("spot: long only, 1x, 0.60% fee", 1.0, True, 0.006), ("spot, no fees", 1.0, True, 0.0),
                    ("futures: long/short 4x, 0.05%", 4.0, False, 0.0005), ("futures, no fees", 4.0, False, 0.0)]
        for label, sess in [(s[0].split(" ")[0], [s]) for s in SESSIONS] + [("All 4", SESSIONS)]:
            for vname, lev, lo, fee in variants:
                cv, tr = orb_curve(days, sess, lev, lo, lambda p, a, f=fee: f * p)
                tr = np.array(tr) if tr else np.zeros(1)
                out[f"{coin} ORB, {label} session, {vname}"] = (
                    cv, f"{len(tr)} trades, won {np.mean(tr > 0):.0%}, average {tr.mean() * 1e4:+.1f} bp of equity")
    pickle.dump(out, open(CACHE / "sessions_crypto.pkl", "wb"))
    return out


# ------------------------------------------------------------------ stocks in play (S&P 500, Alpaca SIP)
def members_by_day() -> pd.Series:
    h = pd.read_csv(CACHE / "sp500_history.csv", parse_dates=["date"]).set_index("date")["tickers"]
    return h.map(lambda s: sorted(set(s.split(","))))


def summarize(df: pd.DataFrame, hist: List[dict], top: int = 40) -> dict:
    """Compact record for one day: RTH daily bar + first-bar volume per session for every symbol, and full session
    bars for the `top` symbols by relative volume (among those passing the filters) in each session."""
    rec = {"daily": {}, "first": {}, "bars": {}}
    groups = {sym: g.droplevel(0) for sym, g in df.groupby(level=0)}
    for sym, g in groups.items():
        t = g.index.time
        rth = g[(t >= time(9, 30)) & (t < time(16))]
        if len(rth):
            rec["daily"][sym] = (rth["open"].iloc[0], rth["high"].max(), rth["low"].min(), rth["close"].iloc[-1],
                                 rth["volume"].sum())
        firsts = []
        for _, a, _ in SESSIONS:
            row = g[t == a]
            firsts.append(float(row["volume"].iloc[0]) if len(row) else 0.0)
        rec["first"][sym] = firsts
    ok = filters(hist)
    for i, (_, a, b) in enumerate(SESSIONS):
        ranked = sorted(((rel_volume(hist, i, s, rec["first"][s][i]), s) for s in ok if s in rec["first"]),
                        reverse=True)[:top]
        for rv, sym in ranked:
            if rv <= 0:
                continue
            g = groups[sym]
            t = g.index.time
            x = g[(t >= a) & (t < b)]
            rec["bars"][(i, sym)] = (np.array([v.hour * 60 + v.minute for v in x.index.time], dtype=np.int16),
                                     x[["open", "high", "low", "close"]].values.astype(np.float32))
    return rec


def stock_days() -> Dict[date, dict]:
    """Download (resumable) one multi-symbol request per trading day, 4 AM-8 PM, keep only the compact record.
    Candidates per session are picked with the 14-day history built from earlier records."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from data import with_retries
    from settings import load_credentials
    creds = load_credentials(BOX / ".env")
    client = StockHistoricalDataClient(creds.api_key, creds.secret_key)
    store = CACHE / "sip_sessions"
    store.mkdir(exist_ok=True)
    recs: Dict[date, dict] = {}
    for f in sorted(store.glob("*.pkl")):
        recs.update(pickle.loads(f.read_bytes()))
    members = members_by_day()
    spy = pickle.loads((CACHE / "bars5m.pkl").read_bytes())["SPY"]
    trading_days = sorted({d for d in spy.index.date})
    today = datetime.now(NY).date()
    month_buf: Dict[date, dict] = {}
    for day in trading_days:
        if day in recs or day >= today:
            continue
        syms = members[members.index <= pd.Timestamp(day)].iloc[-1]
        start = datetime.combine(day, time(4), NY)
        req = StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=start,
                               end=start + timedelta(hours=16), adjustment=Adjustment.RAW, feed=DataFeed.SIP)
        df = with_retries(client.get_stock_bars, req, what=f"bars {day}").df
        if df.empty:
            continue
        df.index = df.index.set_levels(df.index.levels[1].tz_convert(NY), level=1)
        recs[day] = month_buf[day] = summarize(df, history(recs, day))
        if day.day >= 28 or day == trading_days[-1] or len(month_buf) >= 23:
            flush(store, month_buf)
        if len(recs) % 50 == 0:
            print(f"  stocks: {day} ({len(recs)} days)", flush=True)
    flush(store, month_buf)
    return recs


def flush(store: Path, buf: Dict[date, dict]) -> None:
    for key in sorted({(d.year, d.month) for d in buf}):
        f = store / f"{key[0]}-{key[1]:02d}.pkl"
        old = pickle.loads(f.read_bytes()) if f.exists() else {}
        old.update({d: r for d, r in buf.items() if (d.year, d.month) == key})
        f.write_bytes(pickle.dumps(old))
    buf.clear()


def history(recs: Dict[date, dict], day: date, n: int = 15) -> List[dict]:
    prior = [d for d in sorted(recs) if d < day][-n:]
    return [recs[d] for d in prior]


def filters(hist: List[dict]) -> Dict[str, Tuple[float, float]]:
    """{symbol: (ATR14, last close)} for symbols passing price > 5, avg volume >= 1M, ATR > 0.50."""
    if len(hist) < 15:
        return {}
    ok = {}
    for sym in hist[-1]["daily"]:
        rows = [h["daily"].get(sym) for h in hist]
        if any(r is None for r in rows):
            continue
        hi, lo, cl, vol = (np.array([r[k] for r in rows]) for k in (1, 2, 3, 4))
        tr = np.maximum(hi[1:] - lo[1:], np.maximum(abs(hi[1:] - cl[:-1]), abs(lo[1:] - cl[:-1])))
        atr = tr[-14:].mean()
        if cl[-1] > 5 and vol[-14:].mean() >= 1e6 and atr > 0.5:
            ok[sym] = (atr, cl[-1])
    return ok


def rel_volume(hist: List[dict], i: int, sym: str, first_vol: float) -> float:
    past = [h["first"].get(sym, [0, 0, 0, 0])[i] for h in hist[-14:]]
    avg = np.mean(past) if past else 0
    return first_vol / avg if avg > 0 else 0.0


def run_stocks() -> Dict[str, Tuple[pd.Series, str]]:
    recs = stock_days()
    order = sorted(recs)
    out = {}
    for label, idxs in [(s[0], [i]) for i, s in enumerate(SESSIONS)] + [("All 4 sessions", [0, 1, 2, 3])]:
        for cname, costs in (("", True), (" (no costs)", False)):
            eq, curve, trades = 1.0, {}, []
            for n, day in enumerate(order):
                hist = [recs[d] for d in order[max(0, n - 15):n]]
                ok = filters(hist)
                rec = recs[day]
                for i in idxs:
                    a = SESSIONS[i][1]
                    ranked = sorted(((rel_volume(hist, i, s, rec["first"].get(s, [0] * 4)[i]), s) for s in ok
                                     if (i, s) in rec["bars"]), reverse=True)
                    picks = [s for rv, s in ranked if rv >= 1][:20]
                    pnl = 0.0
                    for s in picks:
                        mins, x = rec["bars"][(i, s)]
                        if len(x) < 2 or mins[0] != a.hour * 60 + a.minute:
                            continue
                        o, h, l, c = x[:, 0].astype(float), x[:, 1], x[:, 2], x[:, 3]
                        direction = np.sign(c[0] - o[0])
                        if direction == 0:
                            continue
                        entry = o[1]
                        dist = 0.1 * ok[s][0]
                        stop = entry - direction * dist
                        exit_ = float(c[-1])
                        for k in range(1, len(x)):
                            if (l[k] <= stop) if direction > 0 else (h[k] >= stop):
                                exit_ = min(o[k], stop) if direction > 0 else max(o[k], stop)
                                break
                        shares = min(eq * 0.01 / dist, 4 * eq / 20 / entry)
                        cost = 2 * shares * stock_cost(entry, a) if costs else 0.0
                        p = shares * direction * (exit_ - entry) - cost
                        trades.append(p / eq)
                        pnl += p
                    eq += pnl
                curve[pd.Timestamp(day)] = eq
            tr = np.array(trades) if trades else np.zeros(1)
            out[f"Stocks in play, {label}{cname}"] = (pd.Series(curve), f"{len(trades)} trades, won {np.mean(tr > 0):.0%}, "
                                                                         f"average {tr.mean() * 1e4:+.2f} bp of equity")
    pickle.dump(out, open(CACHE / "sessions_stocks.pkl", "wb"))
    return out


# ------------------------------------------------------------------ report
def report(results: Dict[str, Tuple[pd.Series, str]]) -> str:
    spy5 = pickle.loads((CACHE / "bars5m.pkl").read_bytes())["SPY"]
    spy = spy5.groupby(spy5.index.date)["close"].last()
    spy.index = pd.to_datetime(spy.index)
    lines = ["FOUR-SESSION OPENING-RANGE LAB (rules in sessions.py). Sessions in New York time.", "",
             f"{'strategy':<62} {'from':>10} {'per year':>8} {'worst drop':>10} {'Sharpe':>6} {'to 2020':>8} "
             f"{'2021-now':>8}  verdict"]
    starts = sorted({cv.index[0] for cv, _ in results.values()})
    for st in starts:
        s = spy[spy.index >= st]
        S, S1, S2 = stats(s), stats(s[s.index < SPLIT]) if (s.index < SPLIT).sum() > 250 else None, stats(s[s.index >= SPLIT])
        lines.append(f"{'Hold SPY (price only)':<62} {str(st.date()):>10} {S['cagr']:>+8.1%} {S['dd']:>+10.1%} "
                     f"{S['sharpe']:>6.2f} {(format(S1['cagr'], '+.1%') if S1 else '-'):>8} {S2['cagr']:>+8.1%}  benchmark")
        for name, (cv, note) in results.items():
            if cv.index[0] != st:
                continue
            a = stats(cv[cv.index < SPLIT]) if (cv.index < SPLIT).sum() > 250 else None
            b, x = stats(cv[cv.index >= SPLIT]), stats(cv)
            ok = (x["cagr"] > S["cagr"] and b["cagr"] > S2["cagr"] and x["dd"] >= S["dd"]
                  and (a is None or S1 is None or a["cagr"] > S1["cagr"]))
            verdict = "benchmark" if name.startswith("Hold") else ("PASS" if ok else "fail")
            lines.append(f"{name:<62} {'':>10} {x['cagr']:>+8.1%} {x['dd']:>+10.1%} {x['sharpe']:>6.2f} "
                         f"{(format(a['cagr'], '+.1%') if a else '-'):>8} {b['cagr']:>+8.1%}  {verdict}")
            lines.append(f"{'':<62}   {note}")
        lines.append("")
    lines.append("Past results with simulated fills; not a promise and not financial advice.")
    return "\n".join(lines)


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "report"
    if what == "qqq":
        pickle.dump(run_qqq(), open(CACHE / "sessions_qqq.pkl", "wb"))
    elif what == "crypto":
        run_crypto()
    elif what == "stocks":
        run_stocks()
    results = {}
    for part in ("qqq", "crypto", "stocks"):
        f = CACHE / f"sessions_{part}.pkl"
        if f.exists():
            results.update(pickle.loads(f.read_bytes()))
    text = report(results)
    (HERE / "sessions.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
