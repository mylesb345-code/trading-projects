#!/usr/bin/env python3
"""Opening-range breakout day trader (Alpaca PAPER only), as published by Zarattini & Aziz (2023) and tested in
strategy-lab/daytrade.py: 4x-leveraged QQQ, +18.7%/yr 2016-now vs SPY +13.6%, but +12.3% vs +18.5% since publication.

  python3 bot.py morning [--dry-run]   # ~9:35 AM New York: read the first 5-minute candle and enter
  python3 bot.py close                 # ~3:55 PM New York: close whatever is left and text the day's result

Rules (as published):
  - The 9:30-9:35 candle closed up: buy QQQ. Closed down: short it. Unchanged: no trade.
  - Stop at that candle's low (long) or high (short); target 10x the risk. Both sit on Alpaca as a bracket order, so
    they work even if this script never runs again that day.
  - Size: 1% of equity at risk, at most 4x equity (and within the day-trading buying power).
  - Out by the close. Each morning also closes any QQQ left over if the afternoon run was missed.
Calm-day filter (added 2026-11-04 from strategy-lab/minute.py, rules fixed before that test): no trade when the
previous VIX close was 20 or higher, on Fed decision days, or on jobs-report days (first Friday). On 2016-2026 QQQ at
4x this went from +20.8%/yr, worst drop -27.1%, Sharpe 0.81 to +18.5%/yr, -23.0%, 0.93 (SPY: +13.0%, -34.2%, 0.78).
If the VIX can't be read, the day is traded (the unfiltered rule also beat SPY).
GitHub's timer can start late, so a morning run enters until 10:30 if the stop hasn't been hit (logged as late);
after that the day is skipped. Real-money note: daily day trading needs $25,000+ in a margin account (US
pattern-day-trader rule).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time as clock
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "box-scanner"))

SYMBOL, RISK, MAX_LEV, TARGET_R = "QQQ", 0.01, 4.0, 10
READY_TRADES, READY_MAX_DD = 60, 0.15  # real-money checks, fixed 2026-11-02 before the first trade
ENTRY_AT, LAST_ENTRY, CLOSE_AT = time(9, 35, 10), time(10, 30), time(15, 55)
NY = ZoneInfo("America/New_York")
CALM_VIX = 20.0
FOMC = set("""2026-12-09 2027-01-27 2027-03-17 2027-04-28 2027-06-09 2027-07-28 2027-09-15 2027-10-27
2027-12-08""".split())  # Fed decision days, federalreserve.gov/monetarypolicy/fomccalendars.htm; extend each year


def now() -> datetime:
    return datetime.now(NY)


def notify(title: str, body: str) -> None:
    print(f"[phone] {title}: {body}")
    try:
        topic = os.environ.get("NTFY_TOPIC") or json.loads((ROOT / "trading-bot" / "config.json").read_text())["ntfy_topic"]
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(),
                                                      headers={"Title": title}), timeout=15)
    except Exception as e:
        print(f"alert failed: {type(e).__name__}")


def wait_until(t: time) -> None:
    target = now().replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if target > now():
        print(f"waiting until {t} New York")
        clock.sleep((target - now()).total_seconds())


def clients():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from settings import load_credentials
    creds = load_credentials(ROOT / "box-scanner" / ".env")  # refuses live keys
    trading = TradingClient(creds.api_key, creds.secret_key, paper=True)
    if not str(trading.get_account().account_number).startswith("PA"):
        raise SystemExit("not an Alpaca PAPER account (those start with PA); stopping")
    return trading, StockHistoricalDataClient(creds.api_key, creds.secret_key)


def calm_day(day) -> str:
    """Why today is skipped by the calm-day filter, or "" to trade."""
    if str(day) in FOMC:
        return "Fed decision day"
    if day.weekday() == 4 and day.day <= 7:
        return "jobs-report day"
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1mo")
        r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                              timeout=20).read())["chart"]["result"][0]
        closes = [(datetime.fromtimestamp(t, NY).date(), c) for t, c in
                  zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]
        prev = [c for d, c in closes if d < day][-1]
    except Exception as e:
        print(f"VIX unavailable ({type(e).__name__}); trading without the calm filter")
        return ""
    print(f"previous VIX close {prev:.2f}")
    return f"VIX {prev:.1f} is {CALM_VIX:.0f} or higher (jumpy market)" if prev >= CALM_VIX else ""


def first_candle(data, day) -> tuple:
    """(open, high, low, close) of 9:30-9:35 from real-time IEX minute bars (free; full-market data is 15 min late)."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime.combine(day, time(9, 30), NY)
    df = data.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Minute, start=start,
                                              end=start + timedelta(minutes=5), feed=DataFeed.IEX)).df
    if df is None or len(df) < 3:
        raise RuntimeError(f"only {0 if df is None else len(df)} minute bars for the opening candle")
    return df["open"].iloc[0], df["high"].max(), df["low"].min(), df["close"].iloc[-1]


