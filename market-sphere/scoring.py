"""Indicators and the 0.00-1.00 market score. Pure functions on daily candles (date, open, high, low, close, volume)."""

COMPONENTS = ("trend", "range", "volatility", "volume", "extension")
LABELS = {"trend": "trend", "range": "range position", "volatility": "volatility", "volume": "volume",
          "extension": "not overextended"}
SLOPE_LOOKBACK = 10


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


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


def rolling_mean(values, period):
    out = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def rolling_extreme(values, period, pick):
    return [pick(values[i - period + 1:i + 1]) if i >= period - 1 else None for i in range(len(values))]


def rolling_pct_rank(values, window, min_count):
    """Share of the last `window` values that are at or below the current one."""
    out = [None] * len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        recent = [x for x in values[max(0, i - window + 1):i + 1] if x is not None]
        if len(recent) >= min_count:
            out[i] = sum(1 for x in recent if x <= v) / len(recent)
    return out


def true_ranges(candles):
    out = []
    for i, (_, _, high, low, _, _) in enumerate(candles):
        if i == 0:
            out.append(high - low)
        else:
            prev = candles[i - 1][4]
            out.append(max(high - low, abs(high - prev), abs(low - prev)))
    return out


def build_series(candles):
    closes = [c[4] for c in candles]
    atr = rolling_mean(true_ranges(candles), 14)
    volumes = [c[5] for c in candles]
    atr_pct = [a / c if a is not None and c > 0 else None for a, c in zip(atr, closes)]
    return {
        "dates": [c[0] for c in candles],
        "candles": candles,
        "close": closes,
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "ema200": ema(closes, 200),
        "atr": atr,
        "hi55": rolling_extreme([c[2] for c in candles], 55, max),
        "lo55": rolling_extreme([c[3] for c in candles], 55, min),
        "vol20": rolling_mean(volumes, 20),
        "vol100": rolling_mean(volumes, 100),
        "atr_rank": rolling_pct_rank(atr_pct, 250, 60),
    }


def score_at(s, i, weights, stop_atr, target_r):
    """Score one market on candle i. Returns None until there is enough history (about 210 daily candles)."""
    if i < SLOPE_LOOKBACK:
        return None
    needed = (s["ema200"][i], s["ema50"][i - SLOPE_LOOKBACK], s["atr"][i], s["hi55"][i], s["atr_rank"][i])
    if any(v is None for v in needed) or s["atr"][i] <= 0:
        return None
    c, e20, e50, e200, atr = s["close"][i], s["ema20"][i], s["ema50"][i], s["ema200"][i], s["atr"][i]
    sign = 1 if e50 > e200 else -1
    aligned = sum(sign * gap > 0 for gap in (c - e20, e20 - e50, e50 - e200)) / 3
    slope = sign * (e50 - s["ema50"][i - SLOPE_LOOKBACK]) / (SLOPE_LOOKBACK * atr)
    hi, lo = s["hi55"][i], s["lo55"][i]
    location = (c - lo) / (hi - lo) if hi > lo else 0.5
    rank = s["atr_rank"][i]
    v20, v100 = s["vol20"][i], s["vol100"][i]
    stretch = abs(c - e20) / atr
    parts = {
        "trend": 0.6 * aligned + 0.4 * clamp(slope / 0.15),
        "range": location if sign > 0 else 1 - location,
        "volatility": 1.0 if 0.2 <= rank <= 0.8 else (rank / 0.2 if rank < 0.2 else (1 - rank) / 0.2),
        "volume": clamp((v20 / v100 - 0.8) / 0.5) if v20 and v100 else None,
        "extension": 1.0 if stretch <= 1 else clamp((3 - stretch) / 2),
    }
    score = sum(w * (parts[k] or 0.0) for k, w in weights.items()) / sum(weights.values())
    risk = stop_atr * atr
    return {
        "date": s["dates"][i],
        "direction": "LONG" if sign > 0 else "SHORT",
        "score": round(score, 4),
        "parts": parts,
        "missing": [k for k, v in parts.items() if v is None],
        "close": c,
        "atr": atr,
        "stop": c - sign * risk,
        "target": c + sign * risk * target_r,
        "why": explain(parts, weights),
    }


def explain(parts, weights):
    present = sorted(((weights[k] * v, k) for k, v in parts.items() if v is not None and k in weights), reverse=True)
    text = ", ".join(f"{LABELS[k]} {parts[k]:.2f}" for _, k in present[:2])
    missing = [LABELS[k] for k, v in parts.items() if v is None and k in weights]
    return text + (f"; missing {', '.join(missing)} (scored 0)" if missing else "")
