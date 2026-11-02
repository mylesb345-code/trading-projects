"""Win/loss report for the Alpaca paper account, built from the account's actual fills.

A trade opens when a symbol's position goes from 0 to non-zero and closes when it returns to 0; its profit is
what the sells brought in minus what the buys cost. Each trade is labelled by who placed it: the box scanner, the
AAPL moving-average bot, or you (by hand).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from risk import norm

STOCK_BOT_CONFIG = Path(__file__).resolve().parent.parent / "stock-bot" / "config.json"


@dataclass
class RoundTrip:
    symbol: str
    opened: str
    closed: Optional[str] = None
    qty: float = 0.0
    cost: float = 0.0       # dollars paid for buys (long) / received for sells (short)
    proceeds: float = 0.0   # dollars received for sells (long) / paid for buys (short)
    side: str = "long"
    fills: List[Dict] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost if self.side == "long" else self.cost - self.proceeds


def round_trips(fills: Iterable[Dict]) -> List[RoundTrip]:
    """Group fills (oldest first: symbol, side, qty, price, time) into trades per symbol."""
    trips: List[RoundTrip] = []
    open_: Dict[str, RoundTrip] = {}
    pos: Dict[str, float] = {}
    for f in sorted(fills, key=lambda f: f["time"]):
        sym, q, px = norm(f["symbol"]), float(f["qty"]), float(f["price"])
        signed = q if f["side"] == "buy" else -q
        before = pos.get(sym, 0.0)
        if sym not in open_:
            open_[sym] = RoundTrip(f["symbol"], f["time"], side="long" if signed > 0 else "short")
        t = open_[sym]
        if (signed > 0) == (t.side == "long"):
            t.cost += q * px
            t.qty += q
        else:
            t.proceeds += q * px
        t.fills.append(f)
        pos[sym] = before + signed
        if abs(pos[sym]) < 1e-9:
            t.closed = f["time"]
            trips.append(t)
            del open_[sym]
    return trips + list(open_.values())


def owner(symbol: str, bot_symbols: set) -> str:
    if norm(symbol) in {norm(s) for s in bot_symbols}:
        return "box scanner"
    try:
        if norm(symbol) == norm(json.load(open(STOCK_BOT_CONFIG))["symbol"]):
            return "AAPL bot"
    except Exception:
        pass
    return "manual"


def fetch_fills(trading) -> List[Dict]:
    """Every fill on the account, oldest first (Alpaca account activities, paged)."""
    out, page_token = [], None
    while True:
        params = {"direction": "asc", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        page = trading.get("/account/activities/FILL", params)
        if not page:
            break
        out += [{"symbol": a["symbol"], "side": a["side"], "qty": a["qty"], "price": a["price"],
                 "time": a["transaction_time"]} for a in page]
        if len(page) < 100:
            break
        page_token = page[-1]["id"]
    return out


def build(trading, bot_symbols: set, start_equity: float = 100_000.0) -> "tuple[str, str]":
    """(title, body) of the report."""
    acct = trading.get_account()
    equity, last = float(acct.equity), float(acct.last_equity)
    trips = round_trips(fetch_fills(trading))
    closed = [t for t in trips if t.closed]
    positions = {norm(p.symbol): p for p in trading.get_all_positions()}
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    realized = sum(t.pnl for t in closed)
    unrealized = sum(float(p.unrealized_pl) for p in positions.values())
    lines = [f"Account ${equity:,.2f} · today {equity - last:+,.2f} · since start {equity - start_equity:+,.2f} "
             f"({equity / start_equity - 1:+.2%})"]
    if closed:
        lines.append(f"Closed trades: {len(closed)} · {len(wins)} won, {len(losses)} lost "
                     f"({len(wins) / len(closed):.0%} win rate) · realized {realized:+,.2f}")
        if wins:
            lines.append(f"Average win {sum(t.pnl for t in wins) / len(wins):+,.2f}"
                         + (f" · average loss {sum(t.pnl for t in losses) / len(losses):+,.2f}" if losses else ""))
        for t in sorted(closed, key=lambda t: t.closed)[-8:]:
            lines.append(f"  {'WIN ' if t.pnl > 0 else 'LOSS'} {t.symbol:<6} {t.pnl:+,.2f} ({owner(t.symbol, bot_symbols)}, "
                         f"closed {t.closed[:10]})")
    else:
        lines.append("Closed trades: none yet")
    lines.append(f"Open positions: {len(positions)} · unrealized {unrealized:+,.2f}")
    for sym, p in sorted(positions.items()):
        lines.append(f"  {p.symbol:<6} {float(p.qty):g} @ {float(p.avg_entry_price):,.2f} → {float(p.current_price):,.2f} "
                     f"({float(p.unrealized_pl):+,.2f}, {float(p.unrealized_plpc):+.1%}) [{owner(p.symbol, bot_symbols)}]")
    lines.append("PAPER account. Not financial advice.")
    when = datetime.now(timezone.utc).strftime("%b %d")
    title = f"Trading report {when}: {len(wins)}W/{len(losses)}L, account {equity - start_equity:+,.0f}"
    return title, "\n".join(lines)