def last_price(data) -> float:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest
    return float(data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=SYMBOL,
                                                                     feed=DataFeed.IEX))[SYMBOL].price)


def flatten(trading, why: str) -> bool:
    """Cancel QQQ's open orders (the bracket legs) and close the QQQ position. True if something was closed."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True)):
        if o.symbol == SYMBOL:
            trading.cancel_order_by_id(o.id)
    if not any(p.symbol == SYMBOL for p in trading.get_all_positions()):
        return False
    clock.sleep(2)  # let the cancels land before the close
    order = trading.close_position(SYMBOL)
    for _ in range(30):
        if str(trading.get_order_by_id(order.id).status).endswith("FILLED"):
            break
        clock.sleep(2)
    print(f"closed {SYMBOL} ({why})")
    return True


def morning(dry: bool) -> int:
    trading, data = clients()
    t = now()
    if not dry and not trading.get_clock().is_open and t.time() > time(9, 30):
        print("market closed today")
        return 0
    if not dry and not (time(9, 0) <= t.time() < LAST_ENTRY):
        print(f"{t:%H:%M} New York is outside the morning window; nothing to do")
        return 0
    day = t.date()
    cid = f"orb-{day:%Y%m%d}"
    try:
        trading.get_order_by_client_id(cid)
        print(f"today's order {cid} already exists; nothing to do")
        return 0
    except Exception:
        pass
    if not dry and flatten(trading, "left over from yesterday"):
        notify("PAPER ORB: closed a leftover QQQ position", "Yesterday's afternoon run didn't happen, so the bot "
               "closed the position at this morning's open.")
    skip = calm_day(day)
    if skip:
        print(f"no trade today: {skip}")
        if not dry:
            notify("PAPER ORB: sitting out today", f"No trade today: {skip}. The bot only trades calm days.")
        return 0
    if not dry:
        wait_until(ENTRY_AT)
    o, h, l, c = first_candle(data, day)
    direction = 1 if c > o else -1 if c < o else 0
    late = now().time() > time(9, 45)
    print(f"opening candle {o:.2f} -> {c:.2f} (high {h:.2f}, low {l:.2f}): "
          f"{'LONG' if direction > 0 else 'SHORT' if direction < 0 else 'no trade'}{' (late run)' if late else ''}")
    if direction == 0:
        return 0
    px = last_price(data)
    stop = l if direction > 0 else h
    risk = (px - stop) * direction
    if risk <= 0.01:
        print(f"price {px:.2f} is already past the stop {stop:.2f}; no trade today")
        return 0
    acct = trading.get_account()
    equity, dtbp = float(acct.equity), float(acct.daytrading_buying_power or acct.buying_power)
    # Alpaca checks the lower of the two buying powers, and prices a short market order about 3% above the last
    # trade; 2026-11-04's short was rejected for "insufficient buying power" when sized on dtbp alone
    bp = min(dtbp, float(acct.buying_power))
    qty = math.floor(min(equity * RISK / risk, MAX_LEV * equity / px, 0.9 * bp / (px * 1.03)))
    target = px + direction * TARGET_R * risk
    print(f"{'buy' if direction > 0 else 'short'} {qty} {SYMBOL} near {px:.2f}, stop {stop:.2f}, target "
          f"{target:.2f} (equity ${equity:,.0f}, day-trading buying power ${dtbp:,.0f})")
    if dry or qty < 1:
        return 0
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
    order = trading.submit_order(MarketOrderRequest(
        symbol=SYMBOL, qty=qty, side=OrderSide.BUY if direction > 0 else OrderSide.SELL,
        time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET, client_order_id=cid,
        take_profit=TakeProfitRequest(limit_price=round(target, 2)), stop_loss=StopLossRequest(stop_price=round(stop, 2))))
    notify(f"PAPER ORB: {'LONG' if direction > 0 else 'SHORT'} {qty} QQQ",
           f"First 5 minutes {'up' if direction > 0 else 'down'} ({o:.2f} -> {c:.2f}). Entry near {px:.2f}, stop "
           f"{stop:.2f}, target {target:.2f}; out by the close.{' Entered late (GitHub started the run late).' if late else ''}"
           f" Order {order.status}.")
    return 0


def daily_pnl(trading, since: datetime) -> dict:
    """Realized QQQ P&L per day from the account's regular-hours fills (the bot is flat every night)."""
    out: dict = {}
    page = None
    while True:
        params = {"after": since.isoformat(), "direction": "asc", "page_size": 100}
        if page:
            params["page_token"] = page
        fills = trading.get("/account/activities/FILL", params)
        if not fills:
            break
        for f in fills:
            t = datetime.fromisoformat(f["transaction_time"].replace("Z", "+00:00")).astimezone(NY)
            if f["symbol"] != SYMBOL or not time(9, 30) <= t.time() < time(16):  # sessions.py trades outside these
                continue
            day = t.date()
            sign = 1 if f["side"].startswith("sell") else -1
            out[day] = out.get(day, 0.0) + sign * float(f["qty"]) * float(f["price"])
        if len(fills) < 100:
            break
        page = fills[-1]["id"]
    return out


