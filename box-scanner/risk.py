"""Position sizing and the checks every new entry must pass."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple


@dataclass
class AccountState:
    equity: float
    last_equity: float  # equity at yesterday's close (Alpaca's last_equity)
    held: Set[str] = field(default_factory=set)       # symbols with an open position
    ordered: Set[str] = field(default_factory=set)    # symbols with an open order

    @property
    def open_count(self) -> int:
        return len(self.held | self.ordered)


def norm(symbol: str) -> str:
    """Alpaca shows crypto positions as BTCUSD but trades BTC/USD: compare them the same way."""
    return symbol.replace("/", "").upper()


def position_size(equity: float, entry: float, stop: float, risk_per_trade: float, max_position: float,
                  fractional: bool, step: float = 1e-6) -> float:
    """Units to buy so that hitting the stop loses `risk_per_trade` of equity, capped at `max_position` of equity.
    Whole shares for stocks; crypto rounds down to `step` (the asset's minimum increment)."""
    per_unit = abs(entry - stop)
    if equity <= 0 or entry <= 0 or per_unit <= 0:
        return 0.0
    qty = min(equity * risk_per_trade / per_unit, equity * max_position / entry)
    if not fractional:
        return float(math.floor(qty))
    return math.floor(qty / step) * step


def can_open(state: AccountState, symbol: str, risk: Dict) -> Tuple[bool, str]:
    """May we open a new position in `symbol` right now?"""
    if state.last_equity > 0 and state.equity <= state.last_equity * (1 - risk["daily_loss_limit"]):
        return False, f"daily loss limit hit (equity down {1 - state.equity / state.last_equity:.1%} today)"
    if norm(symbol) in {norm(s) for s in state.held}:
        return False, "already holding it"
    if norm(symbol) in {norm(s) for s in state.ordered}:
        return False, "already has an open order"
    if state.open_count >= risk["max_open_positions"]:
        return False, f"already {state.open_count} positions/orders (max {risk['max_open_positions']})"
    return True, "ok"


def entry_check(live: float, direction: str, signal_close: float, stop: float, target: float, max_chase: float,
                min_reward_risk: float) -> Tuple[bool, str, float]:
    """Is the setup still worth entering at the live price (after the open)? Returns (ok, reason, reward:risk).
    Skips gaps through the stop or the target, prices that ran more than `max_chase` past the signal close, and
    entries whose reward:risk at the live price is below `min_reward_risk`."""
    if live <= 0:
        return False, "no live price", 0.0
    if direction == "long":
        if live <= stop:
            return False, f"opened at {live:.2f}, already below the stop {stop:.2f}", 0.0
        if live >= target:
            return False, f"opened at {live:.2f}, already past the target {target:.2f}", 0.0
        if live > signal_close * (1 + max_chase):
            return False, f"ran {live / signal_close - 1:.1%} above the signal close (max {max_chase:.0%})", 0.0
        rr = (target - live) / (live - stop)
    else:
        if live >= stop:
            return False, f"opened at {live:.2f}, already above the stop {stop:.2f}", 0.0
        if live <= target:
            return False, f"opened at {live:.2f}, already past the target {target:.2f}", 0.0
        if live < signal_close * (1 - max_chase):
            return False, f"fell {1 - live / signal_close:.1%} below the signal close (max {max_chase:.0%})", 0.0
        rr = (live - target) / (stop - live)
    if rr < min_reward_risk:
        return False, f"reward:risk only {rr:.2f} at {live:.2f} (min {min_reward_risk})", rr
    return True, "ok", rr
