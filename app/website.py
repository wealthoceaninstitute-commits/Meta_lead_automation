"""
Public website → CRM.

The website (Next.js, wealthoceaninstitute.co.in) used to post every event to a Google Sheet
through Apps Script. It now posts the same events here, through its own server route, with the
shared secret WEBSITE_INGEST_KEY in the X-Website-Key header.

Event types the website sends
  page_view, section_view                         – visits, traffic sources, how far people scroll
  whatsapp_click {tag, topic}                     – a WhatsApp ticket button (#FreeClass, #Investment…)
  call_click, map_click, share_click
  expert_open {topic}, form_step {topic=step id}  – the "Talk to an expert" form
  expert_form {lead_id, stage, name, phone, …}    – the form itself  → becomes / updates a LEAD
      (also used by the contact page, stage "submitted", and the mutual-fund course page,
       stage "checkout", with a `form` field naming the page)

Leads
  * stage "Filled form, did not press Send"  → lead with web_stage=incomplete, sync_status=web_incomplete
                                               (never auto-messaged; the team calls them)
  * "Pressed Send (opened WhatsApp)" / "Asked for a call back (no WhatsApp)"
                                             → web_stage=whatsapp / callback, sync_status=ok and the
                                               WhatsApp template from Form Config goes out
  * the same form session (lead_id) only ever updates one lead
  * a phone number already in the CRM is never duplicated: the website enquiry is added to that
    lead as a follow-up note instead

Templates are set per website form on the existing Form Config page:
  website-freeclass-monday / -friday / -sunday   (free-class seat confirmation, with date/time/venue)
  website-enquiry                                (invest / learn enquiries; optional)
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from .auth import require_user
from .config import settings
from .db import get_db
from .form_config import infer_day, resolve_session
from .models import FollowUp, FormConfig, Lead, WebsiteEvent
from .utils import clean_phone, now_iso

router = APIRouter(tags=["Website"])

EVENT_TYPES = {
    "page_view", "section_view", "whatsapp_click", "call_click", "map_click", "share_click",
    "expert_open", "form_step", "expert_form", "whatsapp_message",
}
MAX_EVENTS_PER_CALL = 50

STAGE_MAP = {
    "filled form, did not press send": "incomplete",
    "pressed send (opened whatsapp)": "whatsapp",
    "asked for a call back (no whatsapp)": "callback",
    "incomplete": "incomplete", "whatsapp": "whatsapp", "callback": "callback",
    "submitted": "submitted", "checkout": "checkout",
}
STAGE_RANK = {"incomplete": 0, "whatsapp_direct": 1, "whatsapp": 2, "callback": 2, "submitted": 2, "checkout": 2}
SENT_STAGES = ("whatsapp", "callback", "submitted", "checkout")
STAGE_LABEL = {
    "incomplete": "Filled the form but did not press Send",
    "whatsapp": "Sent the form (opened WhatsApp)",
    "callback": "Asked for a call back",
    "whatsapp_direct": "Messaged on WhatsApp from the website",
    "submitted": "Sent a form on the website",
    "checkout": "Started the paid checkout",
}

# The website's free classes. Times must match the website (components/home/WeekSlots.jsx).
WEBSITE_CLASSES = {
    "Monday": {"session_time": "5:00 PM to 7:00 PM", "arrival_time": "4:45 PM",
               "name": "Investment Masterclass (mutual funds and bonds)"},
    "Friday": {"session_time": "1:00 PM to 3:00 PM", "arrival_time": "12:45 PM",
               "name": "Stock market trading awareness"},
    "Sunday": {"session_time": "10:20 AM to 12:30 PM", "arrival_time": "10:05 AM",
               "name": "Investment and trading class"},
}
ENQUIRY_FORM_ID = "website-enquiry"
WEB_FORM_NAME = "Website · Talk to an expert"


def freeclass_form_id(day: str) -> str:
    return f"website-freeclass-{(day or '').lower()}"


# ── setup ───────────────────────────────────────────────────────────────────

def ensure_indexes(db: Session):
    """The auto-migration adds new columns but not their indexes; create them once."""
    from sqlalchemy import text
    for name, col in (("ix_leads_source", "source"), ("ix_leads_web_ref", "web_ref"),
                      ("ix_leads_web_visitor_id", "web_visitor_id")):
        try:
            db.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON leads ({col})'))
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[website] index {name}: {exc}", flush=True)


def seed_website_configs(db: Session) -> int:
    """Form Config rows for the website, created once so templates can be set in the CRM."""
    ensure_indexes(db)
    n = 0
    for day, c in WEBSITE_CLASSES.items():
        fid = freeclass_form_id(day)
        if db.query(FormConfig).filter(FormConfig.form_id == fid).first():
            continue
        db.add(FormConfig(
            form_id=fid, form_name=f"Website free class · {day}", campaign_name=f"Website · Free class {day}",
            day=day, session_time=c["session_time"], arrival_time=c["arrival_time"],
            venue=settings.seminar_venue, wa_language="en", is_active=True,
            auto_created=True, needs_review=True,
            notes=("Website seat requests for the " + day + " free class (" + c["name"] + "). "
                   "Set the WhatsApp seat-confirmation template here; it goes out when someone presses Send "
                   "or asks for a call back. Half-filled forms never get a message.")))
        n += 1
    if not db.query(FormConfig).filter(FormConfig.form_id == ENQUIRY_FORM_ID).first():
        db.add(FormConfig(
            form_id=ENQUIRY_FORM_ID, form_name="Website enquiry", campaign_name="Website · Enquiry",
            day="Auto", venue=settings.seminar_venue, wa_language="en", is_active=True,
            auto_created=True, needs_review=True,
            notes=("Website 'invest my money' / 'learn trading' enquiries. Optional: set a WhatsApp "
                   "acknowledgement template (use only the name / city variables). Leave empty to just call them.")))
        n += 1
    if n:
        db.commit()
    return n


# ── helpers ─────────────────────────────────────────────────────────────────

def _s(v: Any, n: int) -> Optional[str]:
    if v is None:
        return None
    v = str(v).strip()
    return v[:n] if v else None


def _valid_mobile(phone: str) -> bool:
    return bool(re.fullmatch(r"91[6-9]\d{9}", phone or ""))


def _meta_time(dt: Optional[datetime] = None) -> str:
    """Same text format Meta uses for created_time, so date filters treat all leads alike."""
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _stage(ev: dict) -> str:
    return STAGE_MAP.get(str(ev.get("stage") or "").strip().lower(), "incomplete")


def _summary_lines(ev: dict, stage: str) -> list[str]:
    rows = [
        ("Looking for", ev.get("looking_for")), ("Area", ev.get("area")), ("Meet / learn", ev.get("mode")),
        ("Amount / level", ev.get("amount")),
        ("Investing already", ev.get("investing") if ev.get("investing") not in ("-", "") else None),
        ("Best time to call", ev.get("call_time")), ("Language", ev.get("language")),
        ("Email", ev.get("email")), ("Note", ev.get("note")),
        ("Clicked on", ev.get("section")),
    ]
    out = [f"Website form · #{ev.get('tag') or 'General'} · {STAGE_LABEL.get(stage, stage)}"]
    out += [f"{k}: {v}" for k, v in rows if v]
    utm = " / ".join(x for x in (ev.get("utm_source"), ev.get("utm_medium"), ev.get("utm_campaign")) if x)
    if utm:
        out.append(f"Ad / link: {utm}")
    return out


def _web_block(lines: list[str]) -> str:
    return "[Website]\n" + "\n".join(lines) + "\n[/Website]"


def _set_notes(lead: Lead, lines: list[str]):
    """Keep one up-to-date website block inside the lead notes, leaving the team's own notes alone."""
    block = _web_block(lines)
    notes = lead.notes or ""
    if "[Website]" in notes and "[/Website]" in notes:
        notes = re.sub(r"\[Website\].*?\[/Website\]", block, notes, count=1, flags=re.S)
    else:
        notes = (block + ("\n\n" + notes if notes else ""))
    lead.notes = notes


