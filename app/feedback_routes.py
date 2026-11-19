"""
Seminar feedback — the paper form attendees fill at the end of a seminar, entered in the CRM.

* Attendee is either picked from existing leads by mobile number, or added as a walk-in
  (a new lead with source=manual is created so they appear in Leads as well).
* The questions are rows in `feedback_fields` and can be added / edited / hidden / removed /
  re-ordered from the CRM. Name, mobile number and seminar date are fixed and always asked.
"""
import csv
import io
import re
from datetime import date
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .auth import require_user
from .db import get_db
from .models import FeedbackField, Lead, SeminarFeedback
from .utils import clean_phone

router = APIRouter(tags=["feedback"])

FIELD_TYPES = {"text", "textarea", "radio", "checkbox", "referrals"}

# The questions on the printed "Seminar Feedback" form — seeded once, then editable.
DEFAULT_FIELDS = [
    {"key": "profession", "label": "Profession", "field_type": "text"},
    {"key": "seminar_experience", "label": "Seminar Experience", "field_type": "radio",
     "options": ["Very Good", "Good", "Average", "Not upto Expectation"]},
    {"key": "interested_course", "label": "Interested to opt for course", "field_type": "checkbox",
     "options": ["PANCHAJANYA COURSE (Full Course)",
                 "SUVARNA RATNA COURSE (Investment Course)",
                 "MUTUAL FUND & GOVT- CORPORATE BOND (Investment Course)"]},
    {"key": "time_to_join", "label": "Time required to join the course", "field_type": "radio",
     "options": ["Today", "Tomorrow", "Within 1 week", "Don't know"]},
    {"key": "eye_opener", "label": "One Eye Opener from seminar", "field_type": "textarea"},
    {"key": "referrals", "label": "Friend or relative who will benefit from this seminar",
     "field_type": "referrals", "rows": 3},
    {"key": "comments", "label": "Comments (If any)", "field_type": "textarea"},
]


def seed_feedback_fields(db: Session) -> None:
    if db.query(func.count(FeedbackField.id)).scalar():
        return
    for i, f in enumerate(DEFAULT_FIELDS, start=1):
        db.add(FeedbackField(sort_order=i * 10, **f))
    db.commit()


def _field_out(f: FeedbackField) -> dict:
    return {
        "id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type,
        "options": f.options or [], "rows": f.rows, "required": bool(f.required),
        "is_active": bool(f.is_active), "sort_order": f.sort_order or 0,
    }


