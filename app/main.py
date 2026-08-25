from fastapi import FastAPI, Depends, HTTPException, Query, Request, BackgroundTasks
import os
import asyncio
import httpx
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func, and_
from typing import Optional
from datetime import date, datetime, timedelta
from .config import settings
from .db import get_db, init_db
from .models import Lead, FollowUp, WhatsAppMessage
from .auth import LoginIn, TokenOut, create_token, require_user
from .schemas import LeadOut, LeadCreate, LeadUpdate, FollowUpIn, ReplyIn, TestWhatsAppIn
from .utils import clean_phone, get_seminar_details, now_iso
from .whatsapp import send_template_for_lead, send_text_reply, save_outgoing_template_message
from .meta import classify_webhook_and_handle, upsert_lead_from_meta
from .form_config_routes import router as form_config_router
from .template_routes import router as template_router
from .whatsapp_status import apply_status_updates


app = FastAPI(title="WOI Lead CRM API", version="2.0.0")
app.include_router(template_router)
app.include_router(form_config_router)

KEEP_ALIVE_TASK = None

# ---------------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------------

async def keep_alive_ping():
    public_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("APP_URL")
    )
    if not public_url:
        print("Keep-alive disabled: RENDER_EXTERNAL_URL/PUBLIC_BASE_URL/APP_URL missing", flush=True)
        return

    ping_url = public_url.rstrip("/") + "/"
    print(f"Keep-alive enabled. Pinging every 13 minutes: {ping_url}", flush=True)

    while True:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(ping_url)
                print(f"Keep-alive ping: {response.status_code}", flush=True)
        except Exception as exc:
            print(f"Keep-alive ping failed: {exc}", flush=True)
        await asyncio.sleep(13 * 60)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

# Build origins list — always include localhost + any Vercel preview URLs
_origins = [
    settings.frontend_origin,
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
# Filter out empty strings (in case FRONTEND_ORIGIN env var is not set)
origins = [o for o in _origins if o]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://.*\.vercel\.app",  # covers all Vercel preview URLs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup():
    init_db()
    global KEEP_ALIVE_TASK
    keep_alive_enabled = os.getenv("KEEP_ALIVE_ENABLED", "true").lower() in ("true", "1", "yes", "on")
    if keep_alive_enabled:
        KEEP_ALIVE_TASK = asyncio.create_task(keep_alive_ping())


@app.on_event("shutdown")
async def _shutdown():
    global KEEP_ALIVE_TASK
    if KEEP_ALIVE_TASK:
        KEEP_ALIVE_TASK.cancel()
        try:
            await KEEP_ALIVE_TASK
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "woi-lead-crm-backend"}


# ---------------------------------------------------------------------------
# Debug — SECURED (requires auth)
# ---------------------------------------------------------------------------

@app.get("/debug/config")
def debug_config(user: str = Depends(require_user)):
    """Configuration sanity check — protected by auth."""
    return {
        "db": "configured" if settings.database_url else "missing",
        "meta_page_access_token": "configured" if os.getenv("META_PAGE_ACCESS_TOKEN") else "missing",
        "meta_access_token": "configured" if os.getenv("META_ACCESS_TOKEN") else "missing",
        "meta_graph_version": os.getenv("META_GRAPH_VERSION", "v25.0"),
        "whatsapp_enabled": settings.whatsapp_enabled,
        "whatsapp_access_token": "configured" if settings.whatsapp_access_token else "missing",
        "phone_number_id": settings.whatsapp_phone_number_id,
        "template": settings.whatsapp_template_name,
        "language": settings.whatsapp_language_code,
        "seminar_venue": settings.seminar_venue,
        "keep_alive_enabled": os.getenv("KEEP_ALIVE_ENABLED", "true"),
        "render_external_url": os.getenv("RENDER_EXTERNAL_URL", ""),
    }


@app.get("/debug/graph-lead")
def debug_graph_lead(
    leadgen_id: str = "",
    ad_id: str = "",
    user: str = Depends(require_user),
):
    """Dump the raw Graph API response chain (lead -> ad -> adset -> campaign),
    including live token introspection. Pass a real leadgen_id or ad_id.
    """
    from app.debug_graph import diagnose_lead_chain
    return diagnose_lead_chain(leadgen_id=leadgen_id, ad_id=ad_id)


