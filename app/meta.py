"""
Meta lead ingestion.

Golden rules of this module
  1. A lead id that reaches us is SAVED FIRST. Nothing that can fail (Graph API,
     expired token, unknown form, WhatsApp) is allowed to lose it.
  2. Every failure leaves the lead in a recoverable state
        sync_status = pending_fetch | needs_config | no_phone
     and the scheduler (sync.py) retries it automatically once the cause is fixed.
  3. WhatsApp is only sent automatically to FRESH leads (see AUTO_SEND_MAX_AGE_HOURS),
     so back-fills / recoveries can never message people from weeks ago.
"""
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import graph
from .config import settings
from .form_config import resolve_session, parse_created_time
from .models import Lead, FollowUp, WhatsAppMessage, WhatsAppStatusLog
from .utils import clean_phone, now_iso
from .whatsapp import send_whatsapp_template

LEAD_FIELDS_BASE = "id,created_time,field_data,form_id,platform,is_organic,ad_id"
LEAD_FIELDS_EXT = LEAD_FIELDS_BASE + ",ad_name,adset_id,adset_name,campaign_id,campaign_name"
_NAME_CACHE: dict = {}      # object id → (timestamp, {...})
_NAME_TTL = 6 * 3600


# ── small helpers ───────────────────────────────────────────────────────────

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
    if v in ("ig", "instagram") or "instagram" in v:
        return "IG"
    if v in ("fb", "facebook") or "facebook" in v:
        return "FB"
    return v.upper() if v else ""


def _field_map(field_data: list) -> dict:
    out = {}
    for f in field_data or []:
        name = f.get("name", "")
        vals = f.get("values") or []
        val = vals[0] if vals else ""
        out[name] = val
        out[_norm(name)] = val
    return out


def _get_field(flat: dict, *keys, default="") -> str:
    wanted = [_norm(k) for k in keys]
    for k in keys:
        v = flat.get(k)
        if v and str(v).strip():
            return str(v).strip()
    for k in wanted:
        v = flat.get(k)
        if v and str(v).strip():
            return str(v).strip()
    for want in wanted:
        for fk, fv in flat.items():
            if want in _norm(fk) and fv and str(fv).strip():
                return str(fv).strip()
    return default


def _answers_text(field_data: list) -> str:
    """Everything the lead answered, as one string (used to detect a chosen day).
    Name/phone/email style fields are excluded so a surname can't be read as a day."""
    skip = ("name", "phone", "mobile", "email", "city", "whatsapp")
    parts = []
    for f in field_data or []:
        fname = _norm(f.get("name", ""))
        if any(s in fname for s in skip) and "day" not in fname:
            continue
        parts.append(f"{f.get('name', '')} {' '.join(str(v) for v in (f.get('values') or []))}")
    return " | ".join(parts)


def is_fresh(lead: Lead, hours: Optional[int] = None) -> bool:
    """Is this lead recent enough for an automatic WhatsApp?"""
    hours = hours if hours is not None else settings.auto_send_max_age_hours
    dt = parse_created_time(lead.created_time)
    if not dt and lead.created_at:
        dt = lead.created_at if lead.created_at.tzinfo else lead.created_at.replace(tzinfo=timezone.utc)
    if not dt:
        return True
    return datetime.now(timezone.utc) - dt <= timedelta(hours=hours)


# ── Graph lookups ───────────────────────────────────────────────────────────

def fetch_lead_data(lead_id: str) -> dict:
    """Raises graph.GraphError. Tries ad/campaign names too; falls back to the
    base fields if the token isn't allowed to read them."""
    lead_id = _clean(lead_id)
    try:
        return graph.get(lead_id, params={"fields": LEAD_FIELDS_EXT})
    except graph.GraphError as e:
        if e.kind in ("token", "transient", "rate", "gone"):
            raise
        return graph.get(lead_id, params={"fields": LEAD_FIELDS_BASE})


def _cached_lookup(obj_id: str, fields: str) -> dict:
    if not obj_id:
        return {}
    hit = _NAME_CACHE.get(obj_id)
    if hit and time.time() - hit[0] < _NAME_TTL:
        return hit[1]
    try:
        data = graph.get(obj_id, params={"fields": fields}, retries=1)
    except graph.GraphError:
        data = {}   # names are nice-to-have; never fail a lead because of them
    _NAME_CACHE[obj_id] = (time.time(), data)
    return data


def form_info(form_id: str) -> dict:
    return _cached_lookup(form_id, "id,name,status")


