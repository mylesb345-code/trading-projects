#!/usr/bin/env python3
"""S&P 500 trend bot (Alpaca PAPER only): hold SPY while the S&P 500 is above its 200-day average, T-bills (BIL)
otherwise. The only rule that held up in strategy-lab/ on both 1999-2016 and 2017-now data: about the S&P's long-run
return with roughly half the worst drop (-24% vs -55% since 2000).

  python3 bot.py            # run once (GitHub Actions runs it on trading-day mornings)
  python3 bot.py --dry-run  # show what it would do, place nothing

Each run, while the market is open:
  1. Signal: yesterday's completed SPY close vs the average of the last 200 completed closes.
  2. Budget: account equity minus everything the bot doesn't own (e.g. the box scanner's last positions, your manual
     trades; not the day-trading bot's QQQ), less a 2% cash cushion. Money freed when those positions close is picked up automatically.
  3. Sell the side the signal says not to hold, then buy the other side up to the budget. Top-ups smaller than 5% of
     the budget are skipped, so it doesn't trade every day. A second run on the same morning finds nothing to do.
It messages your phone (ntfy) when it trades, and on Fridays with a short summary.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "box-scanner"))

RISK_ON, RISK_OFF, LOOKBACK = "SPY", "BIL", 200
CUSHION, MIN_TOPUP = 0.02, 0.05
STATE = HERE / "state.json"
NEW_YORK = ZoneInfo("America/New_York")


def notify(title: str, body: str) -> None:
    print(f"[phone] {title}: {body}")
    try:
        topic = json.loads((ROOT / "trading-bot" / "config.json").read_text())["ntfy_topic"]
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers={"Title": title})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # a failed alert never stops a trade decision
        print(f"alert failed: {type(e).__name__}")


def signal(creds) -> tuple:
    """(risk_on, last close, 200-day average, date of that close), from completed daily bars only."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    today = datetime.now(NEW_YORK).replace(hour=0, minute=0, second=0, microsecond=0)
    req = StockBarsRequest(symbol_or_symbols=RISK_ON, timeframe=TimeFrame.Day, start=today - timedelta(days=400),
                           end=today, adjustment=Adjustment.ALL, feed=DataFeed.SIP)  # SIP: free when not live
    df = StockHistoricalDataClient(creds.api_key, creds.secret_key).get_stock_bars(req).df.droplevel(0)
    df = df[[t.tz_convert(NEW_YORK).date() < today.date() for t in df.index]]  # completed days only
    closes = df["close"].astype(float)
    if len(closes) < LOOKBACK:
        raise SystemExit(f"only {len(closes)} daily closes; need {LOOKBACK}")
    avg = closes.iloc[-LOOKBACK:].mean()
    return closes.iloc[-1] > avg, closes.iloc[-1], avg, df.index[-1].tz_convert(NEW_YORK).date()


def main(dry: bool) -> int:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
    from settings import load_credentials
    creds = load_credentials(ROOT / "box-scanner" / ".env")  # refuses live keys
    client = TradingClient(creds.api_key, creds.secret_key, paper=True)
    acct = client.get_account()
    if not str(acct.account_number).startswith("PA"):
        raise SystemExit("not an Alpaca PAPER account (those start with PA); stopping")
    if not client.get_clock().is_open and not dry:
        print("market closed: nothing to do")
        return 0
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    up, close, avg, day = signal(creds)
    want, other = (RISK_ON, RISK_OFF) if up else (RISK_OFF, RISK_ON)
    gap = close / avg - 1
    print(f"{day}: SPY {close:.2f} vs 200-day average {avg:.2f} ({gap:+.1%}) -> hold {want}")

    pending = [o for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
               if o.symbol in (RISK_ON, RISK_OFF)]
    if pending:
        print(f"{len(pending)} order(s) still open for {RISK_ON}/{RISK_OFF}; waiting for the next run")
        return 0
    positions = {p.symbol: float(p.market_value) for p in client.get_all_positions()}
    equity = float(acct.equity)
    # QQQ belongs to the day-trading bot (orb-bot/), which is flat every night; leave it out of the budget
    others = sum(v for s, v in positions.items() if s not in (RISK_ON, RISK_OFF, "QQQ"))
    budget = (equity - others) * (1 - CUSHION)
    trades = []

    if positions.get(other, 0) > 0:
        print(f"sell all {other} (${positions[other]:,.0f})")
        if not dry:
            order = client.close_position(other)
            for _ in range(30):  # wait for the fill so the cash is there for the buy
                if str(client.get_order_by_id(order.id).status).endswith("FILLED"):
                    break
                time.sleep(2)
        trades.append(f"sold {other} ${positions[other]:,.0f}")

    have = positions.get(want, 0)
    top_up = budget - have
    if top_up > MIN_TOPUP * budget or (trades and top_up > 1):
        print(f"buy ${top_up:,.2f} of {want}")
        if not dry:
            client.submit_order(MarketOrderRequest(symbol=want, notional=round(top_up, 2), side=OrderSide.BUY,
                                                   time_in_force=TimeInForce.DAY,
                                                   client_order_id=f"trend-{want}-{datetime.now(timezone.utc):%Y%m%d}"))
        trades.append(f"bought {want} ${top_up:,.0f}")
    else:
        print(f"holding {want} ${have:,.0f} (budget ${budget:,.0f}); no trade")

    if dry:
        return 0
    state.setdefault("started", str(day))
    state.setdefault("start_equity", equity)
    state.setdefault("start_spy", close)
    state.update({"last_run": str(datetime.now(timezone.utc)), "holding": want, "spy_close": close,
                  "spy_200d": round(avg, 2)})
    if trades:
        why = (f"SPY closed {gap:+.1%} {'above' if up else 'below'} its 200-day average "
               f"({close:.2f} vs {avg:.2f}).")
        notify(f"PAPER trend bot: now holding {want}", f"{why} {'; '.join(trades)}.")
        state.setdefault("trades", []).append({"day": str(day), "hold": want, "detail": trades})
    if datetime.now(NEW_YORK).weekday() == 4 and state.get("summary_sent") != str(day):
        notify("PAPER account, weekly",
               f"Holding {want}: SPY is {gap:+.1%} vs its 200-day average. Account ${equity:,.0f}, "
               f"{equity / state['start_equity'] - 1:+.1%} since the switch on {state['started']} "
               f"(SPY {close / state['start_spy'] - 1:+.1%}). Rule changes so far: {len(state.get('trades', []))}.")
        state["summary_sent"] = str(day)
    STATE.write_text(json.dumps(state, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
