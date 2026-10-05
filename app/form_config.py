"""
Form Config — maps a Meta lead-form → session (day/time/venue) + WhatsApp template.

Resolution order for an incoming lead (see `resolve_session`):

  1. An active row in `form_configs` for the lead's form_id            (managed in the CRM)
  2. The day the lead picked / the day named in the ad, adset, campaign or form
     name  (e.g. "Free Seminar – Friday")                              (auto, flagged for review)
  3. Nothing could be decided  → the lead is KEPT and flagged `needs_config`;
     no WhatsApp is sent (a wrong date is worse than a short delay).

Adding a new ad therefore needs no code change and – if its name contains the
day – no manual step at all.
"""
import re
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .config import settings
from .models import FormConfig

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_ABBR = {"mon": "monday", "tue": "tuesday", "tues": "tuesday", "wed": "wednesday",
         "thu": "thursday", "thur": "thursday", "thurs": "thursday", "fri": "friday",
         "sat": "saturday", "sun": "sunday"}
_DAY_RE = re.compile(r"\b(" + "|".join(list(WEEKDAYS) + list(_ABBR)) + r")\b", re.I)

# Parameter keys a template body may use (see FormConfig.wa_params)
PARAM_KEYS = ("name", "campaign", "session_day", "session_date",
              "session_time", "arrival_time", "venue", "city")


def day_defaults() -> dict:
    """Session defaults per day (time / arrival; editable through env vars). No WhatsApp template here."""
    return {
        "Friday": {
            "session_time": settings.seminar_friday_time,
            "arrival_time": settings.seminar_friday_arrival,
        },
        "Sunday": {
            "session_time": settings.seminar_sunday_time,
            "arrival_time": settings.seminar_sunday_arrival,
        },
    }


def seed_rows() -> list[dict]:
    """Known production forms – inserted into the DB on first start so they are
    visible/editable on the Form Config page (previously hard-coded only)."""
    d = day_defaults()
    return [
        dict(form_id="2216617835805846", form_name="LIVE TRADING CLASS NEW",
             campaign_name="LIVE TRADING CLASS", day="Friday", **d["Friday"]),
        dict(form_id="1063861336132383", form_name="Free Seminar Form Sunday",
             campaign_name="Free Seminar", day="Sunday", **d["Sunday"]),
    ]


def seed_default_configs(db: Session) -> int:
    n = 0
    for row in seed_rows():
        if not db.query(FormConfig).filter(FormConfig.form_id == row["form_id"]).first():
            db.add(FormConfig(venue=settings.seminar_venue, wa_language="en",
                              is_active=True, auto_created=False, needs_review=False, **row))
            n += 1
    if n:
        db.commit()
    return n


# ── day inference ───────────────────────────────────────────────────────────

def infer_day(*texts) -> Optional[str]:
    """Return 'Friday' etc. if the texts mention exactly ONE weekday, else None."""
    found = set()
    for t in texts:
        for m in _DAY_RE.findall(str(t or "")):
            m = m.lower()
            found.add(_ABBR.get(m, m))
    if len(found) == 1:
        return found.pop().capitalize()
    return None


# ── time helpers ────────────────────────────────────────────────────────────

def parse_start_time(session_time: str) -> Optional[dtime]:
    """'6:00 PM to 8:00 PM' → time(18,0)."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", session_time or "", re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return dtime(h, mi)


def parse_created_time(value) -> Optional[datetime]:
    """Meta created_time ('2026-08-19T07:18:00+0000') → aware datetime (UTC)."""
    if not value:
        return None
    s = str(value).strip()
    for attempt in (s, s.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(attempt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def next_session_date(day_name: str, session_time: str = "", ref: Optional[datetime] = None) -> str:
    """Next occurrence of `day_name` counted from `ref` (the lead's own created
    time – so back-filled leads land in the right week for reports).
    Same-day leads still get *today's* session if it starts > cutoff minutes away."""
    tz = ZoneInfo(settings.seminar_timezone)
    now = (ref or datetime.now(tz)).astimezone(tz)
    target = WEEKDAYS.get((day_name or "").lower(), 6)
    days_ahead = (target - now.weekday()) % 7
    if days_ahead == 0:
        start = parse_start_time(session_time)
        if not start or now >= datetime.combine(now.date(), start, tzinfo=tz) - timedelta(
                minutes=settings.session_cutoff_minutes):
            days_ahead = 7
    return (now.date() + timedelta(days=days_ahead)).strftime("%A, %d %B %Y")


# ── config lookup ───────────────────────────────────────────────────────────

def _row_to_cfg(row: FormConfig) -> dict:
    return {
        "form_name": row.form_name or "",
        "campaign_name": row.campaign_name,
        "day": row.day,
        "session_time": row.session_time or "",
        "arrival_time": row.arrival_time or "",
        "venue": row.venue or settings.seminar_venue,
        "wa_template": row.wa_template or "",
        "wa_language": row.wa_language or "en",
        "wa_params": row.wa_params or "",
        "auto": bool(row.auto_created),
    }


