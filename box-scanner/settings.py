"""Load config.yaml and .env, set up logging, and refuse anything that isn't Alpaca paper trading."""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

ROOT = Path(__file__).resolve().parent
MODES = ("signals_only", "paper_auto")
STRATEGIES = ("darvas_breakout", "range_reversion")
ASSET_CLASSES = ("stocks", "crypto")
BASE_URL_VARS = ("ALPACA_BASE_URL", "APCA_API_BASE_URL", "ALPACA_ENDPOINT", "ALPACA_API_BASE_URL")


@dataclass
class Credentials:
    """API keys and alert secrets. repr() never shows a value, so logging this object is safe."""
    api_key: str
    secret_key: str = field(repr=False)
    data_feed: str = "iex"
    env: Dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return f"Credentials(api_key='{self.api_key[:2]}…', data_feed='{self.data_feed}')"

    def get(self, name: str) -> str:
        return self.env.get(name, "")


def fail(message: str) -> None:
    """Stop with a clear message (used for every paper-only safety check)."""
    sys.exit(f"REFUSING TO START: {message}")


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    with open(path or ROOT / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    if cfg.get("mode") not in MODES:
        fail(f"config.yaml mode must be one of {MODES}, not {cfg.get('mode')!r}")
    if cfg.get("strategy") not in STRATEGIES:
        fail(f"config.yaml strategy must be one of {STRATEGIES}, not {cfg.get('strategy')!r}")
    for ac, tfs in cfg["timeframes"].items():
        for tf in tfs:
            if tf not in ("1Day", "4Hour"):
                fail(f"timeframes.{ac}: unsupported {tf!r} (use 1Day or 4Hour)")
    if "4Hour" in cfg["timeframes"].get("stocks", []):
        fail("timeframes.stocks: stocks are scanned on daily bars only")
    return cfg


def load_credentials(env_path: Optional[Path] = None, environ: Optional[Dict[str, str]] = None) -> Credentials:
    """Read keys from .env (or the real environment). Exits if anything points at a live account."""
    if environ is None:
        from dotenv import load_dotenv
        load_dotenv(env_path or ROOT / ".env")
        environ = dict(os.environ)
    for var in BASE_URL_VARS:
        url = environ.get(var, "")
        if url and "paper-api" not in url:
            fail(f"{var} points at {url}, which is not Alpaca's paper API. Remove it: this bot is paper-only.")
    if environ.get("ALPACA_PAPER", "true").strip().lower() in ("false", "0", "no"):
        fail("ALPACA_PAPER is set to false. This bot only ever trades on paper.")
    key, secret = environ.get("ALPACA_API_KEY", "").strip(), environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        fail("ALPACA_API_KEY and ALPACA_SECRET_KEY are missing. Copy .env.example to .env and add your PAPER keys.")
    if key.startswith("AK"):
        fail("that API key starts with AK, which Alpaca uses for LIVE accounts. Use your paper key (starts with PK).")
    feed = environ.get("ALPACA_DATA_FEED", "iex").strip().lower() or "iex"
    if feed not in ("iex", "sip"):
        fail(f"ALPACA_DATA_FEED must be iex or sip, not {feed!r}")
    return Credentials(key, secret, feed, environ)


class _Redact(logging.Filter):
    """Belt and braces: blank out any secret value that ever reaches a log line."""

    def __init__(self, secrets: list) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "[redacted]")
            record.msg, record.args = msg, ()
        return True


def setup_logging(cfg: Dict[str, Any], creds: Optional[Credentials] = None) -> logging.Logger:
    """Log to logs/scanner.log (rotating) and the console."""
    log_path = ROOT / cfg["logging"]["file"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(cfg["logging"].get("level", "INFO"))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    secrets = [] if creds is None else [creds.secret_key, creds.api_key] + [
        creds.get(k) for k in ("DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD", "NTFY_TOPIC")]
    for handler in (RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5), logging.StreamHandler()):
        handler.setFormatter(fmt)
        handler.addFilter(_Redact(secrets))
        root.addHandler(handler)
    for noisy in ("urllib3", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("box")


def path(cfg: Dict[str, Any], key: str) -> Path:
    """Absolute path for a data.* file setting, creating its folder."""
    p = ROOT / cfg["data"][key]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
