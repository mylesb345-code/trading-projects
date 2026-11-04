#!/usr/bin/env python3
"""Extra-session paper test (Alpaca PAPER only): the ORB bot's rules at the other three sessions the user asked for,
at a quarter of the 9:30 bot's size, with their own scoreboard. The 9:30 bot (bot.py) is unchanged.

  python3 sessions.py early [--dry-run]   # start ~3:50 AM New York: pre-open 4:00-7:00, then London 7:00-9:15
  python3 sessions.py late [--dry-run]    # start ~3:55 PM New York: after-hours 16:00-20:00

Backtest warning (strategy-lab/sessions.py, QQQ 2016-2026, 4x): these sessions lost money every way they were
tested (pre-open -76.9%/yr, London -87.4%/yr, late -85.8%/yr; only 9:30 made money). The user asked for all four
sessions anyway (2026-11-04), so this runs them small on paper to see whether live trading agrees.

Rules, per session (same as the lab, except where noted):
  - The session's first 5-minute candle: up = buy QQQ, down = short, flat = no trade.
  - Stop at that candle's low (long) or high (short); target 10x the risk; otherwise out at the session's end.
  - Size: 0.25% of equity at risk, at most 1x equity (the 9:30 bot risks 1% at up to 4x).
  - London exits at 9:15 instead of 9:30, so its QQQ is gone before the 9:30 bot starts (Alpaca nets QQQ positions).
  - Same calm-day filter as the 9:30 bot (no trade after a VIX close of 20+, on Fed days or jobs-report days).
Outside 9:30-16:00 Alpaca only takes limit orders and holds no stop orders, so this script watches the price every
20 seconds (Yahoo's 1-minute pre/post-market candles; Alpaca's free feed has almost no pre-market prints) and exits
with a limit order priced through the market.
"""
from __future__ import annotations

import json
import math
import sys
import time as clock
import urllib.request
from datetime import date, datetime, time, timedelta

from bot import NY, SYMBOL, calm_day, clients, notify, now, wait_until

RISK, MAX_LEV, TARGET_R = 0.0025, 1.0, 10
SLIP = 0.003  # limit orders priced 0.3% through the last price so they fill like market orders
POLL = 20
SESSIONS = {"early": [("Pre-open", time(4), time(6, 59)), ("London", time(7), time(9, 15))],
            "late": [("After-hours", time(16), time(19, 58))]}
LATE_START = timedelta(minutes=30)  # a session is skipped if this run starts more than 30 min after its open
SCORE_SINCE = datetime(2026, 11, 5, tzinfo=NY)


