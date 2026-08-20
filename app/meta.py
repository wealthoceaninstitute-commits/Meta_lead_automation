import os
import requests
from sqlalchemy.orm import Session
from .config import settings
from .models import Lead, WhatsAppMessage, WhatsAppStatusLog
from .utils import clean_phone, field_map_from_meta, get_lead_field, get_seminar_details, now_iso, detect_preferred_day
from .whatsapp import send_template_for_lead, save_outgoing_template_message

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")

_NAME_CACHE: dict = {}


def _clean_meta_value(value):
    text = str(value or "").strip()
    for prefix in ("l:", "ag:", "as:", "c:", "f:", "p:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def normalize_source(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in ("ig", "instagram"):
        return "IG"
    if text in ("fb", "facebook"):
        return "FB"
    if "instagram" in text:
        return "IG"
    if "facebook" in text:
        return "FB"
    return text.upper()


def _extract_incoming_text(m: dict, msg_type: str) -> str:
    try:
        if msg_type == "text":
            return (m.get("text") or {}).get("body", "") or "[empty message]"
        if msg_type == "button":
            return (m.get("button") or {}).get("text", "") or "[button reply]"
        if msg_type == "interactive":
            inter = m.get("interactive") or {}
            itype = inter.get("type", "")
            if itype == "button_reply":
                return (inter.get("button_reply") or {}).get("title", "") or "[button reply]"
            if itype == "list_reply":
                lr = inter.get("list_reply") or {}
                return lr.get("title", "") or lr.get("description", "") or "[list reply]"
            return "[interactive reply]"
        if msg_type == "reaction":
            emoji = (m.get("reaction") or {}).get("emoji", "")
            return f"Reacted: {emoji}" if emoji else "[reaction]"
        if msg_type in ("image", "video", "document", "audio", "sticker", "voice"):
            caption = (m.get(msg_type) or {}).get("caption", "")
            label = {"image": "📷 Photo", "video": "🎥 Video", "document": "📄 Document",
                     "audio": "🎵 Audio", "voice": "🎙️ Voice message", "sticker": "Sticker"}.get(msg_type, msg_type)
            return f"{label}{(': ' + caption) if caption else ''}"
        if msg_type == "location":
            loc = m.get("location") or {}
            nm = loc.get("name") or ""
            return f"📍 Location{(': ' + nm) if nm else ''}"
        if msg_type == "contacts":
            return "👤 Contact card"
        if msg_type == "unsupported":
            errs = m.get("errors") or []
            if errs:
                title = errs[0].get("title", "") or errs[0].get("message", "")
                return f"[Unsupported message{(': ' + title) if title else ''}]"
            return "[Unsupported message type]"
        return f"[{msg_type} message]"
    except Exception as exc:
        print(f"[WARN] Failed to extract incoming text for type={msg_type}: {exc}", flush=True)
        return f"[{msg_type}]"


def get_meta_token():
    token = (
        os.getenv("META_PAGE_ACCESS_TOKEN")
        or os.getenv("META_ACCESS_TOKEN")
        or os.getenv("PAGE_ACCESS_TOKEN")
        or os.getenv("GRAPH_ACCESS_TOKEN")
        or os.getenv("FACEBOOK_ACCESS_TOKEN")
        or getattr(settings, "meta_access_token", "")
    )
    return str(token or "").strip()


def graph_get(object_id: str, fields: str, timeout: int = 25):
    token = get_meta_token()
    object_id = _clean_meta_value(object_id)

    if not token:
        print("Meta lead fetch skipped: no token found.", flush=True)
        return {"id": object_id, "field_data": [], "fetch_error": "missing_meta_token"}

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{object_id}"
    params = {"access_token": token, "fields": fields}
    response = requests.get(url, params=params, timeout=timeout)
    print(f"Meta GET /{object_id}?fields={fields}: {response.status_code} {response.text[:300]}", flush=True)

    try:
        data = response.json()
    except Exception:
        data = {"id": object_id, "field_data": [], "error_text": response.text}

    if response.status_code >= 400:
        data.setdefault("id", object_id)
        data.setdefault("field_data", [])
        data["fetch_error"] = "graph_error"
        data["http_status"] = response.status_code

    return data


def fetch_lead_details(lead_id: str):
    safe_fields = "id,created_time,field_data,ad_id,form_id,is_organic,platform"
    return graph_get(lead_id, safe_fields, timeout=30)


def fetch_optional_object(object_id: str, fields: str):
    object_id = _clean_meta_value(object_id)
    if not object_id:
        return {}

    cache_key = f"{object_id}:{fields}"
    if cache_key in _NAME_CACHE:
        print(f"[CACHE] Hit for {object_id}", flush=True)
        return _NAME_CACHE[cache_key]

    data = graph_get(object_id, fields, timeout=15)
    if data.get("fetch_error"):
        err_code = (data.get("error") or {}).get("code")
        if err_code == 4:
            print(f"[WARN] Rate limit hit for {object_id} — will retry on next lead", flush=True)
        else:
            print(f"Optional Meta object fetch failed for {object_id}: {data}", flush=True)
        return {}

    _NAME_CACHE[cache_key] = data
    return data


def enrich_ad_form_metadata(data: dict, webhook_value: dict | None = None):
    webhook_value = webhook_value or {}

    webhook_adgroup = _clean_meta_value(webhook_value.get("adgroup_id") or "")
    webhook_ad_id   = _clean_meta_value(webhook_value.get("ad_id") or "")

    data["ad_id"]       = _clean_meta_value(data.get("ad_id") or webhook_ad_id or "")
    data["form_id"]     = _clean_meta_value(data.get("form_id") or webhook_value.get("form_id") or "")
    data["adset_id"]    = _clean_meta_value(data.get("adset_id") or webhook_value.get("adset_id") or webhook_adgroup or "")
    data["campaign_id"] = _clean_meta_value(data.get("campaign_id") or webhook_value.get("campaign_id") or "")

    source = (
        data.get("platform") or data.get("source")
        or webhook_value.get("platform") or webhook_value.get("source")
        or webhook_value.get("publisher_platform") or ""
    )
    data["platform"] = normalize_source(source)

    if data.get("form_id"):
        form = fetch_optional_object(data["form_id"], "id,name")
        data["form_name"] = data.get("form_name") or form.get("name", "")

    if data.get("ad_id") and data["ad_id"] != data.get("adset_id"):
        ad = fetch_optional_object(data["ad_id"], "id,name,adset_id,campaign_id")
        if ad:
            data["ad_name"]     = data.get("ad_name") or ad.get("name", "")
            data["adset_id"]    = _clean_meta_value(data.get("adset_id") or ad.get("adset_id", ""))
            data["campaign_id"] = _clean_meta_value(data.get("campaign_id") or ad.get("campaign_id", ""))
        else:
            print(f"[INFO] ad_id={data['ad_id']} fetch failed — trying as adset_id", flush=True)
            if not data.get("adset_id"):
                data["adset_id"] = data["ad_id"]

    if data.get("adset_id"):
        adset = fetch_optional_object(data["adset_id"], "id,name,campaign_id")
        if adset:
            data["adset_name"]  = data.get("adset_name") or adset.get("name", "")
            data["campaign_id"] = _clean_meta_value(data.get("campaign_id") or adset.get("campaign_id", ""))

    if data.get("campaign_id"):
        campaign = fetch_optional_object(data["campaign_id"], "id,name")
        if campaign:
            data["campaign_name"] = data.get("campaign_name") or campaign.get("name", "")

    if not data.get("campaign_name"):
        fallback = data.get("adset_name") or data.get("ad_name") or data.get("form_name")
        if fallback:
            data["campaign_name"] = fallback
            print(f"[INFO] Using fallback for campaign_name: {fallback!r}", flush=True)
        elif data.get("adset_id"):
            data["campaign_name"] = f"Ad Set {data['adset_id']}"
            print(f"[INFO] No names resolved — using raw adset_id: {data['adset_id']}", flush=True)

    return data


def parse_lead_fields(data: dict) -> dict:
    """
    Extract all lead fields from the merged Meta payload.
    Handles any form field naming convention including long descriptive names
    like 'seminars_happen_sunday_from_10:30_am_to_12:30_pm'.
    Returns a clean dict ready to write to the Lead model.
    """
    fields = field_map_from_meta(data)
    merged = {**data, **fields}

    # --- Name ---
    name = get_lead_field(merged,
        "full_name", "full name", "name", "your_name", "your name",
        "customer_name", "first_name", "first name", "contact_name")

    # --- Phone ---
    raw_phone = get_lead_field(merged,
        "phone", "phone_number", "phone number", "mobile", "mobile_number",
        "mobile number", "whatsapp_number", "whatsapp number",
        "your_phone_number", "your mobile number", "contact_number")
    phone = clean_phone(raw_phone)

    # --- Email ---
    email = get_lead_field(merged, "email", "email_address", "email address")

    # --- City ---
    city = get_lead_field(merged, "city", "location", "place", "your_city")

    # --- Experience ---
    experience = get_lead_field(merged,
        "what_is_your_experience_level_in_stock_market?",
        "what_is_your_current_experience_level?",
        "what is your current experience level?",
        "experience", "experience_level", "current experience level",
        "stock_market_experience", "your_experience")

    # --- Preferred Day ---
    # Try explicit day field first, then fall back to scanning all keys/values
    preferred_day = get_lead_field(merged,
        "please_choose_a_day_for_the_free_seminar",
        "please choose a day for the free seminar",
        "which_session_will_you_attend?",
        "which session will you attend?",
        "seminar_day", "seminar day", "preferred_day", "preferred day",
        "choose_day", "day", "session")

    # If still empty, detect from field names/values (e.g. 'seminars_happen_sunday_from_10:30...')
    if not preferred_day:
        preferred_day = detect_preferred_day(merged)

    seminar = get_seminar_details(preferred_day, merged)

    print(f"[PARSE] name={name!r} phone={phone!r} email={email!r} "
          f"city={city!r} experience={experience!r} preferred_day={preferred_day!r} "
          f"seminar={seminar}", flush=True)

    return {
        "full_name": name,
        "phone": phone,
        "email": email,
        "city": city,
        "experience": experience,
        "preferred_day": preferred_day,
        **seminar,
    }


def upsert_lead_from_meta(db: Session, lead_id: str, raw: dict | None = None,
                           auto_send=True, webhook_value: dict | None = None):
    print("Processing leadgen id:", lead_id, flush=True)
    data = raw or fetch_lead_details(lead_id)
    data = enrich_ad_form_metadata(data, webhook_value=webhook_value)

    parsed = parse_lead_fields(data)

    lead = db.query(Lead).filter(Lead.meta_lead_id == str(lead_id)).first()
    created = False
    if not lead:
        lead = Lead(meta_lead_id=str(lead_id))
        created = True

    lead.created_time  = data.get("created_time") or lead.created_time
    lead.full_name     = parsed["full_name"] or lead.full_name
    lead.phone         = parsed["phone"] or lead.phone
    lead.email         = parsed["email"] or lead.email
    lead.city          = parsed["city"] or lead.city
    lead.experience    = parsed["experience"] or lead.experience
    lead.preferred_day = parsed["preferred_day"] or lead.preferred_day
    lead.status        = lead.status or "New"

    # Seminar details
    lead.seminar_day  = parsed["seminar_day"] or lead.seminar_day
    lead.seminar_date = parsed["seminar_date"] or lead.seminar_date
    lead.seminar_time = parsed["seminar_time"] or lead.seminar_time
    lead.arrival_time = parsed["arrival_time"] or lead.arrival_time
    lead.venue        = parsed["venue"] or lead.venue

    # Ad/campaign metadata
    for k in ["campaign_id", "campaign_name", "adset_id", "adset_name",
              "ad_id", "ad_name", "form_id", "form_name", "platform", "is_organic"]:
        value = data.get(k)
        if k == "platform":
            value = normalize_source(value)
        if value:
            setattr(lead, k, value)

    lead.raw = {**data, **parsed}

    db.add(lead)
    db.commit()
    db.refresh(lead)

    print("Lead saved:", {
        "id": lead.id, "meta_lead_id": lead.meta_lead_id,
        "name": lead.full_name, "phone": lead.phone,
        "campaign": lead.campaign_name, "seminar_day": lead.seminar_day,
        "created": created,
        "fetch_error": (lead.raw or {}).get("fetch_error"),
    }, flush=True)

    wa = None
    if auto_send and not lead.whatsapp_sent and not lead.whatsapp_message_id:
        if not lead.phone:
            print("WhatsApp skipped: phone missing", flush=True)
            wa = {"ok": False, "skipped": True, "reason": "phone_missing_after_db_save", "lead_id": lead.id}
        else:
            wa = send_template_for_lead(db, lead)
            db.refresh(lead)
            print("WhatsApp result:", wa, flush=True)

    return lead, wa


def handle_leadgen_payload(db: Session, payload: dict):
    lead_ids = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            lead_id = value.get("leadgen_id") or value.get("lead_id")
            if lead_id:
                lead_ids.append(str(lead_id))
                upsert_lead_from_meta(db, str(lead_id), auto_send=True, webhook_value=value)
    return {"lead_ids": lead_ids}


def _find_or_create_lead_by_phone(db: Session, phone: str, name: str = ""):
    phone = clean_phone(phone)
    lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.id.desc()).first()
    if not lead:
        lead = Lead(phone=phone, full_name=name or None, status="WhatsApp")
        db.add(lead)
        db.commit()
        db.refresh(lead)
    return lead