def readiness(data, pnl: dict, equity: float) -> str:
    """60+ trades, profitable, ahead of holding QQQ over the same days, worst drop under 15%."""
    if not pnl:
        return "Real-money check: no trades yet."
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    days = sorted(pnl)
    total = sum(pnl.values())
    start_equity = equity - total
    curve, peak, dd = start_equity, start_equity, 0.0
    for d in days:
        curve += pnl[d]
        peak = max(peak, curve)
        dd = min(dd, curve / peak - 1)
    first = data.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Day,
                                                 start=datetime.combine(days[0], time(0), NY),
                                                 end=datetime.combine(days[0], time(23), NY), feed=DataFeed.IEX)).df
    qqq = last_price(data) / float(first["open"].iloc[0]) - 1 if first is not None and len(first) else 0.0
    mine = total / start_equity
    checks = [(len(days) >= READY_TRADES, f"trades {len(days)}/{READY_TRADES}"),
              (total > 0 and mine > qqq, f"return {mine:+.1%} vs holding QQQ {qqq:+.1%}"),
              (dd > -READY_MAX_DD, f"worst drop {dd:.1%} (limit -{READY_MAX_DD:.0%})")]
    ok = all(c for c, _ in checks)
    return ("Real-money check: " + ("READY on paper results; it also needs $25,000+ in a margin account. "
                                     if ok else "not yet. ") + " | ".join(("OK " if c else "NO ") + t for c, t in checks))


def close() -> int:
    trading, data = clients()
    t = now()
    if not trading.get_clock().is_open:
        print("market closed; nothing to do")
        return 0
    if not (time(15, 15) <= t.time() < time(15, 59)):
        print(f"{t:%H:%M} New York is outside the afternoon window; nothing to do")
        return 0
    wait_until(CLOSE_AT)
    flatten(trading, "end of day")
    pnl = daily_pnl(trading, datetime(2026, 11, 1, tzinfo=NY))
    today = pnl.get(t.date())
    days = sorted(pnl)
    total = sum(pnl.values())
    wins = sum(v > 0 for v in pnl.values())
    equity = float(trading.get_account().equity)
    if today is None:
        head, body = "PAPER ORB: no trade today", "No QQQ trade today (calm-day filter, flat opening candle, missed start, or stop already hit)."
    else:
        head = f"PAPER ORB today: {'+' if today >= 0 else '-'}${abs(today):,.0f}"
        body = f"Today's QQQ day trade made {'+' if today >= 0 else '-'}${abs(today):,.0f}."
    body += (f" Since {days[0] if days else t.date()}: {len(days)} trading days, {wins} won, total "
             f"{'+' if total >= 0 else '-'}${abs(total):,.0f}. Account ${equity:,.0f}. ")
    try:
        body += readiness(data, pnl, equity)
    except Exception as e:  # the day's result still goes out
        print(f"readiness check failed: {type(e).__name__}: {e}")
    notify(head, body)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "morning":
        sys.exit(morning("--dry-run" in sys.argv))
    if cmd == "close":
        sys.exit(close())
    sys.exit(__doc__)