def candles(day: date) -> list:
    """Today's QQQ 1-minute candles including pre/post-market: [(time, open, high, low, close)]."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?interval=1m&range=1d&includePrePost=true"
    r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                          timeout=20).read())["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        ts = datetime.fromtimestamp(t, NY)
        if None not in (o, h, l, c) and ts.date() == day:
            out.append((ts, o, h, l, c))
    return out


def fresh_candles(day: date) -> list:
    for attempt in range(6):
        try:
            return candles(day)
        except Exception as e:
            print(f"price feed error ({type(e).__name__}); retrying")
            clock.sleep(10)
    raise RuntimeError("price feed unavailable")


def limit_order(trading, qty: int, buy: bool, px: float, cid: str):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest
    limit = round(px * (1 + SLIP) if buy else px * (1 - SLIP), 2)
    order = trading.submit_order(LimitOrderRequest(symbol=SYMBOL, qty=qty, side=OrderSide.BUY if buy else OrderSide.SELL,
                                                   limit_price=limit, time_in_force=TimeInForce.DAY,
                                                   extended_hours=True, client_order_id=cid))
    for _ in range(12):
        clock.sleep(5)
        order = trading.get_order_by_id(order.id)
        if str(order.status).endswith("FILLED") and float(order.filled_qty) == qty:
            return order
    trading.cancel_order_by_id(order.id)
    clock.sleep(2)
    return trading.get_order_by_id(order.id)


def position_qty(trading) -> int:
    for p in trading.get_all_positions():
        if p.symbol == SYMBOL:
            return int(float(p.qty))
    return 0


def flatten(trading, why: str, px: float, cid: str) -> None:
    """Close any QQQ position with a limit order through the market (works outside regular hours)."""
    for o in trading.get_orders():
        if o.symbol == SYMBOL:
            trading.cancel_order_by_id(o.id)
    for attempt in range(3):
        qty = position_qty(trading)
        if qty == 0:
            return
        limit_order(trading, abs(qty), qty < 0, px, f"{cid}-{attempt}")
        print(f"closing {qty} {SYMBOL} ({why})")
    if position_qty(trading):
        notify("PAPER SESSIONS: could not close QQQ", f"The {why} exit didn't fill; the 9:30 bot will close it.")


def run_session(trading, name: str, start: time, end: time, day: date, dry: bool) -> None:
    tag = f"sess-{day:%Y%m%d}-{name.lower().replace('-', '')}"
    try:
        trading.get_order_by_client_id(f"{tag}-in")
        print(f"{name}: already traded today")
        return
    except Exception:
        pass
    wait_until((datetime.combine(day, start) + timedelta(minutes=5, seconds=20)).time())
    bars = [b for b in fresh_candles(day) if start <= b[0].time() < (datetime.combine(day, start) + timedelta(minutes=5)).time()]
    if len(bars) < 3:
        print(f"{name}: only {len(bars)} candles in the first 5 minutes; no trade")
        return
    o, h, l, c = bars[0][1], max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4]
    direction = 1 if c > o else -1 if c < o else 0
    print(f"{name}: first candle {o:.2f} -> {c:.2f} (high {h:.2f}, low {l:.2f}): "
          f"{'LONG' if direction > 0 else 'SHORT' if direction < 0 else 'no trade'}")
    if direction == 0:
        return
    px = fresh_candles(day)[-1][4]
    stop = l if direction > 0 else h
    risk = (px - stop) * direction
    if risk <= 0.01:
        print(f"{name}: price {px:.2f} already past the stop {stop:.2f}; no trade")
        return
    acct = trading.get_account()
    equity = float(acct.equity)
    bp = min(float(acct.daytrading_buying_power or acct.buying_power), float(acct.buying_power))
    qty = math.floor(min(equity * RISK / risk, MAX_LEV * equity / px, 0.9 * bp / (px * 1.03)))
    target = px + direction * TARGET_R * risk
    print(f"{name}: {'buy' if direction > 0 else 'short'} {qty} {SYMBOL} near {px:.2f}, stop {stop:.2f}, target {target:.2f}")
    if dry or qty < 1:
        return
    order = limit_order(trading, qty, direction > 0, px, f"{tag}-in")
    filled = int(float(order.filled_qty or 0))
    if filled == 0:
        print(f"{name}: entry didn't fill; no trade")
        return
    entry = float(order.filled_avg_price)
    notify(f"PAPER SESSIONS {name}: {'LONG' if direction > 0 else 'SHORT'} {filled} QQQ",
           f"Test trade (small size). Entry {entry:.2f}, stop {stop:.2f}, target {target:.2f}; out by {end:%-I:%M %p}.")
    entered = datetime.now(NY).replace(second=0, microsecond=0)
    why, last = "session end", entry
    while now().time() < end:
        clock.sleep(POLL)
        try:
            since = [b for b in fresh_candles(day) if b[0] >= entered]
        except RuntimeError:
            continue
        if not since:
            continue
        last = since[-1][4]
        if (min(b[3] for b in since) <= stop) if direction > 0 else (max(b[2] for b in since) >= stop):
            why = "stop"
            break
        if (max(b[2] for b in since) >= target) if direction > 0 else (min(b[3] for b in since) <= target):
            why = "target"
            break
    flatten(trading, f"{name} {why}", last, f"{tag}-out")
    notify(f"PAPER SESSIONS {name}: out ({why})", scoreboard(trading))


def scoreboard(trading) -> str:
    """P&L of the extra sessions only: QQQ fills outside 9:30-16:00 since the test started."""
    pnl, trades, page = 0.0, set(), None
    while True:
        params = {"after": SCORE_SINCE.isoformat(), "direction": "asc", "page_size": 100}
        if page:
            params["page_token"] = page
        fills = trading.get("/account/activities/FILL", params) or []
        for f in fills:
            t = datetime.fromisoformat(f["transaction_time"].replace("Z", "+00:00")).astimezone(NY)
            if f["symbol"] != SYMBOL or time(9, 30) <= t.time() < time(16):
                continue
            pnl += (1 if f["side"].startswith("sell") else -1) * float(f["qty"]) * float(f["price"])
            trades.add(f["order_id"])
        if len(fills) < 100:
            break
        page = fills[-1]["id"]
    if position_qty(trading):
        return "Extra-session scoreboard updates once the position is closed."
    return (f"Extra sessions since {SCORE_SINCE:%b %-d}: {'+' if pnl >= 0 else '-'}${abs(pnl):,.0f} over "
            f"{len(trades) // 2} trades (separate from the 9:30 bot's score).")


def main(which: str, dry: bool) -> int:
    trading, _ = clients()
    t = now()
    day = t.date()
    from alpaca.trading.requests import GetCalendarRequest
    if not dry and not trading.get_calendar(GetCalendarRequest(start=day, end=day)):
        print("not a trading day")
        return 0
    skip = calm_day(day)
    if skip:
        print(f"no extra-session trades today: {skip}")
        return 0
    first = datetime.combine(day, SESSIONS[which][0][1], NY)
    if not dry and now() < first - timedelta(minutes=75):
        print(f"{t:%H:%M} New York is too early for the {which} sessions; exiting")  # the job would time out waiting
        return 0
    for name, start, end in SESSIONS[which]:
        if not dry and now() > datetime.combine(day, start, NY) + LATE_START:
            print(f"{name}: started too late; skipped")
            continue
        run_session(trading, name, start, end, day, dry)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in SESSIONS:
        sys.exit(__doc__)
    sys.exit(main(cmd, "--dry-run" in sys.argv))
