"""Same real-account simulation as crypto_lab.py, on the 50 backtest stocks (daily), for the live stock strategy."""
import math, sys
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto_lab as lab
from backtest import STOCKS
from data import BarCache, MarketData
from settings import load_config, load_credentials, path, setup_logging

cfg = load_config(); creds = load_credentials(); setup_logging(cfg, creds)
md = MarketData(creds, cfg, BarCache(path(cfg, "cache_db")))
years = cfg["backtest"]["years"]
data = md.closed_bars(STOCKS, "stocks", "1Day", bars=int(years * 252) + md.bars_needed("stocks"))
setups = [lab.Setup("range_reversion, 10 max", "range_reversion", STOCKS, 10, False),
          lab.Setup("darvas_breakout, 10 max", "darvas_breakout", STOCKS, 10, False)]
pre = {(sym, st): lab.precompute(df, sym, st, cfg, "stocks", "1Day") for st in ("range_reversion", "darvas_breakout") for sym, df in data.items()}
times = sorted({t for df in data.values() for t in df.index})
n = int(years * 252)
start_t, split_t = times[len(times) - n], times[len(times) - int(n * 0.4)]
spy = None
print(f"STOCK LAB: daily, {years:g} years from {str(start_t)[:10]}, {len(data)} stocks, $100k, 1% risk, 10% max position, 10 max open, 0.1% slippage/side")
print(f"{'setup':<26} {'trades':>6} {'win%':>5} {'PF':>5} {'return':>8} {'max DD':>7} {'PF 1st 60%':>10} {'PF last 40%':>11}")
for setup in setups:
    r = lab.simulate(setup, data, pre, {}, times, cfg["backtest"]["slippage"], start_t)
    full = lab.summarize(r.trades, r.curve, lab.START_EQUITY)
    a = lab.summarize([x for x in r.trades if x["time"] < str(split_t)], [1e5], 1e5)["pf"]
    b = lab.summarize([x for x in r.trades if x["time"] >= str(split_t)], [1e5], 1e5)["pf"]
    f = lambda v: "inf" if v == math.inf else f"{v:.2f}"
    print(f"{setup.name:<26} {full['trades']:>6} {full['win_rate']:>5.0%} {f(full['pf']):>5} {full['return']:>+8.1%} {full['max_dd']:>+7.1%} {f(a):>10} {f(b):>11}")
first = {s: df["close"][df.index >= start_t] for s, df in data.items()}
hold = sum(v.iloc[-1] / v.iloc[0] - 1 for v in first.values() if len(v)) / len(first)
print(f"buy and hold the same {len(first)} stocks, equal weight: {hold:+.1%}")
