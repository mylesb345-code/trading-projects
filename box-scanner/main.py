#!/usr/bin/env python3
"""Box-theory swing scanner for US stocks and crypto, with optional Alpaca PAPER trading.

  python main.py check            verify keys, the paper account, data access and alert channels
  python main.py scan crypto      run one crypto scan now (all configured timeframes)
  python main.py scan stocks      run one stock scan now
  python main.py run              run 24/7 on the schedule in config.yaml
  python main.py report           win/loss report for the paper account (also sent as an alert)
  python backtest.py              test both strategies on history before using paper_auto
"""
from __future__ import annotations

import argparse
import sys

from settings import load_config, load_credentials, setup_logging


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Box-theory swing scanner (Alpaca paper only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify keys, account, data and alerts")
    sc = sub.add_parser("scan", help="run one scan now")
    sc.add_argument("asset_class", choices=["stocks", "crypto"])
    sc.add_argument("--trading-days-only", action="store_true",
                    help="stocks: do nothing on weekends and market holidays (Alpaca calendar)")
    sc.add_argument("--market-open-only", action="store_true",
                    help="stocks: do nothing unless the market is open right now (Alpaca clock)")
    sub.add_parser("report", help="win/loss report, printed and sent to the alert channels")
    sub.add_parser("run", help="run 24/7 on the schedule")
    sub.add_parser("test-alert", help="send a test message to every enabled alert channel")
    a = ap.parse_args(argv)

    cfg = load_config()
    creds = load_credentials()
    log = setup_logging(cfg, creds)
    log.info("mode=%s strategy=%s (PAPER only)", cfg["mode"], cfg["strategy"])

    from scanner import Scanner
    if a.cmd == "test-alert":
        from alerts import Alerter
        Alerter(cfg["alerts"], creds).send("Box scanner test", "Alerts from the box-theory scanner will show up here.")
        return 0
    scanner = Scanner(cfg, creds)
    if a.cmd == "check":
        acct = scanner.trading.get_account()
        if not str(acct.account_number).startswith("PA"):
            sys.exit(f"REFUSING: account {acct.account_number} isn't a paper account.")
        log.info("paper account %s: equity $%s, status %s", acct.account_number, acct.equity, acct.status)
        bars = scanner.md.closed_bars(["BTC/USD"], "crypto", "4Hour", bars=5)
        log.info("crypto data OK: last closed BTC/USD 4h bar %s", bars["BTC/USD"].index[-1] if bars else "missing")
        bars = scanner.md.closed_bars(["AAPL"], "stocks", "1Day", bars=5)
        log.info("stock data OK (%s feed): last closed AAPL daily bar %s", creds.data_feed,
                 bars["AAPL"].index[-1] if bars else "missing")
        log.info("alert channels on: %s", ", ".join(k for k, v in cfg["alerts"].items() if v) or "none")
        return 0
    if a.cmd == "report":
        import report
        title, body = report.build(scanner.trading, scanner.store.bot_symbols(), md=scanner.md)
        scanner.alerts.send(title, body)
        return 0
    if a.cmd == "scan":
        if a.asset_class == "stocks" and a.market_open_only and not scanner.trading.get_clock().is_open:
            log.info("market is closed right now (Alpaca clock): no stock scan")
            return 0
        if a.asset_class == "stocks" and a.trading_days_only:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).date()
            if not scanner.is_trading_day(today):
                log.info("%s isn't a trading day (Alpaca calendar): no stock scan", today)
                return 0
        if not scanner.cfg["timeframes"].get(a.asset_class):
            log.info("%s scanning is switched off in config.yaml (timeframes.%s is empty)", a.asset_class, a.asset_class)
            return 0
        signals = scanner.scan(a.asset_class)
        log.info("done: %d new entry signal(s)", len(signals))
        return 0
    from scheduler import build
    log.info("starting 24/7 scheduler (Ctrl+C to stop)")
    try:
        build(scanner, cfg).start()
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
