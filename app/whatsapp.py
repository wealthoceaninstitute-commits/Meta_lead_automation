"""
WhatsApp Cloud API — sends templates and text replies.

Template + variables are driven by FormConfig (wa_template / wa_params), so a new
campaign never needs a code change.
"""
import re

from sqlalchemy.orm import Session

from . import graph
from .config import settings
from .form_config import PARAM_KEYS
from .models import Lead, WhatsAppMessage, WhatsAppTemplate, TemplateHeaderImage
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


class TemplateConfigError(Exception):
    """The template and the form's configuration don't fit together. Raised BEFORE
    anything is sent, so the message tells you exactly what to fix (instead of Meta's
    generic #132000 'number of parameters does not match')."""


# Friendly names people type in Form Config → the keys this app knows.
_KEY_ALIASES = {
    "customer_name": "name", "client_name": "name", "customer": "name", "client": "name",
    "full_name": "name", "lead_name": "name", "first_name": "name",
    "campaign_name": "campaign", "day": "session_day", "date": "session_date",
    "time": "session_time",
}


def _norm_key(k: str) -> str:
    k = (k or "").strip().lower()
    return _KEY_ALIASES.get(k, k)


def _clean_text(v) -> str:
    """Meta rejects newlines / tabs / 5+ spaces inside a template parameter (#132018)."""
    v = re.sub(r"[\r\n\t]+", " ", str(v or ""))
    v = re.sub(r" {5,}", "    ", v).strip()
    return v or "-"


def _load_template(db: Session, name: str, lang: str):
    rows = (db.query(WhatsAppTemplate).filter(WhatsAppTemplate.name == name)
            .order_by(WhatsAppTemplate.id.desc()).all())
    for r in rows:
        if (r.language or "").lower() == (lang or "").lower():
            return r
    return rows[0] if rows else None


def _template_shape(tmpl) -> dict:
    """What the approved template needs at send time: header kind + body variables."""
    raw = (tmpl.meta_raw or {}) if tmpl else {}
    comps = raw.get("components") or []
    header = next((c for c in comps if str(c.get("type", "")).upper() == "HEADER"), None)
    body = next((c for c in comps if str(c.get("type", "")).upper() == "BODY"), None)
    header_fmt = str((header or {}).get("format") or "").upper()
    if not header and tmpl and tmpl.header_type:
        header_fmt = str(tmpl.header_type).upper()
    if header_fmt == "NONE":
        header_fmt = ""
    body_text = (body or {}).get("text") or (tmpl.body_text if tmpl else "") or ""
    named = []
    for n in re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", body_text):
        if n not in named:
            named.append(n)
    numeric = [int(n) for n in re.findall(r"\{\{\s*(\d+)\s*\}\}", body_text)]
    return {"known": bool(tmpl), "header": header_fmt, "named": named,
            "positional": max(numeric) if numeric else 0}


def _header_url(db: Session, template_name: str) -> str:
    """Public URL of the header image for a template.
    1) the image uploaded in CRM → Templates   2) WA_HEADER_IMAGES env fallback
       ("template_name=https://…  other_template=https://…")"""
    row = db.get(TemplateHeaderImage, template_name)
    if row and row.url:
        return row.url
    for pair in re.split(r"[\s,;]+", settings.wa_header_images or ""):
        if "=" in pair:
            name, url = pair.split("=", 1)
            if name.strip() == template_name and url.strip():
                return url.strip()
    return ""


def _build_components(db: Session, lead: "Lead", template_name: str, lang: str = "en",
                      params_spec: str = "") -> list:
    """Header + body components the way the approved template wants them.

    * body values come from the form's "Template variables" (Form Config), in order;
      friendly names such as customer_name are accepted as `name`
    * a template with NAMED variables ({{customer_name}}) gets `parameter_name` on each value
    * a template with an IMAGE/VIDEO/DOCUMENT header gets that media (public link) – WhatsApp
      rejects the message without it
    Raises TemplateConfigError (nothing is sent) when something required is missing."""
    vals = _values(lead)
    keys = [k for k in (_norm_key(x) for x in (params_spec or "").split(",")) if k in PARAM_KEYS]
    shape = _template_shape(_load_template(db, template_name, lang))
    comps: list = []

    # ── header ──
    fmt = shape["header"]
    if fmt in ("IMAGE", "VIDEO", "DOCUMENT"):
        url = _header_url(db, template_name)
        if not url:
            raise TemplateConfigError(
                f"Template '{template_name}' has an {fmt} header but no image is set. In CRM → Templates, "
                f"open '{template_name}' and click 'Upload image'.")
        kind = fmt.lower()
        comps.append({"type": "header", "parameters": [{"type": kind, kind: {"link": url}}]})

    # ── body ──
    params: list = []
    if shape["named"]:
        for i, pname in enumerate(shape["named"]):
            key = keys[i] if i < len(keys) else _norm_key(pname)
            if key not in vals:
                raise TemplateConfigError(
                    f"Template '{template_name}' variable {{{{{pname}}}}} has no value. In Form Config → "
                    f"Template variables use, in order: {', '.join(PARAM_KEYS)} (e.g. 'name').")
            params.append({"type": "text", "parameter_name": pname, "text": _clean_text(vals[key])})
    elif shape["positional"]:
        if len(keys) < shape["positional"]:
            raise TemplateConfigError(
                f"Template '{template_name}' needs {shape['positional']} variable(s) but Form Config lists "
                f"{len(keys)}. Set 'Template variables' (allowed: {', '.join(PARAM_KEYS)}).")
        params = [{"type": "text", "text": _clean_text(vals[k])} for k in keys[:shape["positional"]]]
    elif not shape["known"] and keys:
        # template not synced into the CRM yet → old behaviour (positional values)
        params = [{"type": "text", "text": _clean_text(vals[k])} for k in keys]
    if params:
        comps.append({"type": "body", "parameters": params})
    return comps


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
    try:
        components = _build_components(db, lead, template, lang, params_spec)
    except TemplateConfigError as e:
        # Nothing was sent and nothing is marked as failed for good: once the form /
        # environment is fixed the invite can go out (retry or the Send Invite button).
        lead.whatsapp_error = str(e)[:400]
        db.commit()
        print(f"[WA] NOT SENT (template setup): {e}", flush=True)
        return {"ok": False, "reason": "template_config", "error": str(e), "kind": "config",
                "retryable": False, "template": template}
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone(lead.phone),
        "type": "template",
        "template": {"name": template, "language": {"code": lang},
                     "components": components},
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
