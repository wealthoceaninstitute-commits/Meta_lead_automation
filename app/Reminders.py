"""
Automatic seminar reminders
---------------------------
For every lead whose status is *Confirmed* and who has a seminar date, two WhatsApp
template messages go out by themselves:

    4 hours before the seminar starts   (default template: woi_free_seminar_4hr_reminder)
    1 hour  before the seminar starts   (default template: woi_event_reminder_kannada_1hr)

The start time comes from the seminar day (Friday / Sunday) -> the lead's session time
(Form Config).  If a follow-up moved the lead to another date, that date wins.
Each (lead, reminder, seminar date) is sent at most once - guaranteed by a unique key in
`reminder_logs`.  A reminder that could not be sent (network / Meta hiccup) is retried a few
times while its window is still open.  Leads that get confirmed late simply skip the reminder
whose window has already passed (4hr: skipped when < 2 h are left, 1hr: when < 15 min are left).

Settings live in the DB (AppSetting 'reminder_settings') and are edited in CRM -> Reminders.
"""
import json
import logging
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import graph, tokens
from .alerts import log_event
from .config import settings as app_settings
from .form_config import day_defaults, parse_start_time, WEEKDAYS
from .models import Lead, FollowUp, ReminderLog, WhatsAppMessage
from .utils import now_iso, clean_phone
from . import whatsapp as wa

log = logging.getLogger(__name__)

SETTINGS_KEY = "reminder_settings"
MAX_ATTEMPTS = 3
RETRY_AFTER_MIN = 8

# kind -> (hours before start, minutes before start after which the reminder is too late)
KINDS = {"4hr": (4, 120), "1hr": (1, 15)}

DEFAULTS = {
    "enabled": True,
    "days": ["Friday", "Sunday"],
    "rules": {
        "4hr": {"template": "woi_free_seminar_4hr_reminder", "language": "en", "params": "session_time"},
        "1hr": {"template": "woi_event_reminder_kannada_1hr", "language": "kn", "params": "session_time"},
    },
}


# ── settings ────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.loads(tokens.get_setting(SETTINGS_KEY, "") or "{}")
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        if isinstance(raw.get("days"), list):
            out["days"] = [str(d).capitalize() for d in raw["days"] if str(d).strip()]
        for k in KINDS:
            r = (raw.get("rules") or {}).get(k)
            if isinstance(r, dict):
                for f in ("template", "language", "params"):
                    if f in r:
                        out["rules"][k][f] = str(r[f] or "").strip()
    return out


def save_settings(data: dict) -> dict:
    cur = load_settings()
    if "enabled" in data:
        cur["enabled"] = bool(data["enabled"])
    if isinstance(data.get("days"), list):
        cur["days"] = [str(d).strip().capitalize() for d in data["days"]
                       if str(d).strip().lower() in WEEKDAYS]
    for k in KINDS:
        r = (data.get("rules") or {}).get(k)
        if isinstance(r, dict):
            for f in ("template", "language", "params"):
                if f in r:
                    cur["rules"][k][f] = str(r[f] or "").strip()
    tokens.set_setting(SETTINGS_KEY, json.dumps(cur))
    return cur


# ── time helpers ────────────────────────────────────────────────────────────

def tz():
    return ZoneInfo(app_settings.seminar_timezone)


_DATE_FORMATS = ("%Y-%m-%d", "%A, %d %B %Y", "%d %B %Y", "%d %b %Y", "%d-%m-%Y", "%d/%m/%Y", "%A, %d %b %Y")


def parse_date(value) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    s = s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else s
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def seminar_datetime(db: Session, lead: Lead) -> tuple[datetime | None, str]:
    """(start datetime in seminar timezone, why-not text). A follow-up's date overrides the lead's."""
    latest = (db.query(FollowUp).filter(FollowUp.lead_id == lead.id, FollowUp.session_date.isnot(None),
                                        FollowUp.session_date != "")
              .order_by(FollowUp.id.desc()).first())
    d = parse_date(latest.session_date) if latest else None
    if d is None:
        d = parse_date(lead.session_date)
    if d is None:
        return None, "no seminar date"
    time_text = lead.session_time or ""
    weekday_name = [k for k, v in WEEKDAYS.items() if v == d.weekday()]
    day_name = (weekday_name[0] if weekday_name else "").capitalize()
    # moved to another weekday than the lead's original → use that day's default time
    if day_name and (lead.session_day or "").lower() != day_name.lower():
        time_text = (day_defaults().get(day_name) or {}).get("session_time") or time_text
    start = parse_start_time(time_text)
    if start is None:
        return None, "no session time"
    return datetime.combine(d, start, tzinfo=tz()), ""