def handle_whatsapp_payload(db: Session, payload: dict):
    statuses, messages = [], []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                        for c in value.get("contacts", []) or []}
            for st in value.get("statuses", []) or []:
                mid, status = st.get("id"), st.get("status")
                ts = st.get("timestamp")
                db.add(WhatsAppStatusLog(wa_message_id=mid, status=status,
                    recipient_id=st.get("recipient_id"), timestamp=ts, raw=st))
                lead = db.query(Lead).filter(Lead.whatsapp_message_id == mid).first()
                msg = db.query(WhatsAppMessage).filter(WhatsAppMessage.wa_message_id == mid).first()
                if msg:
                    _rank = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
                    _cur = (msg.status or "accepted").lower()
                    if _cur == "accepted" or _rank.get(status, 0) > _rank.get(_cur, 0):
                        msg.status = status
                    if status == "failed":
                        msg.raw = {
                            "status": "failed", "timestamp": ts,
                            "recipient_id": st.get("recipient_id"),
                            "errors": st.get("errors") or [],
                        }
                if lead and not msg:
                    _bf_raw = {"source": "status_webhook_backfill", "status": st}
                    if status == "failed":
                        _bf_raw["errors"] = st.get("errors") or []
                    msg = save_outgoing_template_message(db, lead, wamid=mid, status=status, raw=_bf_raw)
                if lead:
                    lead.whatsapp_status = status
                    lead.whatsapp_last_status_at = now_iso()
                    if status == "sent":
                        lead.whatsapp_sent = True
                        lead.whatsapp_sent_at = lead.whatsapp_sent_at or now_iso()
                    elif status == "delivered":
                        lead.whatsapp_delivered = True
                        lead.whatsapp_delivered_at = now_iso()
                    elif status == "read":
                        lead.whatsapp_read = True
                        lead.whatsapp_read_at = now_iso()
                    elif status == "failed":
                        lead.whatsapp_failed = True
                        lead.whatsapp_failed_at = now_iso()
                statuses.append(f"{mid}:{status}:{bool(lead)}")
            for m in value.get("messages", []) or []:
                phone = clean_phone(m.get("from"))
                name = contacts.get(phone) or ""
                msg_type = m.get("type", "unknown")
                text = _extract_incoming_text(m, msg_type)
                lead = _find_or_create_lead_by_phone(db, phone, name)
                lead.latest_reply_text = text
                lead.latest_reply_at = now_iso()
                lead.unread_count = (lead.unread_count or 0) + 1
                db.add(WhatsAppMessage(wa_message_id=m.get("id"), lead_id=lead.id,
                    phone=phone, contact_name=name or lead.full_name,
                    direction="incoming", message_type=msg_type, body=text,
                    raw=m, timestamp=m.get("timestamp")))
                messages.append(f"{phone}:{msg_type}")
    db.commit()
    return {"statuses": statuses, "messages": messages}


def classify_webhook_and_handle(db: Session, payload: dict):
    text = str(payload)
    if "leadgen_id" in text or "lead_id" in text:
        return {"type": "leadgen", **handle_leadgen_payload(db, payload)}
    if "statuses" in text or "messages" in text:
        return {"type": "whatsapp", **handle_whatsapp_payload(db, payload)}
    return {"type": "unknown"}
