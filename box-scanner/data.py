"""Market data: Alpaca asset lists and bars, cached in SQLite, closed candles only.

Every DataFrame this module returns is indexed by the bar's CLOSE time in UTC, so the closed-candle rule is a single
comparison: a bar may be used only once its close time has passed.
"""
from __future__ import annotations

import logging
import re
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, TypeVar

import pandas as pd

log = logging.getLogger("box.data")
# alpaca-py sets no network timeout, so a dropped connection (e.g. the computer slept) can hang a scan forever.
# With this, a dead connection errors after 60s and with_retries() tries again.
socket.setdefaulttimeout(60)
NEW_YORK = "America/New_York"
TF_SECONDS = {"1Day": 86400, "4Hour": 14400}
T = TypeVar("T")


# ---------------------------------------------------------------- the closed-candle rule

def bar_close_time(start: pd.Timestamp, timeframe: str, asset_class: str) -> pd.Timestamp:
    """When a bar that STARTS at `start` is final. Stock daily bars close at 4:00 PM New York on their date
    (Alpaca stamps them at midnight New York); crypto bars close one bar-length after they start."""
    start = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start, tz="UTC")
    if asset_class == "stocks" and timeframe == "1Day":
        day = start.tz_convert(NEW_YORK).date()
        return pd.Timestamp(datetime(day.year, day.month, day.day, 16, 0), tz=NEW_YORK).tz_convert("UTC")
    return start + pd.Timedelta(seconds=TF_SECONDS[timeframe])


def closed_only(df: pd.DataFrame, now: Optional[datetime] = None) -> pd.DataFrame:
    """Drop any bar whose close time is still in the future: never trade a forming candle."""
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    return df[df.index <= now]


# ---------------------------------------------------------------- retries

