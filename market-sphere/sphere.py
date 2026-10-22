#!/usr/bin/env python3
"""Paper-only multi-market ranking terminal: score every market, rank them, paper-trade only the #1 setup.

  python sphere.py scan              print the leaderboard for the latest daily close
  python sphere.py run               leaderboard + advance the paper account (add --loop to keep it running)
  python sphere.py backtest          replay the same rules over past data (default 3 years)
  python sphere.py test-alert        send a test phone alert (needs NTFY_TOPIC in .env)

It never places live trades: there is no broker connection and no order code.
"""
import argparse
import csv
import json
import os
import sys
import time
import traceback
import urllib.request
from datetime import date as Date, datetime, timedelta, timezone

import marketdata
import paper
import scoring

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "paper_state.json")
LOG_DIR = os.path.join(HERE, "logs")
NO_GAMMA = "UNCLEAR (no options data)"
TRADE_FIELDS = ["logged_at", "id", "symbol", "rank", "score", "direction", "orderflow", "gamma", "signal_date",
                "entry_date", "entry", "stop", "target", "units", "risk", "exit_date", "exit", "pnl", "r", "result",
                "why", "reason"]


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


def load_config(path=CONFIG_PATH):
    cfg = load_json(path, None)
    if cfg is None:
        sys.exit(f"Missing {path}")
    if cfg.get("paper_only") is not True:
        sys.exit("config.json must keep \"paper_only\": true. This tool has no live trading.")
    unknown = set(cfg["weights"]) - set(scoring.COMPONENTS)
    if unknown:
        sys.exit(f"Unknown score weights: {', '.join(sorted(unknown))}")
    return cfg


def settled_date(now, settle_hours):
    """Latest daily candle that has closed for every market (crypto at 00:00 UTC, US futures by midnight ET)."""
    return (datetime.fromtimestamp(now - settle_hours * 3600, timezone.utc).date() - timedelta(days=1)).isoformat()


def append_csv(path, fields, rows):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def make_notifier():
    topic = os.environ.get("NTFY_TOPIC", "")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

    def notify(title, body):
        if not topic:
            return
        req = urllib.request.Request(f"{server}/{topic}", data=body.encode(), method="POST",
                                     headers={"Title": title, "User-Agent": marketdata.USER_AGENT})
        try:
            urllib.request.urlopen(req, timeout=20).close()
        except Exception as e:
            print(f"(phone alert failed: {e})", file=sys.stderr)
    return notify


class EventLog:
    """Writes every setup, fill, skip, exit and halt to logs/events.csv and prints it; alerts on the important ones."""
    ALERTS = {"SETUP", "FILL", "EXIT", "HALT", "BLOCKED"}

    def __init__(self, log_dir, notify):
        self.dir = log_dir
        self.notify = notify
        os.makedirs(log_dir, exist_ok=True)

    def __call__(self, kind, day, symbol, text):
        append_csv(os.path.join(self.dir, "events.csv"), ["logged_at", "date", "type", "market", "detail"],
                   [{"logged_at": stamp(), "date": day, "type": kind, "market": symbol, "detail": text}])
        print(f"[{kind}] {day} {symbol} {text}".replace("  ", " "))
        if kind in self.ALERTS:
            self.notify(f"PAPER {kind} {symbol}".strip(), f"{day}: {text}")

    def scores(self, day, scores, context):
        ranked = sorted(scores.values(), key=lambda s: (-s["score"], s["symbol"]))
        rows = [dict({"logged_at": stamp(), "date": day, "rank": n, "market": s["symbol"], "score": s["score"],
                      "direction": s["direction"], "missing": " ".join(s["missing"]),
                      "orderflow": context.get(s["symbol"], {}).get("orderflow", ""),
                      "gamma": context.get(s["symbol"], {}).get("gamma", "")},
                     **{k: round(v, 3) if v is not None else "" for k, v in s["parts"].items()})
                for n, s in enumerate(ranked, 1)]
        fields = ["logged_at", "date", "rank", "market", "score", "direction", *scoring.COMPONENTS, "missing",
                  "orderflow", "gamma"]
        append_csv(os.path.join(self.dir, "scores.csv"), fields, rows)

    def trades(self, state, path=None):
        new = state["closed"][state.get("logged_trades", 0):]
        append_csv(path or os.path.join(self.dir, "trades.csv"), TRADE_FIELDS,
                   [dict(t, logged_at=stamp()) for t in new])
        state["logged_trades"] = len(state["closed"])


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_universe(cfg, now, through, days, fetch=marketdata.get_candles):
    candles, sources, errors = {}, {}, {}
    for market in cfg["markets"]:
        try:
            candles[market["symbol"]], sources[market["symbol"]] = fetch(market, days, now, through)
        except Exception as e:
            errors[market["symbol"]] = str(e)
    return candles, sources, errors