FORM_NAMES = {"contact": "Website · Contact page", "mf_course": "Website · Mutual fund course page"}


def _form_name(ev: dict) -> str:
    return FORM_NAMES.get(str(ev.get("form") or ""), WEB_FORM_NAME)


def _event_row(ev: dict, lead_id: Optional[int] = None) -> WebsiteEvent:
    return WebsiteEvent(
        type=_s(ev.get("type"), 40) or "unknown",
        visitor_id=_s(ev.get("visitor_id"), 64), session_id=_s(ev.get("session_id"), 64),
        lead_id=lead_id, event_time=_s(ev.get("time"), 40),
        tag=_s(ev.get("tag"), 40), topic=_s(ev.get("topic"), 255), section=_s(ev.get("section"), 80),
        button=_s(ev.get("button"), 120), page=_s(ev.get("page"), 255), device=_s(ev.get("device"), 20),
        utm_source=_s(ev.get("utm_source"), 120), utm_medium=_s(ev.get("utm_medium"), 120),
        utm_campaign=_s(ev.get("utm_campaign"), 255), utm_content=_s(ev.get("utm_content"), 255),
        referrer=_s(ev.get("referrer"), 500), landed=_s(ev.get("landed"), 40),
        raw={k: v for k, v in ev.items() if k not in ("phone",)} if ev.get("type") == "expert_form" else None,
    )