def with_retries(fn: Callable[..., T], *args, tries: int = 6, base: float = 2.0, what: str = "request", **kw) -> T:
    """Call fn, backing off on rate limits (HTTP 429) and transient errors: 2s, 4s, 8s, ..."""
    for attempt in range(1, tries + 1):
        try:
            return fn(*args, **kw)
        except Exception as e:  # alpaca-py raises APIError/requests errors; both are retried the same way
            if attempt == tries:
                raise
            wait = base ** attempt * (2 if "429" in str(e) or "rate limit" in str(e).lower() else 1)
            log.warning("%s failed (%s: %s); retry %d/%d in %.0fs", what, type(e).__name__, str(e)[:120],
                        attempt, tries - 1, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- local candle cache

class BarCache:
    """Candles in SQLite so each scan downloads only what's new."""

    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS bars (symbol TEXT, timeframe TEXT, close_utc INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, timeframe, close_utc))""")
        # how far back each symbol has been downloaded (so a short first pass never passes for full history)
        self.db.execute("""CREATE TABLE IF NOT EXISTS coverage (symbol TEXT, timeframe TEXT, from_utc INTEGER,
            PRIMARY KEY (symbol, timeframe))""")
        self.db.commit()

    def covered_from(self, symbols: Iterable[str], timeframe: str) -> Dict[str, pd.Timestamp]:
        symbols = list(symbols)
        out: Dict[str, pd.Timestamp] = {}
        for k in range(0, len(symbols), 500):
            chunk = symbols[k:k + 500]
            q = f"SELECT symbol, from_utc FROM coverage WHERE timeframe=? AND symbol IN ({','.join('?' * len(chunk))})"
            for sym, ts in self.db.execute(q, [timeframe, *chunk]):
                out[sym] = pd.Timestamp(ts, unit="s", tz="UTC")
        return out

    def mark_covered(self, symbols: Iterable[str], timeframe: str, start: datetime) -> None:
        ts = int(pd.Timestamp(start).timestamp())
        self.db.executemany("""INSERT INTO coverage VALUES (?,?,?) ON CONFLICT(symbol, timeframe)
            DO UPDATE SET from_utc = MIN(from_utc, excluded.from_utc)""", [(s, timeframe, ts) for s in symbols])
        self.db.commit()

    def last_close(self, symbols: Iterable[str], timeframe: str) -> Dict[str, pd.Timestamp]:
        symbols = list(symbols)
        out: Dict[str, pd.Timestamp] = {}
        for k in range(0, len(symbols), 500):
            chunk = symbols[k:k + 500]
            q = f"SELECT symbol, MAX(close_utc) FROM bars WHERE timeframe=? AND symbol IN ({','.join('?' * len(chunk))}) GROUP BY symbol"
            for sym, ts in self.db.execute(q, [timeframe, *chunk]):
                out[sym] = pd.Timestamp(ts, unit="s", tz="UTC")
        return out

    def put(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        rows = [(symbol, timeframe, int(ts.timestamp()), r.open, r.high, r.low, r.close, r.volume)
                for ts, r in df.iterrows()]
        self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def get(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        rows = self.db.execute("""SELECT close_utc, open, high, low, close, volume FROM bars WHERE symbol=? AND
            timeframe=? ORDER BY close_utc DESC LIMIT ?""", (symbol, timeframe, limit)).fetchall()
        df = pd.DataFrame(rows[::-1], columns=["close_utc", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("close_utc"), unit="s", utc=True)
        return df


# ---------------------------------------------------------------- Alpaca market data

class MarketData:
    """Bars for stocks and crypto via alpaca-py, batched, retried and cached."""

    def __init__(self, creds, cfg: Dict, cache: BarCache) -> None:
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        self.stocks = StockHistoricalDataClient(creds.api_key, creds.secret_key)
        self.crypto = CryptoHistoricalDataClient(creds.api_key, creds.secret_key)
        self.feed, self.cfg, self.cache = creds.data_feed, cfg, cache

    def bars_needed(self, asset_class: str) -> int:
        """Enough bars for SMA200, the range lookback and the box search."""
        return max(self.cfg["data"]["min_bars"], self.cfg["range"]["range_lookback"][asset_class] + 5, 260)

    def _fetch(self, symbols: List[str], asset_class: str, timeframe: str, start: datetime) -> Dict[str, pd.DataFrame]:
        """Bars from `start` to now. Intraday crypto history comes in 60-day pieces: Alpaca answers a 30-day 4-hour
        request in about a second but takes nearly a minute for 400 days, and far longer for years."""
        if timeframe == "1Day":
            return self._fetch_range(symbols, asset_class, timeframe, start, None)
        pieces: List[Dict[str, pd.DataFrame]] = []
        now = datetime.now(timezone.utc)
        cursor = pd.Timestamp(start).to_pydatetime()
        while cursor < now:
            end = min(cursor + timedelta(days=60), now)
            pieces.append(self._fetch_range(symbols, asset_class, timeframe, cursor, end))
            cursor = end
        out: Dict[str, pd.DataFrame] = {}
        for piece in pieces:
            for sym, df in piece.items():
                out[sym] = pd.concat([out[sym], df]) if sym in out else df
        return {s: df[~df.index.duplicated(keep="last")].sort_index() for s, df in out.items()}

    def _fetch_range(self, symbols: List[str], asset_class: str, timeframe: str, start: datetime,
                     end: Optional[datetime]) -> Dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        tf = TimeFrame.Day if timeframe == "1Day" else TimeFrame(4, TimeFrameUnit.Hour)
        if asset_class == "stocks":
            req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start, end=end,
                                   adjustment=Adjustment.ALL, feed=DataFeed.SIP if self.feed == "sip" else DataFeed.IEX)
            resp = with_retries(self.stocks.get_stock_bars, req, what=f"stock bars ({len(symbols)} symbols)")
        else:
            req = CryptoBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start, end=end)
            resp = with_retries(self.crypto.get_crypto_bars, req, what=f"crypto bars ({len(symbols)} symbols)")
        raw = resp.df
        out: Dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return out
        for sym, df in raw.groupby(level=0):
            df = df.droplevel(0)[["open", "high", "low", "close", "volume"]].astype(float)
            df.index = [bar_close_time(ts, timeframe, asset_class) for ts in df.index]
            out[sym] = df
        return out

    def update(self, symbols: List[str], asset_class: str, timeframe: str, bars: int) -> None:
        """Download what's missing for each symbol: everything the first time, only new candles afterwards."""
        if not symbols:
            return
        span = timedelta(seconds=TF_SECONDS[timeframe] * bars * (1.6 if asset_class == "stocks" else 1.05))
        cold_start = datetime.now(timezone.utc) - span - timedelta(days=5)
        last = self.cache.last_close(symbols, timeframe)
        covered = self.cache.covered_from(symbols, timeframe)
        batch = self.cfg["data"]["batch_size"] if asset_class == "stocks" else 50
        # cold = never downloaded far enough back; warm = full history cached, only new candles needed
        cold = [s for s in symbols if s not in last or s not in covered or covered[s] > pd.Timestamp(cold_start)]
        warm = [s for s in symbols if s not in set(cold)]
        jobs = [(cold, cold_start, True)]
        if warm:
            since = min(last[s] for s in warm).to_pydatetime() - timedelta(seconds=TF_SECONDS[timeframe] * 2)
            jobs.append((warm, since, False))
        for group, start, backfill in jobs:
            for k in range(0, len(group), batch):
                chunk = group[k:k + batch]
                try:
                    for sym, df in self._fetch(chunk, asset_class, timeframe, start).items():
                        self.cache.put(sym, timeframe, df)
                    if backfill:
                        self.cache.mark_covered(chunk, timeframe, start)
                except Exception as e:
                    log.error("bars for %d %s symbols failed after retries: %s", len(chunk), asset_class, e)

    def closed_bars(self, symbols: List[str], asset_class: str, timeframe: str, bars: Optional[int] = None,
                    refresh: bool = True) -> Dict[str, pd.DataFrame]:
        """{symbol: DataFrame of CLOSED bars}, newest last."""
        bars = bars or self.bars_needed(asset_class)
        if refresh:
            self.update(symbols, asset_class, timeframe, bars)
        now = datetime.now(timezone.utc)
        out = {}
        for sym in symbols:
            df = closed_only(self.cache.get(sym, timeframe, bars + 5), now).iloc[-bars:]
            if len(df):
                out[sym] = df
        return out


