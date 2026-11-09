#!/usr/bin/env python3
"""TJR-style paper test on SPY (Alpaca PAPER only): liquidity sweep -> market structure shift -> fair value gap.

  python3 tjr_bot.py run [--dry-run]   # start ~9:20 AM New York; watches 9:30-12:30, places at most one bracket order
  python3 tjr_bot.py close             # at ~3:55 PM: cancel SPY orders and close any SPY position
  python3 tjr_bot.py replay 2026-11-06 # walk a recent day minute by minute (Yahoo keeps ~a week of 1-minute bars)

Backtest warning (strategy-lab/tjr.py, 2016-2026, rules fixed before testing): every version lost money, about
-0.04R per trade (SPY version A: -1.9%/yr at 4x). The user asked to paper-trade it anyway (2026-11-08), so it runs
small: 0.25% of equity at risk per trade, at most 1x equity.

Rules (version A of the backtest, unchanged):
  Sweep:  between 9:30 and 11:30 a 1-minute bar trades above yesterday's high (bearish) or below yesterday's low
          (bullish), having been on the other side of it the bar before.
  Shift:  within 30 bars, a bar CLOSES beyond the lowest low (bearish) / highest high (bullish) of the 15 bars before
          the sweep.
  Gap:    the newest fair value gap from the sweep bar to the shift bar (bearish: bar i high < bar i-2 low).
  Order:  limit at the gap's near edge with an attached stop 2 cents beyond the sweep's extreme and a 2R target
          (Alpaca bracket order, held at Alpaca). Cancelled if not filled within 30 bars of the shift, or if price
          reaches the target first. One trade a day. Anything still open is closed at 3:55.
Bars come from Yahoo's 1-minute chart (consolidated prices); orders go to Alpaca.
"""
from __future__ import annotations

import json
import math
import sys
import time as clock
import urllib.request
from datetime import date, datetime, time, timedelta

from bot import NY, clients, notify, now, wait_until

SYMBOL = "SPY"
RISK, MAX_LEV, TARGET_R = 0.0025, 1.0, 2.0
SWEEP_END, LOOKBACK, WINDOW = 11 * 60 + 30, 15, 30
RUN_END = time(12, 31)
SCORE_SINCE = datetime(2026, 11, 9, tzinfo=NY)


