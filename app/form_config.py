"""
Form Config — maps Meta form_id → campaign details + WhatsApp template.

Priority:
  1. DB row in form_configs table (managed via frontend)
  2. Hardcoded FALLBACK_CONFIGS below (safety net)

To add a new campaign: just add it via the frontend Form Config page.
"""

from sqlalchemy.orm import Session
from .models import FormConfig
from .config import settings
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

# ── Fallback config (used when DB has no entry for a form_id) ─────────────
FALLBACK_CONFIGS = {
    "2216617835805846": {
        "form_name":     "LIVE TRADING CLASS NEW",
        "campaign_name": "LIVE TRADING CLASS",
        "day":           "Friday",
        "session_time":  "6:00 PM to 8:00 PM",
        "arrival_time":  "5:45 PM",
        "venue":         settings.seminar_venue,
        "wa_template":   "woi_friday_class_confirmation",
        "wa_language":   "en",
    },
    "1063861336132383": {
        "form_name":     "Free Seminar Form Sunday",
        "campaign_name": "Free Seminar",
        "day":           "Sunday",
        "session_time":  "10:30 AM to 12:30 PM",
        "arrival_time":  "10:15 AM",
        "venue":         settings.seminar_venue,
        "wa_template":   "woi_seminar_registration_followup",
        "wa_language":   "en",
    },
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def get_form_config(form_id: str, db: Session) -> dict | None:
    """Return config dict for a form_id. DB first, fallback second."""
    if not form_id:
        return None

    fid = str(form_id).strip()

    # 1. DB lookup
    row = db.query(FormConfig).filter(
        FormConfig.form_id == fid,
        FormConfig.is_active == True
    ).first()

    if row:
        return {
            "form_name":     row.form_name or "",
            "campaign_name": row.campaign_name,
            "day":           row.day,
            "session_time":  row.session_time or "",
            "arrival_time":  row.arrival_time or "",
            "venue":         row.venue or settings.seminar_venue,
            "wa_template":   row.wa_template or settings.whatsapp_template_name,
            "wa_language":   row.wa_language or "en",
        }

    # 2. Hardcoded fallback
    return FALLBACK_CONFIGS.get(fid)


def get_next_session_date(day_name: str) -> str:
    """Return next occurrence of given weekday as 'Day, DD Month YYYY'."""
    tz = ZoneInfo(settings.seminar_timezone)
    now = datetime.now(tz)
    today = now.date()

    target = WEEKDAYS.get(day_name.lower())
    if target is None:
        # Default to next Sunday
        target = 6

    days_ahead = (target - today.weekday()) % 7
    # If today is the day but past session time, go to next week
    if days_ahead == 0:
        days_ahead = 7

    session_date = today + timedelta(days=days_ahead)
    return session_date.strftime("%A, %d %B %Y")


def resolve_session_details(form_id: str, db: Session) -> dict:
    """
    Given a form_id, return full session details ready to save on the lead.
    Falls back to Sunday seminar if form_id unknown.
    """
    cfg = get_form_config(form_id, db)

    if not cfg:
        # Unknown form — default to Sunday seminar
        cfg = {
            "campaign_name": "Unknown Campaign",
            "day":           "Sunday",
            "session_time":  settings.seminar_sunday_time,
            "arrival_time":  settings.seminar_sunday_arrival,
            "venue":         settings.seminar_venue,
            "wa_template":   settings.whatsapp_template_name,
            "wa_language":   "en",
        }

    return {
        "campaign_name": cfg["campaign_name"],
        "form_name":     cfg.get("form_name", ""),
        "session_day":   cfg["day"],
        "session_date":  get_next_session_date(cfg["day"]),
        "session_time":  cfg.get("session_time", ""),
        "arrival_time":  cfg.get("arrival_time", ""),
        "venue":         cfg.get("venue", settings.seminar_venue),
        "wa_template":   cfg.get("wa_template", settings.whatsapp_template_name),
        "wa_language":   cfg.get("wa_language", "en"),
    }
