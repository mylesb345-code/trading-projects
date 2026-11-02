#!/usr/bin/env python3
"""Moving-average crossover bot for an Alpaca PAPER account.

  python bot.py          check for a crossover and place the order if there is one
  python bot.py --dry    show what it would do, place nothing

Reads settings from config.json and Alpaca paper keys from .env. Fetches the last 100 daily closes with yfinance,
computes the 20- and 50-day simple moving averages, and acts only on a real crossover between the last two
completed days:
  BUY   the 20-day was at or below the 50-day yesterday and is above it today
  SELL  the 20-day was at or above the 50-day yesterday and is below it today (only shares you own are sold)
Every decision is appended to trades.csv. Each crossover is acted on once, even if you run the bot again.
"""
import argparse
import csv
import json
import os
import sys
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# macOS's built-in Python uses LibreSSL, which makes urllib3 print a harmless warning on every run
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "trades.csv")
NEW_YORK = ZoneInfo("America/New_York")


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        cfg = json.load(f)
    if cfg["fast_ma"] >= cfg["slow_ma"]:
        sys.exit("config.json: fast_ma must be smaller than slow_ma.")
    if cfg["history_days"] < cfg["slow_ma"] + 1:
        sys.exit("config.json: history_days must be more than slow_ma.")
    return cfg


def fetch_closes(symbol, days, now=None):
    """The last `days` completed daily closes as a pandas Series indexed by date."""
    import yfinance as yf
    now = now or datetime.now(NEW_YORK)
    data = yf.Ticker(symbol).history(start=(now - timedelta(days=int(days * 1.6) + 10)).date(), interval="1d",
                                     auto_adjust=True)
    if data.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    closes = data["Close"]
    closes.index = [d.date() for d in closes.index]
    if closes.index[-1] == now.date() and (now.hour, now.minute) < (16, 15):
        closes = closes.iloc[:-1]  # today's bar is still forming: only trade on completed days
    return closes.iloc[-days:]


def find_signal(closes, fast, slow):
    """BUY, SELL or None, from the last two completed days, plus the numbers behind it."""
    fast_ma, slow_ma = closes.rolling(fast).mean(), closes.rolling(slow).mean()
    if len(closes) < slow + 1 or slow_ma.iloc[-2] != slow_ma.iloc[-2]:  # need two days with both averages
        raise RuntimeError(f"need at least {slow + 1} days of data, got {len(closes)}")
    f0, s0, f1, s1 = fast_ma.iloc[-2], slow_ma.iloc[-2], fast_ma.iloc[-1], slow_ma.iloc[-1]
    signal = "BUY" if f0 <= s0 and f1 > s1 else "SELL" if f0 >= s0 and f1 < s1 else None
    return signal, {"date": str(closes.index[-1]), "close": float(closes.iloc[-1]), "fast_prev": float(f0),
                    "slow_prev": float(s0), "fast": float(f1), "slow": float(s1)}


class AlpacaPaper:
    """Alpaca's paper-trading API only: paper=True is fixed, and live keys are refused."""

    def __init__(self, key, secret):
        if not key or not secret:
            sys.exit("Missing keys: copy .env.example to .env and paste your Alpaca PAPER keys in.")
        if not key.startswith("PK"):
            sys.exit("REFUSING: that key ID doesn't start with PK, so it isn't an Alpaca paper key. Use paper keys.")
        from alpaca.trading.client import TradingClient
        self.client = TradingClient(key, secret, paper=True)

    def shares_held(self, symbol):
        try:
            return float(self.client.get_open_position(symbol).qty)
        except Exception:
            return 0.0  # no position

    def market_order(self, symbol, side, qty, client_order_id):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        order = self.client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=client_order_id))
        return str(order.id), str(order.status)


def decide(signal, info, cfg, state, broker):
    """What to do about this signal: returns (action, qty, reason)."""
    if not signal:
        return "HOLD", 0, "no crossover between the last two days"
    if state.get("last_signal") == [info["date"], signal]:
        return "SKIP", 0, f"already acted on the {signal} crossover of {info['date']}"
    if signal == "BUY":
        return "BUY", cfg["trade_qty"], "20-day average crossed above the 50-day"
    held = broker.shares_held(cfg["symbol"])
    if held <= 0:
        return "SKIP", 0, "20-day crossed below the 50-day, but there are no shares to sell"
    return "SELL", min(cfg["trade_qty"], held), "20-day average crossed below the 50-day"


def log(row):
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Moving-average crossover bot (Alpaca paper trading).")
    ap.add_argument("--dry", action="store_true", help="show the decision, place no order")
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
    cfg = load_config()
    broker = AlpacaPaper(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
    closes = fetch_closes(cfg["symbol"], cfg["history_days"])
    signal, info = find_signal(closes, cfg["fast_ma"], cfg["slow_ma"])
    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
    action, qty, reason = decide(signal, info, cfg, state, broker)
    print(f"{cfg['symbol']} {info['date']}: close ${info['close']:.2f} | {cfg['fast_ma']}-day ${info['fast']:.2f} "
          f"(was ${info['fast_prev']:.2f}) | {cfg['slow_ma']}-day ${info['slow']:.2f} (was ${info['slow_prev']:.2f})")
    print(f"Decision: {action}{f' {qty:g} share(s)' if qty else ''} ({reason})")
    order_id, status = "", ""
    if action in ("BUY", "SELL") and not a.dry:
        order_id, status = broker.market_order(cfg["symbol"], action, qty, f"ma-{cfg['symbol']}-{info['date']}-{action}")
        state["last_signal"] = [info["date"], signal]
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
        print(f"PAPER order placed: {action} {qty:g} {cfg['symbol']} at market | id {order_id} | status {status}")
    log({"run_at": datetime.now(NEW_YORK).strftime("%Y-%m-%d %H:%M %Z"), "symbol": cfg["symbol"], **info,
         "action": action + (" (dry run)" if a.dry and action in ("BUY", "SELL") else ""), "qty": qty,
         "reason": reason, "order_id": order_id, "status": status})
    return 0


if __name__ == "__main__":
    sys.exit(main())
