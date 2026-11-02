"""One scan: universe -> closed bars -> box-theory signals -> rank -> de-duplicate -> alert -> (paper_auto) orders."""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from alerts import Alerter
from data import BarCache, MarketData, crypto_universe, stock_universe
from risk import can_open, norm, position_size
from settings import Credentials, path
from store import Store
from strategies.box_theory import Bars, Signal, evaluate, params_for, rank

log = logging.getLogger("box.scan")


class Scanner:
    def __init__(self, cfg: Dict, creds: Credentials) -> None:
        from alpaca.trading.client import TradingClient
        self.cfg, self.creds = cfg, creds
        self.store = Store(path(cfg, "state_db"))
        self.md = MarketData(creds, cfg, BarCache(path(cfg, "cache_db")))
        self.trading = TradingClient(creds.api_key, creds.secret_key, paper=True)  # asset lists and calendar
        self.alerts = Alerter(cfg["alerts"], creds)
        self.broker = None
        if cfg["mode"] == "paper_auto":
            from broker import PaperBroker
            self.broker = PaperBroker(creds, self.store)
        self.lock = threading.Lock()  # one scan at a time (the 00:02 crypto scans can coincide)
        self._universe: Dict[str, tuple] = {}

    # ------------------------------------------------------------ universe (rebuilt once a day)
    def universe(self, asset_class: str) -> List[str]:
        today = datetime.now(timezone.utc).date()
        cached = self._universe.get(asset_class)
        if cached and cached[0] == today:
            return cached[1]
        fn = stock_universe if asset_class == "stocks" else crypto_universe
        symbols = fn(self.trading, self.md, self.cfg)
        self._universe[asset_class] = (today, symbols)
        return symbols

    def is_trading_day(self, day: date) -> bool:
        from alpaca.trading.requests import GetCalendarRequest
        cal = self.trading.get_calendar(GetCalendarRequest(start=day, end=day))
        return any(c.date == day for c in cal)

    # ------------------------------------------------------------ the scan
    def scan(self, asset_class: str, timeframes: Optional[List[str]] = None) -> List[Signal]:
        with self.lock:
            return self._scan(asset_class, timeframes or self.cfg["timeframes"][asset_class])

    def _scan(self, asset_class: str, timeframes: List[str]) -> List[Signal]:
        strategy = self.cfg["strategy"]
        symbols = self.universe(asset_class)
        found: List[Signal] = []
        latest: Dict[str, Dict] = {}  # last closed bar per symbol (crypto stop/target checks)
        for tf in timeframes:
            bars = self.md.closed_bars(symbols, asset_class, tf)
            box_p, _ = params_for(self.cfg, asset_class)
            for sym, df in bars.items():
                try:
                    found += evaluate(df, strategy, sym, asset_class, tf, self.cfg, bars=Bars(df, box_p.volume_avg_bars))
                    last = df.iloc[-1]
                    latest[sym] = {"high": last["high"], "low": last["low"], "close": last["close"], "tf": tf}
                except Exception as e:  # one bad symbol never stops the scan
                    log.exception("%s %s %s: skipped (%s)", asset_class, sym, tf, e)
            log.info("%s %s: scanned %d symbols", asset_class, tf, len(bars))

        entries = rank([s for s in found if s.direction in ("long", "short")])
        exits = [s for s in found if s.direction == "exit"]
        new_entries = [s for s in entries if self.store.add_signal(s)]
        log.info("%s: %d entry signals (%d new), %d exit/breakdown closes", asset_class, len(entries),
                 len(new_entries), len(exits))
        for s in new_entries:
            self.alerts.signal(s, self.cfg["mode"])
        self._follow_up_exits(exits)
        if self.broker:
            self._trade(asset_class, new_entries, exits, latest)
        return new_entries

    def _follow_up_exits(self, exits: List[Signal]) -> None:
        """Alert an exit only for symbols we previously alerted a long on (otherwise it's just noise)."""
        for s in exits:
            key = self.store.open_long_signal(s.symbol, s.timeframe, s.strategy)
            if key and self.store.add_signal(s):
                self.store.close_signal(key)
                self.alerts.signal(s, self.cfg["mode"])

    # ------------------------------------------------------------ paper_auto
    def _trade(self, asset_class: str, entries: List[Signal], exits: List[Signal], latest: Dict[str, Dict]) -> None:
        b, risk = self.broker, self.cfg["risk"]
        st = b.state()
        held = {norm(h) for h in st.held}
        # 1. exits for anything we hold
        for s in exits:
            if norm(s.symbol) in held:
                b.close(s.symbol, f"{s.note} ({s.timeframe})")
                self.store.unmanage(s.symbol)
        # 2. crypto stops and targets, kept in code
        if asset_class == "crypto":
            for m in self.store.managed():
                if norm(m["symbol"]) not in held:
                    self.store.unmanage(m["symbol"])  # closed elsewhere
                    continue
                bar = latest.get(m["symbol"])
                if bar and bar["low"] <= m["stop"]:
                    b.close(m["symbol"], f"stop {m['stop']:.6g} hit")
                    self.store.unmanage(m["symbol"])
                elif bar and bar["high"] >= m["target"]:
                    b.close(m["symbol"], f"target {m['target']:.6g} hit")
                    self.store.unmanage(m["symbol"])
        # 3. new entries, best-ranked first
        placed = 0
        for s in entries:
            if placed >= risk["max_new_positions_per_scan"]:
                break
            if s.direction == "short" and (asset_class != "stocks" or not self.cfg.get("allow_short")):
                continue
            st = b.state()  # fresh equity, positions and orders before every order
            ok, why = can_open(st, s.symbol, risk)
            if not ok:
                log.info("skip %s %s: %s", s.direction, s.symbol, why)
                continue
            crypto = asset_class == "crypto"
            qty = position_size(st.equity, s.close, s.stop, risk["risk_per_trade"], risk["max_position"],
                                fractional=crypto, step=b.crypto_increment(s.symbol) if crypto else 1)
            if qty <= 0:
                log.info("skip %s: position size rounds to zero", s.symbol)
                continue
            order = b.crypto_entry(s, qty) if crypto else b.stock_bracket(s, qty)
            if order is not None:
                placed += 1
                if crypto:
                    self.store.manage(s.symbol, qty, s.close, s.stop, s.target, s)
                self.alerts.send(f"PAPER order: {s.direction.upper()} {qty:g} {s.symbol}",
                                 f"stop {s.stop:.6g} · target {s.target:.6g} · order {order.id} ({order.status})")
