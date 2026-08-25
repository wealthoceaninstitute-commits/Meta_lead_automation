"""
WhatsApp Cloud API — sends templates and text replies.
Template selection driven by FormConfig (per-campaign template name).
"""
import requests
from sqlalchemy.orm import Session
from .config import settings
from .models import Lead, WhatsAppMessage
from .utils import now_iso, clean_phone

GRAPH_VERSION = "v21.0"


def _safe(v, fallback="-"):
    return str(v or "").strip() or fallback


def send_whatsapp_template(
    db: Session,
    lead: "Lead",
    template_name: str = None,
    language: str = "en",
    force: bool = False,
) -> dict:
    """
    Send a WhatsApp template message to a lead.
    template_name comes from FormConfig.wa_template for the lead's form_id.
    """
    if not settings.whatsapp_enabled:
        return {"ok": False, "reason": "whatsapp_disabled"}

    if not lead.phone:
        return {"ok": False, "reason": "no_phone"}

    if lead.whatsapp_sent and not force:
        return {"ok": False, "reason": "already_sent", "skipped": True}

    template = template_name or settings.whatsapp_template_name
    lang     = language or settings.whatsapp_language_code

    # Build template components based on template name
    components = _build_components(lead, template)

    url = (f"https://graph.facebook.com/{GRAPH_VERSION}/"
           f"{settings.whatsapp_phone_number_id}/messages")

    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone(lead.phone),
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": lang},
            "components": components,
        },
    }

    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    print(f"[WA] Sending template={template} to={lead.phone}", flush=True)
    r = requests.post(url, json=payload, headers=headers, timeout=20)
    print(f"[WA] Response: {r.status_code} {r.text[:300]}", flush=True)

    try:
        resp = r.json()
    except Exception:
        resp = {"error": r.text}

    if r.status_code == 200 and resp.get("messages"):
        wamid = resp["messages"][0].get("id")
        lead.whatsapp_sent       = True
        lead.whatsapp_status     = "accepted"
        lead.whatsapp_message_id = wamid
        lead.whatsapp_sent_at    = now_iso()
        db.commit()
        save_outgoing_template_message(db, lead, wamid=wamid, status="accepted", raw=resp)
        return {"ok": True, "wamid": wamid, "template": template}

    return {"ok": False, "status": r.status_code, "response": resp, "template": template}


def _build_components(lead: "Lead", template_name: str) -> list:
    """
    Build WhatsApp template components based on template name.
    Add new templates here as needed.
    """
    name         = _safe(lead.full_name, "Customer")
    campaign     = _safe(lead.campaign_name, "our programme")
    session_date = _safe(getattr(lead, "session_date", None) or getattr(lead, "seminar_date", None))
    session_time = _safe(getattr(lead, "session_time", None) or getattr(lead, "seminar_time", None))
    arrival_time = _safe(lead.arrival_time)
    venue        = _safe(lead.venue or settings.seminar_venue)

    # Friday LIVE TRADING CLASS template
    if "friday" in template_name.lower() or "trading" in template_name.lower() or "live" in template_name.lower():
        return [{
            "type": "body",
            "parameters": [
                {"type": "text", "text": name},
                {"type": "text", "text": campaign},
                {"type": "text", "text": session_date},
                {"type": "text", "text": session_time},
                {"type": "text", "text": arrival_time},
                {"type": "text", "text": venue},
            ],
        }]

    # Default Sunday seminar template (woi_seminar_registration_followup)
    return [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": session_date},
            {"type": "text", "text": session_time},
            {"type": "text", "text": arrival_time},
            {"type": "text", "text": venue},
        ],
    }]


def send_template_for_lead(db: Session, lead: "Lead", force: bool = False,
                            template_type: str = "registration") -> dict:
    """
    Legacy wrapper kept for backward compat with existing routes.
    Uses the lead's form_id to look up the right template.
    """
    from .form_config import get_form_config
    cfg = get_form_config(lead.form_id or "", db) if lead.form_id else None
    template = (cfg or {}).get("wa_template") or settings.whatsapp_template_name
    language = (cfg or {}).get("wa_language") or settings.whatsapp_language_code
    return send_whatsapp_template(db, lead, template_name=template, language=language, force=force)


def send_text_reply(db: Session, phone: str, text: str, lead: "Lead" = None) -> dict:
    if not settings.whatsapp_enabled:
        return {"ok": False, "reason": "whatsapp_disabled"}

    p = clean_phone(phone)
    url = (f"https://graph.facebook.com/{GRAPH_VERSION}/"
           f"{settings.whatsapp_phone_number_id}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "to": p, "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, json=payload, headers=headers, timeout=20)
    resp = r.json() if r.text else {}

    wamid = (resp.get("messages") or [{}])[0].get("id")
    msg = WhatsAppMessage(
        wa_message_id=wamid, lead_id=lead.id if lead else None,
        phone=p, contact_name=lead.full_name if lead else None,
        direction="outgoing", message_type="text", body=text,
        status="sent", raw=resp, timestamp=now_iso(),
    )
    db.add(msg); db.commit()
    return {"ok": r.status_code == 200, "wamid": wamid, "status": r.status_code}


def save_outgoing_template_message(db: Session, lead: "Lead", wamid: str = None,
                                    status: str = "accepted", raw: dict = None) -> "WhatsAppMessage":
    if not lead.phone:
        return None
    existing = None
    if wamid:
        existing = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.wa_message_id == wamid).first()
    if not existing and lead.id:
        existing = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.lead_id == lead.id,
            WhatsAppMessage.direction == "outgoing").first()
    if existing:
        if wamid and not existing.wa_message_id:
            existing.wa_message_id = wamid
        existing.status = status
        db.commit()
        return existing

    body = f"[Template sent to {lead.full_name or lead.phone}]"
    msg = WhatsAppMessage(
        wa_message_id=wamid, lead_id=lead.id,
        phone=clean_phone(lead.phone),
        contact_name=lead.full_name,
        direction="outgoing", message_type="template",
        body=body, status=status, raw=raw or {},
        timestamp=now_iso(),
    )
    db.add(msg); db.commit(); db.refresh(msg)
    return msg
