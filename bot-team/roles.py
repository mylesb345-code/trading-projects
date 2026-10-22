"""The four roles of the paper-trading bot team: Scanner, Analyst, Risk Manager and Executor.

PAPER ONLY. The Alpaca executor's web address is fixed to Alpaca's paper-trading API, and there is no setting that
points it anywhere else. The simulated executor never talks to a broker at all.
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

COINBASE = "https://api.exchange.coinbase.com"
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
CANDLE_SECONDS = 900
USER_AGENT = "paper-bot-team/1.0"


class RefuseToTrade(RuntimeError):
    """Raised when anything about the setup is not clearly paper trading."""


class HttpError(RuntimeError):
    pass


def http_json(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if data:
        all_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise HttpError(f"{method} {url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    return json.loads(raw) if raw else None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    avg = sum(values[:period]) / period
    out[period - 1] = avg
    for i in range(period, len(values)):
        avg = values[i] * k + avg * (1 - k)
        out[i] = avg
    return out


def rsi(closes, period=14):
    """Wilder's RSI; None until there are period + 1 closes."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(c, 0) for c in changes[:period]) / period
    loss = sum(max(-c, 0) for c in changes[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            c = changes[i - 1]
            gain = (gain * (period - 1) + max(c, 0)) / period
            loss = (loss * (period - 1) + max(-c, 0)) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr(candles, period=14):
    """Average true range of the last `period` candles (time, open, high, low, close)."""
    if len(candles) <= period:
        return None
    ranges = [max(h - l, abs(h - candles[i - 1][4]), abs(l - candles[i - 1][4]))
              for i, (_, _, h, l, _) in enumerate(candles) if i > 0]
    return sum(ranges[-period:]) / period


class Scanner:
    """Role 1: every 15 minutes, pulls the latest closed 15-minute candles for each coin (Coinbase, no key needed)."""

    def __init__(self, coins, http=http_json):
        self.coins = coins
        self.http = http

    def candles(self, coin, now, count=100):
        forming = int(now // CANDLE_SECONDS) * CANDLE_SECONDS
        rows = self.http("GET", f"{COINBASE}/products/{coin}-USD/candles?granularity={CANDLE_SECONDS}"
                                f"&start={iso(forming - count * CANDLE_SECONDS)}&end={iso(forming)}")
        return sorted((int(t), float(o), float(h), float(l), float(c))
                      for t, l, h, o, c, _ in rows if int(t) + CANDLE_SECONDS <= now)

    def scan(self, now):
        found, problems = {}, {}
        for coin in self.coins:
            try:
                found[coin] = self.candles(coin, now)
            except Exception as e:
                problems[coin] = str(e)
        return found, problems

    def price(self, coin):
        return float(self.http("GET", f"{COINBASE}/products/{coin}-USD/ticker")["price"])


class Analyst:
    """Role 2: trend + momentum. BUY only when the 9 EMA crosses above the 21 EMA on the latest closed candle
    and RSI(14) is between 50 and 70. The stop goes 1.5 ATR below the price and the take-profit at 2x the risk."""

    def __init__(self, cfg):
        self.cfg = cfg

    def evaluate(self, coin, candles):
        cfg = self.cfg
        closes = [c[4] for c in candles]
        fast, slow = ema(closes, cfg["ema_fast"]), ema(closes, cfg["ema_slow"])
        strength = rsi(closes, cfg["rsi_period"])[-1] if closes else None
        volatility = atr(candles, cfg["atr_period"])
        if len(candles) < cfg["ema_slow"] + 2 or None in (fast[-2], slow[-2], strength, volatility):
            return {"coin": coin, "action": "WAIT", "reason": f"only {len(candles)} closed candles so far"}
        if not (fast[-2] <= slow[-2] and fast[-1] > slow[-1]):
            return {"coin": coin, "action": "WAIT", "reason": "no fresh EMA crossover"}
        if not cfg["rsi_min"] <= strength <= cfg["rsi_max"]:
            return {"coin": coin, "action": "WAIT",
                    "reason": f"EMA crossover, but RSI {strength:.1f} is outside {cfg['rsi_min']}-{cfg['rsi_max']}"}
        price = closes[-1]
        stop = price - cfg["stop_atr"] * volatility
        return {"coin": coin, "action": "BUY", "price": price, "stop": stop,
                "take_profit": price + cfg["reward_risk"] * (price - stop), "candle_time": candles[-1][0],
                "reason": f"EMA{cfg['ema_fast']} crossed above EMA{cfg['ema_slow']}, RSI {strength:.1f}"}


class RiskManager:
    """Role 3: approves a trade only if every rule passes, and sizes it so at most 2% of the paper account is at risk
    (fees and slippage included). No leverage: a trade can never cost more than the cash available."""

    def __init__(self, cfg):
        self.cfg = cfg

    def review(self, setup, equity, cash, open_positions, halted):
        cfg = self.cfg
        if cfg.get("paper_only") is not True:
            return {"approved": False, "reason": "config.json is not set to paper_only"}
        if halted:
            return {"approved": False, "reason": f"daily loss limit ({cfg['daily_loss_limit']:.0%}) hit; no new trades today"}
        if setup["coin"] in open_positions:
            return {"approved": False, "reason": "already holding this coin"}
        if len(open_positions) >= cfg["max_open_trades"]:
            return {"approved": False, "reason": f"already {len(open_positions)} open trades (max {cfg['max_open_trades']})"}
        price, stop, target = setup["price"], setup.get("stop"), setup.get("take_profit")
        if stop is None or target is None or not stop < price < target:
            return {"approved": False, "reason": "every trade needs a stop-loss below and a take-profit above the entry"}
        slip, fee = cfg["slippage_bps"] / 10000, cfg["fee_bps"] / 10000
        entry, stop_fill = price * (1 + slip), stop * (1 - slip)
        cost_per_coin = (entry - stop_fill) + (entry + stop_fill) * fee
        qty = min(equity * cfg["risk_per_trade"] / cost_per_coin, cash / (entry * (1 + fee)))
        if qty * entry < cfg["min_order_usd"]:
            return {"approved": False, "reason": f"position would be only ${qty * entry:,.2f} (minimum ${cfg['min_order_usd']})"}
        return {"approved": True, "qty": qty, "risk": qty * cost_per_coin, "notional": qty * entry,
                "reason": f"risking ${qty * cost_per_coin:,.2f} ({qty * cost_per_coin / equity:.2%} of ${equity:,.2f})"}


class SimulatedExecutor:
    """Role 4 without any account: fills paper trades locally at the live price plus slippage and fees."""
    label = "PAPER-SIMULATED"

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        state.setdefault("cash", float(cfg["starting_equity"]))

    def check(self):
        return f"{self.label}: local simulation with ${self.state['cash']:,.2f} paper cash. No broker connection."

    def supports(self, coin):
        return True

    def buy(self, coin, qty, price):
        fill = price * (1 + self.cfg["slippage_bps"] / 10000)
        fee = qty * fill * self.cfg["fee_bps"] / 10000
        self.state["cash"] -= qty * fill + fee
        return {"price": fill, "qty": qty, "fee": fee, "order_id": f"sim-{uuid.uuid4().hex[:8]}"}

    def sell(self, coin, qty, price):
        fill = price * (1 - self.cfg["slippage_bps"] / 10000)
        fee = qty * fill * self.cfg["fee_bps"] / 10000
        self.state["cash"] += qty * fill - fee
        return {"price": fill, "qty": qty, "fee": fee, "order_id": f"sim-{uuid.uuid4().hex[:8]}"}

    def cash(self):
        return self.state["cash"]

    def equity(self, positions, prices):
        return self.state["cash"] + sum(p["qty"] * prices.get(c, p["entry"]) for c, p in positions.items())


class AlpacaPaperExecutor:
    """Role 4 with an Alpaca paper account. Refuses to start unless everything points at paper trading."""
    label = "PAPER-ALPACA"

    def __init__(self, cfg, key, secret, http=http_json, env=os.environ):
        for name in ("ALPACA_BASE_URL", "APCA_API_BASE_URL"):
            if env.get(name) and env[name].rstrip("/") != ALPACA_PAPER_URL:
                raise RefuseToTrade(f"{name} is set to {env[name]}, which is not Alpaca's paper API. Remove it.")
        if not key or not secret:
            raise RefuseToTrade("PAPER_API_KEY and PAPER_API_SECRET must be set in .env (paper keys only).")
        if key.startswith("AK"):
            raise RefuseToTrade("That key ID starts with AK, which Alpaca uses for live accounts. Use your paper key.")
        self.cfg = cfg
        self.http = http
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.tradable = set()

    def _call(self, method, path, body=None):
        return self.http(method, ALPACA_PAPER_URL + path, self.headers, body)

    def check(self):
        account = self._call("GET", "/v2/account")
        if account.get("status") != "ACTIVE":
            raise RefuseToTrade(f"Alpaca paper account status is {account.get('status')}, not ACTIVE.")
        assets = self._call("GET", "/v2/assets?asset_class=crypto&status=active")
        self.tradable = {a["symbol"] for a in assets if a.get("tradable")}
        return (f"{self.label}: connected to {ALPACA_PAPER_URL}, account {account.get('account_number')}, "
                f"paper equity ${float(account['equity']):,.2f}")

    def supports(self, coin):
        return f"{coin}/USD" in self.tradable

    def _wait(self, order_id, tries=30):
        for _ in range(tries):
            order = self._call("GET", f"/v2/orders/{order_id}")
            if order["status"] == "filled":
                return order
            if order["status"] in ("canceled", "expired", "rejected"):
                raise HttpError(f"paper order {order_id} was {order['status']}")
            time.sleep(1)
        raise HttpError(f"paper order {order_id} did not fill within {tries} seconds")

    def _filled(self, order):
        price, qty = float(order["filled_avg_price"]), float(order["filled_qty"])
        return {"price": price, "qty": qty, "fee": qty * price * self.cfg["fee_bps"] / 10000, "order_id": order["id"]}

    def buy(self, coin, qty, price):
        order = self._call("POST", "/v2/orders", {"symbol": f"{coin}/USD", "qty": f"{qty:.9f}", "side": "buy",
                                                  "type": "market", "time_in_force": "gtc"})
        return self._filled(self._wait(order["id"]))

    def sell(self, coin, qty, price):
        order = self._call("DELETE", f"/v2/positions/{coin}USD")
        return self._filled(self._wait(order["id"]))

    def _account(self):
        return self._call("GET", "/v2/account")

    def cash(self):
        account = self._account()
        return float(account.get("non_marginable_buying_power") or account["cash"])

    def equity(self, positions, prices):
        return float(self._account()["equity"])