def _send_template(db: Session, lead: Lead) -> Optional[dict]:
    from .meta import is_fresh
    from .whatsapp import send_template_for_lead
    if not settings.whatsapp_enabled or lead.sync_status != "ok" or lead.whatsapp_sent or lead.whatsapp_message_id:
        return None
    if not is_fresh(lead):
        return {"ok": False, "reason": "stale_lead_not_auto_sent"}
    try:
        return send_template_for_lead(db, lead)
    except Exception as exc:          # a WhatsApp problem must never lose the lead
        db.rollback()
        print(f"[website] WA send failed for lead {lead.id}: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}


def _session_fields(db: Session, tag: str, looking_for: str) -> tuple[str, dict]:
    """(form_id, session fields) for a website lead."""
    if tag == "FreeClass":
        day = infer_day(looking_for)
        if day in WEBSITE_CLASSES:
            fid = freeclass_form_id(day)
            s = resolve_session(db, fid, form_name=f"Website free class · {day}", answers_text=looking_for)
            return fid, {k: s.get(k) for k in ("session_day", "session_date", "session_time", "arrival_time", "venue")}
        return ENQUIRY_FORM_ID, {}
    return ENQUIRY_FORM_ID, {}


def _link_journey(db: Session, lead: Lead, visitor_id: Optional[str]):
    if not visitor_id:
        return
    db.query(WebsiteEvent).filter(WebsiteEvent.visitor_id == visitor_id, WebsiteEvent.lead_id.is_(None)) \
        .update({"lead_id": lead.id}, synchronize_session=False)
    if not lead.web_visitor_id:
        lead.web_visitor_id = visitor_id


# ── the form → lead ─────────────────────────────────────────────────────────

def handle_form(db: Session, ev: dict) -> tuple[Optional[Lead], Optional[dict], str]:
    """Create or update the lead for one expert_form event. Returns (lead, whatsapp_result, action)."""
    phone = clean_phone(ev.get("phone"))
    name = _s(ev.get("name"), 255)
    if not _valid_mobile(phone):
        return None, None, "invalid_phone"
    ref = _s(ev.get("lead_id"), 64)
    stage = _stage(ev)
    tag = _s(ev.get("tag"), 40) or "General"
    looking_for = _s(ev.get("looking_for"), 255) or ""
    lines = _summary_lines(ev, stage)

    lead = db.query(Lead).filter(Lead.web_ref == ref).order_by(Lead.id.desc()).first() if ref else None
    action = "updated"
    if lead is None:
        lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.id.desc()).first()
        if lead is not None:
            action = "existing"
    if lead is None:
        action = "created"

    form_id, sess = _session_fields(db, tag, looking_for)

    if action == "created":
        lead = Lead(
            source="website", platform="Website", status="New",
            full_name=name, phone=phone, city=_s(ev.get("area"), 120), email=_s(ev.get("email"), 255),
            experience=_s(ev.get("investing") if ev.get("investing") not in ("-",) else None, 255),
            preferred_day=sess.get("session_day") or None,
            campaign_name=f"Website · {tag}", form_id=form_id, form_name=_form_name(ev),
            ad_name=_s(ev.get("section"), 255), adset_name=_s(looking_for, 255),
            created_time=_meta_time(), web_ref=ref, web_stage=stage,
            sync_status="web_incomplete" if stage == "incomplete" else "ok",
            raw={"source": "website", "form": {k: v for k, v in ev.items()}},
            **sess,
        )
        _set_notes(lead, lines)
        db.add(lead)
        db.flush()
    elif action == "updated" and lead.source == "website":
        # same website form session → refresh what they typed, never downgrade the stage
        if name:
            lead.full_name = name
        lead.phone = phone
        lead.city = _s(ev.get("area"), 120) or lead.city
        lead.email = _s(ev.get("email"), 255) or lead.email
        if STAGE_RANK.get(stage, 0) >= STAGE_RANK.get(lead.web_stage or "", -1):
            lead.web_stage = stage
        if lead.web_stage != "incomplete" and lead.sync_status == "web_incomplete":
            lead.sync_status = "ok"
        if not lead.campaign_name or lead.campaign_name.startswith("Website · "):
            lead.campaign_name = f"Website · {tag}"
        if form_id != lead.form_id and sess:
            lead.form_id = form_id
            for k, v in sess.items():
                setattr(lead, k, v)
            lead.preferred_day = sess.get("session_day") or lead.preferred_day
        lead.adset_name = _s(looking_for, 255) or lead.adset_name
        lead.raw = {**(lead.raw or {}), "source": "website", "form": {k: v for k, v in ev.items()}}
        _set_notes(lead, _summary_lines(ev, lead.web_stage))
    else:
        # the person is already in the CRM (Meta ad, WhatsApp, manual …): add a follow-up, no duplicate
        mark = f"[web:{ref}]" if ref else "[web]"
        fu = (db.query(FollowUp).filter(FollowUp.lead_id == lead.id, FollowUp.remarks.like(f"%{mark}%"))
              .order_by(FollowUp.id.desc()).first()) if ref else None
        text = "\n".join(lines)
        if fu:
            fu.response = text
        else:
            n = db.query(func.count(FollowUp.id)).filter(FollowUp.lead_id == lead.id).scalar() or 0
            db.add(FollowUp(lead_id=lead.id, followup_no=n + 1, followup_date=date.today().isoformat(),
                            response=text, session_date=sess.get("session_date"),
                            remarks=f"Website enquiry {mark}"))
        if not lead.full_name and name:
            lead.full_name = name
        if not lead.email and ev.get("email"):
            lead.email = _s(ev.get("email"), 255)
        if STAGE_RANK.get(stage, 0) >= STAGE_RANK.get(lead.web_stage or "", -1):
            lead.web_stage = stage
        lead.web_ref = ref or lead.web_ref
        lead.next_followup_at = lead.next_followup_at or date.today().isoformat()

    _link_journey(db, lead, _s(ev.get("visitor_id"), 64))
    db.commit()
    db.refresh(lead)

    wa = None
    if lead.source == "website" and lead.web_stage in SENT_STAGES:
        wa = _send_template(db, lead)
    return lead, wa, action


