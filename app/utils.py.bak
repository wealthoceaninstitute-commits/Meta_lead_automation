from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from .config import settings

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def clean_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if len(digits) == 10:
        return "91" + digits
    return digits

def first_nonempty(*vals):
    for v in vals:
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def _normalize_key(value: str) -> str:
    """Normalize Meta/CSV field keys so variants like phone_number, Phone Number, phone-number match."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

def field_map_from_meta(lead_data: dict):
    """
    Convert Meta field_data list into a flat dictionary.
    Keeps the original field key and also a normalized key.
    Example: phone_number, Phone Number, phone-number -> phone_number.
    """
    out = {}

    for f in lead_data.get("field_data", []) or []:
        name = f.get("name")
        values = f.get("values") or []
        value = values[0] if values else ""
        if name:
            out[str(name)] = value
            out[_normalize_key(name)] = value

    return out

def get_lead_field(lead_data: dict, *keys, default: str = "") -> str:
    """
    Robust extraction from Meta lead payload. Supports:
    1) flat keys from Graph API response
    2) normalized keys from field_map_from_meta
    3) raw field_data list
    """
    if not isinstance(lead_data, dict):
        return default

    wanted = [_normalize_key(k) for k in keys]

    # direct + normalized lookup over all top-level keys
    normalized = {}
    for k, v in lead_data.items():
        normalized[str(k)] = v
        normalized[_normalize_key(k)] = v

    for k in keys:
        value = normalized.get(k)
        if value is not None and str(value).strip():
            return str(value).strip()

    for k in wanted:
        value = normalized.get(k)
        if value is not None and str(value).strip():
            return str(value).strip()

    # Meta field_data fallback
    for f in lead_data.get("field_data", []) or []:
        fname = _normalize_key(f.get("name"))
        if fname in wanted:
            values = f.get("values") or []
            if values and str(values[0]).strip():
                return str(values[0]).strip()

    return default

def detect_preferred_day(data: dict) -> str:
    text = " ".join(str(v or "") for v in data.values()).lower()
    if "sunday" in text or "sun" in text:
        return "Sunday"
    if "thursday" in text or "thu" in text:
        return "Thursday"
    return ""

def format_indian_date(d):
    return d.strftime("%A, %d %B %Y")

def next_weekday_date(now, target_weekday, event_start):
    days_ahead = target_weekday - now.weekday()  # Monday=0
    if days_ahead < 0:
        days_ahead += 7
    if days_ahead == 0 and now.time() >= event_start:
        days_ahead = 7
    return now.date() + timedelta(days=days_ahead)

def get_seminar_details(preferred_day: str = "", data: dict | None = None):
    tz = ZoneInfo(settings.seminar_timezone)
    now = datetime.now(tz)

    # Meta form sends values like "Sunday, 10AM" or "Thursday, 6PM" — not bare day names.
    # Normalize: extract just the day word so the schedules dict lookup works correctly.
    raw_pref = (preferred_day or detect_preferred_day(data or {})).strip()
    if raw_pref:
        # detect_preferred_day already handles keyword extraction; apply it to the raw value too
        pref = detect_preferred_day({"__val__": raw_pref}).lower() or raw_pref.lower()
    else:
        pref = ""

    schedules = {
        "thursday": {
            "label": "Thursday", "weekday": 3, "start": time(18, 0),
            "seminar_time": settings.seminar_thursday_time,
            "arrival_time": settings.seminar_thursday_arrival,
        },
        "sunday": {
            "label": "Sunday", "weekday": 6, "start": time(10, 30),
            "seminar_time": settings.seminar_sunday_time,
            "arrival_time": settings.seminar_sunday_arrival,
        },
    }
    if pref in schedules:
        cfg = schedules[pref]
    else:
        candidates = []
        for _, c in schedules.items():
            d = next_weekday_date(now, c["weekday"], c["start"])
            candidates.append((datetime.combine(d, c["start"], tzinfo=tz), c, d))
        _, cfg, chosen_date = min(candidates, key=lambda x: x[0])
        return {
            "seminar_day": cfg["label"], "seminar_date": format_indian_date(chosen_date),
            "seminar_time": cfg["seminar_time"], "arrival_time": cfg["arrival_time"],
            "venue": settings.seminar_venue,
        }
    d = next_weekday_date(now, cfg["weekday"], cfg["start"])
    return {
        "seminar_day": cfg["label"], "seminar_date": format_indian_date(d),
        "seminar_time": cfg["seminar_time"], "arrival_time": cfg["arrival_time"],
        "venue": settings.seminar_venue,
    }
