"""
WhatsApp Cloud API — sends templates and text replies.

Template + variables are driven by FormConfig (wa_template / wa_params), so a new
campaign never needs a code change.
"""
from sqlalchemy.orm import Session

from . import graph
from .config import settings
from .form_config import PARAM_KEYS
from .models import Lead, WhatsAppMessage
from .utils import now_iso, clean_phone

# WhatsApp error codes that will never succeed on retry
PERMANENT_WA_CODES = {
    131026,  # message undeliverable (not on WhatsApp)
    131030,  # recipient not in allowed list (test number)
    131021,  # recipient same as sender
    132000, 132001, 132005, 132007, 132012, 132015, 132016,  # template / parameter problems
    131009,  # parameter value invalid
    131051,  # unsupported message type
    133010,  # phone number not registered
}


def _safe(v, fallback="-"):
    return str(v or "").strip() or fallback


def _values(lead: "Lead") -> dict:
    return {
        "name": _safe(lead.full_name, "Customer"),
        "campaign": _safe(lead.campaign_name, "our programme"),
        "session_day": _safe(lead.session_day),
        "session_date": _safe(lead.session_date),
        "session_time": _safe(lead.session_time),
        "arrival_time": _safe(lead.arrival_time),
        "venue": _safe(lead.venue or settings.seminar_venue),
        "city": _safe(lead.city),
    }


def _build_components(lead: "Lead", template_name: str, params_spec: str = "") -> list:
    """Body parameters in the order given by `params_spec` ("name,session_date,…"),
    taken from the form's config. No built-in layouts: no spec = no variables."""
    vals = _values(lead)
    keys = [k.strip() for k in (params_spec or "").split(",") if k.strip() in PARAM_KEYS]
    if not keys:
        return []          # template without body variables
    return [{"type": "body", "parameters": [{"type": "text", "text": vals[k]} for k in keys]}]


def _fmt_error(e: "graph.GraphError") -> str:
    return str(e)[:400]


def send_whatsapp_template(
    db: Session,
    lead: "Lead",
    template_name: str = None,
    language: str = "en",
    force: bool = False,
    params_spec: str = "",
) -> dict:
    """Send a template to a lead. Never raises: returns {ok, retryable, error,…}."""
    if not settings.whatsapp_enabled:
        return {"ok": False, "reason": "whatsapp_disabled", "retryable": False}
    if not lead.phone:
        return {"ok": False, "reason": "no_phone", "retryable": False}
    if (lead.whatsapp_sent or lead.whatsapp_message_id) and not force:
        return {"ok": False, "reason": "already_sent", "skipped": True, "retryable": False}
    if not (template_name or "").strip():
        return {"ok": False, "reason": "no_template", "retryable": False,
                "error": "No WhatsApp template is set for this form (CRM → Form Config)"}
    if not settings.whatsapp_phone_number_id:
        return {"ok": False, "reason": "no_phone_number_id", "retryable": False,
                "error": "WHATSAPP_PHONE_NUMBER_ID is not set"}

    template = template_name.strip()
    lang = language or "en"
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone(lead.phone),
        "type": "template",
        "template": {"name": template, "language": {"code": lang},
                     "components": _build_components(lead, template, params_spec)},
    }
    print(f"[WA] Sending template={template} to={lead.phone}", flush=True)
    lead.wa_attempts = (lead.wa_attempts or 0) + 1
    try:
        resp = graph.post(f"{settings.whatsapp_phone_number_id}/messages", kind="wa", json=payload, retries=1)
    except graph.GraphError as e:
        permanent = e.code in PERMANENT_WA_CODES
        lead.whatsapp_error = _fmt_error(e)
        if permanent:
            lead.whatsapp_failed = True
            lead.whatsapp_status = "failed"
            lead.whatsapp_failed_at = now_iso()
        db.commit()
        print(f"[WA] FAILED {e}", flush=True)
        return {"ok": False, "error": str(e), "kind": e.kind, "code": e.code,
                "retryable": (not permanent) and e.kind in ("token", "rate", "transient", "other"),
                "template": template}

    wamid = (resp.get("messages") or [{}])[0].get("id")
    lead.whatsapp_sent = True
    lead.whatsapp_status = "accepted"
    lead.whatsapp_message_id = wamid
    lead.whatsapp_sent_at = now_iso()
    lead.whatsapp_error = None
    lead.whatsapp_failed = False
    db.commit()
    save_outgoing_template_message(db, lead, wamid=wamid, status="accepted", raw=resp)
    return {"ok": True, "wamid": wamid, "template": template}


def send_template_for_lead(db: Session, lead: "Lead", force: bool = False,
                           template_type: str = "registration") -> dict:
    """Used by the manual 'Send Invite' button: picks template/params from the lead's form config."""
    from .form_config import get_form_config
    cfg = get_form_config(lead.form_id or "", db) if lead.form_id else None
    template = (cfg or {}).get("wa_template") or ""
    language = (cfg or {}).get("wa_language") or "en"
    return send_whatsapp_template(db, lead, template_name=template, language=language, force=force,
                                  params_spec=(cfg or {}).get("wa_params", ""))


def send_text_reply(db: Session, phone: str, text: str, lead: "Lead" = None) -> dict:
    if not settings.whatsapp_enabled:
        return {"ok": False, "reason": "whatsapp_disabled"}
    p = clean_phone(phone)
    payload = {"messaging_product": "whatsapp", "to": p, "type": "text", "text": {"body": text}}
    try:
        resp = graph.post(f"{settings.whatsapp_phone_number_id}/messages", kind="wa", json=payload, retries=1)
    except graph.GraphError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "code": e.code}

    wamid = (resp.get("messages") or [{}])[0].get("id")
    db.add(WhatsAppMessage(
        wa_message_id=wamid, lead_id=lead.id if lead else None,
        phone=p, contact_name=lead.full_name if lead else None,
        direction="outgoing", message_type="text", body=text,
        status="sent", raw=resp, timestamp=now_iso(),
    ))
    db.commit()
    return {"ok": True, "wamid": wamid}


def save_outgoing_template_message(db: Session, lead: "Lead", wamid: str = None,
                                   status: str = "accepted", raw: dict = None) -> "WhatsAppMessage":
    if not lead.phone:
        return None
    existing = None
    if wamid:
        existing = db.query(WhatsAppMessage).filter(WhatsAppMessage.wa_message_id == wamid).first()
    elif lead.id:
        # maintenance backfill (no wamid): never create a duplicate bubble
        existing = db.query(WhatsAppMessage).filter(
            WhatsAppMessage.lead_id == lead.id, WhatsAppMessage.direction == "outgoing").first()
    if existing:
        if wamid and not existing.wa_message_id:
            existing.wa_message_id = wamid
        existing.status = status
        db.commit()
        return existing

    msg = WhatsAppMessage(
        wa_message_id=wamid, lead_id=lead.id, phone=clean_phone(lead.phone),
        contact_name=lead.full_name, direction="outgoing", message_type="template",
        body=f"[Template sent to {lead.full_name or lead.phone}]", status=status,
        raw=raw or {}, timestamp=now_iso(),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg
