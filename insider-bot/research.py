#!/usr/bin/env python3
"""Insider cluster-buy research: copy company insiders who buy their own stock, using the SEC's bulk Form 4 data.
Read-only, no orders.

  python research.py

Rules fixed BEFORE any result was seen (2026-11-02), from the best-documented finding in insider-trading research:
  Signal  2+ different officers/directors (not pure 10% holders) make open-market purchases (Form 4 code P) within
          30 days, together $100,000+. The signal date is the filing date that completes the cluster. One signal
          per stock per 90 days.
  Trade   buy at the next trading day's open after the filing date (the filing is public by then), hold 63 trading
          days (about 3 months), sell at that day's close. 10% of equity per position, max 10 open, 0.1% slippage
          per side. Only stocks at $5+ with $10M+ average daily dollar volume (the ones the bot can trade).
  Pass    the bot's 5 real-money checks: 30+ trades, profit factor 1.3+ overall and in both the first 60% and the
          last 40%, worst drawdown under 10%, and beating the S&P 500 over the same period.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
BOX = HERE.parent / "box-scanner"
sys.path.insert(0, str(BOX))

QUARTERS = ["2023q4", "2024q1", "2024q2", "2024q3", "2024q4", "2025q1", "2025q2", "2025q3", "2025q4", "2026q1",
            "2026q2", "2026q3"]
CLUSTER_DAYS, MIN_INSIDERS, MIN_VALUE, COOLDOWN_DAYS = 30, 2, 100_000, 90
HOLD_DAYS, POSITION_PCT, MAX_OPEN, SLIPPAGE = 63, 0.10, 10, 0.001
MIN_PRICE, MIN_DOLLAR_VOLUME = 5.0, 10_000_000
START, SPLIT_SHARE = "2024-04-29", 0.6


def sec_date(s: str) -> date:
    return datetime.strptime(s, "%d-%b-%Y").date()


def read_tsv(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t")


def load_purchases(cache: Path) -> List[Dict]:
    """Every open-market purchase by an officer or director, with its filing date."""
    out = []
    for q in QUARTERS:
        with zipfile.ZipFile(cache / f"{q}_form345.zip" if (cache / f"{q}_form345.zip").exists() else cache / f"{q}.zip") as z:
            subs = {r["ACCESSION_NUMBER"]: r for r in read_tsv(z, "SUBMISSION.tsv")
                    if r["DOCUMENT_TYPE"] in ("4", "4/A") and r["ISSUERTRADINGSYMBOL"]}
            owners = defaultdict(list)
            for r in read_tsv(z, "REPORTINGOWNER.tsv"):
                owners[r["ACCESSION_NUMBER"]].append(r)
            for r in read_tsv(z, "NONDERIV_TRANS.tsv"):
                acc = r["ACCESSION_NUMBER"]
                if acc not in subs or r["TRANS_CODE"] != "P" or r["TRANS_ACQUIRED_DISP_CD"] != "A":
                    continue
                rel = " ".join(o["RPTOWNER_RELATIONSHIP"] for o in owners.get(acc, []))
                if "Officer" not in rel and "Director" not in rel:
                    continue  # pure 10% holders and "other" (often funds) don't count
                try:
                    shares, price = float(r["TRANS_SHARES"]), float(r["TRANS_PRICEPERSHARE"])
                except ValueError:
                    continue
                if shares <= 0 or price <= 0:
                    continue
                s = subs[acc]
                out.append({"symbol": s["ISSUERTRADINGSYMBOL"].strip().upper(), "filed": sec_date(s["FILING_DATE"]),
                            "traded": sec_date(r["TRANS_DATE"]), "owner": owners[acc][0]["RPTOWNERCIK"] if owners[acc] else acc,
                            "value": shares * price})
    return out


def cluster_signals(purchases: List[Dict]) -> List[Dict]:
    """(symbol, signal date) whenever 2+ insiders' purchases within 30 days reach $100k, at most once per 90 days."""
    by_sym = defaultdict(list)
    for p in purchases:
        by_sym[p["symbol"]].append(p)
    signals = []
    for sym, ps in by_sym.items():
        last = None
        for d in sorted({p["filed"] for p in ps}):
            window = [p for p in ps if p["filed"] <= d and p["traded"] >= d - timedelta(days=CLUSTER_DAYS)]
            insiders = {p["owner"] for p in window}
            value = sum(p["value"] for p in window)
            if len(insiders) >= MIN_INSIDERS and value >= MIN_VALUE and (last is None or (d - last).days >= COOLDOWN_DAYS):
                signals.append({"symbol": sym, "date": d, "insiders": len(insiders), "value": value})
                last = d
    return sorted(signals, key=lambda s: s["date"])


def main() -> int:
    from data import BarCache, MarketData
    from settings import load_config, load_credentials, setup_logging
    cfg = load_config(BOX / "config.yaml")
    creds = load_credentials(BOX / ".env")
    log = setup_logging(dict(cfg, logging={"file": "../insider-bot/cache/research.log", "level": "INFO"}), creds)
    cache = HERE / "cache"
    purchases = load_purchases(cache)
    signals = [s for s in cluster_signals(purchases) if str(s["date"]) >= START]
    log.info("insider purchases: %d; cluster signals since %s: %d", len(purchases), START, len(signals))
    md = MarketData(creds, dict(cfg, data=dict(cfg["data"], cache_db="../insider-bot/cache/bars.sqlite")),
                    BarCache(cache / "bars.sqlite"))
    import re
    valid = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")  # SEC data has some garbled tickers ("N O G"); Alpaca rejects them
    symbols = sorted({s["symbol"] for s in signals if valid.match(s["symbol"])} | {"SPY"})
    bars = md.closed_bars(symbols, "stocks", "1Day", bars=700)
    log.info("price history for %d of %d symbols", len(bars), len(symbols))

    def day(ts) -> str:
        return str(ts.tz_convert("America/New_York").date())

    days = {sym: [day(t) for t in df.index] for sym, df in bars.items()}
    spy_days = days["SPY"]
    cash, equity_curve, open_pos, trades = 100_000.0, [], {}, []
    queue = defaultdict(list)  # trading day -> signals to buy at that day's open
    for s in signals:
        nxt = next((d for d in spy_days if d > str(s["date"])), None)
        if nxt:
            queue[nxt].append(s)
    skipped = defaultdict(int)
    for d in spy_days:
        if d < START:
            continue
        # exits: sell positions that reached their holding period, at today's close
        for sym in list(open_pos):
            p = open_pos[sym]
            i = days[sym].index(d) if d in days[sym] else None
            if i is not None and i - p["i"] >= HOLD_DAYS:
                px = bars[sym]["close"].iloc[i] * (1 - SLIPPAGE)
                cash += p["units"] * px
                trades.append({"symbol": sym, "entry_day": p["day"], "exit_day": d, "pnl": p["units"] * (px - p["entry"]),
                               "ret": px / p["entry"] - 1})
                del open_pos[sym]
        # mark equity
        def mark() -> float:
            eq = cash
            for sym, p in open_pos.items():
                df = bars[sym]
                i = max(k for k, dd in enumerate(days[sym]) if dd <= d) if days[sym][0] <= d else p["i"]
                eq += p["units"] * df["close"].iloc[i]
            return eq
        # entries at today's open
        for s in sorted(queue.get(d, []), key=lambda s: -s["value"]):
            sym = s["symbol"]
            if sym not in bars or d not in days[sym]:
                skipped["no price data"] += 1
                continue
            i = days[sym].index(d)
            df = bars[sym]
            if i < 20 or df["close"].iloc[i - 1] < MIN_PRICE or \
                    (df["close"].iloc[i - 20:i] * df["volume"].iloc[i - 20:i]).mean() < MIN_DOLLAR_VOLUME:
                skipped["too small or illiquid"] += 1
                continue
            if sym in open_pos or len(open_pos) >= MAX_OPEN:
                skipped["already full (10 positions)" if len(open_pos) >= MAX_OPEN else "already held"] += 1
                continue
            entry = df["open"].iloc[i] * (1 + SLIPPAGE)
            size = min(mark() * POSITION_PCT, cash)
            if size <= 0:
                continue
            open_pos[sym] = {"units": size / entry, "entry": entry, "i": i, "day": d}
            cash -= size
        equity_curve.append((d, mark()))
    # close anything still open at the last close (marked, for the summary)
    final = equity_curve[-1][1]
    for sym, p in open_pos.items():
        px = bars[sym]["close"].iloc[-1]
        trades.append({"symbol": sym, "entry_day": p["day"], "exit_day": "open", "pnl": p["units"] * (px - p["entry"]),
                       "ret": px / p["entry"] - 1})

    def stats(ts):
        won = sum(t["pnl"] for t in ts if t["pnl"] > 0)
        lost = -sum(t["pnl"] for t in ts if t["pnl"] <= 0)
        return len(ts), (sum(t["pnl"] > 0 for t in ts) / len(ts) if ts else 0), (won / lost if lost else float("inf"))

    peak, dd = 0.0, 0.0
    for _, e in equity_curve:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    split = equity_curve[int(len(equity_curve) * SPLIT_SHARE)][0]
    n, win, pf = stats(trades)
    n1, _, pf1 = stats([t for t in trades if t["entry_day"] < split])
    n2, _, pf2 = stats([t for t in trades if t["entry_day"] >= split])
    spy = bars["SPY"]
    spy = spy[[day(t) >= START for t in spy.index]]["close"]
    spy_ret = spy.iloc[-1] / spy.iloc[0] - 1
    spy_dd = (spy / spy.cummax() - 1).min()
    ret = final / 100_000 - 1
    checks = [(n >= 30, f"trades {n}"), (pf >= 1.3 and pf1 >= 1.3 and pf2 >= 1.3,
                                           f"won per $1 lost {pf:.2f} (older {pf1:.2f}, newer {pf2:.2f})"),
              (dd > -0.10, f"worst drop {dd:.1%}"), (ret > spy_ret, f"return {ret:+.1%} vs S&P 500 {spy_ret:+.1%}")]
    lines = [f"INSIDER CLUSTER-BUY TEST: {START} to {equity_curve[-1][0]}, $100k, 10% per position, max 10, 3-month hold",
             f"signals {len(signals)} | skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items()),
             f"trades {n} | won {win:.0%} | won per $1 lost {pf:.2f} | return {ret:+.1%} | worst drop {dd:+.1%}",
             f"older 60%: {n1} trades, PF {pf1:.2f} | newer 40%: {n2} trades, PF {pf2:.2f}",
             f"S&P 500 same period: {spy_ret:+.1%} (its worst drop {spy_dd:+.1%})",
             "checks: " + " | ".join(("PASS " if ok else "FAIL ") + t for ok, t in checks)]
    (cache / "research_result.txt").write_text("\n".join(lines) + "\n")
    with open(cache / "research_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0]))
        w.writeheader()
        w.writerows(trades)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
