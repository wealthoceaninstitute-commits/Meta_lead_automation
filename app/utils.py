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
    """
    Normalize Meta field keys for fuzzy matching.
    Colons, spaces, dashes, dots → underscore. Lowercase. Strip leading/trailing underscores.
    e.g. 'seminars_happen_sunday_from_10:30_am_to_12:30_pm' → 'seminars_happen_sunday_from_10_30_am_to_12_30_pm'
    """
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

def field_map_from_meta(lead_data: dict) -> dict:
    """
    Flatten Meta field_data list into a dict.
    Stores both the raw key and the normalized key so lookups are fuzzy.
    """
    out = {}
    for f in lead_data.get("field_data", []) or []:
        name = f.get("name")
        values = f.get("values") or []
        value = values[0] if values else ""
        if name:
            out[str(name)] = value           # exact key
            out[_normalize_key(name)] = value  # normalized key
    return out

def get_lead_field(lead_data: dict, *keys, default: str = "") -> str:
    """
    Robust extraction from a merged Meta lead dict.
    Tries exact match first, then normalized match, then scans field_data list directly.
    """
    if not isinstance(lead_data, dict):
        return default

    wanted_normalized = [_normalize_key(k) for k in keys]

    # Build a lookup that includes both raw and normalized versions of every top-level key
    lookup = {}
    for k, v in lead_data.items():
        lookup[str(k)] = v
        lookup[_normalize_key(k)] = v

    # 1. Exact key match
    for k in keys:
        v = lookup.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()

    # 2. Normalized key match
    for k in wanted_normalized:
        v = lookup.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()

    # 3. Scan raw field_data list (handles keys not yet in the flat map)
    for f in lead_data.get("field_data", []) or []:
        fname_norm = _normalize_key(f.get("name", ""))
        if fname_norm in wanted_normalized:
            vals = f.get("values") or []
            if vals and str(vals[0]).strip():
                return str(vals[0]).strip()

    # 4. Substring match on normalized keys — catches long descriptive field names
    #    e.g. 'seminars_happen_sunday_from_10_30_am_to_12_30_pm' contains 'sunday'
    for wanted_word in wanted_normalized:
        for k, v in lookup.items():
            if wanted_word in _normalize_key(k) and v is not None and str(v).strip():
                return str(v).strip()

    return default

def detect_preferred_day(data: dict) -> str:
    """
    Detect seminar day from ANY field name or value in the lead payload.
    Handles descriptive field names like 'seminars_happen_sunday_from_10:30_am_to_12:30_pm'.
    """
    # Search both keys and values
    all_text = " ".join(
        _normalize_key(str(k)) + " " + _normalize_key(str(v))
        for k, v in data.items()
        if v is not None
    ).lower()

    if "sunday" in all_text or "sun" in all_text:
        return "Sunday"
    if "thursday" in all_text or "thu" in all_text:
        return "Thursday"
    return ""

def format_indian_date(d):
    return d.strftime("%A, %d %B %Y")

def next_weekday_date(now, target_weekday, event_start):
    days_ahead = target_weekday - now.weekday()
    if days_ahead < 0:
        days_ahead += 7
    if days_ahead == 0 and now.time() >= event_start:
        days_ahead = 7
    return now.date() + timedelta(days=days_ahead)

def get_seminar_details(preferred_day: str = "", data: dict | None = None):
    tz = ZoneInfo(settings.seminar_timezone)
    now = datetime.now(tz)

    data = data or {}
    raw_pref = str(preferred_day or "").strip()

    # Try to detect day from the preferred_day string value itself
    if raw_pref:
        pref = detect_preferred_day({"__val__": raw_pref}).lower()
    else:
        pref = ""

    # Fallback: scan the full lead data dict (catches descriptive field names)
    if not pref and data:
        pref = detect_preferred_day(data).lower()

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
        d = next_weekday_date(now, cfg["weekday"], cfg["start"])
    else:
        # Default to next upcoming seminar
        candidates = []
        for _, c in schedules.items():
            d = next_weekday_date(now, c["weekday"], c["start"])
            candidates.append((datetime.combine(d, c["start"], tzinfo=tz), c, d))
        _, cfg, d = min(candidates, key=lambda x: x[0])

    return {
        "seminar_day": cfg["label"],
        "seminar_date": format_indian_date(d),
        "seminar_time": cfg["seminar_time"],
        "arrival_time": cfg["arrival_time"],
        "venue": settings.seminar_venue,
    }
