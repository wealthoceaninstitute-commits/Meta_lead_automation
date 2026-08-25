"""
WhatsApp Template Management Routes
------------------------------------
POST   /templates/upload-media          Upload header media → get handle
POST   /templates                       Submit template to Meta for approval
GET    /templates                       List all templates (with cached status)
GET    /templates/{id}/status           Refresh status from Meta
DELETE /templates/{id}                  Delete template from Meta + DB
POST   /templates/{id}/send-bulk        Send template to contacts from CSV body
"""

import csv
import io
import json
import logging
import re
import requests
from typing import Any, Optional, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import require_user
from .config import settings
from .db import get_db
from .models import WhatsAppTemplate, WhatsAppMessage
from .utils import now_iso, clean_phone
from .whatsapp_status import extract_status_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/templates", tags=["templates"])

GRAPH = "https://graph.facebook.com"
GV = "v25.0"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _wa_headers():
    return {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }


# Cache the WABA ID after first successful lookup
_WABA_ID_CACHE: str = ""


def _waba_id() -> str:
    """
    Return the WhatsApp Business Account ID.

    Resolution order:
    1. WHATSAPP_BUSINESS_ACCOUNT_ID env var (fastest)
    2. Derived from WHATSAPP_PHONE_NUMBER_ID via Graph API (auto)
    3. Derived from META_PAGE_ACCESS_TOKEN phone number list

    The result is cached in memory for the lifetime of the process.
    """
    global _WABA_ID_CACHE

    if _WABA_ID_CACHE:
        return _WABA_ID_CACHE

    # 1. Explicit env var
    waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()
    if waba:
        _WABA_ID_CACHE = waba
        return waba

    token = (settings.whatsapp_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="WHATSAPP_ACCESS_TOKEN is not set.")

    phone_id = (settings.whatsapp_phone_number_id or "").strip()

    # 2. Look up via phone number ID → whatsapp_business_account
    if phone_id:
        r = requests.get(
            f"{GRAPH}/{GV}/{phone_id}",
            headers=_wa_headers(),
            params={"fields": "whatsapp_business_account"},
            timeout=15,
        )
        if r.status_code == 200:
            waba = r.json().get("whatsapp_business_account", {}).get("id", "")
            if waba:
                _WABA_ID_CACHE = waba
                logger.info("Auto-resolved WABA ID from phone number: %s", waba)
                return waba

    # 3. Look up via token's own business account list
    r2 = requests.get(
        f"{GRAPH}/{GV}/me/businesses",
        headers=_wa_headers(),
        timeout=15,
    )
    if r2.status_code == 200:
        biz_list = r2.json().get("data", [])
        if biz_list:
            # Pick first; each business may have multiple WABAs
            biz_id = biz_list[0].get("id", "")
            if biz_id:
                r3 = requests.get(
                    f"{GRAPH}/{GV}/{biz_id}/owned_whatsapp_business_accounts",
                    headers=_wa_headers(),
                    timeout=15,
                )
                if r3.status_code == 200:
                    waba_list = r3.json().get("data", [])
                    if waba_list:
                        waba = waba_list[0].get("id", "")
                        if waba:
                            _WABA_ID_CACHE = waba
                            logger.info("Auto-resolved WABA ID from business: %s", waba)
                            return waba

    raise HTTPException(
        status_code=503,
        detail=(
            "Cannot determine WhatsApp Business Account ID. "
            "Add WHATSAPP_BUSINESS_ACCOUNT_ID to your Render environment variables. "
            "Find it in Meta Business Manager → Business Settings → WhatsApp Accounts."
        ),
    )


def _app_id() -> str:
    """
    Return the Meta App ID, required for the Resumable Upload API used to attach a
    header image/video/document sample when submitting a template for approval.

    The resumable upload edge lives on the APP node (POST /{app_id}/uploads) — NOT
    on the WhatsApp Business Account. Using the WABA ID here is what produced the
    error: "Object with ID '<waba>' does not exist ... or does not support this operation".
    """
    app_id = (getattr(settings, "meta_app_id", None) or "").strip()
    if not app_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "META_APP_ID is not set. It is required to upload a template header "
                "sample image. Find it in the Meta App Dashboard "
                "(developers.facebook.com/apps → your app → App ID) and add it to your "
                "Render environment variables as META_APP_ID."
            ),
        )
    return app_id


def _extract_body_var_tokens(text: str) -> list:
    """
    Return the variable tokens inside a body's {{ }} placeholders, in order of first
    appearance and de-duplicated. e.g. "Hi {{name}}, on {{date}}" -> ["name", "date"];
    "Hi {{1}} on {{2}}" -> ["1", "2"].
    """
    tokens: list = []
    for m in re.findall(r"\{\{\s*([^}]+?)\s*\}\}", text or ""):
        tok = m.strip()
        if tok and tok not in tokens:
            tokens.append(tok)
    return tokens