def yahoo_bars(day: date):
    """(prev_day_high, prev_day_low, today's completed regular-session 1-minute bars as lists o,h,l,c,mins)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?interval=1m&range=5d&includePrePost=false"
    for attempt in range(5):
        try:
            r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                                  timeout=20).read())["chart"]["result"][0]
            break
        except Exception as e:
            print(f"price feed error ({type(e).__name__}); retrying")
            clock.sleep(5)
    else:
        raise RuntimeError("Yahoo 1-minute bars unavailable")
    q = r["indicators"]["quote"][0]
    rows = [(datetime.fromtimestamp(t, NY), o, h, l, c) for t, o, h, l, c in
            zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
    rows = [x for x in rows if time(9, 30) <= x[0].time() < time(16)]
    prior_days = sorted({x[0].date() for x in rows if x[0].date() < day})
    if not prior_days:
        raise RuntimeError("no previous session in Yahoo's data")
    prev = [x for x in rows if x[0].date() == prior_days[-1]]
    cutoff = now() - timedelta(minutes=1) if day == now().date() else datetime.combine(day, time(16), NY)
    today = [x for x in rows if x[0].date() == day and x[0] <= cutoff]
    cols = list(zip(*today)) if today else [[], [], [], [], []]
    mins = [t.hour * 60 + t.minute for t in cols[0]]
    return max(x[2] for x in prev), min(x[3] for x in prev), list(cols[1]), list(cols[2]), list(cols[3]), list(cols[4]), mins


def find_setup(o, h, l, c, mins, pdh, pdl, start_s):
    """First sweep at or after bar start_s whose shift has happened and left a gap -> dict, else None."""
    n = len(c)
    for s in range(max(1, start_s), n):
        if mins[s] > SWEEP_END:
            return None
        for direction, level in ((-1, pdh), (1, pdl)):
            swept = (h[s] > level and h[s - 1] <= level) if direction < 0 else (l[s] < level and l[s - 1] >= level)
            if not swept:
                continue
            lo, hi = min(l[max(0, s - LOOKBACK):s]), max(h[max(0, s - LOOKBACK):s])
            m = next((k for k in range(s + 1, min(n, s + WINDOW + 1))
                      if (direction < 0 and c[k] < lo) or (direction > 0 and c[k] > hi)), None)
            if m is None:
                continue  # no shift (yet): a later bar may still produce one, so this sweep is re-checked next minute
            gap = None
            for i in range(m, max(s + 1, 2) - 1, -1):  # same bars as the backtest: shift bar back to the bar after the sweep
                if direction < 0 and h[i] < l[i - 2]:
                    gap = h[i]
                    break
                if direction > 0 and l[i] > h[i - 2]:
                    gap = l[i]
                    break
            if gap is None:
                continue
            extreme = max(h[s:m + 1]) if direction < 0 else min(l[s:m + 1])
            stop = extreme + 0.02 if direction < 0 else extreme - 0.02
            risk = (stop - gap) * -direction
            if risk <= 0.01:
                continue
            return {"s": s, "m": m, "direction": direction, "entry": round(gap, 2), "stop": round(stop, 2),
                    "target": round(gap + direction * TARGET_R * risk, 2), "deadline": m + WINDOW,
                    "level": "yesterday's high" if direction < 0 else "yesterday's low"}
    return None


def size(trading, entry, stop):
    acct = trading.get_account()
    equity = float(acct.equity)
    bp = min(float(acct.daytrading_buying_power or acct.buying_power), float(acct.buying_power))
    return max(0, math.floor(min(equity * RISK / abs(entry - stop), MAX_LEV * equity / entry, 0.9 * bp / (entry * 1.03))))


def place(trading, setup, qty, tag):
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
    side = OrderSide.BUY if setup["direction"] > 0 else OrderSide.SELL
    return trading.submit_order(LimitOrderRequest(
        symbol=SYMBOL, qty=qty, side=side, time_in_force=TimeInForce.DAY, limit_price=setup["entry"],
        order_class=OrderClass.BRACKET, take_profit=TakeProfitRequest(limit_price=setup["target"]),
        stop_loss=StopLossRequest(stop_price=setup["stop"]), client_order_id=tag))


def run(dry: bool, replay_day: date | None = None) -> int:
    """Watch for a setup and place at most one filled trade. dry/replay: no orders; fills judged from the bars."""
    live = replay_day is None and not dry
    trading = None if replay_day else clients()[0]
    day = replay_day or now().date()
    attempt = 0
    if replay_day is None:
        if not dry and not trading.get_clock().is_open and now().time() > time(9, 30):
            print("market closed today")
            return 0
        if now().time() >= RUN_END:
            print("too late for today's setups")
            return 0
        if now() < datetime.combine(day, time(9, 30), NY) - timedelta(minutes=75):
            print("too early; exiting")
            return 0
        for k in range(1, 10):  # earlier attempts today (a restarted run): skip if one filled, else number on
            try:
                o = trading.get_order_by_client_id(f"tjr-{day:%Y%m%d}-{k}")
            except Exception:
                break
            attempt = k
            if float(o.filled_qty or 0) > 0:
                print("already traded today")
                return 0
        wait_until(time(9, 31, 5))
    full = yahoo_bars(day) if replay_day else None
    next_s, pending, step = 1, None, 1
    while True:
        if replay_day:
            pdh, pdl, *cols = full
            step += 1
            if step > len(cols[4]) or cols[4][step - 1] >= RUN_END.hour * 60 + RUN_END.minute:
                break
            o, h, l, c, mins = (x[:step] for x in cols)
        else:
            if now().time() >= RUN_END:
                break
            pdh, pdl, o, h, l, c, mins = yahoo_bars(day)
        if pending:
            after = range(pending["placed_at"], len(c))
            if live:
                status = str(trading.get_order_by_id(pending["order"].id).status)
                filled = status.endswith("FILLED") and "PARTIALLY" not in status
            else:
                filled = any((pending["direction"] < 0 and h[k] >= pending["entry"]) or
                             (pending["direction"] > 0 and l[k] <= pending["entry"]) for k in after)
            if filled:
                side = "LONG" if pending["direction"] > 0 else "SHORT"
                if live:
                    notify(f"PAPER TJR: {side} {pending['qty']} SPY",
                           f"Test trade (small). Swept {pending['level']}, shift + gap. Entry {pending['entry']}, "
                           f"stop {pending['stop']}, target {pending['target']} (2R); out by 3:55 PM.")
                print(f"filled: {side} @ {pending['entry']} (stop {pending['stop']}, target {pending['target']})")
                return 0
            ran_away = any((pending["direction"] < 0 and l[k] <= pending["target"]) or
                           (pending["direction"] > 0 and h[k] >= pending["target"]) for k in after)
            if len(c) - 1 > pending["deadline"] or ran_away:
                print(f"cancelling unfilled setup ({'price reached the target first' if ran_away else 'not filled within 30 bars'})")
                if live:
                    try:
                        trading.cancel_order_by_id(pending["order"].id)
                    except Exception as e:
                        print(f"cancel failed ({type(e).__name__}); it may have just filled")
                next_s, pending = pending["s"] + 1, None
        if not pending:
            setup = find_setup(o, h, l, c, mins, pdh, pdl, next_s)
            if setup:
                side = "buy" if setup["direction"] > 0 else "short"
                qty = size(trading, setup["entry"], setup["stop"]) if trading else 0
                m = mins[setup["m"]]
                print(f"setup at {m // 60}:{m % 60:02d}: swept {setup['level']} "
                      f"({pdh if setup['direction'] < 0 else pdl:.2f}); {side} limit {setup['entry']}, stop {setup['stop']}, "
                      f"target {setup['target']}" + (f", {qty} shares" if trading else ""))
                setup["placed_at"] = len(c)
                if live:
                    if qty < 1:
                        next_s = setup["s"] + 1
                        continue
                    attempt += 1
                    setup["order"], setup["qty"] = place(trading, setup, qty, f"tjr-{day:%Y%m%d}-{attempt}"), qty
                pending = setup
            elif mins and mins[-1] > SWEEP_END + WINDOW:
                break
        if replay_day is None:
            nxt = now().replace(second=5, microsecond=0) + timedelta(minutes=1)
            clock.sleep(max(1.0, (nxt - now()).total_seconds()))
    if pending and live:
        try:
            trading.cancel_order_by_id(pending["order"].id)
        except Exception:
            pass
    print(f"no TJR trade ({day})")
    return 0


def scoreboard(trading) -> str:
    """P&L of the TJR test only: SPY fills since it started."""
    pnl, trades, page = 0.0, set(), None
    while True:
        params = {"after": SCORE_SINCE.isoformat(), "direction": "asc", "page_size": 100}
        if page:
            params["page_token"] = page
        fills = trading.get("/account/activities/FILL", params) or []
        for f in fills:
            if f["symbol"] != SYMBOL:
                continue
            pnl += (1 if f["side"].startswith("sell") else -1) * float(f["qty"]) * float(f["price"])
            trades.add(f["order_id"])
        if len(fills) < 100:
            break
        page = fills[-1]["id"]
    return f"TJR test since {SCORE_SINCE:%b %-d}: {'+' if pnl >= 0 else '-'}${abs(pnl):,.0f} over {len(trades) // 2} trades."


def close() -> int:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    trading = clients()[0]
    wait_until(time(15, 55))
    for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True)):
        if o.symbol == SYMBOL:
            trading.cancel_order_by_id(o.id)
    if any(p.symbol == SYMBOL for p in trading.get_all_positions()):
        clock.sleep(2)
        order = trading.close_position(SYMBOL)
        for _ in range(30):
            if str(trading.get_order_by_id(order.id).status).endswith("FILLED"):
                break
            clock.sleep(2)
        notify("PAPER TJR: closed SPY at 3:55", scoreboard(trading))
        return 0
    day = now().date()
    try:  # traded today but already out (stop or target hit): still send the result
        o = trading.get_order_by_client_id(f"tjr-{day:%Y%m%d}-1")
        if float(o.filled_qty or 0) > 0 or any(
                float(getattr(trading.get_order_by_client_id(f"tjr-{day:%Y%m%d}-{k}"), "filled_qty", 0) or 0) > 0
                for k in range(2, 4)):
            notify("PAPER TJR: today's trade is closed", scoreboard(trading))
    except Exception:
        print("no TJR trade today")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        sys.exit(run("--dry-run" in sys.argv))
    if cmd == "close":
        sys.exit(close())
    if cmd == "replay" and len(sys.argv) > 2:
        sys.exit(run(True, date.fromisoformat(sys.argv[2])))
    sys.exit(__doc__)
