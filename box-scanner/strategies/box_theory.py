"""Box theory signals: Darvas breakouts and trading-range reversion.

Everything here is pure: arrays in, signals out. No network, no clock. The same functions drive the live scanner
and the backtest, so what the backtest measures is exactly what the bot trades.

Inputs are fully CLOSED candles only (the data layer enforces that). Signals are evaluated on the latest bar, i.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

CENTRAL = "America/Chicago"


@dataclass(frozen=True)
class BoxParams:
    lookback_high: int = 50
    confirm_bars: int = 3
    min_box_bars: int = 5
    min_box_pct: float = 0.03
    max_box_pct: float = 0.15
    breakout_buffer: float = 0.002
    volume_multiple: float = 1.5
    volume_avg_bars: int = 20
    trend_filter: bool = True
    target_multiple: float = 2.0
    max_search_bars: int = 250


@dataclass(frozen=True)
class RangeParams:
    range_lookback: int = 252
    zone_pct: float = 0.25


def params_for(cfg: Dict[str, Any], asset_class: str) -> "tuple[BoxParams, RangeParams]":
    b, r = cfg["box"], cfg["range"]
    box = BoxParams(b["lookback_high"], b["confirm_bars"], b["min_box_bars"], b["min_box_pct"],
                    b["max_box_pct"][asset_class], b["breakout_buffer"], b["volume_multiple"], b["volume_avg_bars"],
                    b["trend_filter"], cfg["risk"]["target_multiple"], b.get("max_search_bars", 250))
    return box, RangeParams(r["range_lookback"][asset_class], r["zone_pct"])


@dataclass
class Box:
    top: float
    bottom: float
    top_index: int
    bottom_index: int
    bars: int

    @property
    def height_pct(self) -> float:
        return (self.top - self.bottom) / self.bottom


@dataclass
class Signal:
    """One actionable signal. direction: long, short, or exit (close an existing long)."""
    symbol: str
    asset_class: str
    timeframe: str
    strategy: str
    direction: str
    box_top: float
    box_bottom: float
    box_height_pct: float
    close: float
    volume_ratio: float
    stop: float
    target: float
    reward_risk: float
    bar_close_utc: datetime
    note: str = ""
    rank: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Same symbol + same box + same direction = same signal: never alerted or traded twice."""
        return (f"{self.symbol}|{self.timeframe}|{self.strategy}|{self.direction}|"
                f"{self.box_top:.6g}|{self.box_bottom:.6g}")

    @property
    def time_utc(self) -> str:
        return self.bar_close_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @property
    def time_central(self) -> str:
        return pd.Timestamp(self.bar_close_utc).tz_convert(CENTRAL).strftime("%Y-%m-%d %H:%M %Z")

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.update(bar_close_utc=self.time_utc, bar_close_central=self.time_central, key=self.key)
        return d


class Bars:
    """Closed candles as numpy arrays plus the indicators every rule needs, computed once."""

    def __init__(self, df: pd.DataFrame, volume_avg_bars: int = 20) -> None:
        self.times = list(df.index)  # bar CLOSE times, UTC
        self.o, self.h, self.l, self.c, self.v = (df[k].to_numpy(dtype=float) for k in
                                                  ("open", "high", "low", "close", "volume"))
        close = df["close"].astype(float)
        self.sma50 = close.rolling(50).mean().to_numpy()
        self.sma200 = close.rolling(200).mean().to_numpy()
        # average volume of the N bars BEFORE each bar (the bar itself is excluded)
        self.vol_avg = df["volume"].astype(float).rolling(volume_avg_bars).mean().shift(1).to_numpy()
        self._rollmax: Dict[int, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.c)

    def rolling_high(self, n: int) -> np.ndarray:
        if n not in self._rollmax:
            self._rollmax[n] = pd.Series(self.h).rolling(n).max().to_numpy()
        return self._rollmax[n]

    def volume_ratio(self, i: int) -> float:
        avg = self.vol_avg[i]
        return float(self.v[i] / avg) if avg and avg == avg and avg > 0 else 0.0


# ---------------------------------------------------------------- Darvas boxes

def find_box(bars: Bars, end: int, p: BoxParams) -> Optional[Box]:
    """The most recent valid Darvas box built from bars up to and including `end`.

    Top: the latest high that is the highest high of the `lookback_high` bars ending there and that no bar since has
    exceeded (at least `confirm_bars` bars must follow it). If that top has no valid box (bottom not confirmed yet,
    bottom undercut, too short, too shallow or too tall), there is no box: an older top never counts once a newer
    one has formed.
    Bottom: after the top, the first running low that the next `confirm_bars` bars don't undercut.
    """
    h, l, C = bars.h, bars.l, p.confirm_bars
    rmax = bars.rolling_high(p.lookback_high)
    later_max = -np.inf  # highest high after candidate t, up to end
    for k in range(end, end - C, -1):
        if k >= 0:
            later_max = max(later_max, h[k])
    for t in range(end - C, max(p.lookback_high - 2, end - p.max_search_bars), -1):
        if t < 0:
            return None
        if h[t] >= rmax[t] and h[t] >= later_max:
            return _box_from_top(bars, t, end, p)
        later_max = max(later_max, h[t])
    return None


def _box_from_top(bars: Bars, t: int, end: int, p: BoxParams) -> Optional[Box]:
    l, C, top = bars.l, p.confirm_bars, bars.h[t]
    running = np.inf
    for j in range(t + 1, end - C + 1):
        running = min(running, l[j])
        if l[j] <= running and l[j + 1:j + 1 + C].min() >= l[j]:
            bottom = l[j]
            if l[j:end + 1].min() < bottom:
                return None  # bottom already undercut: the box broke down
            box = Box(top, bottom, t, j, end - t + 1)
            if box.bars < p.min_box_bars or not p.min_box_pct <= box.height_pct <= p.max_box_pct:
                return None
            return box
    return None  # bottom not confirmed yet: box still forming


def darvas_signals(bars: Bars, i: int, p: BoxParams, meta: Dict[str, str], allow_short: bool = False) -> List[Signal]:
    """Signals on bar i from the box formed by bars up to i - 1."""
    if i < max(p.lookback_high, 2):
        return []
    box = find_box(bars, i - 1, p)
    if box is None:
        return []
    c, prev = bars.c[i], bars.c[i - 1]
    height = box.top - box.bottom
    vr = bars.volume_ratio(i)
    common = dict(box_top=box.top, box_bottom=box.bottom, box_height_pct=box.height_pct, close=c, volume_ratio=vr,
                  bar_close_utc=bars.times[i], extra={"box_bars": box.bars}, **meta)
    out: List[Signal] = []
    if prev <= box.top and c > box.top * (1 + p.breakout_buffer) and vr >= p.volume_multiple:
        trend_ok = (not p.trend_filter) or (bars.sma200[i] == bars.sma200[i] and c > bars.sma50[i] > bars.sma200[i])
        if trend_ok:
            stop, target = box.bottom * 0.995, box.top + height * p.target_multiple
            if c > stop:
                out.append(Signal(direction="long", stop=stop, target=target,
                                  reward_risk=(target - c) / (c - stop), note="Darvas breakout", **common))
    elif c < box.bottom:
        out.append(Signal(direction="exit", stop=float("nan"), target=float("nan"), reward_risk=float("nan"),
                          note="close below box bottom", **common))
        if allow_short and meta["asset_class"] == "stocks":
            stop, target = box.top * 1.005, box.bottom - height * p.target_multiple
            if stop > c > target > 0:
                out.append(Signal(direction="short", stop=stop, target=target,
                                  reward_risk=(c - target) / (stop - c), note="Darvas breakdown", **common))
    return out


# ---------------------------------------------------------------- trading-range reversion

def range_box(bars: Bars, i: int, p: RangeParams) -> Optional["tuple[float, float]"]:
    """(high, low) of the `range_lookback` bars before bar i."""
    if i < p.range_lookback:
        return None
    return float(bars.h[i - p.range_lookback:i].max()), float(bars.l[i - p.range_lookback:i].min())


def range_signals(bars: Bars, i: int, p: RangeParams, meta: Dict[str, str], **_: Any) -> List[Signal]:
    rb = range_box(bars, i, p)
    if rb is None:
        return []
    hi, lo = rb
    if hi <= lo:
        return []
    zone = (hi - lo) * p.zone_pct
    bottom_zone, top_zone = lo + zone, hi - zone
    o, h, l, c = bars.o[i], bars.h[i], bars.l[i], bars.c[i]
    common = dict(box_top=hi, box_bottom=lo, box_height_pct=(hi - lo) / lo, close=c, volume_ratio=bars.volume_ratio(i),
                  bar_close_utc=bars.times[i], extra={"bottom_zone": bottom_zone, "top_zone": top_zone}, **meta)
    out: List[Signal] = []
    if l <= bottom_zone and c > o and c > bars.c[i - 1] and c <= bottom_zone:  # confirmed bounce, still in the zone
        stop, target = lo * 0.99, top_zone
        if target > c > stop:
            out.append(Signal(direction="long", stop=stop, target=target, reward_risk=(target - c) / (c - stop),
                              note="bounce off the bottom zone", **common))
    if h >= top_zone:
        out.append(Signal(direction="exit", stop=float("nan"), target=float("nan"), reward_risk=float("nan"),
                          note="reached the top zone", **common))
    return out


STRATEGIES = {"darvas_breakout": darvas_signals, "range_reversion": range_signals}


def evaluate(df: pd.DataFrame, strategy: str, symbol: str, asset_class: str, timeframe: str, cfg: Dict[str, Any],
             i: Optional[int] = None, bars: Optional[Bars] = None) -> List[Signal]:
    """Signals for one symbol on its latest closed bar (or on bar i, for the backtest)."""
    box_p, range_p = params_for(cfg, asset_class)
    bars = bars or Bars(df, box_p.volume_avg_bars)
    if len(bars) < 3:
        return []
    i = len(bars) - 1 if i is None else i
    meta = {"symbol": symbol, "asset_class": asset_class, "timeframe": timeframe, "strategy": strategy}
    if strategy == "darvas_breakout":
        return darvas_signals(bars, i, box_p, meta, allow_short=cfg.get("allow_short", False))
    return range_signals(bars, i, range_p, meta)


def rank(signals: List[Signal]) -> List[Signal]:
    """Best first: higher volume ratio, then tighter box, then better reward:risk."""
    def rr(s: Signal) -> float:
        return s.reward_risk if s.reward_risk == s.reward_risk else 0.0
    ordered = sorted(signals, key=lambda s: (-s.volume_ratio, s.box_height_pct, -rr(s)))
    for n, s in enumerate(ordered, 1):
        s.rank = n
    return ordered