def _build_meta_payload(t: "TemplateIn") -> dict:
    """Convert our TemplateIn schema into the Meta Graph API payload."""
    components = []

    # HEADER
    if t.header_type and t.header_type != "NONE":
        if t.header_type == "TEXT" and t.header_text:
            comp: dict = {
                "type": "HEADER",
                "format": "TEXT",
                "text": t.header_text,
            }
            # Header variables
            if t.header_variables:
                comp["example"] = {"header_text": t.header_variables}
            components.append(comp)
        elif t.header_type in ("IMAGE", "VIDEO", "DOCUMENT") and t.header_media_handle:
            comp = {
                "type": "HEADER",
                "format": t.header_type,
                "example": {"header_handle": [t.header_media_handle]},
            }
            components.append(comp)
        elif t.header_type == "LOCATION":
            components.append({"type": "HEADER", "format": "LOCATION"})

    # BODY
    body_comp: dict = {"type": "BODY", "text": t.body_text}
    # Meta needs a DIFFERENT example format depending on the variable style:
    #   • Named vars  {{name}}      → example.body_text_named_params [{param_name, example}]
    #   • Positional  {{1}} {{2}}   → example.body_text [[ "v1", "v2" ]]
    # Sending the positional format for a named-variable body triggers error 100
    # "Invalid parameter". We detect the style from the body text itself.
    var_tokens = _extract_body_var_tokens(t.body_text)
    sample_values = list(t.body_variables or [])
    if var_tokens:
        is_named = any(not tok.isdigit() for tok in var_tokens)
        if is_named:
            named_params = []
            for i, tok in enumerate(var_tokens):
                example_val = sample_values[i] if i < len(sample_values) else tok
                named_params.append({"param_name": tok, "example": str(example_val)})
            body_comp["example"] = {"body_text_named_params": named_params}
        else:
            # positional — one row of example values in numeric order
            ordered = sorted(var_tokens, key=lambda x: int(x))
            vals = [
                str(sample_values[i]) if i < len(sample_values) else f"Sample{i+1}"
                for i in range(len(ordered))
            ]
            body_comp["example"] = {"body_text": [vals]}
    components.append(body_comp)

    # FOOTER
    if t.footer_text:
        components.append({"type": "FOOTER", "text": t.footer_text})

    # BUTTONS
    # NOTE: t.buttons is a list of Pydantic ButtonIn objects, so use attribute
    # access (b.type / b.text), NOT dict .get() — the latter raises AttributeError
    # and 500s the whole request before CORS headers attach (browser shows
    # "Failed to fetch"). Normalise to a dict first so the logic is simple.
    if t.buttons:
        btn_list = []
        for b in t.buttons:
            bd = b.model_dump() if hasattr(b, "model_dump") else (b.dict() if hasattr(b, "dict") else dict(b))
            btype = bd.get("type")
            btext = bd.get("text") or ""
            if btype == "QUICK_REPLY":
                btn_list.append({"type": "QUICK_REPLY", "text": btext})
            elif btype == "URL":
                raw_url = (bd.get("url") or "").strip()
                url_example = (bd.get("url_example") or "").strip()
                # If the full link was typed into the example box instead of the URL
                # box (easy to do in the UI), fall back to it as the actual URL.
                if not raw_url and url_example:
                    raw_url = url_example
                url_btn: dict = {"type": "URL", "text": btext, "url": raw_url}
                # Meta requires `example` ONLY for DYNAMIC urls containing {{1}}.
                if "{{" in raw_url and url_example:
                    url_btn["example"] = [url_example]
                btn_list.append(url_btn)
            elif btype == "PHONE_NUMBER":
                btn_list.append({
                    "type": "PHONE_NUMBER",
                    "text": btext,
                    "phone_number": (bd.get("phone_number") or "").strip(),
                })
        if btn_list:
            components.append({"type": "BUTTONS", "buttons": btn_list})

    return {
        "name": t.name,
        "category": t.category,
        "language": t.language,
        "components": components,
    }


# ─────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────

class ButtonIn(BaseModel):
    type: str                          # QUICK_REPLY | URL | PHONE_NUMBER
    text: str
    url: Optional[str] = None          # for URL buttons
    url_example: Optional[str] = None  # filled URL example for approval
    phone_number: Optional[str] = None


class TemplateIn(BaseModel):
    name: str
    category: str = "MARKETING"        # MARKETING | UTILITY | AUTHENTICATION
    language: str = "en"

    header_type: Optional[str] = None  # TEXT | IMAGE | VIDEO | DOCUMENT | LOCATION | NONE
    header_text: Optional[str] = None
    header_variables: Optional[List[str]] = None
    header_media_handle: Optional[str] = None

    body_text: str
    body_variables: Optional[List[str]] = None  # example values for {{1}} {{2}} …

    footer_text: Optional[str] = None
    buttons: Optional[List[ButtonIn]] = None


class BulkContactIn(BaseModel):
    phone: str
    name: Optional[str] = None
    # extra variable values keyed by {{1}}, {{2}} or just list position
    variables: Optional[List[str]] = None


class BulkSendIn(BaseModel):
    contacts: List[BulkContactIn]
    variable_map: Optional[List[str]] = None
    header_image_handle: Optional[str] = None  # uploaded image handle for IMAGE header templates


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@router.post("/upload-media")
async def upload_header_media(
    file: UploadFile = File(...),
    user: str = Depends(require_user),
):
    """
    Upload image/video/document to Meta Resumable Upload API.
    Returns the media handle (h:...) to use in header_media_handle
    when SUBMITTING a template for approval.

    NOTE: This handle is only valid for template creation, NOT for sending.
    For send-time image uploads, use /upload-send-media instead.
    """
    content = await file.read()
    file_size = len(content)
    mime = file.content_type or "application/octet-stream"

    # Step 1: Create upload session on the APP node (Resumable Upload API).
    # This edge is /{app_id}/uploads — using the WABA ID here fails with
    # "Object ... does not support this operation".
    session_url = f"{GRAPH}/{GV}/{_app_id()}/uploads"
    r = requests.post(
        session_url,
        params={
            "file_length": file_size,
            "file_type": mime,
            "file_name": file.filename,
            "access_token": settings.whatsapp_access_token,
        },
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=r.json())

    upload_session_id = r.json().get("id")
    if not upload_session_id:
        raise HTTPException(status_code=502, detail="No upload session id from Meta")

    # Step 2: Upload binary
    upload_url = f"{GRAPH}/{GV}/{upload_session_id}"
    r2 = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {settings.whatsapp_access_token}",
            "file_offset": "0",
            "Content-Type": mime,
        },
        data=content,
        timeout=60,
    )
    if r2.status_code not in (200, 201):
        raise HTTPException(status_code=r2.status_code, detail=r2.json())

    handle = r2.json().get("h")
    if not handle:
        raise HTTPException(status_code=502, detail="No handle returned from Meta upload")

    return {"handle": handle, "filename": file.filename, "size": file_size, "mime": mime}


@router.post("/upload-send-media")
async def upload_send_media(
    file: UploadFile = File(...),
    user: str = Depends(require_user),
):
    """
    Upload image/video/document to WhatsApp Cloud API for use at SEND TIME.
    Uses /{phone_number_id}/media — returns a media_id (numeric string).

    Use this when the user picks an image to send with a template that has
    an IMAGE/VIDEO/DOCUMENT header component. Pass the returned media_id
    as header_image_handle in the send-bulk request body.

    Different from /upload-media which returns an h: handle only valid
    for template submission/approval.
    """
    content = await file.read()
    mime = file.content_type or "image/jpeg"
    phone_id = (settings.whatsapp_phone_number_id or "").strip()
    if not phone_id:
        raise HTTPException(status_code=503, detail="WHATSAPP_PHONE_NUMBER_ID is not set")

    url = f"{GRAPH}/{GV}/{phone_id}/media"

    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (file.filename, content, mime)},
        timeout=60,
    )

    logger.info("upload-send-media status=%s body=%s", r.status_code, r.text[:300])

    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=r.json())

    media_id = r.json().get("id")
    if not media_id:
        raise HTTPException(status_code=502, detail="No media ID returned from WhatsApp")

    return {"media_id": media_id, "filename": file.filename, "mime": mime}


