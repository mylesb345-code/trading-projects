"""SQLite record of every signal (for de-duplication), every order event, and crypto stops/targets kept in code."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from strategies.box_theory import Signal


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS signals (key TEXT PRIMARY KEY, created_utc TEXT, symbol TEXT, asset_class TEXT,
                timeframe TEXT, strategy TEXT, direction TEXT, status TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, time_utc TEXT, symbol TEXT,
                event TEXT, request TEXT, response TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS managed (symbol TEXT PRIMARY KEY, qty REAL, entry REAL, stop REAL, target REAL,
                strategy TEXT, timeframe TEXT, signal_key TEXT, opened_utc TEXT);
        """)
        self.db.commit()

    def add_signal(self, s: Signal) -> bool:
        """Save a signal. True if it's new; False if this symbol + box + direction was seen before."""
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?)",
                                  (s.key, _now(), s.symbol, s.asset_class, s.timeframe, s.strategy, s.direction,
                                   "open" if s.direction in ("long", "short") else "info",
                                   json.dumps(s.as_dict(), default=str)))
            self.db.commit()
            return cur.rowcount == 1

    def open_long_signal(self, symbol: str, timeframe: str, strategy: str) -> Optional[str]:
        """The key of an alerted long on this symbol that hasn't had an exit yet (signals_only follow-up)."""
        row = self.db.execute("""SELECT key FROM signals WHERE symbol=? AND timeframe=? AND strategy=? AND
            direction='long' AND status='open' ORDER BY created_utc DESC LIMIT 1""", (symbol, timeframe, strategy)).fetchone()
        return row[0] if row else None

    def close_signal(self, key: str) -> None:
        with self.lock:
            self.db.execute("UPDATE signals SET status='closed' WHERE key=?", (key,))
            self.db.commit()

    def log_order(self, symbol: str, event: str, request: Any = None, response: Any = None,
                  error: Optional[str] = None) -> None:
        with self.lock:
            self.db.execute("INSERT INTO orders (time_utc, symbol, event, request, response, error) VALUES (?,?,?,?,?,?)",
                            (_now(), symbol, event, json.dumps(request, default=str) if request is not None else None,
                             json.dumps(response, default=str) if response is not None else None, error))
            self.db.commit()

    def manage(self, symbol: str, qty: float, entry: float, stop: float, target: float, s: Signal) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO managed VALUES (?,?,?,?,?,?,?,?,?)",
                            (symbol, qty, entry, stop, target, s.strategy, s.timeframe, s.key, _now()))
            self.db.commit()

    def managed(self) -> List[Dict[str, Any]]:
        cols = ["symbol", "qty", "entry", "stop", "target", "strategy", "timeframe", "signal_key", "opened_utc"]
        return [dict(zip(cols, r)) for r in self.db.execute("SELECT * FROM managed")]

    def unmanage(self, symbol: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM managed WHERE symbol=?", (symbol,))
            self.db.commit()