def index_series(candles):
    series = {sym: scoring.build_series(c) for sym, c in candles.items()}
    index = {sym: {d: i for i, d in enumerate(s["dates"])} for sym, s in series.items()}
    return series, index


def bars_and_scores(series, index, day, cfg):
    """Candles and scores for the markets that have a candle on `day`. A market without one is not ranked."""
    bars, scores = {}, {}
    for sym, s in series.items():
        i = index[sym].get(day)
        if i is None:
            continue
        bars[sym] = s["candles"][i]
        result = scoring.score_at(s, i, cfg["weights"], cfg["stop_atr"], cfg["target_r"])
        if result:
            result["symbol"] = sym
            scores[sym] = result
    return bars, scores


def replay(cfg, candles, start, log=None):
    """Backtest: run the paper engine over every date from `start`, with the exact rules used live."""
    series, index = index_series(candles)
    engine = paper.PaperEngine(cfg, log=log)
    for day in sorted({d for s in series.values() for d in s["dates"] if d >= start}):
        bars, scores = bars_and_scores(series, index, day, cfg)
        engine.step(day, bars, scores)
    return engine.state


def live_context(cfg, flow, sleep=time.sleep):
    """Order flow for crypto markets on Coinbase (shown on the board, not part of the score) and gamma status.

    Order flow is read twice, persist_seconds apart, so the board can say whether walls held or were pulled.
    """
    of = cfg["orderflow"]
    wait = of.get("persist_seconds", 0)
    crypto = [m["symbol"] for m in cfg["markets"] if of["enabled"] and m["source"] == "coinbase"]
    first = {}
    for sym in crypto:
        try:
            first[sym] = flow(sym, of["depth_pct"], of["trades"])
        except Exception as e:
            first[sym] = e
    if crypto and wait:
        sleep(wait)
    context = {}
    for market in cfg["markets"]:
        sym = market["symbol"]
        entry = {"gamma": NO_GAMMA}
        if sym in first:
            try:
                if isinstance(first[sym], Exception):
                    raise first[sym]
                f = flow(sym, of["depth_pct"], of["trades"], first[sym]) if wait else first[sym]
                entry["orderflow"] = f"{f['score']} {f['bias']}"
                entry["orderflow_detail"] = f
            except Exception as e:
                entry["orderflow"] = f"error: {e}"[:40]
        context[sym] = entry
    return context


def describe_flow(f):
    """The Order-Flow Agent's read in words: score, bias, key levels, liquidity and main risk."""
    if not f:
        return None
    levels = [f"{name} wall {w[0]:.6g} ({w[1]:.4g}){'' if held is None else ' held' if held else ' PULLED'}"
              for name, w, held in (("bid", f.get("bid_wall"), f.get("bid_wall_held")),
                                    ("ask", f.get("ask_wall"), f.get("ask_wall_held"))) if w]
    return (f"Order flow {f['score']}/100 {f['bias']} | key levels: {', '.join(levels) or 'none'} | "
            f"liquidity {f.get('liquidity', 'single snapshot')} | main risk: {f.get('main_risk', 'walls can vanish')}")