def ad_info(ad_id: str) -> dict:
    d = _cached_lookup(ad_id, "id,name,adset{id,name},campaign{id,name}")
    return {
        "ad_name": d.get("name", ""),
        "adset_id": (d.get("adset") or {}).get("id", ""),
        "adset_name": (d.get("adset") or {}).get("name", ""),
        "campaign_id": (d.get("campaign") or {}).get("id", ""),
        "campaign_name": (d.get("campaign") or {}).get("name", ""),
    }


# ── incoming WhatsApp text helper ───────────────────────────────────────────

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


# ── main entry point ────────────────────────────────────────────────────────

def upsert_lead_from_meta(db: Session, lead_id: str, raw: dict | None = None,
                          auto_send: bool = True, webhook_value: dict | None = None,
                          force_refetch: bool = False):
    """
    Save → fetch → resolve → (maybe) WhatsApp. Never raises for expected failures.
    Returns (lead, wa_result_or_None).
    """
    lead_id = _clean(lead_id)
    wv = webhook_value or {}
    print(f"[Meta] Processing lead {lead_id}", flush=True)

    # 1. SAVE FIRST ─────────────────────────────────────────────────────────
    lead = db.query(Lead).filter(Lead.meta_lead_id == lead_id).first()
    created = lead is None
    if created:
        lead = Lead(meta_lead_id=lead_id, status="New", sync_status="pending_fetch")
        ct = wv.get("created_time")
        if isinstance(ct, (int, float)):
            ct = datetime.fromtimestamp(ct, tz=timezone.utc).isoformat()
        lead.created_time = ct or None
        lead.form_id = _clean(wv.get("form_id") or "") or None
        lead.ad_id = _clean(wv.get("ad_id") or wv.get("adgroup_id") or "") or None
        db.add(lead)
        db.commit()
        db.refresh(lead)

    # 2. FETCH (only when we don't already have the data) ───────────────────
    have_data = bool(lead.phone or lead.full_name) and lead.sync_status not in ("pending_fetch", None)
    data = raw
    if data is None and (force_refetch or not have_data):
        try:
            data = fetch_lead_data(lead_id)
        except graph.GraphError as e:
            lead.fetch_attempts = (lead.fetch_attempts or 0) + 1
            lead.sync_error = f"Could not fetch lead from Meta: {e}"
            lead.sync_status = "unrecoverable" if e.kind == "gone" else "pending_fetch"
            db.commit()
            print(f"[Meta] fetch failed for {lead_id}: {e}", flush=True)
            return lead, {"ok": False, "reason": "fetch_failed", "error": str(e), "kind": e.kind}
    if not data and lead.raw and isinstance(lead.raw, dict):
        data = lead.raw.get("graph_data") or {}      # re-process from what we stored earlier
    data = data or {}

    # 3. PARSE ──────────────────────────────────────────────────────────────
    form_id = _clean(data.get("form_id") or wv.get("form_id") or lead.form_id or "")
    ad_id = _clean(data.get("ad_id") or wv.get("adgroup_id") or wv.get("ad_id") or lead.ad_id or "")
    platform = _source(data.get("platform") or wv.get("platform") or wv.get("publisher_platform") or "")
    field_data = data.get("field_data") or []
    fields = _field_map(field_data)
    merged = {**data, **fields}

    name = _get_field(merged, "full_name", "full name", "name", "first_name", "your_name")
    phone = clean_phone(_clean(_get_field(merged, "phone", "phone_number", "mobile", "mobile_number",
                                          "whatsapp_number", "your_phone_number")))
    email = _get_field(merged, "email", "email_address")
    city = _get_field(merged, "city", "location", "place")
    exp = _get_field(merged, "what_is_your_current_experience_level?",
                     "what_is_your_experience_level_in_stock_market?", "experience")

    # 4. NAMES (ad / adset / campaign / form) – best effort ──────────────────
    ad_meta = {k: data.get(k, "") for k in ("ad_name", "adset_id", "adset_name", "campaign_id", "campaign_name")}
    if ad_id and not (ad_meta["ad_name"] and ad_meta["campaign_name"]):
        looked = ad_info(ad_id)
        ad_meta = {k: ad_meta.get(k) or looked.get(k, "") for k in looked}
    f_info = form_info(form_id) if form_id else {}
    form_name = f_info.get("name", "") or lead.form_name or ""

    # 5. RESOLVE SESSION ────────────────────────────────────────────────────
    ref = parse_created_time(data.get("created_time") or lead.created_time)
    session = resolve_session(
        db, form_id, form_name=form_name, answers_text=_answers_text(field_data),
        context_names=(ad_meta["ad_name"], ad_meta["adset_name"], ad_meta["campaign_name"]), ref=ref)
    print(f"[Meta] form_id={form_id} → resolved={session['resolved']} via={session['source']} "
          f"campaign={session['campaign_name']} day={session['session_day']}", flush=True)

    # 6. SAVE ───────────────────────────────────────────────────────────────
    lead.created_time = data.get("created_time") or lead.created_time
    lead.full_name = name or lead.full_name
    lead.phone = phone or lead.phone

    # The customer may have messaged on WhatsApp before this ad lead was pulled in; that
    # created a placeholder lead (status "WhatsApp", no meta id). Fold it into this lead
    # so the person is one lead with one chat, not two.
    if phone:
        try:
            for ph in (db.query(Lead).filter(Lead.phone == phone, Lead.id != lead.id,
                                             Lead.meta_lead_id.is_(None), Lead.status == "WhatsApp").all()):
                db.query(WhatsAppMessage).filter(WhatsAppMessage.lead_id == ph.id).update({"lead_id": lead.id})
                db.query(FollowUp).filter(FollowUp.lead_id == ph.id).update({"lead_id": lead.id})
                if ph.latest_reply_text and not lead.latest_reply_text:
                    lead.latest_reply_text = ph.latest_reply_text
                    lead.latest_reply_at = ph.latest_reply_at
                lead.unread_count = (lead.unread_count or 0) + (ph.unread_count or 0)
                db.delete(ph)
            db.flush()
        except Exception as exc:
            db.rollback()
            print(f"[Meta] placeholder merge skipped: {exc}", flush=True)
            lead = db.query(Lead).filter(Lead.meta_lead_id == lead_id).first()
    lead.email = email or lead.email
    lead.city = city or lead.city
    lead.experience = exp or lead.experience
    lead.status = lead.status or "New"

    lead.form_id = form_id or lead.form_id
    lead.form_name = session["form_name"] or form_name or lead.form_name
    lead.campaign_name = session["campaign_name"] or lead.campaign_name
    lead.ad_id = ad_id or lead.ad_id
    lead.ad_name = ad_meta["ad_name"] or lead.ad_name
    lead.adset_id = ad_meta["adset_id"] or lead.adset_id
    lead.adset_name = ad_meta["adset_name"] or lead.adset_name
    lead.campaign_id = ad_meta["campaign_id"] or lead.campaign_id
    lead.platform = platform or lead.platform
    if data.get("is_organic") is not None:
        lead.is_organic = str(data.get("is_organic")).lower()

    if session["resolved"]:
        lead.preferred_day = session["session_day"]
        lead.session_day = session["session_day"]
        lead.session_date = session["session_date"]
        lead.session_time = session["session_time"]
        lead.arrival_time = session["arrival_time"]
        lead.venue = session["venue"]

    if not session["resolved"]:
        lead.sync_status = "needs_config"
        lead.sync_error = (f"Form {form_id or '?'} ({form_name or 'unnamed'}) has no session day configured. "
                           f"Open Form Config and set the day — this lead will then be processed automatically.")
    elif not lead.phone:
        lead.sync_status = "no_phone"
        lead.sync_error = "The form had no phone number."
    else:
        lead.sync_status = "ok"
        lead.sync_error = None
    lead.fetch_attempts = 0 if data else lead.fetch_attempts

    lead.raw = {"webhook_value": wv, "graph_data": data, "form_id": form_id, "ad_id": ad_id,
                "session": {k: v for k, v in session.items() if k != "wa_params"}}
    db.add(lead)
    db.commit()
    db.refresh(lead)
    print(f"[Meta] Lead saved: id={lead.id} name={lead.full_name!r} phone={lead.phone!r} "
          f"sync={lead.sync_status} created={created}", flush=True)

    # 7. WHATSAPP ───────────────────────────────────────────────────────────
    wa = None
    if auto_send and lead.sync_status == "ok" and not lead.whatsapp_sent and not lead.whatsapp_message_id \
            and not lead.whatsapp_failed:
        if not is_fresh(lead):
            wa = {"ok": False, "reason": "stale_lead_not_auto_sent"}
        else:
            wa = send_whatsapp_template(db, lead, template_name=session["wa_template"],
                                        language=session["wa_language"], params_spec=session["wa_params"])
            db.refresh(lead)
            print(f"[WA] Result: {wa}", flush=True)
    return lead, wa


# ── webhook handlers ────────────────────────────────────────────────────────

def handle_leadgen_payload(db: Session, payload: dict):
    lead_ids, errors = [], []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            if change.get("field") not in (None, "leadgen"):
                continue
            value = change.get("value") or {}
            lead_id = value.get("leadgen_id") or value.get("lead_id")
            if not lead_id:
                continue
            lead_ids.append(str(lead_id))
            try:
                upsert_lead_from_meta(db, str(lead_id), auto_send=True, webhook_value=value)
            except Exception as exc:     # last line of defence – the id is already saved
                db.rollback()
                errors.append(f"{lead_id}: {type(exc).__name__}: {exc}")
                print(f"[Meta] ERROR processing {lead_id}: {exc}", flush=True)
    return {"lead_ids": lead_ids, "errors": errors}


def handle_whatsapp_payload(db: Session, payload: dict):
    statuses, messages = [], []
    _rank = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                        for c in value.get("contacts", []) or []}

            for st in value.get("statuses", []) or []:
                mid, status = st.get("id"), (st.get("status") or "").lower()
                db.add(WhatsAppStatusLog(wa_message_id=mid, status=status,
                                         recipient_id=st.get("recipient_id"),
                                         timestamp=st.get("timestamp"), raw=st))
                lead = db.query(Lead).filter(Lead.whatsapp_message_id == mid).first()
                msg = db.query(WhatsAppMessage).filter(WhatsAppMessage.wa_message_id == mid).first()

                if msg:
                    cur = (msg.status or "accepted").lower()
                    if _rank.get(status, 0) >= _rank.get(cur, 0):
                        msg.status = status
                    if status == "failed":
                        msg.raw = {"status": "failed", "errors": st.get("errors") or [], "timestamp": st.get("timestamp")}

                if lead:
                    lead.whatsapp_last_status_at = now_iso()
                    cur = (lead.whatsapp_status or "accepted").lower()
                    # never move backwards (webhooks can arrive out of order)
                    if status == "failed" or _rank.get(status, 0) >= _rank.get(cur, 0):
                        lead.whatsapp_status = status
                    if status == "sent":
                        lead.whatsapp_sent = True
                        lead.whatsapp_sent_at = lead.whatsapp_sent_at or now_iso()
                    elif status == "delivered":
                        lead.whatsapp_sent = True
                        lead.whatsapp_delivered = True
                        lead.whatsapp_delivered_at = lead.whatsapp_delivered_at or now_iso()
                    elif status == "read":
                        lead.whatsapp_sent = lead.whatsapp_delivered = lead.whatsapp_read = True
                        lead.whatsapp_read_at = lead.whatsapp_read_at or now_iso()
                    elif status == "failed":
                        errs = st.get("errors") or []
                        e0 = errs[0] if errs else {}
                        lead.whatsapp_failed = True
                        lead.whatsapp_failed_at = now_iso()
                        lead.whatsapp_error = (f"({e0.get('code')}) {e0.get('title') or e0.get('message') or 'failed'}"
                                               if e0 else "Delivery failed")
                statuses.append(f"{mid}:{status}")

            for m in value.get("messages", []) or []:
                phone = clean_phone(m.get("from"))
                name = contacts.get(m.get("from")) or contacts.get(phone) or ""
                msg_type = m.get("type", "unknown")
                text = _extract_incoming_text(m, msg_type)
                if m.get("id") and db.query(WhatsAppMessage).filter(
                        WhatsAppMessage.wa_message_id == m.get("id")).first():
                    continue                         # duplicate delivery
                lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.id.desc()).first()
                if not lead:
                    lead = Lead(phone=phone, full_name=name or None, status="WhatsApp", sync_status="ok")
                    db.add(lead)
                    db.commit()
                    db.refresh(lead)
                lead.latest_reply_text = text
                lead.latest_reply_at = now_iso()
                lead.unread_count = (lead.unread_count or 0) + 1
                db.add(WhatsAppMessage(
                    wa_message_id=m.get("id"), lead_id=lead.id, phone=phone,
                    contact_name=name or lead.full_name, direction="incoming",
                    message_type=msg_type, body=text, raw=m, timestamp=m.get("timestamp")))
                messages.append(f"{phone}:{msg_type}")
    db.commit()
    return {"statuses": statuses, "messages": messages}


def classify_webhook_and_handle(db: Session, payload: dict):
    """Route by structure (not by searching the payload text)."""
    kinds = set()
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            if change.get("field") == "leadgen" or value.get("leadgen_id"):
                kinds.add("leadgen")
            elif value.get("messaging_product") == "whatsapp" or value.get("statuses") or value.get("messages"):
                kinds.add("whatsapp")
    out: dict = {"type": "+".join(sorted(kinds)) or "unknown"}
    if "leadgen" in kinds:
        out.update(handle_leadgen_payload(db, payload))
    if "whatsapp" in kinds:
        out.update(handle_whatsapp_payload(db, payload))
    return out