@router.post("")
def create_template(
    data: TemplateIn,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Submit a new WhatsApp message template to Meta for approval."""
    waba = _waba_id()
    payload = _build_meta_payload(data)

    r = requests.post(
        f"{GRAPH}/{GV}/{waba}/message_templates",
        headers=_wa_headers(),
        json=payload,
        timeout=30,
    )
    meta_data = r.json()
    logger.info("Meta template submit: %s %s", r.status_code, meta_data)

    meta_id = meta_data.get("id")
    status = meta_data.get("status", "PENDING")

    if r.status_code not in (200, 201) and not meta_id:
        err = meta_data.get("error", {}) or {}
        # Meta's most useful text is usually in error_user_msg / error_user_title.
        detail = (
            err.get("error_user_msg")
            or err.get("error_user_title")
            or err.get("message")
            or str(meta_data)
        )
        subcode = err.get("error_subcode")
        if subcode:
            detail = f"{detail} (subcode {subcode})"
        raise HTTPException(status_code=r.status_code, detail=detail)

    tmpl = WhatsAppTemplate(
        meta_template_id=meta_id,
        name=data.name,
        category=data.category,
        language=data.language,
        status=status,
        header_type=data.header_type,
        header_text=data.header_text,
        header_media_handle=data.header_media_handle,
        body_text=data.body_text,
        body_variables=data.body_variables,
        footer_text=data.footer_text,
        buttons=[b.dict() for b in data.buttons] if data.buttons else None,
        meta_raw=meta_data,
    )
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    return _tmpl_out(tmpl)


@router.get("")
def list_templates(
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """List all templates stored locally."""
    templates = db.query(WhatsAppTemplate).order_by(WhatsAppTemplate.id.desc()).all()
    return [_tmpl_out(t) for t in templates]


@router.get("/{template_id}/status")
def refresh_template_status(
    template_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Pull latest approval status from Meta and update the DB record."""
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if not tmpl.meta_template_id:
        raise HTTPException(status_code=400, detail="No Meta template ID stored")

    r = requests.get(
        f"{GRAPH}/{GV}/{tmpl.meta_template_id}",
        headers=_wa_headers(),
        params={"fields": "id,name,status,category,language,quality_score,rejected_reason,components"},
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json())

    meta_data = r.json()
    tmpl.status = meta_data.get("status", tmpl.status)
    tmpl.rejection_reason = meta_data.get("rejected_reason")
    tmpl.meta_raw = meta_data
    db.commit()
    db.refresh(tmpl)
    return _tmpl_out(tmpl)


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Delete template from Meta and from local DB."""
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if tmpl.meta_template_id:
        waba = _waba_id()
        r = requests.delete(
            f"{GRAPH}/{GV}/{waba}/message_templates",
            headers=_wa_headers(),
            params={"hsm_id": tmpl.meta_template_id, "name": tmpl.name},
            timeout=20,
        )
        logger.info("Meta template delete: %s %s", r.status_code, r.text)

    db.delete(tmpl)
    db.commit()
    return {"ok": True, "deleted_id": template_id}


@router.post("/{template_id}/send-bulk")
def send_bulk(
    template_id: int,
    data: BulkSendIn,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Send an approved template to a list of contacts.
    Each contact can carry its own variable values.
    """
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if tmpl.status not in ("APPROVED", "approved"):
        raise HTTPException(status_code=400, detail=f"Template status is '{tmpl.status}', not APPROVED")

    # If this template has an IMAGE/VIDEO/DOCUMENT header, a media_id is mandatory on
    # every message. Fail fast with a clear error instead of a misleading "variables empty".
    real_header_type = _get_real_header_type(tmpl)
    if real_header_type in ("IMAGE", "VIDEO", "DOCUMENT") and not data.header_image_handle:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This template has a {real_header_type} header. Upload the "
                f"{real_header_type.lower()} first — its media_id must be sent with every message."
            ),
        )

    url = f"{GRAPH}/{GV}/{settings.whatsapp_phone_number_id}/messages"

    # Log what we received to diagnose variable issues
    logger.info("SEND-BULK template=%s contacts=%d first_vars=%s body_text_preview=%s",
        tmpl.name,
        len(data.contacts),
        data.contacts[0].variables if data.contacts else "NO_CONTACTS",
        (tmpl.body_text or "")[:60]
    )

    results = []
    for contact in data.contacts:
        phone = clean_phone(contact.phone)
        if not phone:
            results.append({"phone": contact.phone, "ok": False, "error": "invalid phone"})
            continue

        components = _build_send_components(tmpl, contact, data.header_image_handle)

        # Empty components list means a required variable had no value
        if components == [] and tmpl.body_text and re.search(r"\{\{[a-zA-Z_]", tmpl.body_text or ""):
            results.append({
                "phone": contact.phone,
                "name": contact.name,
                "ok": False,
                "wamid": None,
                "error": "One or more template variables are empty — check CSV column mapping",
            })
            continue

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": tmpl.name,
                "language": {"code": tmpl.language},
                "components": components,
            },
        }

        logger.info("Sending template payload: %s", payload)
        r = requests.post(url, json=payload, headers=_wa_headers(), timeout=20)
        try:
            resp = r.json()
        except Exception:
            resp = {"raw": r.text}

        ok = r.status_code in (200, 201)
        wamid = ((resp.get("messages") or [{}])[0]).get("id") if ok else None

        if ok and wamid:
            db.add(WhatsAppMessage(
                wa_message_id=wamid,
                phone=phone,
                contact_name=contact.name,
                direction="outgoing",
                message_type="template",
                body=f"[Template: {tmpl.name}]",
                status="accepted",
                raw=resp,
                timestamp=now_iso(),
            ))

        error_detail = None
        if not ok:
            err = resp.get("error", {})
            code = err.get("code", "")
            msg = err.get("message", "")
            error_sub = err.get("error_data", {}).get("details", "") if isinstance(err.get("error_data"), dict) else ""
            error_detail = f"(#{code}) {msg}" + (f" — {error_sub}" if error_sub else "")
            logger.error("Send failed phone=%s code=%s msg=%s payload=%s", phone, code, msg, payload)

        results.append({
            "phone": contact.phone,
            "name": contact.name,
            "ok": ok,
            "wamid": wamid,
            "error": error_detail,
        })

    db.commit()

    sent = sum(1 for r in results if r["ok"])
    # wamids lets the frontend poll /message-status for live delivery counts
    wamids = [r["wamid"] for r in results if r.get("wamid")]
    return {
        "total": len(results),
        "sent": sent,
        "failed": len(results) - sent,
        "results": results,
        "wamids": wamids,
    }


# ─────────────────────────────────────────────────────────────
# Delivery status polling (live counts after a bulk send)
# ─────────────────────────────────────────────────────────────

class MessageStatusIn(BaseModel):
    wamids: List[str]


# Terminal states — once every message is in one of these, the frontend can stop polling.
_TERMINAL_STATES = {"read", "failed"}