def format_board(cfg, day, scores, series, index, sources, errors, context, state):
    names = {m["symbol"]: m["name"] for m in cfg["markets"]}
    lines = [f"Leaderboard for the {day} daily close (paper only, threshold {cfg['min_score']:.2f})",
             f"{'#':<3} {'Market':<26} {'Score':>5} {'Dir':<5} {'Trend':>5} {'Range':>5} {'Volat':>5} "
             f"{'Volume':>6} {'NotExt':>6}  {'Order flow':<12} {'Data':<16} Gamma"]
    ranked = sorted(scores.values(), key=lambda s: (-s["score"], s["symbol"]))
    for n, s in enumerate(ranked, 1):
        p = s["parts"]
        cells = [f"{p[k]:.2f}" if p[k] is not None else "n/a" for k in ("trend", "range", "volatility", "volume",
                                                                         "extension")]
        ctx = context.get(s["symbol"], {})
        data = f"{sources.get(s['symbol'], '?')} {s['date'][5:]}"
        lines.append(f"{n:<3} {s['symbol'] + ' ' + names.get(s['symbol'], ''):<26.26} {s['score']:>5.2f} "
                     f"{s['direction']:<5} {cells[0]:>5} {cells[1]:>5} {cells[2]:>5} {cells[3]:>6} {cells[4]:>6}  "
                     f"{ctx.get('orderflow', '-'):<12.12} {data:<16.16} {ctx.get('gamma', NO_GAMMA)}")
    if ranked:
        top = ranked[0]
        verdict = "passes" if top["score"] >= cfg["min_score"] else "is below"
        lines.append(f"Top setup: {top['symbol']} {top['direction']} | score {top['score']:.2f} {verdict} the "
                     f"threshold | entry zone: next open near {top['close']:.6g} | stop {top['stop']:.6g} | "
                     f"target {top['target']:.6g} ({cfg['target_r']:g}R) | why: {top['why']}")
        flow_text = describe_flow(context.get(top["symbol"], {}).get("orderflow_detail"))
        if flow_text:
            lines.append(f"           {flow_text}")
    for m in cfg["markets"]:
        sym = m["symbol"]
        if sym in scores:
            continue
        label = f"{sym} {m['name']}"
        if sym in errors:
            note = f"ERROR {errors[sym]}"
        elif sym not in index:
            note = "no data"
        elif day not in index[sym]:
            note = f"no candle for {day} (last {series[sym]['dates'][-1]})"
        else:
            note = "not enough history yet (needs about 210 daily candles)"
        lines.append(f"--  {label:<26.26} {note[:90]}")
    pos, pending = state.get("position"), state.get("pending")
    lines.append(f"Paper account ${state['equity']:,.2f} | open: "
                 + (f"#{pos['id']} {pos['symbol']} {pos['direction']} from {pos['entry']:.6g}" if pos else "none")
                 + " | waiting to fill: " + (f"{pending['symbol']} {pending['direction']}" if pending else "none"))
    used = sorted({v for v in sources.values()})
    lines.append(f"Data: {', '.join(used) or 'none'}. Order flow is a single snapshot, context only; walls can vanish.")
    return "\n".join(lines)


