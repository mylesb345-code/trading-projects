"""Market data for the scanner: daily candles from Coinbase and Yahoo Finance, plus a Coinbase order-flow snapshot.

Candles are normalized to (date "YYYY-MM-DD", open, high, low, close, volume), dated in the market's own time zone,
so a Monday gold bar and a Monday BTC bar line up on the same date.
"""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DAY = 86400
USER_AGENT = "Mozilla/5.0 (compatible; paper-trading-research/1.0)"


def http_get_json(url, tries=3, wait=2.0, sleep=time.sleep):
    """GET JSON, retrying dropped connections, timeouts and rate limits (HTTP 429/5xx) with a growing pause."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == tries:
                raise
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError):
            if attempt == tries:
                raise
        sleep(wait * attempt)


def day_of(t, offset=0):
    return datetime.fromtimestamp(t + offset, timezone.utc).strftime("%Y-%m-%d")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_coinbase(symbol, days, now, get=http_get_json):
    """Daily candles from Coinbase Exchange (UTC days), paged 300 at a time (its per-request limit)."""
    rows = {}
    end = int(now // DAY) * DAY
    for _ in range(-(-days // 300)):
        start = end - 299 * DAY
        page = get(f"https://api.exchange.coinbase.com/products/{symbol}/candles"
                   f"?granularity=86400&start={iso(start)}&end={iso(end)}")
        for t, low, high, open_, close, volume in page:
            rows[day_of(int(t))] = (float(open_), float(high), float(low), float(close), float(volume))
        if len(page) < 300:
            break
        end = start - DAY
        time.sleep(0.3)
    return rows


def fetch_yahoo(symbol, days, now, get=http_get_json):
    """Daily candles from Yahoo Finance's chart endpoint, dated in the exchange's time zone."""
    span = "2y" if days <= 730 else "5y" if days <= 1825 else "10y"
    data = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
               f"?range={span}&interval=1d")
    chart = data.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo error: {chart['error']}")
    result = chart["result"][0]
    offset = int(result.get("meta", {}).get("gmtoffset") or 0)
    quote = result["indicators"]["quote"][0]
    rows = {}
    for i, t in enumerate(result.get("timestamp") or []):
        o, h, l, c, v = (quote[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        rows[day_of(int(t), offset)] = (float(o), float(h), float(l), float(c), float(v or 0))
    return rows


SOURCES = {"coinbase": fetch_coinbase, "yahoo": fetch_yahoo}
FALLBACKS = {"coinbase": ["coinbase", "yahoo"], "yahoo": ["yahoo"]}


def get_candles(market, days, now, through_date, sources=SOURCES):
    """Candles up to and including through_date, oldest first. Tries the market's source, then its fallbacks."""
    problems = []
    for name in FALLBACKS.get(market["source"], [market["source"]]):
        try:
            rows = sources[name](market["symbol"], days, now)
            candles = sorted((d,) + v for d, v in rows.items() if d <= through_date)
            if not candles:
                raise RuntimeError("no candles")
            return candles, name
        except Exception as e:
            problems.append(f"{name}: {e}")
    raise RuntimeError("; ".join(problems))


def coinbase_orderflow(symbol, depth_pct=0.5, n_trades=200, earlier=None, get=http_get_json):
    """One snapshot of the Coinbase order book and recent trades. Context only: walls can be pulled at any time.

    Pass an earlier result for the same market as `earlier` to see whether liquidity near the price grew or shrank in
    between, and whether the biggest bid and ask walls are still there.
    """
    book = get(f"https://api.exchange.coinbase.com/products/{symbol}/book?level=2")
    bids = [(float(p), float(s)) for p, s, *_ in book["bids"]]
    asks = [(float(p), float(s)) for p, s, *_ in book["asks"]]
    if not bids or not asks:
        raise RuntimeError("empty order book")
    mid = (bids[0][0] + asks[0][0]) / 2
    near_bids = [b for b in bids if b[0] >= mid * (1 - depth_pct / 100)]
    near_asks = [a for a in asks if a[0] <= mid * (1 + depth_pct / 100)]
    bid_size, ask_size = sum(s for _, s in near_bids), sum(s for _, s in near_asks)
    imbalance = (bid_size - ask_size) / (bid_size + ask_size) if bid_size + ask_size else 0.0
    trades = get(f"https://api.exchange.coinbase.com/products/{symbol}/trades?limit={n_trades}")
    # Coinbase reports the resting (maker) order's side, so side "sell" means an aggressive buyer took it.
    taken_by_buyers = sum(float(t["size"]) for t in trades if t["side"] == "sell")
    total = sum(float(t["size"]) for t in trades)
    buy_ratio = taken_by_buyers / total if total else 0.5
    score = round(max(0, min(100, 50 + 25 * imbalance + 25 * (2 * buy_ratio - 1))))
    result = {
        "score": score,
        "bias": "BULLISH" if score >= 60 else "BEARISH" if score <= 40 else "NEUTRAL",
        "imbalance": imbalance,
        "aggressive_buy_ratio": buy_ratio,
        "bid_wall": max(near_bids, key=lambda b: b[1]) if near_bids else None,
        "ask_wall": max(near_asks, key=lambda a: a[1]) if near_asks else None,
        "mid": mid,
        "depth": bid_size + ask_size,
        "near_bids": near_bids,
        "near_asks": near_asks,
    }
    result.update(compare_books(earlier, result))
    return result


def compare_books(old, new):
    """How the book changed between two snapshots: liquidity appearing or disappearing, and walls held or pulled."""
    if not old:
        return {"liquidity": "single snapshot", "bid_wall_held": None, "ask_wall_held": None,
                "main_risk": "single snapshot: walls can be pulled at any moment"}
    change = new["depth"] / old["depth"] - 1 if old["depth"] else 0.0
    trend = "appearing" if change >= 0.10 else "disappearing" if change <= -0.10 else "steady"

    def held(wall, side):
        return None if wall is None else any(p == wall[0] and s >= 0.5 * wall[1] for p, s in side)

    bid_held, ask_held = held(old["bid_wall"], new["near_bids"]), held(old["ask_wall"], new["near_asks"])
    risks = [f"{name} wall at {wall[0]:.6g} was pulled" for name, wall, ok in
             (("bid", old["bid_wall"], bid_held), ("ask", old["ask_wall"], ask_held)) if ok is False]
    if trend == "disappearing":
        risks.append("liquidity near the price is thinning")
    return {"liquidity": f"{trend} ({change:+.0%})", "bid_wall_held": bid_held, "ask_wall_held": ask_held,
            "main_risk": "; ".join(risks) or "walls held for now, but can still be pulled; one exchange only"}