@app.post("/admin/backfill-blank-leads")
def backfill_blank_leads(
    lead_id: str = "",
    limit: int = 50,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Re-fetch leads saved with blank name/phone (e.g. captured while the Meta
    token was expired) and update them in place — keyed by meta_lead_id, so no
    duplicates are created.

    Doubles as a LIVE token test: if the current token is bad, each result carries
    a 'fetch_error'; if it's good, name/phone get populated.

      POST /admin/backfill-blank-leads                 -> fix up to `limit` blank leads
      POST /admin/backfill-blank-leads?lead_id=<id>    -> fix one specific meta_lead_id

    WhatsApp is NOT re-sent during backfill (auto_send=False).
    """
    if lead_id:
        leads = db.query(Lead).filter(Lead.meta_lead_id == str(lead_id)).all()
    else:
        leads = (
            db.query(Lead)
            .filter(
                Lead.meta_lead_id.isnot(None),
                or_(
                    Lead.full_name.is_(None), Lead.full_name == "",
                    Lead.phone.is_(None), Lead.phone == "",
                ),
            )
            .order_by(Lead.id.desc())
            .limit(limit)
            .all()
        )

    results = []
    for lead in leads:
        before = {"name": lead.full_name, "phone": lead.phone}
        try:
            updated, _wa = upsert_lead_from_meta(db, lead.meta_lead_id, auto_send=False)
            raw = updated.raw or {}
            fetch_error = raw.get("fetch_error") or raw.get("error_text")
            results.append({
                "meta_lead_id": lead.meta_lead_id,
                "db_id": lead.id,
                "before": before,
                "after": {"name": updated.full_name, "phone": updated.phone},
                "http_status": raw.get("http_status"),
                "fetch_error": fetch_error,
                "fixed": bool(updated.phone) and not before["phone"],
            })
        except Exception as exc:
            results.append({
                "meta_lead_id": lead.meta_lead_id,
                "db_id": lead.id,
                "before": before,
                "error": f"{type(exc).__name__}: {exc}",
            })

    fixed = sum(1 for r in results if r.get("fixed"))
    any_fetch_error = any(r.get("fetch_error") for r in results)
    return {
        "scanned": len(results),
        "fixed": fixed,
        "token_verdict": (
            "TOKEN STILL BAD — every re-fetch returned an error. The token the running "
            "process is using is not valid. Most likely: env var changed but service NOT "
            "redeployed, or the new token is in the wrong slot (must be META_PAGE_ACCESS_TOKEN)."
            if any_fetch_error and fixed == 0
            else "TOKEN OK — leads were re-fetched and name/phone populated."
            if fixed > 0
            else "Nothing to fix, or no change — inspect individual results below."
        ),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn):
    if data.username != settings.admin_username or data.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return TokenOut(access_token=create_token(data.username))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

@app.get("/leads")
def list_leads(
    q: str = "",
    status: str = "",
    day: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "latest",
    unread: bool = False,
    limit: int = Query(500, le=5000),
    offset: int = 0,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    from datetime import datetime as _dt
    query = db.query(Lead)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.full_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.campaign_name.ilike(like),
            Lead.ad_name.ilike(like),
            Lead.platform.ilike(like),
            Lead.latest_reply_text.ilike(like),
        ))
    if status:
        query = query.filter(Lead.status == status)
    if day:
        query = query.filter(Lead.seminar_day == day)
    if unread:
        query = query.filter(Lead.unread_count > 0)
    # Filter by lead created_time date range
    if date_from:
        try:
            query = query.filter(Lead.created_time >= date_from)
        except Exception:
            pass
    if date_to:
        try:
            query = query.filter(Lead.created_time <= date_to + "T23:59:59")
        except Exception:
            pass
    total = query.count()
    # Sort order
    if sort == "oldest":
        query = query.order_by(Lead.id.asc())
    else:
        query = query.order_by(Lead.id.desc())
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "rows": [LeadOut.model_validate(r).model_dump() for r in rows]}

@app.get("/debug/token-shape")
def debug_token_shape(user: str = Depends(require_user)):
    """Safely inspect the token the RUNNING process holds — never reveals it.
    Shows length, first/last 4 chars, and whether whitespace/quotes are baked in.
    """
    import os

    def shape(name):
        t = os.getenv(name) or ""
        return {
            "present": bool(t),
            "length": len(t),
            "first4": t[:4],                 # valid Meta tokens start with 'EAA'
            "last4": t[-4:],
            "starts_with_EAA": t.startswith("EAA"),
            "has_space_inside": " " in t,
            "has_newline_inside": ("\n" in t) or ("\r" in t),
            "has_quote": ('"' in t) or ("'" in t),
            "changes_after_strip": t != t.strip(),
        }

    checked = ["META_PAGE_ACCESS_TOKEN", "META_ACCESS_TOKEN",
               "PAGE_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN", "FACEBOOK_ACCESS_TOKEN"]
    shapes = {n: shape(n) for n in checked}

    # which token get_meta_token() actually picks (same priority order)
    winner = next((n for n in checked if os.getenv(n)), None)
    return {"winning_var": winner, "shapes": shapes}


@app.post("/leads", response_model=LeadOut)
def create_lead(data: LeadCreate, db: Session = Depends(get_db), user: str = Depends(require_user)):
    payload = data.model_dump()

    # Clean/normalise values before creating the SQLAlchemy model.
    # Do NOT pass phone separately again after **payload, otherwise Python raises:
    # TypeError: Lead() got multiple values for keyword argument 'phone'
    payload["phone"] = clean_phone(data.phone)
    payload["platform"] = payload.get("platform") or "FB"
    payload["campaign_name"] = payload.get("campaign_name") or "Manual"

    seminar = get_seminar_details(data.preferred_day, payload)
    lead = Lead(
        **payload,
        raw={"source": "manual", "platform": payload.get("platform")},
        **seminar,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


@app.patch("/leads/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: int, data: LeadUpdate, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        if k == "phone" and v:
            v = clean_phone(v)
        setattr(lead, k, v)
    db.commit()
    db.refresh(lead)
    return lead


@app.post("/leads/{lead_id}/send-whatsapp")
def send_lead_whatsapp(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    # Manual "Send Invitation" — use invitation template and force=True so it
    # always sends even if the auto registration message was already delivered,
    # and a new outgoing WhatsAppMessage row is saved for the inbox.
    return send_template_for_lead(db, lead, force=True, template_type="invitation")


# ---------------------------------------------------------------------------
# Follow-ups (per lead)
# ---------------------------------------------------------------------------

@app.get("/leads/{lead_id}/followups")
def get_followups(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    rows = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).order_by(FollowUp.id.asc()).all()
    return {"rows": [
        {
            "id": r.id, "lead_id": r.lead_id, "followup_no": r.followup_no,
            "followup_date": r.followup_date, "response": r.response,
            "confirmed": r.confirmed, "seminar_date": r.seminar_date,
            "next_followup_date": r.next_followup_date, "remarks": r.remarks,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]}


def _sync_lead_from_followups(db: Session, lead: Lead):
    """Keep lead status / next follow-up in sync with the latest follow-up."""
    latest = (
        db.query(FollowUp)
        .filter(FollowUp.lead_id == lead.id)
        .order_by(FollowUp.id.desc())
        .first()
    )
    if latest:
        if latest.confirmed:
            lead.status = latest.confirmed
        lead.next_followup_at = latest.next_followup_date or lead.next_followup_at
    else:
        lead.next_followup_at = ""


def _renumber_followups(db: Session, lead_id: int):
    rows = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).order_by(FollowUp.id.asc()).all()
    for idx, row in enumerate(rows, start=1):
        row.followup_no = idx


@app.post("/leads/{lead_id}/followups")
def add_followup(lead_id: int, data: FollowUpIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    count = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).count()
    fu = FollowUp(lead_id=lead_id, followup_no=count + 1, **data.model_dump())
    lead.status = data.confirmed or lead.status
    lead.next_followup_at = data.next_followup_date or lead.next_followup_at
    if data.remarks:
        lead.notes = ((lead.notes or "") + "\n" + data.remarks).strip()
    db.add(fu)
    db.commit()
    db.refresh(fu)
    return {"success": True, "id": fu.id}


@app.patch("/leads/{lead_id}/followups/{followup_id}")
def update_followup(lead_id: int, followup_id: int, data: FollowUpIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    fu = db.query(FollowUp).filter(FollowUp.id == followup_id, FollowUp.lead_id == lead_id).first()
    if not fu:
        raise HTTPException(404, "Follow-up not found")

    for key, value in data.model_dump().items():
        setattr(fu, key, value)

    _sync_lead_from_followups(db, lead)
    db.commit()
    db.refresh(fu)
    return {"success": True, "id": fu.id}


@app.delete("/leads/{lead_id}/followups/{followup_id}")
def delete_followup(lead_id: int, followup_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    fu = db.query(FollowUp).filter(FollowUp.id == followup_id, FollowUp.lead_id == lead_id).first()
    if not fu:
        raise HTTPException(404, "Follow-up not found")

    db.delete(fu)
    db.flush()
    _renumber_followups(db, lead_id)
    _sync_lead_from_followups(db, lead)
    db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Follow-ups worklist — OPTIMISED (single query, no N+1)
# ---------------------------------------------------------------------------

def _parse_followup_date(value):
    """Parse YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY into date object."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            pass
    return None


def _date_to_iso(value):
    d = _parse_followup_date(value)
    return d.isoformat() if d else ""


@app.get("/followups/due")
def list_due_followups(
    bucket: str = Query("today", description="today, tomorrow, overdue, week, all, range"),
    from_date: str = "",
    to_date: str = "",
    status: str = "",
    q: str = "",
    limit: int = Query(300, le=1000),
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Follow-up worklist. Uses a single JOIN query (no N+1) to load leads
    with their latest follow-up in one round-trip to the database.
    """
    today = date.today()
    bucket = (bucket or "today").lower().strip()

    if bucket == "tomorrow":
        start = end = today + timedelta(days=1)
    elif bucket == "week":
        start = today
        end = today + timedelta(days=7)
    elif bucket == "overdue":
        start = None
        end = today - timedelta(days=1)
    elif bucket == "all":
        start = end = None
    elif bucket == "range":
        start = _parse_followup_date(from_date)
        end = _parse_followup_date(to_date) or start
    else:
        start = end = today

    # -----------------------------------------------------------------------
    # Single query: fetch leads + eagerly load all their followups at once.
    # SQLAlchemy resolves the latest followup in Python from the preloaded
    # collection — zero additional queries regardless of lead count.
    # -----------------------------------------------------------------------
    query = db.query(Lead).options(joinedload(Lead.followups))

    if status:
        query = query.filter(Lead.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.full_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.campaign_name.ilike(like),
            Lead.ad_name.ilike(like),
            Lead.platform.ilike(like),
            Lead.latest_reply_text.ilike(like),
        ))

    # Only fetch leads that have a next_followup_at set, or have at least one followup
    candidates = (
        query
        .order_by(Lead.updated_at.desc(), Lead.id.desc())
        .limit(5000)
        .all()
    )

    rows = []
    for lead in candidates:
        # Latest followup is already loaded — no extra query
        sorted_fus = sorted(lead.followups, key=lambda f: f.id, reverse=True)
        latest = sorted_fus[0] if sorted_fus else None

        due_raw = lead.next_followup_at or (latest.next_followup_date if latest else "")
        due = _parse_followup_date(due_raw)

        if not due:
            continue

        include = True
        if bucket == "overdue":
            include = due <= end
        elif bucket == "all":
            include = True
        else:
            if start and due < start:
                include = False
            if end and due > end:
                include = False

        if not include:
            continue

        rows.append({
            "lead": LeadOut.model_validate(lead).model_dump(),
            "due_date": due.isoformat(),
            "followup": {
                "id": latest.id if latest else None,
                "followup_no": latest.followup_no if latest else None,
                "followup_date": latest.followup_date if latest else "",
                "response": latest.response if latest else "",
                "confirmed": latest.confirmed if latest else "",
                "seminar_date": latest.seminar_date if latest else "",
                "next_followup_date": latest.next_followup_date if latest else due_raw,
                "remarks": latest.remarks if latest else "",
                "created_at": str(latest.created_at) if latest else "",
            },
        })

    rows.sort(key=lambda r: (r["due_date"] or "9999-99-99", -(r["lead"]["id"] or 0)))
    return {"total": len(rows), "rows": rows[:limit]}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.get("/reports/summary")
def reports(db: Session = Depends(get_db), user: str = Depends(require_user)):
    total = db.query(Lead).count()
    sent = db.query(Lead).filter(Lead.whatsapp_sent == True).count()
    delivered = db.query(Lead).filter(Lead.whatsapp_delivered == True).count()
    unread = db.query(Lead).filter(Lead.unread_count > 0).count()
    from sqlalchemy import case as sa_case
    # Normalise status case so 'new' and 'New' merge into one bucket
    status_label = func.initcap(func.lower(Lead.status))
    statuses = db.query(status_label, func.count(Lead.id)).group_by(status_label).all()
    days = db.query(Lead.seminar_day, func.count(Lead.id)).filter(Lead.seminar_day.isnot(None), Lead.seminar_day != "").group_by(Lead.seminar_day).all()
    return {
        "total": total, "sent": sent, "delivered": delivered, "unread": unread,
        "by_status": dict(statuses), "by_day": dict(days),
    }


@app.get("/reports/seminar")
def reports_seminar(
    date_from: str = None,
    date_to: str = None,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Per-seminar-date breakdown.

    Important fix:
    Lead does not have `confirmed` or `response` columns. Those values live in
    the latest FollowUp row. The earlier version tried `lead.confirmed` and
    `lead.response`, which caused a 500 error and the frontend showed
    "Failed to fetch".
    """
    from collections import defaultdict
    from datetime import date as _date, datetime as _dt

    def parse_date(value):
        """Parse common seminar date formats into a date object."""
        if not value:
            return None

        raw = str(value).strip()
        if not raw:
            return None

        # Strip weekday prefix: "Thursday, 28 May 2026" -> "28 May 2026"
        if "," in raw:
            raw = raw.split(",", 1)[1].strip()

        formats = (
            "%Y-%m-%d",
            "%d %B %Y",
            "%d %b %Y",
            "%d-%m-%Y",
            "%d/%m/%Y",
        )
        for fmt in formats:
            try:
                return _dt.strptime(raw, fmt).date()
            except Exception:
                pass
        return None

    def latest_followup(lead):
        rows = getattr(lead, "followups", None) or []
        if not rows:
            return None
        return sorted(rows, key=lambda f: f.id or 0, reverse=True)[0]

    def norm(value):
        return str(value or "").strip().lower()

    def is_attended(value):
        v = norm(value)
        return v == "attended" or "attended" in v

    def is_confirmed(value):
        v = norm(value)
        return v == "confirmed" or v == "confirm" or "will attend" in v

    def is_missed(value):
        v = norm(value)
        return v == "missed" or "missed" in v

    not_picked_keywords = (
        "not picked",
        "not reachable",
        "switched off",
        "switch off",
        "wrong number",
        "no answer",
        "busy",
        "call not received",
        "not received",
    )

    def is_not_picked(response):
        r = norm(response)
        return bool(r) and any(k in r for k in not_picked_keywords)

    try:
        from_date = parse_date(date_from) if date_from else None
        to_date = parse_date(date_to) if date_to else None
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date range")

    # Load followups with leads in one query to avoid slow N+1 report calls.
    leads = (
        db.query(Lead)
        .options(joinedload(Lead.followups))
        .filter(Lead.seminar_date.isnot(None), Lead.seminar_date != "")
        .all()
    )

    buckets = defaultdict(list)

    for lead in leads:
        fu = latest_followup(lead)

        # If the follow-up has a corrected seminar date, prefer it.
        # Normalize every accepted input to an actual date before grouping.
        # This merges values like "21 May (Thu)", "Thursday, 21 May 2026",
        # and "2026-05-21" into one row instead of showing duplicates.
        seminar_label = (getattr(fu, "seminar_date", None) if fu else None) or lead.seminar_date
        seminar_dt = parse_date(seminar_label)
        if seminar_dt is None:
            continue

        if from_date and seminar_dt < from_date:
            continue
        if to_date and seminar_dt > to_date:
            continue

        status_value = (getattr(fu, "confirmed", None) if fu else None) or lead.status
        response_value = (getattr(fu, "response", None) if fu else None) or ""

        buckets[seminar_dt].append({
            "status": status_value,
            "response": response_value,
        })

    def report_date_label(d: _date) -> str:
        # Frontend already converts "Thursday, 21 May 2026" to "21 May (Thu)".
        # Keep this canonical payload shape so old + new UI both look clean.
        return d.strftime("%A, %d %B %Y")

    rows = []
    for seminar_dt, items in sorted(buckets.items(), key=lambda x: x[0] or _date.min):
        rows.append({
            "seminar_date": report_date_label(seminar_dt),
            "seminar_date_iso": seminar_dt.isoformat(),
            "total": len(items),
            "confirmed": sum(1 for x in items if is_confirmed(x["status"])),
            "attended": sum(1 for x in items if is_attended(x["status"])),
            "missed": sum(1 for x in items if is_missed(x["status"])),
            "call_not_picked": sum(1 for x in items if is_not_picked(x["response"])),
            "call_picked": sum(1 for x in items if x["response"] and not is_not_picked(x["response"])),
        })

    return {"rows": rows}


# ---------------------------------------------------------------------------
# WhatsApp Inbox
# ---------------------------------------------------------------------------

@app.get("/whatsapp/conversations")
def conversations(db: Session = Depends(get_db), user: str = Depends(require_user)):
    # Build a per-phone "last activity" map from the actual message table.
    # This is the TRUE chronological signal — unlike Lead.updated_at, which
    # changes on any row edit (e.g. resetting unread_count when a chat is
    # opened) and would make chats jump to the top without a new message.
    leads = (
        db.query(Lead)
        .filter(or_(Lead.latest_reply_text.isnot(None), Lead.whatsapp_message_id.isnot(None)))
        .limit(300)
        .all()
    )

    # Latest message timestamp per phone (max id = most recent inserted message)
    last_msg = (
        db.query(
            WhatsAppMessage.phone,
            func.max(WhatsAppMessage.id).label("max_id"),
        )
        .group_by(WhatsAppMessage.phone)
        .all()
    )
    last_id_by_phone = {clean_phone(p): mid for (p, mid) in last_msg}

    # Also fetch the actual timestamp of each phone's latest message for display
    latest_ts_by_phone = {}
    if last_id_by_phone:
        max_ids = list(last_id_by_phone.values())
        ts_rows = (
            db.query(WhatsAppMessage.phone, WhatsAppMessage.timestamp, WhatsAppMessage.created_at)
            .filter(WhatsAppMessage.id.in_(max_ids))
            .all()
        )
        for ph, ts, ca in ts_rows:
            latest_ts_by_phone[clean_phone(ph)] = ts or (str(ca) if ca else None)

    def activity_key(l):
        # Prefer the newest message row id (monotonic, insertion-ordered).
        mid = last_id_by_phone.get(clean_phone(l.phone or ""), 0) or 0
        return mid

    leads.sort(key=activity_key, reverse=True)

    rows = []
    for l in leads:
        d = LeadOut.model_validate(l).model_dump()
        ph = clean_phone(l.phone or "")
        # last_activity_at = timestamp of the most recent message (in or out)
        d["last_activity_at"] = latest_ts_by_phone.get(ph) or l.latest_reply_at or l.whatsapp_sent_at
        rows.append(d)

    return {"rows": rows}


@app.get("/whatsapp/conversations/{phone}")
def thread(phone: str, db: Session = Depends(get_db), user: str = Depends(require_user)):
    p = clean_phone(phone)
    msgs = db.query(WhatsAppMessage).filter(WhatsAppMessage.phone == p).order_by(WhatsAppMessage.id.asc()).all()
    lead = db.query(Lead).filter(Lead.phone == p).order_by(Lead.id.desc()).first()
    if lead:
        lead.unread_count = 0
        db.commit()
    return {
        "lead": LeadOut.model_validate(lead).model_dump() if lead else None,
        "messages": [
            {
                "id": m.id, "wa_message_id": m.wa_message_id, "direction": m.direction,
                "body": m.body, "status": m.status, "message_type": m.message_type,
                "timestamp": m.timestamp or (str(m.created_at) if m.created_at else None),
                "created_at": str(m.created_at) if m.created_at else None,
            }
            for m in msgs
        ],
    }


@app.post("/whatsapp/reply")
def reply(data: ReplyIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = (
        db.get(Lead, data.lead_id)
        if data.lead_id
        else db.query(Lead).filter(Lead.phone == clean_phone(data.phone)).order_by(Lead.id.desc()).first()
    )
    return send_text_reply(db, data.phone, data.text, lead)


# ---------------------------------------------------------------------------
# Meta webhook
# ---------------------------------------------------------------------------

@app.get("/webhook/meta-leads")
def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.meta_verify_token:
        return int(params.get("hub.challenge", "0"))
    raise HTTPException(status_code=403, detail="Invalid verify token")


@app.post("/webhook/meta-leads")
async def meta_webhook(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    payload = await request.json()
    print("Webhook hit:", payload)
    result = classify_webhook_and_handle(db, payload)
    print("Webhook result:", result)
    return {"success": True, "result": result}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

@app.post("/maintenance/backfill-whatsapp-chatbox")
def backfill_whatsapp_chatbox(db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Create outgoing chat bubbles for older leads whose WhatsApp template was already sent."""
    leads = db.query(Lead).filter(Lead.whatsapp_sent == True).all()
    created_or_updated = 0
    skipped = 0
    for lead in leads:
        if not lead.phone:
            skipped += 1
            continue
        save_outgoing_template_message(db, lead)
        created_or_updated += 1
    return {"success": True, "created_or_updated": created_or_updated, "skipped": skipped}


@app.post("/maintenance/backfill-meta-metadata")
def backfill_meta_metadata(
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Re-fetch old Meta leads to fill campaign name, ad name, form name and FB/IG source.
    This does not resend WhatsApp messages.
    """
    leads = (
        db.query(Lead)
        .filter(Lead.meta_lead_id.isnot(None))
        .order_by(Lead.id.desc())
        .limit(limit)
        .all()
    )

    updated = 0
    errors = []
    for lead in leads:
        try:
            before = (lead.campaign_name, lead.ad_name, lead.form_name, lead.platform)
            upsert_lead_from_meta(db, str(lead.meta_lead_id), auto_send=False)
            db.refresh(lead)
            after = (lead.campaign_name, lead.ad_name, lead.form_name, lead.platform)
            if before != after:
                updated += 1
        except Exception as exc:
            errors.append({"lead_id": lead.id, "meta_lead_id": lead.meta_lead_id, "error": str(exc)})

    return {"success": True, "checked": len(leads), "updated": updated, "errors": errors[:20]}


# ---------------------------------------------------------------------------
# Test endpoints — gated by TEST_ENDPOINTS_ENABLED env flag
# ---------------------------------------------------------------------------

def _require_test_mode():
    """Raises 403 if TEST_ENDPOINTS_ENABLED is not explicitly set to true."""
    enabled = os.getenv("TEST_ENDPOINTS_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        raise HTTPException(status_code=403, detail="Test endpoints disabled. Set TEST_ENDPOINTS_ENABLED=true to enable.")


@app.post("/test/whatsapp")
def test_whatsapp(data: TestWhatsAppIn, db: Session = Depends(get_db)):
    _require_test_mode()
    seminar = get_seminar_details(data.please_choose_a_day_for_the_free_seminar, data.model_dump())
    lead = Lead(
        full_name=data.name, phone=clean_phone(data.phone), campaign_name="Test",
        preferred_day=data.please_choose_a_day_for_the_free_seminar,
        raw={"source": "test"}, **seminar,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"success": True, "lead_id": lead.id, "seminar": seminar, "whatsapp": send_template_for_lead(db, lead)}


@app.post("/test/meta-lead/{lead_id}")
def test_meta_lead(lead_id: str, db: Session = Depends(get_db)):
    _require_test_mode()
    lead, wa = upsert_lead_from_meta(db, lead_id, auto_send=True)
    return {"success": True, "lead": LeadOut.model_validate(lead).model_dump(), "whatsapp": wa}
