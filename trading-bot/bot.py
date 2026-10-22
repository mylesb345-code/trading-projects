#!/usr/bin/env python3
"""BTC daily swing bot: checks the market around the clock, paper-trades the rule, and pushes alerts to your phone.

The rule is the one that passed its backtest on BTC daily candles (35 trades over 3 years, profit factor 3.16):
  LONG  when the daily close is above EMA50 and EMA50 is above EMA200; exit on a daily close below EMA50.
  SHORT when the daily close is below EMA50 and EMA50 is below EMA200; exit on a daily close above EMA50.

Each run pulls public daily candles (Coinbase, falling back to Kraken), replays the rule, paper-trades every
change, and sends the alert through ntfy (a free phone app). It never places real orders: Trench.io has no API,
so you place the trade yourself when an alert arrives.

  python bot.py               run one check (for a scheduler: GitHub Actions, cron, Task Scheduler)
  python bot.py --loop        keep running and check every few minutes (the computer must stay on)
  python bot.py --test-alert  send a test push to your phone
"""
import argparse
import csv
import json
import os
import secrets
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
JOURNAL_PATH = os.path.join(HERE, "journal.csv")
DAY = 86400
USER_AGENT = "personal-swing-bot/1.0"
KRAKEN_PAIRS = {"BTC-USD": "XBTUSD", "ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD"}
JOURNAL_FIELDS = ["coin", "tested", "side", "entry_date", "entry_price", "exit_date", "exit_price",
                  "multiplier", "result_pct", "after_fee_pct", "wiped_out"]