# ── WhatsApp tickets typed by the website ──────────────────────────────────

_TICKET_RE = re.compile(r"#(\w+)\s*·\s*([^\n]*)")
_FROM_SITE_RE = re.compile(r"\(sent from the website · ([^)]*)\)", re.I)


def parse_ticket(text: str) -> Optional[dict]:
    """'Hi Wealth Ocean Institute,\\n#FreeClass · Monday · …\\n…Name: Ravi\\n(sent from the website · Free classes)'"""
    if not text or "sent from the website" not in text.lower():
        return None
    m = _TICKET_RE.search(text)
    src = _FROM_SITE_RE.search(text)
    fields = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            if v.strip():
                fields[k.strip().lower()] = v.strip()
    return {"tag": m.group(1) if m else "General", "topic": (m.group(2).strip() if m else ""),
            "section": src.group(1).strip() if src else "", "name": fields.get("name"),
            "city": fields.get("city / town") or fields.get("area") or fields.get("city"),
            "fields": fields}


def on_incoming_whatsapp(db: Session, lead: Lead, text: str, created: bool) -> Optional[dict]:
    """Called for every incoming WhatsApp message. A website ticket from a number the CRM had not
    seen becomes a proper website lead; every ticket is logged for the 'clicked vs actually sent' report."""
    t = parse_ticket(text)
    if not t:
        return None
    db.add(WebsiteEvent(type="whatsapp_message", lead_id=lead.id, tag=_s(t["tag"], 40),
                        topic=_s(t["topic"], 255), section=_s(t["section"], 80),
                        event_time=now_iso(), raw={"text": text[:1500]}))
    wa = None
    if created or (lead.status == "WhatsApp" and not lead.meta_lead_id and not lead.source):
        looking = f"{t['topic']}"
        form_id, sess = _session_fields(db, t["tag"], looking)
        lead.source = "website"
        lead.platform = "Website"
        lead.status = "New"
        lead.web_stage = "whatsapp_direct"
        lead.campaign_name = f"Website · {t['tag']}"
        lead.form_id, lead.form_name = form_id, "Website · WhatsApp button"
        lead.ad_name = _s(t["section"], 255)
        lead.adset_name = _s(t["topic"], 255)
        lead.created_time = lead.created_time or _meta_time()
        if t.get("name") and not lead.full_name:
            lead.full_name = _s(t["name"], 255)
        if t.get("city") and not lead.city:
            lead.city = _s(t["city"], 120)
        for k, v in sess.items():
            setattr(lead, k, v)
        lead.preferred_day = sess.get("session_day") or lead.preferred_day
        _set_notes(lead, [f"Website WhatsApp button · #{t['tag']} · {t['topic']}",
                          *(f"{k.title()}: {v}" for k, v in t["fields"].items())])
        db.commit()
        if t["tag"] == "FreeClass":
            wa = _send_template(db, lead)
    else:
        db.commit()
    return wa