SPHERE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Sphere</title>
<style>
:root { --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --line:#d9d9d4; --long:#1a7f4b; --short:#c0392b; --top:#d4a017; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --fg:#ecebe6; --muted:#9a9993; --line:#33332f;
  --long:#3fbf7f; --short:#e8665a; --top:#f2c94c; } }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }
main { max-width:960px; margin:0 auto; padding:20px 16px 40px; }
h1 { font-size:20px; margin:0 0 4px; } p { margin:0 0 12px; color:var(--muted); }
canvas { width:100%; max-width:560px; aspect-ratio:1; display:block; margin:8px auto; cursor:grab; touch-action:none; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:500; } tr.top td { font-weight:600; }
.wrap { overflow-x:auto; } .long { color:var(--long); } .short { color:var(--short); }
</style></head><body><main>
<h1>Market Sphere: __DAY__ daily close</h1>
<p>PAPER ONLY. Bigger, brighter points score higher; green is long, red is short; the ringed point is #1.
Threshold __MIN__. Drag to turn the sphere.</p>
<canvas id="c" width="1120" height="1120"></canvas>
<div class="wrap"><table><thead><tr><th>#</th><th>Market</th><th>Score</th><th>Direction</th><th>Order flow</th>
<th>Gamma</th><th>Data</th></tr></thead><tbody id="rows"></tbody></table></div>
</main><script>
const M = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const rows = document.getElementById("rows");
M.forEach((m, i) => { const tr = document.createElement("tr"); if (i === 0) tr.className = "top";
  [i + 1, m.name, m.score.toFixed(2), m.dir, m.flow, m.gamma, m.data].forEach((v, j) => {
    const td = document.createElement("td"); td.textContent = v;
    if (j === 3) td.className = m.dir === "LONG" ? "long" : "short"; tr.appendChild(td); });
  rows.appendChild(tr); });
const n = M.length, golden = Math.PI * (3 - Math.sqrt(5));
const pts = M.map((m, i) => { const y = n > 1 ? 1 - 2 * i / (n - 1) : 0, r = Math.sqrt(1 - y * y);
  return { ...m, x: Math.cos(golden * i) * r, y, z: Math.sin(golden * i) * r, top: i === 0 }; });
const cv = document.getElementById("c"), g = cv.getContext("2d");
let rotY = 0, rotX = -0.3, drag = null, auto = true;
function draw() {
  const W = cv.width, R = W * 0.36, cx = W / 2, cy = W / 2;
  g.clearRect(0, 0, W, W); g.strokeStyle = css("--line"); g.lineWidth = 2;
  g.beginPath(); g.arc(cx, cy, R, 0, 2 * Math.PI); g.stroke();
  const p = pts.map(q => { const x1 = q.x * Math.cos(rotY) + q.z * Math.sin(rotY), z1 = -q.x * Math.sin(rotY) + q.z * Math.cos(rotY);
    const y1 = q.y * Math.cos(rotX) - z1 * Math.sin(rotX), z2 = q.y * Math.sin(rotX) + z1 * Math.cos(rotX);
    return { ...q, sx: cx + x1 * R, sy: cy - y1 * R, d: z2 }; }).sort((a, b) => a.d - b.d);
  for (const q of p) { const front = (q.d + 1) / 2, size = 6 + 26 * q.score;
    g.globalAlpha = 0.25 + 0.75 * front * (0.4 + 0.6 * q.score);
    g.fillStyle = css(q.dir === "LONG" ? "--long" : "--short");
    g.beginPath(); g.arc(q.sx, q.sy, size, 0, 2 * Math.PI); g.fill();
    if (q.top) { g.globalAlpha = 1; g.strokeStyle = css("--top"); g.lineWidth = 5;
      g.beginPath(); g.arc(q.sx, q.sy, size + 9, 0, 2 * Math.PI); g.stroke(); }
    if (q.top || front > 0.55) { g.globalAlpha = q.top ? 1 : front; g.fillStyle = css("--fg");
      g.font = (q.top ? "600 " : "") + "24px system-ui"; g.fillText(q.sym, q.sx + size + 6, q.sy + 8); } }
  g.globalAlpha = 1; }
cv.addEventListener("pointerdown", e => { drag = [e.clientX, e.clientY]; auto = false; cv.setPointerCapture(e.pointerId); });
cv.addEventListener("pointermove", e => { if (!drag) return; rotY += (e.clientX - drag[0]) * 0.01;
  rotX += (e.clientY - drag[1]) * 0.01; drag = [e.clientX, e.clientY]; draw(); });
cv.addEventListener("pointerup", () => { drag = null; });
(function tick() { if (auto) { rotY += 0.004; draw(); } requestAnimationFrame(tick); })();
draw();
</script></body></html>
"""


def write_sphere(path, cfg, day, scores, sources, context):
    """Step 9's visual: every ranked market as a point on a sphere, #1 highlighted. Opens in any browser."""
    names = {m["symbol"]: m["name"] for m in cfg["markets"]}
    ranked = sorted(scores.values(), key=lambda s: (-s["score"], s["symbol"]))
    data = [{"sym": s["symbol"], "name": f"{s['symbol']} {names.get(s['symbol'], '')}", "score": s["score"],
             "dir": s["direction"], "flow": context.get(s["symbol"], {}).get("orderflow", "-"),
             "gamma": context.get(s["symbol"], {}).get("gamma", NO_GAMMA),
             "data": f"{sources.get(s['symbol'], '?')} {s['date']}"} for s in ranked]
    html = (SPHERE_HTML.replace("__DAY__", day).replace("__MIN__", f"{cfg['min_score']:.2f}")
            .replace("__DATA__", json.dumps(data).replace("</", "<\\/")))
    with open(path, "w") as f:
        f.write(html)


def run_live(cfg, now, notify, fetch=marketdata.get_candles, flow=marketdata.coinbase_orderflow,
             state_path=STATE_PATH, log_dir=LOG_DIR, advance=True):
    through = settled_date(now, cfg["settle_hours"])
    candles, sources, errors = load_universe(cfg, now, through, cfg["history_days"], fetch)
    series, index = index_series(candles)
    state = load_json(state_path, None) or paper.new_state(cfg)
    dates = sorted({d for s in series.values() for d in s["dates"]})
    todo = [d for d in dates if d > (state["last_date"] or "")] if advance else []
    if advance and state["last_date"] is None:
        todo = todo[-1:]
    context = live_context(cfg, flow)
    log = EventLog(log_dir, notify)
    for problem in sorted(errors.items()):
        print(f"[DATA] {problem[0]}: {problem[1]}", file=sys.stderr)
    engine = paper.PaperEngine(cfg, state, log)
    for day in todo:
        bars, scores = bars_and_scores(series, index, day, cfg)
        engine.step(day, bars, scores, context if day == todo[-1] else None)
        log.scores(day, scores, context if day == todo[-1] else {})
    if advance:
        log.trades(state)
        save_json(state_path, state)
    board_day = todo[-1] if todo else (dates[-1] if dates else through)
    _, scores = bars_and_scores(series, index, board_day, cfg)
    print(format_board(cfg, board_day, scores, series, index, sources, errors, context, state))
    os.makedirs(log_dir, exist_ok=True)
    write_sphere(os.path.join(log_dir, "sphere.html"), cfg, board_day, scores, sources, context)
    print(f"Sphere view: {os.path.join(log_dir, 'sphere.html')} (open it in a browser)")
    return state


def print_report(report):
    rows = [("All", report["all"]), ("First 70%", report["first_70pct"]), ("Last 30%", report["last_30pct"])]
    print(f"{'Period':<10} {'Trades':>6} {'Win%':>6} {'AvgR':>6} {'AvgWinR':>7} {'AvgLossR':>8} {'PF':>5} "
          f"{'MaxDD':>7} {'Sharpe':>6} {'Return':>7}")

    def fmt(v, spec, scale=1):
        return format(v * scale, spec) if v is not None else "n/a"

    for name, m in rows:
        print(f"{name:<10} {m['trades']:>6} {fmt(m['win_rate'], '6.1f', 100)} {fmt(m['avg_r'], '+6.2f')} "
              f"{fmt(m['avg_win_r'], '+7.2f')} {fmt(m['avg_loss_r'], '+8.2f')} {fmt(m['profit_factor'], '5.2f')} "
              f"{fmt(m['max_drawdown'], '6.1f', 100)}% {fmt(m['sharpe'], '6.2f')} {fmt(m['total_return'], '+6.1f', 100)}%")


def run_backtest(cfg, now, years, fetch=marketdata.get_candles, log_dir=LOG_DIR):
    through = settled_date(now, cfg["settle_hours"])
    start = (Date.fromisoformat(through) - timedelta(days=round(365 * years))).isoformat()
    candles, sources, errors = load_universe(cfg, now, through, round(365 * years) + 400, fetch)
    for sym, problem in sorted(errors.items()):
        print(f"[DATA] {sym} left out: {problem}", file=sys.stderr)
    if not candles:
        sys.exit("No market data could be downloaded, so there is nothing to backtest.")
    state = replay(cfg, candles, start)
    curve, trades = state["curve"], state["closed"]
    split = curve[int(len(curve) * 0.7)][0] if curve else start
    first = [p for p in curve if p[0] < split]
    report = {
        "period": [start, through], "markets": sorted(candles), "left_out": errors, "settings": {
            k: cfg[k] for k in ("weights", "min_score", "risk_per_trade", "daily_loss_limit", "stop_atr", "target_r",
                                "max_hold_days", "fee_bps", "slippage_bps", "starting_equity")},
        "all": paper.summarize(trades, curve, cfg["starting_equity"]),
        "first_70pct": paper.summarize([t for t in trades if t["entry_date"] < split], first, cfg["starting_equity"]),
        "last_30pct": paper.summarize([t for t in trades if t["entry_date"] >= split],
                                      [p for p in curve if p[0] >= split],
                                      first[-1][1] if first else cfg["starting_equity"]),
    }
    os.makedirs(log_dir, exist_ok=True)
    trade_path = os.path.join(log_dir, "backtest_trades.csv")
    if os.path.exists(trade_path):
        os.remove(trade_path)
    EventLog(log_dir, lambda *a: None).trades(dict(state, logged_trades=0), trade_path)
    save_json(os.path.join(log_dir, "backtest_summary.json"), report)
    print(f"Backtest {start} to {through}: {len(candles)} markets, paper only, ${cfg['starting_equity']:,} start, "
          f"fees {cfg['fee_bps']} bps + slippage {cfg['slippage_bps']} bps per side. Weights were not tuned.")
    print_report(report)
    print(f"Trades: {trade_path}\nSummary: {os.path.join(log_dir, 'backtest_summary.json')}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Paper-only multi-market ranking terminal.")
    parser.add_argument("command", choices=["scan", "run", "backtest", "test-alert"])
    parser.add_argument("--loop", action="store_true", help="with run: keep running, checking every loop_minutes")
    parser.add_argument("--years", type=float, default=3.0, help="with backtest: how many years to replay")
    args = parser.parse_args(argv)
    load_env()
    cfg = load_config()
    notify = make_notifier()
    if args.command == "test-alert":
        if not os.environ.get("NTFY_TOPIC"):
            sys.exit("Set NTFY_TOPIC in .env first (see .env.example).")
        notify("PAPER test", "Alerts from your market scanner will show up here.")
        return 0
    if args.command == "backtest":
        run_backtest(cfg, time.time(), args.years)
        return 0
    while True:
        try:
            run_live(cfg, time.time(), notify, advance=args.command == "run")
        except Exception:
            traceback.print_exc()
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(cfg["loop_minutes"] * 60)


if __name__ == "__main__":
    sys.exit(main())