def _feedback_out(r: SeminarFeedback, lead: Optional[Lead] = None) -> dict:
    return {
        "id": r.id, "lead_id": r.lead_id, "seminar_date": r.seminar_date,
        "full_name": r.full_name, "phone": r.phone, "walk_in": bool(r.walk_in),
        "answers": r.answers or {}, "labels": r.labels or {},
        "lead_status": lead.status if lead else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return (s or "field")[:60]


def _clean_options(options: Optional[List[str]]) -> List[str]:
    out = []
    for o in options or []:
        o = str(o).strip()
        if o and o not in out:
            out.append(o)
    return out


# ---------------------------------------------------------------------------
# Form fields
# ---------------------------------------------------------------------------

class FieldIn(BaseModel):
    label: str
    field_type: str = "text"
    options: Optional[List[str]] = None
    rows: Optional[int] = None
    required: bool = False
    is_active: bool = True


class FieldPatch(BaseModel):
    label: Optional[str] = None
    field_type: Optional[str] = None
    options: Optional[List[str]] = None
    rows: Optional[int] = None
    required: Optional[bool] = None
    is_active: Optional[bool] = None


class ReorderIn(BaseModel):
    ids: List[int]


def _validate_field(field_type: str, options: List[str]) -> None:
    if field_type not in FIELD_TYPES:
        raise HTTPException(400, f"field_type must be one of: {', '.join(sorted(FIELD_TYPES))}")
    if field_type in ("radio", "checkbox") and len(options) < 2:
        raise HTTPException(400, "Give at least 2 options for a choice question")


@router.get("/feedback/fields")
def list_fields(include_inactive: bool = True, db: Session = Depends(get_db), user: str = Depends(require_user)):
    seed_feedback_fields(db)
    q = db.query(FeedbackField)
    if not include_inactive:
        q = q.filter(FeedbackField.is_active.is_(True))
    rows = q.order_by(FeedbackField.sort_order, FeedbackField.id).all()
    return {"rows": [_field_out(f) for f in rows], "types": sorted(FIELD_TYPES)}


@router.post("/feedback/fields")
def add_field(data: FieldIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    label = data.label.strip()
    if not label:
        raise HTTPException(400, "Question label is required")
    options = _clean_options(data.options)
    _validate_field(data.field_type, options)
    base = key = _slug(label)
    n = 2
    while db.query(FeedbackField).filter(FeedbackField.key == key).first():
        key = f"{base}_{n}"; n += 1
    last = db.query(func.max(FeedbackField.sort_order)).scalar() or 0
    f = FeedbackField(
        key=key, label=label, field_type=data.field_type,
        options=options if data.field_type in ("radio", "checkbox") else None,
        rows=max(1, min(data.rows or 3, 20)) if data.field_type == "referrals" else None,
        required=data.required, is_active=data.is_active, sort_order=last + 10,
    )
    db.add(f); db.commit(); db.refresh(f)
    return _field_out(f)


@router.patch("/feedback/fields/{field_id}")
def update_field(field_id: int, data: FieldPatch, db: Session = Depends(get_db), user: str = Depends(require_user)):
    f = db.get(FeedbackField, field_id)
    if not f:
        raise HTTPException(404, "Question not found")
    if data.label is not None:
        if not data.label.strip():
            raise HTTPException(400, "Question label is required")
        f.label = data.label.strip()
    ftype = data.field_type if data.field_type is not None else f.field_type
    options = _clean_options(data.options) if data.options is not None else (f.options or [])
    _validate_field(ftype, options)
    f.field_type = ftype
    f.options = options if ftype in ("radio", "checkbox") else None
    if ftype == "referrals":
        f.rows = max(1, min(data.rows if data.rows is not None else (f.rows or 3), 20))
    else:
        f.rows = None
    if data.required is not None:
        f.required = data.required
    if data.is_active is not None:
        f.is_active = data.is_active
    db.commit(); db.refresh(f)
    return _field_out(f)


@router.post("/feedback/fields/reorder")
def reorder_fields(data: ReorderIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    for i, fid in enumerate(data.ids, start=1):
        f = db.get(FeedbackField, fid)
        if f:
            f.sort_order = i * 10
    db.commit()
    return {"ok": True}


@router.delete("/feedback/fields/{field_id}")
def delete_field(field_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Removes the question from the form. Answers already saved are kept (with their label)."""
    f = db.get(FeedbackField, field_id)
    if not f:
        raise HTTPException(404, "Question not found")
    db.delete(f); db.commit()
    return {"ok": True}


@router.post("/feedback/fields/reset")
def reset_fields(db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Put back the questions of the printed form (custom questions are removed)."""
    db.query(FeedbackField).delete()
    db.commit()
    seed_feedback_fields(db)
    return list_fields(True, db, user)


# ---------------------------------------------------------------------------
# Attendee lookup by phone
# ---------------------------------------------------------------------------

@router.get("/feedback/lookup")
def lookup_attendee(phone: str = Query(..., min_length=3), db: Session = Depends(get_db), user: str = Depends(require_user)):
    digits = re.sub(r"\D+", "", phone)
    if len(digits) < 3:
        return {"rows": []}
    tail = digits[-10:]
    leads = (db.query(Lead).filter(Lead.phone.ilike(f"%{tail}%"))
             .order_by(Lead.created_at.desc()).limit(10).all())
    out = []
    for l in leads:
        fb_count = db.query(func.count(SeminarFeedback.id)).filter(SeminarFeedback.lead_id == l.id).scalar()
        out.append({
            "id": l.id, "full_name": l.full_name, "phone": l.phone, "city": l.city,
            "status": l.status, "session_day": l.session_day, "session_date": l.session_date,
            "campaign_name": l.campaign_name, "source": l.source, "feedback_count": fb_count,
        })
    return {"rows": out}


# ---------------------------------------------------------------------------
# Feedback entries
# ---------------------------------------------------------------------------

class FeedbackIn(BaseModel):
    lead_id: Optional[int] = None          # set when picked from leads
    full_name: str = ""
    phone: str = ""
    seminar_date: str = ""                 # YYYY-MM-DD, defaults to today
    answers: dict[str, Any] = {}
    mark_attended: bool = True             # set the lead's status to "Attended"
    add_referrals_as_leads: bool = False   # create leads for the referred friends


def _clean_answers(answers: dict, fields: List[FeedbackField]) -> dict:
    by_key = {f.key: f for f in fields}
    out: dict[str, Any] = {}
    for key, val in (answers or {}).items():
        f = by_key.get(key)
        if not f:
            continue
        if f.field_type == "checkbox":
            vals = [str(v).strip() for v in (val or []) if str(v).strip()] if isinstance(val, list) else []
            if vals:
                out[key] = vals
        elif f.field_type == "referrals":
            refs = []
            for r in val or []:
                if not isinstance(r, dict):
                    continue
                name = str(r.get("name") or "").strip()
                ph = str(r.get("phone") or "").strip()
                if name or ph:
                    refs.append({"name": name, "phone": clean_phone(ph) if ph else ""})
            if refs:
                out[key] = refs
        else:
            v = str(val or "").strip()
            if v:
                out[key] = v
    for f in fields:
        if f.required and f.is_active and key_missing(out, f.key):
            raise HTTPException(400, f"'{f.label}' is required")
    return out


def key_missing(d: dict, key: str) -> bool:
    return key not in d or d[key] in ("", [], None)


def _save_referral_leads(db: Session, answers: dict, fields: List[FeedbackField], by_name: str) -> int:
    added = 0
    for f in fields:
        if f.field_type != "referrals":
            continue
        for r in answers.get(f.key) or []:
            ph = r.get("phone")
            if not ph or len(ph) < 10:
                continue
            if db.query(Lead).filter(Lead.phone == ph).first():
                continue
            db.add(Lead(
                full_name=r.get("name") or "", phone=ph, source="manual",
                campaign_name="Seminar referral", platform="Referral", status="New",
                notes=f"Referred by {by_name} (seminar feedback)",
                sync_status="ok", raw={"source": "feedback_referral"},
            ))
            added += 1
    return added


@router.post("/feedback")
def create_feedback(data: FeedbackIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    seed_feedback_fields(db)
    fields = db.query(FeedbackField).all()
    answers = _clean_answers(data.answers, fields)
    phone = clean_phone(data.phone)
    name = data.full_name.strip()
    seminar_date = (data.seminar_date or date.today().isoformat())[:10]

    lead = db.get(Lead, data.lead_id) if data.lead_id else None
    if data.lead_id and not lead:
        raise HTTPException(404, "Lead not found")
    walk_in = lead is None
    if lead:
        phone = phone or lead.phone or ""
        name = name or lead.full_name or ""
        if name and not lead.full_name:
            lead.full_name = name
    else:
        if len(phone) < 10:
            raise HTTPException(400, "Enter a valid 10-digit mobile number")
        if not name:
            raise HTTPException(400, "Name is required")
        # Walk-in: reuse a lead with the same number if one exists, else create one
        lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.created_at.desc()).first()
        if not lead:
            lead = Lead(
                full_name=name, phone=phone, source="manual",
                campaign_name="Seminar walk-in", platform="Walk-in", status="New",
                session_date=seminar_date, sync_status="ok", raw={"source": "feedback_walk_in"},
            )
            db.add(lead); db.flush()
    if data.mark_attended:
        lead.status = "Attended"

    labels = {f.key: f.label for f in fields if f.key in answers}
    fb = SeminarFeedback(
        lead_id=lead.id, seminar_date=seminar_date, full_name=name, phone=phone,
        walk_in=walk_in, answers=answers, labels=labels,
    )
    db.add(fb)
    referrals_added = _save_referral_leads(db, answers, fields, name or phone) if data.add_referrals_as_leads else 0
    db.commit(); db.refresh(fb)
    return {**_feedback_out(fb, lead), "referral_leads_added": referrals_added}


class FeedbackPatch(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    seminar_date: Optional[str] = None
    answers: Optional[dict[str, Any]] = None


@router.patch("/feedback/{feedback_id}")
def update_feedback(feedback_id: int, data: FeedbackPatch, db: Session = Depends(get_db), user: str = Depends(require_user)):
    fb = db.get(SeminarFeedback, feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")
    if data.full_name is not None:
        fb.full_name = data.full_name.strip()
    if data.phone is not None:
        fb.phone = clean_phone(data.phone)
    if data.seminar_date:
        fb.seminar_date = data.seminar_date[:10]
    if data.answers is not None:
        fields = db.query(FeedbackField).all()
        answers = _clean_answers(data.answers, fields)
        # keep answers to questions that were removed from the form since
        for k, v in (fb.answers or {}).items():
            if k not in {f.key for f in fields}:
                answers.setdefault(k, v)
        fb.answers = answers
        fb.labels = {**(fb.labels or {}), **{f.key: f.label for f in fields if f.key in answers}}
    db.commit(); db.refresh(fb)
    return _feedback_out(fb, db.get(Lead, fb.lead_id) if fb.lead_id else None)


@router.delete("/feedback/{feedback_id}")
def delete_feedback(feedback_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    fb = db.get(SeminarFeedback, feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")
    db.delete(fb); db.commit()
    return {"ok": True}


def _filtered(db: Session, q: str, date_from: str, date_to: str, walk_in: str):
    query = db.query(SeminarFeedback)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(SeminarFeedback.full_name.ilike(like), SeminarFeedback.phone.ilike(like)))
    if date_from:
        query = query.filter(SeminarFeedback.seminar_date >= date_from[:10])
    if date_to:
        query = query.filter(SeminarFeedback.seminar_date <= date_to[:10])
    if walk_in == "yes":
        query = query.filter(SeminarFeedback.walk_in.is_(True))
    elif walk_in == "no":
        query = query.filter(SeminarFeedback.walk_in.is_(False))
    return query


@router.get("/feedback")
def list_feedback(
    q: str = "", date_from: str = "", date_to: str = "", walk_in: str = "",
    limit: int = Query(500, le=5000), offset: int = 0,
    db: Session = Depends(get_db), user: str = Depends(require_user),
):
    query = _filtered(db, q, date_from, date_to, walk_in)
    total = query.count()
    rows = query.order_by(SeminarFeedback.seminar_date.desc(), SeminarFeedback.id.desc()).offset(offset).limit(limit).all()
    lead_ids = {r.lead_id for r in rows if r.lead_id}
    leads = {l.id: l for l in db.query(Lead).filter(Lead.id.in_(lead_ids)).all()} if lead_ids else {}
    dates = [d for (d,) in db.query(SeminarFeedback.seminar_date).distinct().order_by(SeminarFeedback.seminar_date.desc()).limit(60).all() if d]
    return {"total": total, "rows": [_feedback_out(r, leads.get(r.lead_id)) for r in rows], "dates": dates}


@router.get("/feedback/export")
def export_feedback(
    q: str = "", date_from: str = "", date_to: str = "", walk_in: str = "",
    db: Session = Depends(get_db), user: str = Depends(require_user),
):
    rows = _filtered(db, q, date_from, date_to, walk_in).order_by(SeminarFeedback.seminar_date.desc(), SeminarFeedback.id.desc()).all()
    fields = db.query(FeedbackField).order_by(FeedbackField.sort_order, FeedbackField.id).all()
    cols = [(f.key, f.label, f.field_type) for f in fields]
    known = {f.key for f in fields}
    extra = {}
    for r in rows:   # answers to questions removed from the form
        for k in (r.answers or {}):
            if k not in known and k not in extra:
                extra[k] = (r.labels or {}).get(k, k)
    cols += [(k, lbl, "") for k, lbl in extra.items()]

    def fmt(v):
        if isinstance(v, list):
            return "; ".join(f"{x.get('name', '')} {x.get('phone', '')}".strip() if isinstance(x, dict) else str(x) for x in v)
        return "" if v is None else str(v)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Seminar date", "Name", "Mobile", "Walk-in"] + [c[1] for c in cols])
    for r in rows:
        a = r.answers or {}
        w.writerow([r.seminar_date, r.full_name, r.phone, "Yes" if r.walk_in else "No"] + [fmt(a.get(c[0])) for c in cols])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="seminar_feedback.csv"'})
