"""
Meta lead ingestion — new architecture.

Flow:
  Webhook → extract form_id + ad_id + lead_id
          → fetch field_data from Graph API
          → resolve campaign/session from FormConfig (DB) by form_id
          → save Lead
          → send WhatsApp using template from FormConfig
"""

import os
import re
import requests
from sqlalchemy.orm import Session
from .config import settings
from .models import Lead, WhatsAppMessage, WhatsAppStatusLog
from .utils import clean_phone, now_iso
from .form_config import resolve_session_details
from .whatsapp import send_whatsapp_template, save_outgoing_template_message

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")
_CACHE: dict = {}


def _clean(value):
    """Strip Meta CSV/webhook prefixes: l: ag: as: c: f: p:"""
    text = str(value or "").strip()
    for p in ("l:", "ag:", "as:", "c:", "f:", "p:"):
        if text.startswith(p):
            return text[len(p):]
    return text


def _norm(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _source(value):
    v = str(value or "").strip().lower()
    if v in ("ig", "instagram") or "instagram" in v: return "IG"
    if v in ("fb", "facebook") or "facebook" in v:   return "FB"
    return v.upper() if v else ""


def get_token():
    for env in ["META_PAGE_ACCESS_TOKEN", "META_ACCESS_TOKEN",
                "PAGE_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN"]:
        t = os.getenv(env, "").strip()
        if t:
            return t
    return ""


def graph_get(object_id: str, fields: str, timeout: int = 20):
    token = get_token()
    oid   = _clean(object_id)
    if not token:
        return {"id": oid, "field_data": [], "fetch_error": "no_token"}

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{oid}"
    r   = requests.get(url, params={"access_token": token, "fields": fields}, timeout=timeout)
    print(f"[Graph] GET /{oid}?fields={fields} → {r.status_code}", flush=True)

    try:
        data = r.json()
    except Exception:
        return {"id": oid, "field_data": [], "fetch_error": "json_parse_error"}

    if r.status_code >= 400:
        data.setdefault("id", oid)
        data.setdefault("field_data", [])
        data["fetch_error"] = "graph_error"
    return data


def fetch_lead_data(lead_id: str) -> dict:
    return graph_get(lead_id, "id,created_time,field_data,form_id,platform,is_organic,ad_id")


def _field_map(field_data: list) -> dict:
    """Flatten field_data list to a dict with both raw and normalized keys."""
    out = {}
    for f in field_data or []:
        name = f.get("name", "")
        vals = f.get("values") or []
        val  = vals[0] if vals else ""
        out[name] = val
        out[_norm(name)] = val
    return out


def _get_field(flat: dict, *keys, default="") -> str:
    wanted = [_norm(k) for k in keys]
    for k in keys:
        v = flat.get(k)
        if v and str(v).strip(): return str(v).strip()
    for k in wanted:
        v = flat.get(k)
        if v and str(v).strip(): return str(v).strip()
    # Substring match on normalized field names (handles long descriptive names)
    for want in wanted:
        for fk, fv in flat.items():
            if want in _norm(fk) and fv and str(fv).strip():
                return str(fv).strip()
    return default


def _extract_incoming_text(m: dict, msg_type: str) -> str:
    try:
        if msg_type == "text":
            return (m.get("text") or {}).get("body", "") or "[empty]"
        if msg_type == "button":
            return (m.get("button") or {}).get("text", "") or "[button]"
        if msg_type == "interactive":
            inter = m.get("interactive") or {}
            itype = inter.get("type", "")
            if itype == "button_reply":
                return (inter.get("button_reply") or {}).get("title", "") or "[button reply]"
            if itype == "list_reply":
                lr = inter.get("list_reply") or {}
                return lr.get("title", "") or lr.get("description", "") or "[list reply]"
        media_types = {"image": "📷 Photo", "video": "🎥 Video", "document": "📄 Document",
                       "audio": "🎵 Audio", "voice": "🎙️ Voice", "sticker": "Sticker"}
        if msg_type in media_types:
            cap = (m.get(msg_type) or {}).get("caption", "")
            return f"{media_types[msg_type]}{': ' + cap if cap else ''}"
        return f"[{msg_type}]"
    except Exception:
        return f"[{msg_type}]"


def upsert_lead_from_meta(db: Session, lead_id: str, raw: dict | None = None,
                           auto_send: bool = True, webhook_value: dict | None = None):
    """
    Main entry point. Fetches lead, resolves campaign from form_id, saves to DB, sends WA.
    """
    print(f"[Meta] Processing lead {lead_id}", flush=True)
    wv = webhook_value or {}

    # 1. Fetch field_data from Graph API
    data = raw or fetch_lead_data(lead_id)

    # 2. Extract IDs from both Graph response and webhook value
    form_id = _clean(data.get("form_id") or wv.get("form_id") or "")
    ad_id   = _clean(data.get("ad_id")   or wv.get("adgroup_id") or wv.get("ad_id") or "")
    platform = _source(data.get("platform") or wv.get("platform") or wv.get("publisher_platform") or "")

    # 3. Resolve campaign + session details from FormConfig (DB → fallback)
    session = resolve_session_details(form_id, db)
    print(f"[Meta] form_id={form_id} → campaign={session['campaign_name']} day={session['session_day']}", flush=True)

    # 4. Parse field_data
    fields = _field_map(data.get("field_data", []))
    merged = {**data, **fields}

    name = _get_field(merged, "full_name", "full name", "name", "first_name", "your_name")

    raw_phone = _get_field(merged, "phone", "phone_number", "mobile",
                            "mobile_number", "whatsapp_number", "your_phone_number")
    phone = clean_phone(_clean(raw_phone))

    email = _get_field(merged, "email", "email_address")
    city  = _get_field(merged, "city", "location", "place")
    exp   = _get_field(merged, "what_is_your_current_experience_level?",
                        "what_is_your_experience_level_in_stock_market?", "experience")

    print(f"[Meta] Parsed: name={name!r} phone={phone!r} email={email!r}", flush=True)

    # 5. Upsert lead
    lead = db.query(Lead).filter(Lead.meta_lead_id == str(lead_id)).first()
    created = not bool(lead)
    if not lead:
        lead = Lead(meta_lead_id=str(lead_id))

    lead.created_time  = data.get("created_time") or lead.created_time
    lead.full_name     = name     or lead.full_name
    lead.phone         = phone    or lead.phone
    lead.email         = email    or lead.email
    lead.city          = city     or lead.city
    lead.experience    = exp      or lead.experience
    lead.status        = lead.status or "New"

    # Campaign from FormConfig
    lead.form_id       = form_id  or lead.form_id
    lead.form_name     = session["form_name"]   or lead.form_name
    lead.campaign_name = session["campaign_name"] or lead.campaign_name
    lead.ad_id         = ad_id    or lead.ad_id
    lead.platform      = platform or lead.platform
    lead.preferred_day = session["session_day"] or lead.preferred_day
    lead.is_organic    = str(data.get("is_organic", "")).lower() or lead.is_organic

    # Session details
    lead.session_day   = session["session_day"]
    lead.session_date  = session["session_date"]
    lead.session_time  = session["session_time"]
    lead.arrival_time  = session["arrival_time"]
    lead.venue         = session["venue"]

    lead.raw = {
        "webhook_value": wv,
        "graph_data": data,
        "form_id": form_id,
        "ad_id": ad_id,
        "session": session,
        "fetch_error": data.get("fetch_error"),
    }

    db.add(lead)
    db.commit()
    db.refresh(lead)

    print(f"[Meta] Lead saved: id={lead.id} name={lead.full_name} phone={lead.phone} "
          f"campaign={lead.campaign_name} session_day={lead.session_day} created={created}", flush=True)

    # 6. Send WhatsApp
    wa = None
    if auto_send and not lead.whatsapp_sent and not lead.whatsapp_message_id:
        if not lead.phone:
            print("[WA] Skipped — no phone", flush=True)
            wa = {"ok": False, "reason": "no_phone"}
        else:
            wa = send_whatsapp_template(
                db, lead,
                template_name=session["wa_template"],
                language=session["wa_language"],
            )
            db.refresh(lead)
            print(f"[WA] Result: {wa}", flush=True)

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
                msg  = db.query(WhatsAppMessage).filter(WhatsAppMessage.wa_message_id == mid).first()

                if msg:
                    _rank = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
                    cur = (msg.status or "accepted").lower()
                    if _rank.get(status, 0) >= _rank.get(cur, 0):
                        msg.status = status

                if lead:
                    lead.whatsapp_status        = status
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
                statuses.append(f"{mid}:{status}")

            for m in value.get("messages", []) or []:
                phone    = clean_phone(m.get("from"))
                name     = contacts.get(phone) or ""
                msg_type = m.get("type", "unknown")
                text     = _extract_incoming_text(m, msg_type)

                lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.id.desc()).first()
                if not lead:
                    lead = Lead(phone=phone, full_name=name or None, status="WhatsApp")
                    db.add(lead); db.commit(); db.refresh(lead)

                lead.latest_reply_text = text
                lead.latest_reply_at   = now_iso()
                lead.unread_count      = (lead.unread_count or 0) + 1
                db.add(WhatsAppMessage(
                    wa_message_id=m.get("id"), lead_id=lead.id,
                    phone=phone, contact_name=name or lead.full_name,
                    direction="incoming", message_type=msg_type, body=text,
                    raw=m, timestamp=m.get("timestamp"),
                ))
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
