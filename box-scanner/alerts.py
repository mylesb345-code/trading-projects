"""Signal alerts to the console/log, Discord, Telegram, email and ntfy. A failing channel never stops a scan,
and no URL, token or password is ever written to the log."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Dict

import requests

from strategies.box_theory import Signal

log = logging.getLogger("box.alerts")


def fmt_price(x: float) -> str:
    if x != x:  # NaN
        return "-"
    return f"${x:,.2f}" if x >= 1 else f"${x:.6g}"


def format_signal(s: Signal, mode: str) -> "tuple[str, str]":
    """(title, body) for one signal."""
    verb = {"long": "LONG", "short": "SHORT", "exit": "EXIT"}[s.direction]
    title = f"{verb} {s.symbol} ({s.timeframe}, {s.strategy.replace('_', ' ')})"
    lines = [
        f"{s.note}" + (f" · rank #{s.rank}" if s.rank and s.direction != "exit" else ""),
        f"Close {fmt_price(s.close)} · volume {s.volume_ratio:.2f}x average",
        f"Box {fmt_price(s.box_bottom)} – {fmt_price(s.box_top)} (height {s.box_height_pct:.1%})",
    ]
    if s.direction != "exit":
        lines.append(f"Stop {fmt_price(s.stop)} · target {fmt_price(s.target)} · reward:risk {s.reward_risk:.2f}")
    lines += [f"{s.asset_class} · bar closed {s.time_utc} / {s.time_central}",
              "PAPER account" if mode == "paper_auto" else "Signal only (no order placed)",
              "Not financial advice."]
    return title, "\n".join(lines)


class Alerter:
    def __init__(self, cfg_alerts: Dict, creds) -> None:
        self.on, self.creds = cfg_alerts, creds

    def signal(self, s: Signal, mode: str) -> None:
        self.send(*format_signal(s, mode))

    def send(self, title: str, body: str) -> None:
        log.info("ALERT %s | %s", title, body.replace("\n", " | "))
        for name, fn in (("discord", self._discord), ("telegram", self._telegram), ("email", self._email),
                         ("ntfy", self._ntfy)):
            if self.on.get(name):
                try:
                    fn(title, body)
                except Exception as e:  # never log e itself: some libraries put the URL (with its token) in it
                    status = getattr(getattr(e, "response", None), "status_code", "")
                    log.error("%s alert failed (%s %s)", name, type(e).__name__, status)

    def _discord(self, title: str, body: str) -> None:
        url = self.creds.get("DISCORD_WEBHOOK_URL")
        if not url:
            raise ValueError("DISCORD_WEBHOOK_URL not set")
        requests.post(url, json={"content": f"**{title}**\n{body}"}, timeout=15).raise_for_status()

    def _telegram(self, title: str, body: str) -> None:
        token, chat = self.creds.get("TELEGRAM_BOT_TOKEN"), self.creds.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": f"{title}\n{body}"}, timeout=15).raise_for_status()

    def _email(self, title: str, body: str) -> None:
        c = self.creds
        host, to = c.get("SMTP_HOST"), c.get("ALERT_EMAIL_TO")
        if not host or not to:
            raise ValueError("SMTP_HOST / ALERT_EMAIL_TO not set")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, c.get("SMTP_USER") or to, to
        msg.set_content(body)
        with smtplib.SMTP(host, int(c.get("SMTP_PORT") or 587), timeout=20) as smtp:
            smtp.starttls()
            if c.get("SMTP_USER"):
                smtp.login(c.get("SMTP_USER"), c.get("SMTP_PASSWORD"))
            smtp.send_message(msg)

    def _ntfy(self, title: str, body: str) -> None:
        topic = self.creds.get("NTFY_TOPIC")
        if not topic:
            raise ValueError("NTFY_TOPIC not set")
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode(), headers={"Title": title}, timeout=15
                      ).raise_for_status()