# ── maintenance ─────────────────────────────────────────────────────────────

def purge_old_events(db: Session) -> int:
    keep = max(30, settings.website_events_keep_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep)
    n = db.query(WebsiteEvent).filter(WebsiteEvent.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return n


# ── routes ──────────────────────────────────────────────────────────────────

@router.post("/public/website/events")
async def ingest(request: Request, db: Session = Depends(get_db),
                 x_website_key: str = Header(default="")):
    if not settings.website_ingest_key:
        raise HTTPException(503, "Website ingest is off: set WEBSITE_INGEST_KEY")
    if x_website_key != settings.website_ingest_key:
        raise HTTPException(401, "Bad website key")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    events = body.get("events") if isinstance(body, dict) and "events" in body else body
    if isinstance(events, dict):
        events = [events]
    if not isinstance(events, list):
        raise HTTPException(400, "Expected an event or {events:[…]}")
    events = [e for e in events if isinstance(e, dict)][:MAX_EVENTS_PER_CALL]

    out = {"stored": 0, "leads": []}
    for ev in events:
        if ev.get("type") not in EVENT_TYPES or ev.get("type") == "whatsapp_message":
            continue
        try:
            lead = None
            if ev.get("type") == "expert_form":
                lead, wa, action = handle_form(db, ev)
                if lead:
                    out["leads"].append({"lead_id": lead.id, "action": action, "stage": lead.web_stage,
                                         "whatsapp": (wa or {}).get("ok") if wa else None})
            row = _event_row(ev, lead.id if lead else None)
            if row.lead_id is None and row.visitor_id:
                known = db.query(Lead.id).filter(Lead.web_visitor_id == row.visitor_id).order_by(Lead.id.desc()).first()
                row.lead_id = known[0] if known else None
            db.add(row)
            db.commit()
            out["stored"] += 1
        except Exception as exc:
            db.rollback()
            print(f"[website] event failed: {type(exc).__name__}: {exc}", flush=True)
    return out


def _range(date_from: str, date_to: str):
    tz = ZoneInfo(settings.seminar_timezone)
    today = datetime.now(tz).date()
    try:
        d_to = date.fromisoformat(date_to) if date_to else today
        d_from = date.fromisoformat(date_from) if date_from else d_to - timedelta(days=29)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-11-18")
    start = datetime.combine(d_from, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d_to + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    return d_from, d_to, start, end, tz


def _ref_name(ref: Optional[str]) -> str:
    if not ref or ref == "direct":
        return "Direct / typed"
    host = (urlparse(ref).netloc or ref).lower().removeprefix("www.").removeprefix("m.").removeprefix("l.")
    for k, v in (("facebook", "Facebook"), ("instagram", "Instagram"), ("google", "Google"),
                 ("youtube", "YouTube"), ("whatsapp", "WhatsApp"), ("bing", "Bing")):
        if k in host:
            return v
    return host or "Other"


def _traffic_source(e: WebsiteEvent) -> str:
    if e.utm_source:
        return e.utm_source.strip().title()
    return _ref_name(e.referrer)


FORM_STEPS = [("kind", "What would you like to do?"), ("need", "Goal / class"), ("amt", "Amount / level"),
              ("exp", "Investing already?"), ("where", "City and how to meet"), ("when", "Best time"),
              ("you", "Name and mobile")]


@router.get("/reports/website")
def website_report(date_from: str = "", date_to: str = "", db: Session = Depends(get_db),
                   user: str = Depends(require_user)):
    d_from, d_to, start, end, tz = _range(date_from, date_to)
    rows = (db.query(WebsiteEvent).filter(WebsiteEvent.created_at >= start, WebsiteEvent.created_at < end)
            .order_by(WebsiteEvent.id.asc()).limit(300000).all())

    def local_day(e):
        ts = e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=timezone.utc)
        return ts.astimezone(tz).date().isoformat()

    by_type = Counter(e.type for e in rows)
    visitors = {e.visitor_id for e in rows if e.visitor_id}
    sessions = {e.session_id for e in rows if e.session_id}

    days = []
    d = d_from
    idx = {}
    while d <= d_to:
        idx[d.isoformat()] = len(days)
        days.append({"date": d.isoformat(), "visitors": set(), "page_views": 0, "whatsapp_clicks": 0,
                     "calls": 0, "form_opens": 0, "leads": 0})
        d += timedelta(days=1)
    for e in rows:
        i = idx.get(local_day(e))
        if i is None:
            continue
        b = days[i]
        if e.visitor_id:
            b["visitors"].add(e.visitor_id)
        b["page_views"] += e.type == "page_view"
        b["whatsapp_clicks"] += e.type == "whatsapp_click"
        b["calls"] += e.type == "call_click"
        b["form_opens"] += e.type == "expert_open"

    web_leads = (db.query(Lead).filter(Lead.source == "website", Lead.created_at >= start, Lead.created_at < end)
                 .order_by(Lead.id.desc()).all())
    for l in web_leads:
        ts = l.created_at if l.created_at.tzinfo else l.created_at.replace(tzinfo=timezone.utc)
        i = idx.get(ts.astimezone(tz).date().isoformat())
        if i is not None:
            days[i]["leads"] += 1
    for b in days:
        b["visitors"] = len(b["visitors"])

    # existing CRM people who used the website form in this period (no new lead was made)
    touched_existing = {e.lead_id for e in rows if e.type == "expert_form" and e.lead_id} - {l.id for l in web_leads}

    # traffic sources: first page_view of every session
    first_view = {}
    for e in rows:
        if e.type == "page_view" and e.session_id and e.session_id not in first_view:
            first_view[e.session_id] = e
    sources = Counter(_traffic_source(e) for e in first_view.values())
    campaigns = Counter(e.utm_campaign for e in first_view.values() if e.utm_campaign)
    devices = Counter((e.device or "unknown") for e in first_view.values())

    # sections people reached (distinct sessions)
    sec_sessions = defaultdict(set)
    for e in rows:
        if e.type == "section_view" and e.section:
            sec_sessions[e.section].add(e.session_id or e.visitor_id or e.id)
    section_reach = {k: len(v) for k, v in sec_sessions.items()}

    wa = [e for e in rows if e.type == "whatsapp_click"]
    wa_msgs = [e for e in rows if e.type == "whatsapp_message"]

    # form funnel by form session (lead_id from the website) – fall back to browser session
    opened = {e.session_id or e.visitor_id for e in rows if e.type == "expert_open"}
    step_reach = defaultdict(set)
    for e in rows:
        if e.type == "form_step" and e.topic:
            step_reach[e.topic].add(e.session_id or e.visitor_id)
    forms = [e for e in rows if e.type == "expert_form"]
    form_refs = defaultdict(lambda: "incomplete")
    for e in forms:
        ref = (e.raw or {}).get("lead_id") or e.id
        st = STAGE_MAP.get(str((e.raw or {}).get("stage") or "").lower(), "incomplete")
        if STAGE_RANK.get(st, 0) >= STAGE_RANK.get(form_refs[ref], 0):
            form_refs[ref] = st
    form_counts = Counter(form_refs.values())

    buttons = Counter((e.section or "?", e.button or "?") for e in rows
                      if e.type in ("whatsapp_click", "call_click", "map_click", "share_click", "expert_open"))

    status_of = Counter((l.status or "New") for l in web_leads)
    tot_visitors = len(visitors)
    submitted = sum(form_counts.get(k, 0) for k in SENT_STAGES)

    return {
        "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
        "totals": {
            "visitors": tot_visitors, "sessions": len(sessions), "page_views": by_type.get("page_view", 0),
            "whatsapp_clicks": by_type.get("whatsapp_click", 0), "whatsapp_messages": len(wa_msgs),
            "calls": by_type.get("call_click", 0), "maps": by_type.get("map_click", 0),
            "shares": by_type.get("share_click", 0), "form_opens": by_type.get("expert_open", 0),
            "forms_submitted": submitted, "forms_incomplete": form_counts.get("incomplete", 0),
            "new_leads": len(web_leads), "existing_leads_returned": len(touched_existing),
            "confirmed": sum(n for s, n in status_of.items() if "confirm" in s.lower() or "attend" in s.lower()),
            "lead_rate": round(100 * len(web_leads) / tot_visitors, 1) if tot_visitors else 0,
        },
        "by_day": days,
        "whatsapp_by_tag": dict(Counter(e.tag or "General" for e in wa).most_common()),
        "whatsapp_by_section": dict(Counter(e.section or "?" for e in wa).most_common()),
        "whatsapp_messages_by_tag": dict(Counter(e.tag or "General" for e in wa_msgs).most_common()),
        "sources": dict(sources.most_common(12)),
        "campaigns": dict(campaigns.most_common(12)),
        "devices": dict(devices.most_common()),
        "section_reach": section_reach,
        "form_funnel": {
            "opened": len(opened),
            "steps": [{"id": k, "label": lbl, "reached": len(step_reach.get(k, ()))} for k, lbl in FORM_STEPS],
            "incomplete": form_counts.get("incomplete", 0),
            "whatsapp": form_counts.get("whatsapp", 0), "callback": form_counts.get("callback", 0),
            "other_forms": form_counts.get("submitted", 0) + form_counts.get("checkout", 0),
        },
        "leads_by_status": dict(status_of.most_common()),
        "leads_by_tag": dict(Counter((l.campaign_name or "").replace("Website · ", "") or "General"
                                     for l in web_leads).most_common()),
        "top_buttons": [{"section": s, "button": b, "clicks": n} for (s, b), n in buttons.most_common(15)],
        "recent_leads": [{
            "id": l.id, "name": l.full_name, "phone": l.phone, "tag": (l.campaign_name or "").replace("Website · ", ""),
            "stage": l.web_stage, "status": l.status, "section": l.ad_name, "looking_for": l.adset_name,
            "session_day": l.session_day, "session_date": l.session_date,
            "whatsapp_sent": bool(l.whatsapp_sent), "created_time": l.created_time,
        } for l in web_leads[:40]],
    }


@router.get("/website/journey/{lead_id}")
def lead_journey(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Everything this person did on the website before and after becoming a lead."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    # this lead's own events, plus anonymous events from the same browser that no other lead owns
    cond = WebsiteEvent.lead_id == lead_id
    if lead.web_visitor_id:
        cond = or_(cond, and_(WebsiteEvent.visitor_id == lead.web_visitor_id, WebsiteEvent.lead_id.is_(None)))
    q = db.query(WebsiteEvent).filter(cond)
    ev = q.order_by(WebsiteEvent.id.asc()).limit(500).all()
    return {"lead_id": lead_id, "events": [{
        "type": e.type, "time": e.event_time or (e.created_at.isoformat() if e.created_at else None),
        "section": e.section, "button": e.button, "tag": e.tag, "topic": e.topic,
        "device": e.device, "source": _traffic_source(e) if e.type == "page_view" else None,
        "utm_campaign": e.utm_campaign,
    } for e in ev]}
