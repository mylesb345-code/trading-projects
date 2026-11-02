"""Alpaca PAPER trading: account state, bracket orders for stocks, plain entries for crypto, closing positions.

Always paper=True. On start-up the account itself is checked too: Alpaca paper account numbers start with "PA";
anything else stops the bot.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any, Dict, Optional

from risk import AccountState, norm
from settings import fail
from store import Store
from strategies.box_theory import Signal

log = logging.getLogger("box.broker")


def round_price(x: float) -> float:
    """Alpaca accepts cents for prices of $1 and up, 4 decimals below $1."""
    return round(x, 2) if x >= 1 else round(x, 4)


class PaperBroker:
    def __init__(self, creds, store: Store) -> None:
        from alpaca.trading.client import TradingClient
        self.client = TradingClient(creds.api_key, creds.secret_key, paper=True)
        self.store = store
        acct = self.client.get_account()
        if not str(acct.account_number).startswith("PA"):
            fail(f"account {acct.account_number} doesn't look like an Alpaca PAPER account (those start with PA).")
        log.info("connected to Alpaca PAPER account %s (status %s)", acct.account_number, acct.status)

    # ------------------------------------------------------------ reading
    def state(self) -> AccountState:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        acct = self.client.get_account()
        held = {p.symbol for p in self.client.get_all_positions()}
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        # bracket stop/target legs belong to held positions; only pending ENTRY orders count as "ordered"
        ordered = {o.symbol for o in orders if norm(o.symbol) not in {norm(h) for h in held}}
        return AccountState(float(acct.equity), float(acct.last_equity), held, ordered)

    def is_trading_day(self, day: date) -> bool:
        from alpaca.trading.requests import GetCalendarRequest
        cal = self.client.get_calendar(GetCalendarRequest(start=day, end=day))
        return any(c.date == day for c in cal)

    def crypto_increment(self, symbol: str) -> float:
        try:
            asset = self.client.get_asset(symbol)
            return float(asset.min_trade_increment or 1e-6)
        except Exception:
            return 1e-6

    # ------------------------------------------------------------ orders
    def _submit(self, symbol: str, event: str, request: Any) -> Optional[Any]:
        """Submit and log request, response or error. Never raises: one failed order mustn't stop a scan."""
        req_log = request.model_dump() if hasattr(request, "model_dump") else str(request)
        try:
            order = self.client.submit_order(request)
            resp = {"id": str(order.id), "status": str(order.status), "qty": str(order.qty),
                    "filled_qty": str(order.filled_qty), "filled_avg_price": str(order.filled_avg_price)}
            self.store.log_order(symbol, event, req_log, resp)
            log.info("PAPER %s %s: order %s %s", event, symbol, resp["id"], resp["status"])
            return order
        except Exception as e:
            self.store.log_order(symbol, event, req_log, error=f"{type(e).__name__}: {e}")
            log.error("PAPER %s %s failed: %s: %s", event, symbol, type(e).__name__, e)
            return None

    def stock_bracket(self, s: Signal, qty: float) -> Optional[Any]:
        """Market entry plus stop-loss plus take-profit, good till canceled so the exits stay live.
        Placed after the close, it waits for the next open."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
        req = MarketOrderRequest(symbol=s.symbol, qty=int(qty),
                                 side=OrderSide.BUY if s.direction == "long" else OrderSide.SELL,
                                 time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
                                 take_profit=TakeProfitRequest(limit_price=round_price(s.target)),
                                 stop_loss=StopLossRequest(stop_price=round_price(s.stop)),
                                 client_order_id="box-" + hashlib.sha1(s.key.encode()).hexdigest()[:24])
        return self._submit(s.symbol, f"entry {s.direction} bracket", req)

    def crypto_entry(self, s: Signal, qty: float) -> Optional[Any]:
        """Crypto: plain market buy (Alpaca has no crypto brackets); the stop and target are watched in code."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        req = MarketOrderRequest(symbol=s.symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC)
        return self._submit(s.symbol, "entry long (crypto, stop/target in code)", req)

    def close(self, symbol: str, reason: str) -> bool:
        """Cancel the symbol's open orders (bracket legs), then close the position at market."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        try:
            for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)):
                if norm(o.symbol) == norm(symbol):
                    self.client.cancel_order_by_id(o.id)
            order = self.client.close_position(symbol.replace("/", "") if "/" in symbol else symbol)
            self.store.log_order(symbol, f"close ({reason})", None, {"id": str(order.id), "status": str(order.status)})
            log.info("PAPER close %s (%s): order %s", symbol, reason, order.id)
            return True
        except Exception as e:
            self.store.log_order(symbol, f"close ({reason})", None, error=f"{type(e).__name__}: {e}")
            log.error("PAPER close %s failed: %s: %s", symbol, type(e).__name__, e)
            return False