# ---------------------------------------------------------------- universes

LEVERAGED_INVERSE_VOL = re.compile(r"(?i)\b(ultra\w*|short|inverse|bear|bull|leveraged|vix|volatility|-?[1-4]x)\b")


def is_leveraged_inverse_or_volatility(name: str) -> bool:
    """2x/3x, inverse/short and VIX/volatility products, judged by the fund's name
    (e.g. "ProShares UltraPro Short QQQ", "Direxion Daily Semiconductor Bear 3X Shares", "... VIX Short-Term ETN")."""
    return bool(LEVERAGED_INVERSE_VOL.search(name or ""))


excluded_funds: set = set()  # filled by stock_universe(): funds the strategy must never hold


def raw_assets(trading, asset_class: str) -> List[Dict]:
    """Active assets as plain dicts. Read raw on purpose: alpaca-py rejects the whole list whenever Alpaca adds
    something its models don't know yet (e.g. the new "TXSE" Texas Stock Exchange in 2026)."""
    return with_retries(trading.get, "/assets", {"status": "active", "asset_class": asset_class},
                        what=f"{asset_class} asset list")


def stock_universe(trading, md: MarketData, cfg: Dict) -> List[str]:
    """Active, tradable, exchange-listed US stocks at $5+ with $10M+ average daily dollar volume."""
    u = cfg["universe"]["stocks"]
    assets = raw_assets(trading, "us_equity")
    excluded_funds.clear()
    excluded_funds.update(a["symbol"] for a in assets if is_leveraged_inverse_or_volatility(a.get("name", "")))
    listed = [a["symbol"] for a in assets if a.get("tradable") and "/" not in a["symbol"] and "." not in a["symbol"]
              and (not u["exclude_otc"] or str(a.get("exchange", "")).upper() != "OTC")
              and not (u.get("exclude_leveraged_inverse_volatility", True)
                       and is_leveraged_inverse_or_volatility(a.get("name", "")))]
    log.info("stocks: %d active, tradable, listed", len(listed))
    n = u["avg_volume_days"]
    md.update(listed, "stocks", "1Day", n + 5)  # cheap first pass: just enough bars to check price and volume
    keep = []
    for sym, df in md.closed_bars(listed, "stocks", "1Day", bars=n, refresh=False).items():
        if len(df) >= n and df["close"].iloc[-1] >= u["min_price"] and \
                (df["close"] * df["volume"]).mean() >= u["min_avg_dollar_volume"]:
            keep.append(sym)
    keep.sort()
    if u.get("max_symbols"):
        keep = keep[:u["max_symbols"]]
    log.info("stocks: %d pass the price and dollar-volume filters", len(keep))
    return keep


def crypto_universe(trading, md: MarketData, cfg: Dict) -> List[str]:
    """Every */USD pair Alpaca trades with at least $1M traded in the last 24 hours."""
    u = cfg["universe"]["crypto"]
    assets = raw_assets(trading, "crypto")
    pairs = sorted(a["symbol"] for a in assets if a.get("tradable") and a["symbol"].endswith("/" + u["quote_currency"]))
    keep = []
    for sym, df in md.closed_bars(pairs, "crypto", "4Hour", bars=6).items():  # last 24h = six 4-hour bars
        if (df["close"] * df["volume"]).sum() >= u["min_24h_dollar_volume"]:
            keep.append(sym)
    log.info("crypto: %d of %d USD pairs pass the 24h dollar-volume filter", len(keep), len(pairs))
    return keep
