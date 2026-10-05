from datetime import datetime, timezone
import re


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits


def first_nonempty(*vals):
    for v in vals:
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def get_seminar_details(preferred_day: str = "", data: dict | None = None):
    """Session details for a manually added lead (kept for old call-sites)."""
    from .form_config import manual_session, infer_day
    day = infer_day(preferred_day) or "Sunday"
    s = manual_session(day)
    return {
        "session_day": s["session_day"], "session_date": s["session_date"],
        "session_time": s["session_time"], "arrival_time": s["arrival_time"], "venue": s["venue"],
    }