def day_of(dt: datetime) -> str:
    return dt.strftime("%A")


# ── decision per lead ───────────────────────────────────────────────────────

def plan_for(db: Session, lead: Lead, cfg: dict, now: datetime, logs: dict) -> dict:
    """What should happen to this lead, per reminder."""
    start, why = seminar_datetime(db, lead)
    out = {"lead_id": lead.id, "start": start, "reasons": [why] if why else [], "kinds": {}}
    if start is None:
        return out
    if day_of(start) not in cfg["days"]:
        out["reasons"].append(f"{day_of(start)} is not enabled")
        return out
    iso = start.date().isoformat()
    for kind, (hours, late_min) in KINDS.items():
        rule = cfg["rules"][kind]
        opens = start - timedelta(hours=hours)
        closes = start - timedelta(minutes=late_min)
        row = logs.get((lead.id, kind, iso))
        info = {"opens_at": opens, "closes_at": closes, "template": rule["template"], "state": "waiting"}
        if row and row.status == "sent":
            info["state"] = "sent"
        elif row and row.status == "sending":
            info["state"] = "sending"
        elif not rule["template"]:
            info["state"] = "no_template"
        elif now > closes:
            info["state"] = "failed_final" if (row and row.status == "failed") else "missed"
        elif now < opens:
            info["state"] = "waiting"
        elif row and row.status == "failed" and (row.attempts or 0) >= MAX_ATTEMPTS:
            info["state"] = "failed_final"
        elif row and row.status == "failed" and _recent(row, now):
            info["state"] = "retry_later"
        else:
            info["state"] = "due"
        out["kinds"][kind] = info
    return out


def _recent(row: ReminderLog, now: datetime) -> bool:
    try:
        ts = row.updated_at or row.created_at
        if ts is None:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo("UTC"))
        return (now - ts).total_seconds() < RETRY_AFTER_MIN * 60
    except Exception:
        return False


def _load_logs(db: Session, lead_ids: list[int]) -> dict:
    if not lead_ids:
        return {}
    rows = db.query(ReminderLog).filter(ReminderLog.lead_id.in_(lead_ids)).all()
    return {(r.lead_id, r.kind, r.session_date): r for r in rows}


def confirmed_leads(db: Session):
    since = datetime.now(tz()) - timedelta(days=90)
    q = (db.query(Lead).filter(Lead.phone.isnot(None), Lead.phone != "")
         .filter(Lead.status.ilike("confirmed"))
         .filter(Lead.created_at >= since))
    return q.all()


# ── sending ─────────────────────────────────────────────────────────────────

def send_reminder(db: Session, lead: Lead, template: str, language: str, params: str) -> dict:
    """Send a reminder template WITHOUT touching the lead's invite fields
    (whatsapp_sent / delivered / read keep describing the original invite)."""
    if not app_settings.whatsapp_enabled:
        return {"ok": False, "error": "WhatsApp is disabled", "retryable": False}
    if not app_settings.whatsapp_phone_number_id:
        return {"ok": False, "error": "WHATSAPP_PHONE_NUMBER_ID is not set", "retryable": False}
    try:
        components = wa._build_components(db, lead, template, language or "en", params or "")
    except wa.TemplateConfigError as e:
        return {"ok": False, "error": str(e), "retryable": False}
    payload = {"messaging_product": "whatsapp", "to": clean_phone(lead.phone), "type": "template",
               "template": {"name": template, "language": {"code": language or "en"}, "components": components}}
    try:
        resp = graph.post(f"{app_settings.whatsapp_phone_number_id}/messages", kind="wa", json=payload, retries=1)
    except graph.GraphError as e:
        permanent = e.code in wa.PERMANENT_WA_CODES
        return {"ok": False, "error": str(e)[:400], "retryable": (not permanent) and e.kind in
                ("token", "rate", "transient", "other"), "code": e.code}
    wamid = (resp.get("messages") or [{}])[0].get("id")
    try:
        rendered = wa.render_template_message(db, template, language or "en", components)
    except Exception:
        rendered = None
    wa.save_outgoing_template_message(db, lead, wamid=wamid, status="accepted", raw=resp, rendered=rendered)
    return {"ok": True, "wamid": wamid}