def get_form_config(form_id: str, db: Session) -> Optional[dict]:
    """Active DB config for a form_id (or None)."""
    if not form_id:
        return None
    row = db.query(FormConfig).filter(
        FormConfig.form_id == str(form_id).strip(), FormConfig.is_active == True  # noqa: E712
    ).first()
    return _row_to_cfg(row) if row else None


def ensure_form_row(db: Session, form_id: str, form_name: str = "", day: Optional[str] = None,
                    campaign_name: str = "") -> FormConfig:
    """Create a placeholder/auto row for a form we have never seen.
    * day inferable & known defaults → active, auto_created, needs_review
    * otherwise → inactive placeholder (day='Auto') so it shows up in the UI to fill in."""
    row = db.query(FormConfig).filter(FormConfig.form_id == form_id).first()
    if row:
        return row
    d = day_defaults().get(day or "")
    if d:
        row = FormConfig(
            form_id=form_id, form_name=form_name or "", campaign_name=campaign_name or form_name or day,
            day=day, session_time=d["session_time"], arrival_time=d["arrival_time"],
            venue=settings.seminar_venue,
            wa_language="en", is_active=True, auto_created=True, needs_review=True,
            notes="Auto-created: day detected from the form/ad name. Please confirm, and set a WhatsApp template if invites should go out.")
    else:
        row = FormConfig(
            form_id=form_id, form_name=form_name or "", campaign_name=campaign_name or form_name or "New form",
            day="Auto", venue=settings.seminar_venue, wa_language="en",
            is_active=False, auto_created=True, needs_review=True,
            notes="Auto-discovered form. Choose the session day and activate it.")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def resolve_session(db: Session, form_id: str, *, form_name: str = "", answers_text: str = "",
                    context_names: tuple = (), ref: Optional[datetime] = None) -> dict:
    """
    Decide campaign/session details for a lead.
      answers_text   – everything the lead answered (e.g. "Which day? Sunday")
      context_names  – ad name, adset name, Meta campaign name …
    Returns a dict with `resolved` (bool) and `source`.
    """
    cfg = get_form_config(form_id, db) if form_id else None
    source = "config"

    if cfg and (cfg["day"] or "").lower() not in ("", "auto"):
        pass
    else:
        # Row missing / inactive / day=Auto → infer.
        picked = infer_day(answers_text)                                  # lead's own choice wins
        named = infer_day(form_name, *context_names)
        day = picked or named
        d = day_defaults().get(day or "")
        if day and d:
            if not picked and form_id:                                    # form itself is day-specific
                ensure_form_row(db, form_id, form_name, day,
                                campaign_name=form_name or context_names[0] if context_names else form_name)
            cfg = {
                "form_name": form_name, "campaign_name": (cfg or {}).get("campaign_name") or form_name or f"{day} session",
                "day": day, "session_time": d["session_time"], "arrival_time": d["arrival_time"],
                "venue": settings.seminar_venue, "wa_template": "",
                "wa_language": "en", "wa_params": "",
            }
            source = "answer" if picked else "name"
        else:
            if form_id:
                ensure_form_row(db, form_id, form_name)                  # placeholder for the UI
            return {
                "resolved": False, "source": "unknown", "campaign_name": form_name or "Unmapped form",
                "form_name": form_name, "session_day": "", "session_date": "", "session_time": "",
                "arrival_time": "", "venue": settings.seminar_venue,
                "wa_template": "", "wa_language": "en", "wa_params": "",
            }

    return {
        "resolved": True, "source": source,
        "campaign_name": cfg["campaign_name"], "form_name": cfg.get("form_name", "") or form_name,
        "session_day": cfg["day"],
        "session_date": next_session_date(cfg["day"], cfg.get("session_time", ""), ref),
        "session_time": cfg.get("session_time", ""), "arrival_time": cfg.get("arrival_time", ""),
        "venue": cfg.get("venue") or settings.seminar_venue,
        "wa_template": cfg.get("wa_template") or "",
        "wa_language": cfg.get("wa_language") or "en",
        "wa_params": cfg.get("wa_params", ""),
    }


# Back-compat name used by older call-sites
def resolve_session_details(form_id: str, db: Session) -> dict:
    return resolve_session(db, form_id)


def manual_session(day: str) -> dict:
    """Session details for a lead added by hand in the CRM."""
    d = day_defaults().get(day) or day_defaults()["Sunday"]
    day = day if day in day_defaults() else "Sunday"
    return {
        "session_day": day,
        "session_date": next_session_date(day, d["session_time"]),
        "session_time": d["session_time"], "arrival_time": d["arrival_time"],
        "venue": settings.seminar_venue,
    }
