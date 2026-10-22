#!/usr/bin/env python3
"""BTC volatility clock: learns when BTC moves the most, and pings your phone when an unusually big move is under way.

It measures movement, not profit. The hours when BTC moves the most are also the hours when it can move hardest
against you, so a busy hour means more risk as well as more opportunity. It never places orders.

  python volclock.py            one hourly check: big-move alert, plus the weekly report on Sundays
  python volclock.py --report   print the "when does BTC move the most" report now (and send it to your phone)
"""
import argparse
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import bot

HOUR = 3600
SYMBOL = "BTC-USD"
STATE_PATH = os.path.join(bot.HERE, "volclock_state.json")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULTS = {
    "vol_lookback_days": 90,
    "vol_spike_ratio": 2.5,     # alert when an hour moves at least 2.5x its normal amount for that time of day
    "vol_spike_min_pct": 1.0,   # ...and at least 1% from high to low, so quiet-hour blips don't alert
    "vol_report_weekday": 6,    # Sunday
    "vol_timezone": "America/New_York",
}


def local_time(t, tz):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(t, ZoneInfo(tz))
    except Exception:  # no time zone data: fall back to UTC and say so in the labels
        return datetime.fromtimestamp(t, timezone.utc)


def hour_label(h):
    return f"{(h - 1) % 12 + 1} {'AM' if h < 12 else 'PM'}"


def fetch_coinbase_hourly(days, now, get=bot.http_get_json):
    """Hourly candles from Coinbase Exchange, paged 300 at a time."""
    rows = {}
    end = int(now // HOUR) * HOUR
    for _ in range(-(-days * 24 // 300)):
        start = end - 299 * HOUR
        page = get(f"https://api.exchange.coinbase.com/products/{SYMBOL}/candles"
                   f"?granularity=3600&start={bot.iso(start)}&end={bot.iso(end)}")
        for t, low, high, open_, close, _volume in page:
            rows[int(t)] = (float(open_), float(high), float(low), float(close))
        if not page:
            break
        end = start - HOUR
        time.sleep(0.3)
    return rows


def fetch_kraken_hourly(days, now, get=bot.http_get_json):
    """Hourly candles from Kraken (its most recent 720, about 30 days)."""
    data = get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60")
    if data.get("error"):
        raise RuntimeError(f"Kraken error {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    return {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in series}


def get_hourly(days, now, sources=(("Coinbase", fetch_coinbase_hourly), ("Kraken", fetch_kraken_hourly))):
    """Completed hourly candles (time, open, high, low, close), oldest first."""
    problems = []
    for name, fetch in sources:
        try:
            rows = fetch(days, now)
            done = sorted((t,) + v for t, v in rows.items() if t + HOUR <= now and t >= now - days * 86400 - HOUR)
            if len(done) < 24 * 14:
                raise RuntimeError(f"only {len(done)} hourly candles")
            return done, name
        except Exception as e:
            problems.append(f"{name}: {e}")
    raise RuntimeError("; ".join(problems))


def range_pct(c):
    """How far BTC travelled inside one hour: high to low, as a % of the open."""
    return (c[2] - c[3]) / c[1] * 100 if c[1] else 0.0


def profile(candles, tz):
    """Median hourly range for each hour of the day, and average daily movement for each weekday."""
    by_hour, by_day = {h: [] for h in range(24)}, {d: [] for d in range(7)}
    for c in candles:
        when = local_time(c[0], tz)
        r = range_pct(c)
        by_hour[when.hour].append(r)
        by_day[when.weekday()].append(r)
    hours = {h: statistics.median(v) for h, v in by_hour.items() if v}
    days = {d: statistics.mean(v) * 24 for d, v in by_day.items() if v}  # rough "% moved in a day" for comparison
    return hours, days


def report(candles, source, cfg):
    tz = cfg["vol_timezone"]
    hours, days = profile(candles, tz)
    busiest = sorted(hours, key=hours.get, reverse=True)
    quiet = sorted(hours, key=hours.get)
    span = f"{len(candles) // 24} days of hourly prices from {source}"
    zone = "New York time" if tz == "America/New_York" else tz
    lines = [f"When BTC moves the most ({span}, {zone}):",
             "Busiest hours: " + ", ".join(f"{hour_label(h)} ({hours[h]:.2f}%)" for h in busiest[:3]),
             "Quietest hours: " + ", ".join(f"{hour_label(h)} ({hours[h]:.2f}%)" for h in quiet[:3])]
    if days:
        ranked = sorted(days, key=days.get, reverse=True)
        lines.append(f"Busiest day: {DAYS[ranked[0]]}. Quietest: {DAYS[ranked[-1]]}.")
    lines.append(f"(% = typical high-to-low move inside that hour. The busiest hour moves {hours[busiest[0]] / hours[quiet[0]]:.1f}x "
                 "the quietest.)")
    lines.append("Bigger moves cut both ways: more room to win and to lose, especially with a multiplier.")
    return "\n".join(lines)


def spike(candles, cfg):
    """The latest completed hour, if it moved far more than that time of day normally does."""
    *history, last = candles
    hours, _ = profile(history, cfg["vol_timezone"])
    hour = local_time(last[0], cfg["vol_timezone"]).hour
    normal, moved = hours.get(hour), range_pct(last)
    if not normal or moved < cfg["vol_spike_min_pct"] or moved < cfg["vol_spike_ratio"] * normal:
        return None
    change = (last[4] / last[1] - 1) * 100
    return {"time": last[0], "moved": moved, "normal": normal, "change": change, "close": last[4], "hour": hour}


def run_check(cfg, state, now, notify, fetch=get_hourly, force_report=False):
    candles, source = fetch(cfg["vol_lookback_days"], now)
    s = spike(candles, cfg)
    if s and state.get("last_spike") != s["time"]:
        state["last_spike"] = s["time"]
        when = local_time(s["time"], cfg["vol_timezone"])
        direction = "UP" if s["change"] >= 0 else "DOWN"
        swing = (bot.load_json(bot.STATE_PATH, {}).get("coins", {}).get(SYMBOL) or {}).get("position") or "FLAT"
        notify(f"BTC big move {direction} {s['change']:+.1f}%",
               f"The {hour_label(when.hour)} hour ({when:%a %b %d}) moved {s['moved']:.2f}% from high to low, "
               f"{s['moved'] / s['normal']:.1f}x normal for that hour. It closed at {bot.money(s['close'])}.\n"
               f"The swing rule's position is {swing}; it only changes on a daily close.\n"
               "This is a heads-up about movement, not a trade signal. Moves this size can reverse fast.",
               "high")
    week = datetime.fromtimestamp(now, timezone.utc).strftime("%G-W%V")
    today = local_time(now, cfg["vol_timezone"]).weekday()
    if force_report or (today == cfg["vol_report_weekday"] and state.get("last_report") != week):
        state["last_report"] = week
        notify("BTC volatility clock", report(candles, source, cfg), "default")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="BTC volatility clock: when BTC moves the most. No orders.")
    parser.add_argument("--report", action="store_true", help="send the weekly report now")
    args = parser.parse_args(argv)
    cfg = dict(DEFAULTS)
    cfg.update(bot.load_config())
    notify = bot.make_notifier(cfg)
    state = bot.load_json(STATE_PATH, {})
    try:
        run_check(cfg, state, time.time(), notify, force_report=args.report)
    except Exception as e:
        print(f"Volatility clock skipped this hour: {e}", file=sys.stderr)
        return 0  # a missed hour is fine; the swing bot's own failure alerts cover data outages
    bot.save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