def _claim(db: Session, lead: Lead, kind: str, iso: str, template: str,
           row: ReminderLog | None) -> ReminderLog | None:
    """Reserve this reminder BEFORE sending, so two workers / overlapping runs can never both send it."""
    if row is None:
        row = ReminderLog(lead_id=lead.id, kind=kind, session_date=iso, template=template,
                          status="sending", attempts=0)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        return row
    n = (db.query(ReminderLog)
         .filter(ReminderLog.id == row.id, ReminderLog.status == "failed", ReminderLog.attempts == row.attempts)
         .update({"status": "sending"}))
    db.commit()
    if not n:
        return None
    db.refresh(row)
    return row


def _record(db: Session, row: ReminderLog, template: str, res: dict):
    row.template = template
    row.attempts = (row.attempts or 0) + 1
    row.status = "sent" if res.get("ok") else "failed"
    row.wamid = res.get("wamid") or row.wamid
    row.error = None if res.get("ok") else str(res.get("error") or "")[:500]
    if not res.get("ok") and not res.get("retryable"):
        row.attempts = MAX_ATTEMPTS          # no point retrying a template/parameter problem
    db.commit()


def run_due(db: Session | None = None, now: datetime | None = None) -> dict:
    """Send every reminder that is due right now. Safe to call as often as you like."""
    from .db import SessionLocal
    own = db is None
    db = db or SessionLocal()
    summary = {"checked": 0, "sent": 0, "failed": 0, "enabled": True}
    try:
        cfg = load_settings()
        if not cfg["enabled"]:
            summary["enabled"] = False
            return summary
        if not tokens.get_wa_token():
            summary["error"] = "WhatsApp token missing"
            return summary
        now = now or datetime.now(tz())
        leads = confirmed_leads(db)
        logs = _load_logs(db, [l.id for l in leads])
        for lead in leads:
            plan = plan_for(db, lead, cfg, now, logs)
            if not plan["kinds"]:
                continue
            summary["checked"] += 1
            iso = plan["start"].date().isoformat()
            for kind, info in plan["kinds"].items():
                if info["state"] != "due":
                    continue
                rule = cfg["rules"][kind]
                row = _claim(db, lead, kind, iso, rule["template"], logs.get((lead.id, kind, iso)))
                if row is None:
                    continue
                try:
                    res = send_reminder(db, lead, rule["template"], rule["language"], rule["params"])
                    _record(db, row, rule["template"], res)
                except Exception as exc:                     # includes unique-key races
                    db.rollback()
                    log.warning("reminder %s lead %s: %s", kind, lead.id, exc)
                    continue
                if res.get("ok"):
                    summary["sent"] += 1
                else:
                    summary["failed"] += 1
                    log_event("reminder_failed",
                              f"{kind} reminder to {lead.full_name or lead.phone} failed: {res.get('error')}", "warn")
        if summary["sent"]:
            log_event("reminders_sent", f"{summary['sent']} seminar reminder(s) sent", "info")
        return summary
    finally:
        if own:
            db.close()


def upcoming(db: Session, hours_ahead: int = 24 * 8) -> list[dict]:
    """Confirmed leads with a seminar in the next days and the state of each reminder."""
    cfg = load_settings()
    now = datetime.now(tz())
    leads = confirmed_leads(db)
    logs = _load_logs(db, [l.id for l in leads])
    rows = []
    for lead in leads:
        plan = plan_for(db, lead, cfg, now, logs)
        start = plan["start"]
        if start is None or start < now - timedelta(hours=2) or start > now + timedelta(hours=hours_ahead):
            continue
        iso = start.date().isoformat()
        kinds = {}
        for kind, info in plan["kinds"].items():
            row = logs.get((lead.id, kind, iso))
            kinds[kind] = {"state": info["state"], "opens_at": info["opens_at"].isoformat(),
                           "template": info["template"], "error": row.error if row else None,
                           "attempts": row.attempts if row else 0}
        rows.append({"lead_id": lead.id, "name": lead.full_name, "phone": lead.phone,
                     "start": start.isoformat(), "session_date": iso, "day": day_of(start),
                     "reasons": plan["reasons"], "kinds": kinds})
    rows.sort(key=lambda r: r["start"])
    return rows