@router.post("/message-status")
def message_status(
    data: MessageStatusIn,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Given the wa_message_ids returned by /send-bulk, return each message's current
    delivery status plus aggregate counts. The frontend polls this after a bulk send
    to render live Sent / Delivered / Read / Failed counts.

    Statuses are updated asynchronously by the webhook handler (whatsapp_status.apply_status_updates)
    as Meta delivers status callbacks. Until a callback arrives a message stays "accepted".
    """
    wamids = [w for w in (data.wamids or []) if w]
    if not wamids:
        return {"total": 0, "counts": {}, "statuses": {}, "delivered_total": 0, "all_terminal": True}

    rows = (
        db.query(WhatsAppMessage)
        .filter(WhatsAppMessage.wa_message_id.in_(wamids))
        .all()
    )
    by_id = {row.wa_message_id: row for row in rows}

    counts = {"accepted": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0, "pending": 0}
    statuses = {}
    all_terminal = True

    for wamid in wamids:
        row = by_id.get(wamid)
        if not row:
            # Send record not committed yet (rare race) — treat as pending
            statuses[wamid] = {"status": "pending", "error_code": None, "error_title": None}
            counts["pending"] += 1
            all_terminal = False
            continue

        st = (row.status or "accepted").lower()
        if st not in counts:
            st = "accepted"

        err_code = err_title = None
        if st == "failed":
            err_code, err_title = extract_status_error(row.raw)

        statuses[wamid] = {"status": st, "error_code": err_code, "error_title": err_title}
        counts[st] += 1
        if st not in _TERMINAL_STATES:
            all_terminal = False

    # A read message is also delivered — show a combined "delivered or better" total.
    delivered_total = counts["delivered"] + counts["read"]

    return {
        "total": len(wamids),
        "counts": counts,
        "delivered_total": delivered_total,
        "all_terminal": all_terminal,
        "statuses": statuses,
    }


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _header_info_from_meta_raw(meta_raw: dict) -> dict:
    """
    Inspect meta_raw.components to determine the template's HEADER component.

    IMPORTANT — how WhatsApp headers actually work:
      A template can have at most one HEADER component. Its `format` is one of
      TEXT / IMAGE / VIDEO / DOCUMENT / LOCATION.

      For IMAGE / VIDEO / DOCUMENT headers, Meta REQUIRES an `example.header_handle`
      at template-creation time (this is what Meta's editor calls the "Media sample").
      That example is ALWAYS present on an approved media header — it is NOT a sign
      that the header is "preview only". At SEND time you must upload a fresh media_id
      via /{phone_id}/media and pass it as the header parameter, exactly like the
      standalone send_4hrs_reminder.py script does.

      So: any HEADER with a real format (not NONE) is a send-time header that needs
      a parameter on every message.

    Returns dict with keys:
      has_header_component  bool      — HEADER component exists at all
      is_send_time_header   bool      — caller must supply a parameter at send time
      is_media_sample_only  bool      — kept for backwards-compat, always False now
      header_format         str|None  — 'TEXT' | 'IMAGE' | 'VIDEO' | 'DOCUMENT'
    """
    result = {
        "has_header_component": False,
        "is_send_time_header": False,
        "is_media_sample_only": False,  # deprecated: media headers always send the media
        "header_format": None,
    }
    if not meta_raw:
        return result

    for comp in meta_raw.get("components", []):
        if comp.get("type", "").upper() != "HEADER":
            continue

        fmt = comp.get("format", "").upper()
        if not fmt or fmt == "NONE":
            break  # no real header

        # TEXT, IMAGE, VIDEO and DOCUMENT headers all require a parameter at send time.
        result["has_header_component"] = True
        result["header_format"] = fmt
        result["is_send_time_header"] = True
        break

    return result


def _has_real_header_component(tmpl: WhatsAppTemplate) -> bool:
    """True if this template requires a header component at send time (not just a media sample)."""
    if tmpl.meta_raw:
        info = _header_info_from_meta_raw(tmpl.meta_raw)
        return info["is_send_time_header"]
    # No meta_raw: fall back to DB field (may be stale)
    return bool(tmpl.header_type and tmpl.header_type not in ("NONE", ""))


def _get_real_header_type(tmpl: WhatsAppTemplate) -> Optional[str]:
    """
    Return the header format only if it is a real send-time header.
    Returns None for media-sample-only templates even if they have a HEADER component in meta_raw.
    """
    if tmpl.meta_raw:
        info = _header_info_from_meta_raw(tmpl.meta_raw)
        if info["is_send_time_header"]:
            return info["header_format"]
        return None
    if tmpl.header_type and tmpl.header_type not in ("NONE", ""):
        return tmpl.header_type
    return None


def _build_send_components(tmpl: WhatsAppTemplate, contact: BulkContactIn, header_image_handle: Optional[str] = None) -> list:
    """Build the template components array for a single send call."""
    components = []
    variables = contact.variables or []

    # Determine the real header format from meta_raw (Meta is the source of truth).
    # The DB header_type field can be stale, so we trust meta_raw via this helper.
    real_header_type = _get_real_header_type(tmpl)  # 'TEXT' | 'IMAGE' | 'VIDEO' | 'DOCUMENT' | None

    if real_header_type == "TEXT":
        # TEXT header with a variable placeholder → send the text param
        if tmpl.header_text and "{{" in (tmpl.header_text or ""):
            param_val = variables[0] if variables else (contact.name or "")
            if param_val:
                clean_hdr = re.sub(r"[\r\n\t]+", " ", str(param_val)).strip()
                components.append({
                    "type": "header",
                    "parameters": [{"type": "text", "text": clean_hdr}],
                })

    elif real_header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
        # Media header — REQUIRED on every message. Send the freshly uploaded media_id.
        # header_image_handle here MUST be a media_id from /{phone_id}/media
        # (the /upload-send-media endpoint), exactly like send_4hrs_reminder.py.
        # Do NOT pass an h: resumable-upload handle or the approval example handle —
        # those cause: "Object with ID ... does not exist / missing permissions".
        if header_image_handle:
            media_key = real_header_type.lower()  # 'image' / 'video' / 'document'
            components.append({
                "type": "header",
                "parameters": [{
                    "type": media_key,
                    media_key: {"id": header_image_handle},
                }],
            })
        else:
            # No media_id provided but the template requires one → fail fast with a clear reason
            logger.error(
                "Template %s has a %s header but no media_id was provided for send",
                tmpl.name, real_header_type,
            )
            return []  # signals caller to report "header image required"
    # real_header_type is None → template has no header, send body only

    # Body params
    # Meta stores body text as {{1}}, {{2}} positional even when named vars were used
    # body_variables may store the var names (e.g. ["event_time", "venue_name"])
    # variables list from CSV is already in correct positional order
    body_params = []
    numeric_placeholders = re.findall(r"\{\{(\d+)\}\}", tmpl.body_text or "")
    named_placeholders = re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", tmpl.body_text or "")

    # Determine how many params are needed
    if numeric_placeholders:
        param_count = len(numeric_placeholders)
    elif named_placeholders:
        seen: set = set()
        param_count = len([n for n in named_placeholders if not (n in seen or seen.add(n))])
    else:
        param_count = 0

    # Get stored variable names for parameter_name field
    stored_var_names = tmpl.body_variables or []  # e.g. ["event_time", "venue_name"]

    for i in range(param_count):
        if i < len(variables) and variables[i]:
            val = str(variables[i])
        elif named_placeholders and i < len(named_placeholders):
            vname = named_placeholders[i]
            if vname in ("name", "customer_name", "client_name", "client"):
                val = contact.name or ""
            else:
                val = ""
        else:
            val = contact.name if i == 0 else ""

        # Sanitize: Meta error #132018 is thrown when param text contains
        # newlines, tab characters, or more than 4 consecutive spaces.
        raw_val = str(val).strip() if val else ""
        clean_val = re.sub(r"[\r\n\t]+", " ", raw_val)   # newlines/tabs → single space
        clean_val = re.sub(r" {5,}", "    ", clean_val)    # 5+ consecutive spaces → 4 max
        clean_val = clean_val.strip()

        param: dict = {"type": "text", "text": clean_val}

        # Include parameter_name if we have the variable name stored
        # Meta requires this for templates approved with named variables
        if i < len(stored_var_names) and stored_var_names[i]:
            param["parameter_name"] = stored_var_names[i]
        elif i < len(named_placeholders):
            param["parameter_name"] = named_placeholders[i]

        body_params.append(param)

    if body_params:
        empty_params = [i+1 for i, p in enumerate(body_params) if not p["text"].strip()]
        if empty_params:
            logger.error("Empty params at positions %s for template %s", empty_params, tmpl.name)
            return []
        components.append({"type": "body", "parameters": body_params})

    return components


def _extract_header_media_url(meta_raw: dict) -> Optional[str]:
    """Pull the image/video/document URL from Meta's raw template response if present."""
    if not meta_raw:
        return None
    components = meta_raw.get("components", [])
    for comp in components:
        if comp.get("type", "").upper() == "HEADER":
            fmt = comp.get("format", "").upper()
            if fmt in ("IMAGE", "VIDEO", "DOCUMENT"):
                # Meta returns example.header_handle or example.header_url
                ex = comp.get("example", {})
                urls = ex.get("header_url", []) or ex.get("header_handle", [])
                if urls:
                    return urls[0]
    return None


def _tmpl_out(t: WhatsAppTemplate) -> dict:
    header_media_url = _extract_header_media_url(t.meta_raw or {})
    return {
        "id": t.id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "category": t.category,
        "language": t.language,
        "status": t.status,
        "header_type": t.header_type,
        "header_text": t.header_text,
        "header_media_handle": t.header_media_handle,
        "header_media_url": header_media_url,   # ← direct URL for preview
        "body_text": t.body_text,
        "body_variables": t.body_variables,
        "footer_text": t.footer_text,
        "buttons": t.buttons,
        "rejection_reason": t.rejection_reason,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "meta_raw": t.meta_raw or {},
        # Authoritative header flags computed from meta_raw (not stale DB header_type).
        # Use these in the frontend to decide whether to show the image upload section.
        **(_header_info_from_meta_raw(t.meta_raw) if t.meta_raw else {
            "has_header_component": False,
            "is_send_time_header": False,
            "is_media_sample_only": False,
            "header_format": None,
        }),
        # Convenience aliases used by existing frontend code
        "has_real_header": _has_real_header_component(t),
        "real_header_type": _get_real_header_type(t),
    }



@router.get("/debug")
def debug_template_config(user: str = Depends(require_user)):
    """
    Diagnoses token permissions and WABA access.
    Tests both WHATSAPP_ACCESS_TOKEN and META_PAGE_ACCESS_TOKEN.
    """
    wa_token  = (settings.whatsapp_access_token or "").strip()
    page_token = (settings.meta_page_access_token or "").strip()
    phone_id  = (settings.whatsapp_phone_number_id or "").strip()
    explicit_waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()

    result: dict = {
        "graph_version": GV,
        "phone_number_id": phone_id or "NOT SET",
        "explicit_waba_id": explicit_waba or "NOT SET",
        "tokens_tested": [],
        "recommendation": None,
        "steps": [],
    }

    def test_token(token: str, label: str) -> dict:
        """Run a full permission check on a token."""
        info: dict = {
            "label": label,
            "preview": token[:14] + "..." if token else "EMPTY",
            "identity": None,
            "token_type": None,
            "can_read_waba": False,
            "template_list_status": None,
            "error": None,
        }
        if not token:
            info["error"] = "Token is empty"
            return info

        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # /me — don't request "type" field, it's not available in newer Graph versions
        r = requests.get(f"{GRAPH}/{GV}/me", headers=hdrs,
                         params={"fields": "id,name"}, timeout=10)
        if r.status_code == 200:
            me = r.json()
            info["identity"] = f"{me.get('name','?')} (id:{me.get('id','?')})"
            info["token_type"] = "ok"
        else:
            # /me failing is not fatal — token may still work for WABA
            info["identity"] = f"(could not resolve /me: {r.status_code})"
            info["token_type"] = "unknown"

        # Try WABA templates directly
        if explicit_waba:
            r2 = requests.get(
                f"{GRAPH}/{GV}/{explicit_waba}/message_templates",
                headers=hdrs,
                params={"limit": 2, "fields": "id,name,status"},
                timeout=15,
            )
            info["template_list_status"] = r2.status_code
            if r2.status_code == 200:
                info["can_read_waba"] = True
                info["template_sample"] = [
                    {"name": t.get("name"), "status": t.get("status")}
                    for t in r2.json().get("data", [])[:2]
                ]
            else:
                info["template_list_error"] = r2.json().get("error", {}).get("message", r2.text[:150])
        return info

    # Test both tokens
    wa_result   = test_token(wa_token,   "WHATSAPP_ACCESS_TOKEN")
    page_result = test_token(page_token, "META_PAGE_ACCESS_TOKEN")
    result["tokens_tested"] = [wa_result, page_result]

    # Determine recommendation
    if wa_result["can_read_waba"]:
        result["recommendation"] = "WHATSAPP_ACCESS_TOKEN works. Click Pull from Meta."
        result["working_token"] = "WHATSAPP_ACCESS_TOKEN"
    elif page_result["can_read_waba"]:
        result["recommendation"] = (
            "META_PAGE_ACCESS_TOKEN can read templates. "
            "Set WHATSAPP_ACCESS_TOKEN = same value as META_PAGE_ACCESS_TOKEN in Render, then redeploy."
        )
        result["working_token"] = "META_PAGE_ACCESS_TOKEN"
    else:
        result["recommendation"] = (
            "Neither token has whatsapp_business_management permission. "
            "You need a PERMANENT System User token. Steps: "
            "1) business.facebook.com → Settings → Users → System Users → "
            "2) Create/select a system user → Generate New Token → "
            "3) Select your app → tick 'whatsapp_business_management' + 'whatsapp_business_messaging' → "
            "4) Copy the token → set as WHATSAPP_ACCESS_TOKEN in Render."
        )
        result["working_token"] = None

    # Quick WABA resolve check
    result["waba_id_to_use"] = explicit_waba or "NOT SET — add WHATSAPP_BUSINESS_ACCOUNT_ID to Render"

    return result



@router.get("/test-token")
def test_token_raw(user: str = Depends(require_user)):
    """
    Calls the WABA message_templates endpoint with the stored token.
    Returns the raw response so you can see exactly what Meta says.
    No processing — pure passthrough.
    """
    token = (settings.whatsapp_access_token or "").strip()
    explicit_waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()

    result = {
        "token_length": len(token),
        "token_first_20": token[:20] + "..." if len(token) > 20 else token,
        "token_last_10": "..." + token[-10:] if len(token) > 10 else token,
        "waba_id": explicit_waba,
        "calls": [],
    }

    headers = {"Authorization": f"Bearer {token}"}

    # Call 1: /me
    r1 = requests.get(f"{GRAPH}/{GV}/me", headers=headers,
                      params={"fields": "id,name"}, timeout=10)
    result["calls"].append({
        "url": f"{GRAPH}/{GV}/me",
        "status": r1.status_code,
        "response": r1.json() if r1.headers.get("content-type","").startswith("application/json") else r1.text[:200],
    })

    # Call 2: message_templates
    if explicit_waba:
        r2 = requests.get(
            f"{GRAPH}/{GV}/{explicit_waba}/message_templates",
            headers=headers,
            params={"fields": "id,name,status", "limit": 3},
            timeout=15,
        )
        result["calls"].append({
            "url": f"{GRAPH}/{GV}/{explicit_waba}/message_templates",
            "status": r2.status_code,
            "response": r2.json() if r2.headers.get("content-type","").startswith("application/json") else r2.text[:300],
        })

    # Call 3: debug_token (self-introspect)
    r3 = requests.get(
        f"{GRAPH}/{GV}/debug_token",
        headers=headers,
        params={"input_token": token},
        timeout=10,
    )
    if r3.status_code == 200:
        data = r3.json().get("data", {})
        result["token_debug"] = {
            "is_valid": data.get("is_valid"),
            "type": data.get("type"),
            "app_id": data.get("app_id"),
            "expires_at": "never" if data.get("expires_at") == 0 else data.get("expires_at"),
            "scopes": data.get("scopes", []),
            "error": data.get("error"),
        }
    else:
        result["token_debug"] = {"status": r3.status_code, "response": r3.text[:200]}

    return result



@router.get("/inspect-all")
def inspect_all_templates(db: Session = Depends(get_db)):
    """Public debug — list all template IDs and names."""
    templates = db.query(WhatsAppTemplate).all()
    return [{"id": t.id, "name": t.name, "body_text": (t.body_text or "")[:80], "body_variables": t.body_variables} for t in templates]


@router.get("/{template_id}/inspect")
def inspect_template(
    template_id: int,
    db: Session = Depends(get_db),
):
    # No auth — debug only, remove after fixing
    """Show exactly what is stored in DB + what payload will be sent."""
    import re as _re
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Not found")

    body_text = tmpl.body_text or ""
    named = _re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", body_text)
    numeric = _re.findall(r"\{\{(\d+)\}\}", body_text)

    # Simulate what would be sent with dummy variables
    dummy_vars = [f"VAR_{i+1}" for i in range(max(len(named), len(numeric), len(tmpl.body_variables or [])))]

    contact_sim = BulkContactIn(phone="919741308822", name="Test", variables=dummy_vars)
    try:
        components = _build_send_components(tmpl, contact_sim)
    except Exception as e:
        components = f"ERROR: {e}"

    return {
        "id": tmpl.id,
        "name": tmpl.name,
        "body_text": body_text,
        "body_text_named_placeholders": named,
        "body_text_numeric_placeholders": numeric,
        "stored_body_variables": tmpl.body_variables,
        "header_type": tmpl.header_type,
        "meta_raw_components": (tmpl.meta_raw or {}).get("components", []),
        "simulated_send_payload_components": components,
    }


@router.post("/fix-headers")
def fix_header_types(
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Fix templates that have wrong header_type in DB by re-checking meta_raw.
    Run once after deploying to correct any mis-synced templates.
    """
    templates = db.query(WhatsAppTemplate).all()
    fixed = []
    for t in templates:
        if not t.meta_raw:
            continue
        components = t.meta_raw.get("components", [])
        has_header = any(c.get("type","").upper() == "HEADER" for c in components)
        if not has_header and t.header_type:
            old_val = t.header_type
            t.header_type = None
            t.header_text = None
            t.header_media_handle = None
            fixed.append({"name": t.name, "old_header_type": old_val})
        elif has_header:
            for c in components:
                if c.get("type","").upper() == "HEADER":
                    fmt = c.get("format","").upper()
                    if fmt and fmt != "NONE":
                        if t.header_type != fmt:
                            fixed.append({"name": t.name, "old": t.header_type, "new": fmt})
                            t.header_type = fmt
                    else:
                        if t.header_type:
                            fixed.append({"name": t.name, "old_header_type": t.header_type, "new": None})
                            t.header_type = None
    db.commit()
    return {"fixed": len(fixed), "details": fixed}


@router.post("/sync-from-meta")
def sync_templates_from_meta(
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Pull all approved templates from Meta WABA and upsert into local DB.
    This lets you import templates that were created directly in WhatsApp Manager.
    """
    waba = _waba_id()
    r = requests.get(
        f"{GRAPH}/{GV}/{waba}/message_templates",
        headers=_wa_headers(),
        params={
            "fields": "id,name,status,category,language,components,quality_score,rejected_reason",
            "limit": 100,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json())

    meta_templates = r.json().get("data", [])
    synced = []
    skipped = []

    for mt in meta_templates:
        meta_id = mt.get("id")
        name = mt.get("name")
        existing = (
            db.query(WhatsAppTemplate)
            .filter(WhatsAppTemplate.meta_template_id == meta_id)
            .first()
        )

        # Parse components back into our fields
        components = mt.get("components", [])
        header_type = None
        header_text = None
        body_text = ""
        footer_text = None
        buttons = []
        body_variables = []

        # Reset header fields — only set if HEADER component actually exists
        header_type = None
        header_text = None

        for comp in components:
            ctype = comp.get("type", "").upper()
            if ctype == "HEADER":
                fmt = comp.get("format", "NONE").upper()
                if fmt and fmt != "NONE":
                    header_type = fmt
                if fmt == "TEXT":
                    header_text = comp.get("text", "")
            elif ctype == "BODY":
                body_text = comp.get("text", "")
                # Extract example variable values if present
                ex = comp.get("example", {})
                if ex.get("body_text"):
                    body_variables = ex["body_text"][0] if ex["body_text"] else []
                # Also check for named variable examples (newer Meta API format)
                if ex.get("body_text_named_params"):
                    # Store as ordered list of var names for CSV column mapping
                    named = ex["body_text_named_params"]
                    body_variables = [p.get("param_name", f"var{i+1}") for i, p in enumerate(named)]
            elif ctype == "FOOTER":
                footer_text = comp.get("text", "")
            elif ctype == "BUTTONS":
                for b in comp.get("buttons", []):
                    bt = b.get("type", "").upper()
                    btn = {"type": bt, "text": b.get("text", "")}
                    if bt == "URL":
                        btn["url"] = b.get("url", "")
                    elif bt == "PHONE_NUMBER":
                        btn["phone_number"] = b.get("phone_number", "")
                    buttons.append(btn)

        if existing:
            existing.status = mt.get("status")
            existing.rejection_reason = mt.get("rejected_reason")
            existing.meta_raw = mt
            existing.body_text = body_text or existing.body_text
            existing.footer_text = footer_text
            existing.buttons = buttons or existing.buttons
            # Always sync header fields from Meta — DB may be stale
            existing.header_type = header_type
            existing.header_text = header_text
            if body_variables:
                existing.body_variables = body_variables
            db.commit()
            skipped.append(name)
        else:
            tmpl = WhatsAppTemplate(
                meta_template_id=meta_id,
                name=name,
                category=mt.get("category", "MARKETING"),
                language=mt.get("language", "en"),
                status=mt.get("status"),
                header_type=header_type,
                header_text=header_text,
                body_text=body_text,
                body_variables=body_variables if body_variables else None,
                footer_text=footer_text,
                buttons=buttons if buttons else None,
                rejection_reason=mt.get("rejected_reason"),
                meta_raw=mt,
            )
            db.add(tmpl)
            synced.append(name)

    db.commit()
    return {
        "ok": True,
        "total_from_meta": len(meta_templates),
        "newly_imported": len(synced),
        "updated_existing": len(skipped),
        "imported": synced,
        "updated": skipped,
    }
