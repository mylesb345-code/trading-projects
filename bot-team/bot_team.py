#!/usr/bin/env python3
"""Paper-trading bot team: Scanner -> Analyst -> Risk Manager -> Executor. PAPER MONEY ONLY.

  python bot_team.py check      check settings, keys and market data before you run it
  python bot_team.py run        run around the clock (scans every 15 minutes, checks stops every minute)
  python bot_team.py once       one pass, then stop
  python bot_team.py summary    results so far, including buy-and-hold

Kill switch: create a file named STOP in this folder. The bot closes every paper position and shuts down.
"""
import argparse
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import roles

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_DIR = os.path.join(HERE, "logs")
STOP_PATH = os.path.join(HERE, "STOP")
APPROVAL_CHAIN = "Scanner > Analyst > Risk Manager > Executor"
SETTLE_SECONDS = 20
DOWNTIME_SECONDS = 300
TRADE_FIELDS = ["closed_at", "mode", "asset", "side", "opened_at", "entry", "exit", "qty", "notional", "stop_loss",
                "take_profit", "fees_est", "slippage_est", "pnl", "pnl_pct", "exit_reason", "approved_by",
                "analyst_reason", "risk_reason", "entry_order", "exit_order"]


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_env(path=os.path.join(HERE, ".env")):
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def append_csv(path, fields, row):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def read_csv(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


class Logs:
    """Everything the team does goes into CSV files in logs/, including skipped setups and errors."""

    def __init__(self, log_dir):
        self.dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def decision(self, t, coin, decision, reason):
        append_csv(self.path("decisions.csv"), ["time", "asset", "decision", "reason"],
                   {"time": stamp(t), "asset": coin, "decision": decision, "reason": reason})

    def trade(self, row):
        append_csv(self.path("trades.csv"), TRADE_FIELDS, row)

    def equity(self, t, value):
        append_csv(self.path("equity.csv"), ["time", "equity"], {"time": stamp(t), "equity": round(value, 2)})

    def downtime(self, start, end):
        append_csv(self.path("downtime.csv"), ["from", "to", "minutes"],
                   {"from": stamp(start), "to": stamp(end), "minutes": round((end - start) / 60)})

    def error(self, t, where, err):
        append_csv(self.path("errors.csv"), ["time", "where", "error"],
                   {"time": stamp(t), "where": where, "error": str(err)[:500]})
        print(f"{stamp(t)} ERROR in {where}: {err}", file=sys.stderr)


class BotTeam:
    def __init__(self, cfg, state, scanner, analyst, risk, executor, logs, stop_path=STOP_PATH):
        self.cfg, self.state = cfg, state
        self.scanner, self.analyst, self.risk, self.executor = scanner, analyst, risk, executor
        self.logs = logs
        self.stop_path = stop_path
        state.setdefault("positions", {})

    def say(self, t, text):
        print(f"{stamp(t)} [{self.executor.label}] {text}", flush=True)

    def tick(self, now):
        """One pass: kill switch, stop/take-profit checks, a scan once a new 15-minute candle has closed."""
        if os.path.exists(self.stop_path):
            self.kill(now, "STOP file found")
            return False
        st = self.state
        gap = now - st.get("last_tick", now)
        if gap > max(DOWNTIME_SECONDS, 3 * self.cfg["check_seconds"]):
            self.logs.downtime(st["last_tick"], now)
            self.say(now, f"The bot was not running for {gap / 60:,.0f} minutes (logged in logs/downtime.csv).")
        st["last_tick"] = now
        day = stamp(now)[:10]
        if st.get("day") != day:
            if st.get("day"):
                print(self.summary(now))
            st["day"], st["day_start_equity"] = day, None
        self.manage_exits(now)
        # Give the exchange a few seconds to publish a candle before scanning it.
        latest_closed = int((now - SETTLE_SECONDS) // roles.CANDLE_SECONDS) * roles.CANDLE_SECONDS - roles.CANDLE_SECONDS
        if latest_closed > st.get("last_slot", 0):
            self.scan(now, latest_closed)
            st["last_slot"] = latest_closed
        return True

    def manage_exits(self, now):
        for coin, pos in list(self.state["positions"].items()):
            try:
                price = self.scanner.price(coin)
            except Exception as e:
                self.logs.error(now, f"price check {coin}", e)
                continue
            if price <= pos["stop"]:
                self.close(coin, price, "stop-loss", now)
            elif price >= pos["take_profit"]:
                self.close(coin, price, "take-profit", now)
            elif now - pos["opened_ts"] >= self.cfg["max_hold_hours"] * 3600:
                self.close(coin, price, "time limit", now)

    def scan(self, now, latest_closed=None):
        st, cfg = self.state, self.cfg
        found, problems = self.scanner.scan(now)
        if latest_closed is not None:
            for coin, candles in list(found.items()):
                if not candles or candles[-1][0] < latest_closed:
                    problems[coin] = (f"stale data: newest candle is {stamp(candles[-1][0]) if candles else 'missing'}, "
                                      f"expected {stamp(latest_closed)}")
                    del found[coin]
        for coin, err in problems.items():
            self.logs.decision(now, coin, "MISSED", f"market data error: {err}")
        prices = {c: k[-1][4] for c, k in found.items() if k}
        if not st.get("start_prices") and prices:
            st["start_prices"], st["started_at"] = prices, now
        equity = self.executor.equity(st["positions"], prices)
        if st.get("start_equity") is None:
            st["start_equity"] = equity
        if st.get("day_start_equity") is None:
            st["day_start_equity"] = equity
        if equity <= st["day_start_equity"] * (1 - cfg["daily_loss_limit"]) and st.get("halted_day") != st["day"]:
            st["halted_day"] = st["day"]
            self.say(now, f"DAILY LOSS LIMIT: paper equity ${equity:,.2f} is down {cfg['daily_loss_limit']:.0%} today. "
                          "No new trades until tomorrow (UTC).")
        self.logs.equity(now, equity)
        cash, setups = self.executor.cash(), 0
        for coin, candles in found.items():
            setup = self.analyst.evaluate(coin, candles)
            if setup["action"] != "BUY":
                self.logs.decision(now, coin, "WAIT", setup["reason"])
                continue
            setups += 1
            if not self.executor.supports(coin):
                self.logs.decision(now, coin, "SKIPPED", "not tradable on this paper account")
                continue
            review = self.risk.review(setup, equity, cash, st["positions"], st.get("halted_day") == st["day"])
            if not review["approved"]:
                self.logs.decision(now, coin, "REJECTED", f"Risk Manager: {review['reason']}")
                self.say(now, f"{coin} setup rejected by the Risk Manager: {review['reason']}")
                continue
            try:
                fill = self.executor.buy(coin, review["qty"], setup["price"])
            except Exception as e:
                self.logs.error(now, f"buy {coin}", e)
                self.logs.decision(now, coin, "MISSED", f"Executor error: {e}")
                continue
            st["positions"][coin] = {
                "entry": fill["price"], "qty": fill["qty"], "stop": setup["stop"], "take_profit": setup["take_profit"],
                "opened_ts": now, "opened_at": stamp(now), "signal_price": setup["price"], "entry_fee": fill["fee"],
                "entry_order": fill["order_id"], "analyst_reason": setup["reason"], "risk_reason": review["reason"]}
            cash = self.executor.cash()
            self.logs.decision(now, coin, "OPENED", f"{setup['reason']}; {review['reason']}")
            self.say(now, f"PAPER BUY {coin}: {fill['qty']:.6g} at ${fill['price']:,.4f} | stop ${setup['stop']:,.4f} | "
                          f"take-profit ${setup['take_profit']:,.4f} | {review['reason']} | approved by {APPROVAL_CHAIN}")
        self.say(now, f"Scan done: {len(found)} coins, {setups} setup(s), {len(st['positions'])} open, "
                      f"paper equity ${equity:,.2f}")

    def close(self, coin, price, reason, now):
        pos = self.state["positions"][coin]
        try:
            fill = self.executor.sell(coin, pos["qty"], price)
        except Exception as e:
            self.logs.error(now, f"sell {coin}", e)
            return
        qty = pos["qty"]
        fees = pos["entry_fee"] + fill["fee"]
        pnl = (fill["price"] - pos["entry"]) * qty - fees
        self.logs.trade({
            "closed_at": stamp(now), "mode": self.executor.label, "asset": coin, "side": "LONG",
            "opened_at": pos["opened_at"], "entry": round(pos["entry"], 6), "exit": round(fill["price"], 6),
            "qty": f"{qty:.9g}", "notional": round(qty * pos["entry"], 2), "stop_loss": round(pos["stop"], 6),
            "take_profit": round(pos["take_profit"], 6), "fees_est": round(fees, 4),
            "slippage_est": round(qty * ((pos["entry"] - pos["signal_price"]) + (price - fill["price"])), 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / (qty * pos["entry"]) * 100, 3), "exit_reason": reason,
            "approved_by": APPROVAL_CHAIN, "analyst_reason": pos["analyst_reason"], "risk_reason": pos["risk_reason"],
            "entry_order": pos["entry_order"], "exit_order": fill["order_id"]})
        del self.state["positions"][coin]
        self.say(now, f"PAPER SELL {coin} ({reason}) at ${fill['price']:,.4f}: P&L ${pnl:,.2f} after estimated fees")

    def kill(self, now, why):
        self.say(now, f"KILL SWITCH ({why}): closing every paper position and shutting down.")
        for coin in list(self.state["positions"]):
            try:
                self.close(coin, self.scanner.price(coin), "kill switch", now)
            except Exception as e:
                self.logs.error(now, f"kill switch {coin}", e)
        print(self.summary(now))

    def summary(self, now, prices=None):
        st = self.state
        pnls = [float(r["pnl"]) for r in read_csv(self.logs.path("trades.csv"))]
        wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
        curve = [float(r["equity"]) for r in read_csv(self.logs.path("equity.csv"))]
        peak, max_dd = None, 0.0
        for eq in curve:
            peak = eq if peak is None else max(peak, eq)
            max_dd = min(max_dd, eq / peak - 1)
        lines = [f"===== PAPER RESULTS ({self.executor.label}) as of {stamp(now)} ====="]
        if st.get("started_at"):
            lines.append(f"Running since {stamp(st['started_at'])}")
        lines.append(f"Trades: {len(pnls)} ({len(wins)} wins, {len(losses)} losses)"
                     + (f", win rate {len(wins) / len(pnls):.0%}" if pnls else ""))
        if pnls:
            lines.append(f"Average win ${sum(wins) / len(wins) if wins else 0:,.2f} | "
                         f"average loss ${sum(losses) / len(losses) if losses else 0:,.2f}")
        lines.append(f"Total paper P&L on closed trades: ${sum(pnls):,.2f} (after estimated fees)")
        if curve and st.get("start_equity"):
            lines.append(f"Paper account: ${st['start_equity']:,.2f} -> ${curve[-1]:,.2f} "
                         f"({curve[-1] / st['start_equity'] - 1:+.2%}) | max drawdown {max_dd:.2%}")
        starts = st.get("start_prices") or {}
        if starts:
            changes = []
            for coin, start in starts.items():
                try:
                    now_price = (prices or {}).get(coin) or self.scanner.price(coin)
                    changes.append(now_price / start - 1)
                except Exception:
                    pass
            if changes:
                lines.append(f"Buy and hold the same {len(changes)} coins in equal amounts: "
                             f"{sum(changes) / len(changes):+.2%}")
        lines.append(f"Open paper positions: {', '.join(st['positions']) or 'none'}")
        missed = sum(r["decision"] == "MISSED" for r in read_csv(self.logs.path("decisions.csv")))
        down = sum(int(r["minutes"]) for r in read_csv(self.logs.path("downtime.csv")))
        lines.append(f"Reliability: {len(read_csv(self.logs.path('errors.csv')))} errors, {missed} missed scans, "
                     f"{down:,} minutes of downtime")
        text = "\n".join(lines)
        with open(self.logs.path("summaries.txt"), "a") as f:
            f.write(text + "\n\n")
        return text


def load_config(path=CONFIG_PATH):
    cfg = load_json(path, None)
    if cfg is None:
        sys.exit(f"Missing {path}")
    if cfg.get("paper_only") is not True:
        sys.exit("REFUSING TO TRADE: config.json must keep \"paper_only\": true.")
    return cfg


def build(cfg, state, log_dir=LOG_DIR):
    if cfg["executor"] == "alpaca":
        executor = roles.AlpacaPaperExecutor(cfg, os.environ.get("PAPER_API_KEY"), os.environ.get("PAPER_API_SECRET"))
    elif cfg["executor"] == "simulated":
        executor = roles.SimulatedExecutor(cfg, state)
    else:
        raise roles.RefuseToTrade(f"executor must be \"simulated\" or \"alpaca\", not {cfg['executor']!r}")
    return BotTeam(cfg, state, roles.Scanner(cfg["coins"]), roles.Analyst(cfg), roles.RiskManager(cfg), executor,
                   Logs(log_dir))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Paper-trading bot team (paper money only).")
    parser.add_argument("command", choices=["check", "run", "once", "summary"])
    args = parser.parse_args(argv)
    load_env()
    cfg = load_config()
    state = load_json(STATE_PATH, {})
    if state.get("mode") and state["mode"] != cfg["executor"]:
        sys.exit(f"state.json belongs to the {state['mode']} executor. Delete state.json to switch.")
    try:
        team = build(cfg, state)
        print(team.executor.check())
    except roles.RefuseToTrade as e:
        sys.exit(f"REFUSING TO TRADE: {e}")
    state["mode"] = cfg["executor"]
    if args.command == "check":
        candles = team.scanner.candles(cfg["coins"][0], time.time())
        print(f"Market data OK: {len(candles)} closed 15-minute candles for {cfg['coins'][0]}, "
              f"last close ${candles[-1][4]:,.4f}")
        return 0
    if args.command == "summary":
        print(team.summary(time.time()))
        return 0
    print(f"PAPER MODE ({team.executor.label}). Scanning {len(cfg['coins'])} coins every 15 minutes. "
          f"Kill switch: create a file named STOP in {HERE}. Ctrl+C pauses without closing positions.")
    try:
        while True:
            try:
                keep_going = team.tick(time.time())
            except Exception as e:
                team.logs.error(time.time(), "main loop", e)
                traceback.print_exc()
                keep_going = True
            save_json(STATE_PATH, state)
            if not keep_going:
                print("Stopped by the kill switch. Delete the STOP file to run again.")
                return 0
            if args.command == "once":
                return 0
            time.sleep(cfg["check_seconds"])
    except KeyboardInterrupt:
        save_json(STATE_PATH, state)
        print("\nPaused. Open paper positions stay open and are picked up the next time you run it.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
