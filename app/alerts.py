"""
Alerts + audit events.

`log_event`  – always writes a row to system_events (shown on the System page).
`notify`     – pushes a message to every configured channel (Telegram, e-mail,
               generic webhook) with a per-key cool-down so a broken token
               produces ONE alert, not one per failed lead.
"""
import json
import logging
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import requests

from .config import settings
from .db import SessionLocal
from .models import AppSetting, SystemEvent

log = logging.getLogger(__name__)
MAX_EVENTS = 300


def log_event(kind: str, message: str, level: str = "info") -> None:
    print(f"[event:{level}] {kind}: {message}", flush=True)
    try:
        with SessionLocal() as db:
            db.add(SystemEvent(kind=kind, level=level, message=str(message)[:2000]))
            db.commit()
            # prune
            n = db.query(SystemEvent).count()
            if n > MAX_EVENTS + 50:
                cutoff = (db.query(SystemEvent.id).order_by(SystemEvent.id.desc())
                          .offset(MAX_EVENTS).first())
                if cutoff:
                    db.query(SystemEvent).filter(SystemEvent.id <= cutoff[0]).delete()
                    db.commit()
    except Exception as exc:  # never let logging break the app
        log.error("log_event failed: %s", exc)


def channels_configured() -> list[str]:
    ch = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        ch.append("telegram")
    if settings.smtp_host and settings.alert_email_to:
        ch.append("email")
    if settings.alert_webhook_url:
        ch.append("webhook")
    return ch


def _cooldown_ok(key: str, hours: float) -> bool:
    """True (and stamps now) if `key` has not fired within `hours`."""
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, f"alert:{key}")
            now = datetime.now(timezone.utc)
            if row and row.value:
                try:
                    last = datetime.fromisoformat(row.value)
                    if now - last < timedelta(hours=hours):
                        return False
                except ValueError:
                    pass
            if not row:
                row = AppSetting(key=f"alert:{key}")
                db.add(row)
            row.value = now.isoformat()
            db.commit()
            return True
    except Exception as exc:
        log.error("cooldown check failed: %s", exc)
        return True


def clear_cooldown(key: str) -> None:
    """Call when a problem is resolved so the next occurrence alerts immediately."""
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, f"alert:{key}")
            if row:
                db.delete(row)
                db.commit()
    except Exception:
        pass


def notify(key: str, subject: str, body: str, level: str = "error", cooldown_hours: float = 12) -> bool:
    log_event(f"alert:{key}", f"{subject} — {body}", level)
    if not _cooldown_ok(key, cooldown_hours):
        return False
    text = f"WOI CRM — {subject}\n\n{body}"
    sent = False
    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": text}, timeout=15)
            sent = sent or r.ok
        except Exception as exc:
            log.error("telegram alert failed: %s", exc)
    if settings.alert_webhook_url:
        try:
            r = requests.post(settings.alert_webhook_url,
                              data=text.encode("utf-8"),
                              headers={"Title": subject[:200], "Content-Type": "text/plain; charset=utf-8"},
                              timeout=15)
            sent = sent or r.ok
        except Exception as exc:
            log.error("webhook alert failed: %s", exc)
    if settings.smtp_host and settings.alert_email_to:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[WOI CRM] {subject}"
            msg["From"] = settings.smtp_user or settings.alert_email_to
            msg["To"] = settings.alert_email_to
            msg.set_content(text)
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
            sent = True
        except Exception as exc:
            log.error("email alert failed: %s", exc)
    return sent
