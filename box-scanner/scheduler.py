"""The 24/7 schedule (APScheduler). Crypto: after every 4-hour close and the daily close. Stocks: 9:40 AM New York
on trading days, after the opening rush. A heartbeat every 15 minutes. A crashing scan is logged and the process
carries on."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from scanner import Scanner

log = logging.getLogger("box.schedule")
NEW_YORK = ZoneInfo("America/New_York")


def safe(name: str, fn: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        started = datetime.now(timezone.utc)
        try:
            fn()
            log.info("%s finished in %.0fs", name, (datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            log.exception("%s crashed; the scheduler keeps running", name)
    return run


def build(scanner: Scanner, cfg: Dict) -> BlockingScheduler:
    sch = cfg["schedule"]
    sched = BlockingScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1,
                                                            "misfire_grace_time": 900})
    tfs: List[str] = cfg["timeframes"]["crypto"]

    def heartbeat() -> None:
        nxt = sorted((j.next_run_time, j.name) for j in sched.get_jobs() if j.next_run_time and j.name != "heartbeat")
        log.info("heartbeat: alive, mode=%s, strategy=%s; next: %s", cfg["mode"], cfg["strategy"],
                 ", ".join(f"{n} {t.astimezone(timezone.utc):%H:%M UTC}" for t, n in nxt[:3]))

    sched.add_job(heartbeat, "interval", minutes=sch["heartbeat_minutes"], name="heartbeat",
                  next_run_time=datetime.now(timezone.utc))
    if "4Hour" in tfs:
        sched.add_job(safe("crypto 4h scan", lambda: scanner.scan("crypto", ["4Hour"])),
                      CronTrigger(hour="0,4,8,12,16,20", minute=sch["crypto_4h_minute"], timezone="UTC"),
                      name="crypto 4h scan")
    if "1Day" in tfs:
        hh, mm = (int(x) for x in sch["crypto_daily"].split(":"))
        sched.add_job(safe("crypto daily scan", lambda: scanner.scan("crypto", ["1Day"])),
                      CronTrigger(hour=hh, minute=mm, timezone="UTC"), name="crypto daily scan")

    def stocks() -> None:
        today = datetime.now(NEW_YORK).date()
        if not scanner.is_trading_day(today):
            log.info("stocks: %s isn't a trading day (Alpaca calendar); no scan", today)
            return
        scanner.scan("stocks")

    hh, mm = (int(x) for x in sch["stocks_scan_time"].split(":"))
    sched.add_job(safe("stock scan", stocks), CronTrigger(day_of_week="mon-fri", hour=hh, minute=mm,
                                                           timezone=NEW_YORK), name="stock scan")
    return sched