DEFAULTS = {
    "symbols": {"BTC-USD": "tested", "ETH-USD": "untested", "SOL-USD": "untested"},
    "ema_fast": 50,
    "ema_slow": 200,
    "history_days": 1000,
    "paper_multiplier": 1,
    "trench_fee_on_wins": 0.08,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "daily_status": True,
    "loop_minutes": 15,
    "alert_after_failed_runs": 3,
}
TRANSITIONS = {
    (None, "LONG"): "GO LONG",
    (None, "SHORT"): "GO SHORT",
    ("LONG", None): "EXIT your LONG",
    ("SHORT", None): "EXIT your SHORT",
    ("LONG", "SHORT"): "FLIP: exit your LONG, then go SHORT",
    ("SHORT", "LONG"): "FLIP: exit your SHORT, then go LONG",
}
TRENCH_SIDE = {"LONG": "Up", "SHORT": "Down"}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    cfg.update(load_json(path, {}))
    return cfg


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_str(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def money(x):
    return f"${x:,.2f}"


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_coinbase(symbol, days, now, get=http_get_json):
    """Daily candles from Coinbase Exchange, paged 300 at a time (its per-request limit)."""
    rows = {}
    end = int(now // DAY) * DAY
    for _ in range(-(-days // 300)):
        start = end - 299 * DAY
        page = get(f"https://api.exchange.coinbase.com/products/{symbol}/candles"
                   f"?granularity=86400&start={iso(start)}&end={iso(end)}")
        for t, low, high, open_, close, _volume in page:
            rows[int(t)] = (float(open_), float(high), float(low), float(close))
        if len(page) < 300:
            break
        end = start - DAY
        time.sleep(0.3)
    return rows


def fetch_kraken(symbol, days, now, get=http_get_json):
    """Daily candles from Kraken (its most recent 720)."""
    pair = KRAKEN_PAIRS.get(symbol, symbol.replace("-", ""))
    data = get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440")
    if data.get("error"):
        raise RuntimeError(f"Kraken error {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    return {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in series}


SOURCES = [("Coinbase", fetch_coinbase), ("Kraken", fetch_kraken)]


def get_candles(symbol, days, now, min_needed, sources=SOURCES):
    """Completed daily candles, oldest first, as (time, open, high, low, close). Tries each source in turn."""
    problems = []
    for name, fetch in sources:
        try:
            rows = fetch(symbol, days, now)
            done = sorted((t,) + v for t, v in rows.items() if t + DAY <= now)
            if len(done) < min_needed:
                raise RuntimeError(f"only {len(done)} daily candles, need {min_needed}")
            return done, name
        except Exception as e:
            problems.append(f"{name}: {e}")
    raise RuntimeError("; ".join(problems))


def ema(values, period):
    """Exponential moving average seeded with a simple average; None until there are enough values."""
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


def replay_rule(candles, fast_line, slow_line):
    """Walk the rule through every candle. Returns (position, (entry_time, entry_price) or None, latest values)."""
    position, entry = None, None
    for i, candle in enumerate(candles):
        close, f, s = candle[4], fast_line[i], slow_line[i]
        if f is None or s is None:
            continue
        if (position == "LONG" and close < f) or (position == "SHORT" and close > f):
            position, entry = None, None
        if position is None:
            if close > f > s:
                position = "LONG"
            elif close < f < s:
                position = "SHORT"
            if position:
                entry = (candle[0], close)
    latest = {"time": candles[-1][0], "close": candles[-1][4], "ema_fast": fast_line[-1], "ema_slow": slow_line[-1]}
    return position, entry, latest


def paper_result(side, entry_price, exit_price, worst_price, multiplier, fee_on_wins):
    """Percent result on the stake. A move of 1/multiplier against you wipes the stake (Trench's payout multiplier)."""
    sign = 1 if side == "LONG" else -1
    if (worst_price / entry_price - 1) * sign <= -1 / multiplier:
        return -100.0, -100.0, True
    result = (exit_price / entry_price - 1) * sign * multiplier * 100
    after_fee = result * (1 - fee_on_wins) if result > 0 else result
    return result, after_fee, False


def worst_price(candles, side, after_time, through_time):
    """Lowest low (long) or highest high (short) after entry, up to and including the exit candle."""
    prices = [c[3] if side == "LONG" else c[2] for c in candles if after_time < c[0] <= through_time]
    if not prices:
        return None
    return min(prices) if side == "LONG" else max(prices)


def append_journal(path, row):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def close_paper(st, coin, tested, candles, t, close, cfg, journal_path):
    p = st["paper"]
    mult, fee = cfg["paper_multiplier"], cfg["trench_fee_on_wins"]
    worst = worst_price(candles, p["side"], p["entry_time"], t) or close
    result, after_fee, wiped = paper_result(p["side"], p["entry_price"], close, worst, mult, fee)
    append_journal(journal_path, {
        "coin": coin, "tested": "yes" if tested else "no", "side": p["side"],
        "entry_date": day_str(p["entry_time"]), "entry_price": round(p["entry_price"], 2),
        "exit_date": day_str(t), "exit_price": round(close, 2), "multiplier": mult,
        "result_pct": round(result, 2), "after_fee_pct": round(after_fee, 2), "wiped_out": "yes" if wiped else "no",
    })
    st["paper"] = None
    line = (f"Paper result: {p['side']} {money(p['entry_price'])} -> {money(close)} at x{mult} = {result:+.2f}% "
            f"({after_fee:+.2f}% after Trench's {fee:.0%} fee on wins)")
    return line + (f". Wiped out: price moved {100 / mult:.1f}% against it." if wiped else "")


def why_line(latest):
    close, f, s = latest["close"], latest["ema_fast"], latest["ema_slow"]
    return f"Daily close {money(close)} | EMA50 {money(f)} | EMA200 {money(s)}"


def status_line(coin, tested, st, latest, cfg):
    label = f"{coin} (tested)" if tested else f"{coin} (paper test only, don't trade)"
    pos = st["position"] or "FLAT"
    parts = [f"{label}: {pos}"]
    if st.get("signal"):
        parts[0] += f" since {day_str(st['signal'][0])} at {money(st['signal'][1])}"
    parts.append(f"close {money(latest['close'])}")
    p = st.get("paper")
    if p:
        sign = 1 if p["side"] == "LONG" else -1
        open_pct = (latest["close"] / p["entry_price"] - 1) * sign * cfg["paper_multiplier"] * 100
        parts.append(f"bot's paper trade {open_pct:+.2f}% (before fee)")
    if st["position"] == "LONG":
        parts.append(f"exit if a daily close is below {money(latest['ema_fast'])}")
    elif st["position"] == "SHORT":
        parts.append(f"exit if a daily close is above {money(latest['ema_fast'])}")
    return " | ".join(parts)


def run_once(cfg, state, now, notify, fetch=get_candles, journal_path=JOURNAL_PATH):
    fast, slow = cfg["ema_fast"], cfg["ema_slow"]
    coins = state.setdefault("coins", {})
    first_run = not coins
    alerts, status, errors = [], [], []
    newest_candle = None
    for symbol, grade in cfg["symbols"].items():
        coin, tested = symbol.split("-")[0], grade == "tested"
        try:
            candles, source = fetch(symbol, cfg["history_days"], now, 2 * slow)
        except Exception as e:
            errors.append(f"{coin}: {e}")
            continue
        closes = [c[4] for c in candles]
        position, entry, latest = replay_rule(candles, ema(closes, fast), ema(closes, slow))
        t, close = latest["time"], latest["close"]
        st = coins.get(symbol)
        if st is None:
            st = coins[symbol] = {"position": position, "signal": entry, "paper": None, "last_candle": t}
            if position:
                st["paper"] = {"side": position, "entry_time": t, "entry_price": close}
            newest_candle = max(newest_candle or t, t)
        elif t > st["last_candle"]:
            newest_candle = max(newest_candle or t, t)
            st["last_candle"] = t
            if position != st["position"]:
                lines = [f"{coin}: {TRANSITIONS[(st['position'], position)]}"]
                if st["position"]:
                    lines.append(f"Trench: close your {coin} {TRENCH_SIDE[st['position']]} position.")
                if position:
                    lines.append(f"Trench: open {coin} {TRENCH_SIDE[position]}. The backtest used x1 (no leverage).")
                lines.append(why_line(latest))
                if position == "LONG":
                    lines.append("The exit alert comes on a daily close below EMA50.")
                elif position == "SHORT":
                    lines.append("The exit alert comes on a daily close above EMA50.")
                if st.get("paper"):
                    lines.append(close_paper(st, coin, tested, candles, t, close, cfg, journal_path))
                if position:
                    st["paper"] = {"side": position, "entry_time": t, "entry_price": close}
                lines.append(f"Candle {day_str(t)} (closes 8 PM ET, 7 PM ET in winter). Prices from {source}.")
                st["position"], st["signal"] = position, entry
                if tested:
                    alerts.append((f"{coin} {TRANSITIONS[(None, position)] if position else 'EXIT'}", "\n".join(lines)))
                else:
                    status.append(f"{coin} paper test changed today: " + lines[0].split(": ", 1)[1])
            else:
                st["signal"] = entry
        status.append(status_line(coin, tested, st, latest, cfg))

    for title, body in alerts:
        notify(title, body, "high")
    if newest_candle is not None and cfg["daily_status"]:
        if first_run:
            title = "Bot is on"
            status.append("You'll get an alert here when the BTC rule says enter, exit or flip, "
                          "plus one status message after each daily close.")
        else:
            title = f"Daily check {day_str(newest_candle)}"
        notify(title, "\n".join(status), "default")

    limit = cfg["alert_after_failed_runs"]
    if errors:
        print("Problems: " + "; ".join(errors), file=sys.stderr)
        state["failed_runs"] = state.get("failed_runs", 0) + 1
        if state["failed_runs"] == limit:
            notify("Bot can't get prices", "\n".join(errors) + f"\nFailed {limit} checks in a row. It keeps trying.",
                   "high")
    else:
        if state.get("failed_runs", 0) >= limit:
            notify("Bot is back", "Price data is working again.", "default")
        state["failed_runs"] = 0
    return state


def make_notifier(cfg):
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    server = cfg["ntfy_server"].rstrip("/")

    def notify(title, body, priority="default"):
        print(f"\n== {title} ==\n{body}\n", flush=True)
        if not topic:
            return
        req = urllib.request.Request(f"{server}/{topic}", data=body.encode(), method="POST",
                                     headers={"Title": title, "Priority": priority, "User-Agent": USER_AGENT})
        try:
            urllib.request.urlopen(req, timeout=20).close()
        except Exception as e:
            print(f"(phone alert failed: {e})", file=sys.stderr)
    return notify


def ensure_topic(cfg, path=CONFIG_PATH):
    """Pick a hard-to-guess ntfy channel name on first use and save it, so only you see the alerts."""
    if os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic"):
        return
    cfg["ntfy_topic"] = "swing-bot-" + secrets.token_hex(6)
    saved = load_json(path, {})
    saved["ntfy_topic"] = cfg["ntfy_topic"]
    save_json(path, saved)
    print(f"Created your alert channel: {cfg['ntfy_topic']}\n"
          f"In the ntfy phone app, tap + and subscribe to exactly that name.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="BTC daily swing bot: alerts and paper trading, no real orders.")
    parser.add_argument("--loop", action="store_true", help="keep running; check every loop_minutes")
    parser.add_argument("--test-alert", action="store_true", help="send a test push to your phone and exit")
    args = parser.parse_args(argv)
    cfg = load_config()
    ensure_topic(cfg)
    notify = make_notifier(cfg)
    if args.test_alert:
        notify("Test alert", "Alerts from your trading bot will show up here.")
        return 0
    while True:
        state = load_json(STATE_PATH, {})
        try:
            run_once(cfg, state, time.time(), notify)
            save_json(STATE_PATH, state)
        except Exception:
            traceback.print_exc()
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(cfg["loop_minutes"] * 60)


if __name__ == "__main__":
    sys.exit(main())
