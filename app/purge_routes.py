"""
Delete old leads (admin). Three safeguards:
  1. preview   – shows exactly how much would go (and how much has real data)
  2. backup    – CSV of everything that would be deleted
  3. confirm   – the request must carry confirm="DELETE"; cut-off cannot be inside the last 7 days
"""
import csv
import io
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session

from .alerts import log_event
from .auth import require_user
from .db import get_db
from .models import Lead, FollowUp, WhatsAppMessage

router = APIRouter(prefix="/admin/leads", tags=["Admin"])


def _cutoff(before: str) -> str:
    try:
        d = date.fromisoformat(before)
    except ValueError:
        raise HTTPException(400, "Use a date like 2026-09-01")
    if d > date.today() - timedelta(days=7):
        raise HTTPException(400, "For safety the cut-off must be at least 7 days in the past.")
    return d.isoformat()


def _old_filter(cut: str):
    """Received before `cut`: Meta created_time (ISO text) when present, else the CRM created_at."""
    cut_dt = datetime.fromisoformat(cut)
    return or_(
        and_(Lead.created_time.isnot(None), Lead.created_time != "", Lead.created_time < cut),
        and_(or_(Lead.created_time.is_(None), Lead.created_time == ""), Lead.created_at < cut_dt),
    )


@router.get("/purge-preview")
def purge_preview(before: str = Query(...), db: Session = Depends(get_db), user: str = Depends(require_user)):
    cut = _cutoff(before)
    f = _old_filter(cut)
    ids = [r[0] for r in db.query(Lead.id).filter(f).all()]
    total = len(ids)
    with_phone = db.query(func.count(Lead.id)).filter(f, Lead.phone.isnot(None), Lead.phone != "").scalar() or 0
    followups = confirmed = msgs = 0
    for i in range(0, total, 500):
        chunk = ids[i:i + 500]
        followups += db.query(func.count(FollowUp.id)).filter(FollowUp.lead_id.in_(chunk)).scalar() or 0
        msgs += db.query(func.count(WhatsAppMessage.id)).filter(WhatsAppMessage.lead_id.in_(chunk)).scalar() or 0
        confirmed += db.query(func.count(Lead.id)).filter(Lead.id.in_(chunk), Lead.status == "Confirmed").scalar() or 0
    return {"before": cut, "leads": total, "with_phone": with_phone, "without_phone": total - with_phone,
            "followups": followups, "whatsapp_messages": msgs, "confirmed_leads": confirmed}


@router.get("/purge-export")
def purge_export(before: str = Query(...), db: Session = Depends(get_db), user: str = Depends(require_user)):
    """CSV backup of the leads that a purge with the same cut-off would delete."""
    cut = _cutoff(before)
    rows = db.query(Lead).filter(_old_filter(cut)).order_by(Lead.id.asc()).all()
    cols = ["id", "meta_lead_id", "created_time", "created_at", "full_name", "phone", "email", "city", "status",
            "campaign_name", "session_day", "session_date", "whatsapp_sent", "sync_status"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([getattr(r, c, "") if getattr(r, c, None) is not None else "" for c in cols])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="leads_before_{cut}.csv"'})


class PurgeIn(BaseModel):
    before: str
    confirm: str = ""


@router.post("/purge")
def purge(body: PurgeIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    if body.confirm != "DELETE":
        raise HTTPException(400, 'Type DELETE to confirm.')
    cut = _cutoff(body.before)
    ids = [r[0] for r in db.query(Lead.id).filter(_old_filter(cut)).all()]
    deleted = 0
    for i in range(0, len(ids), 300):
        chunk = ids[i:i + 300]
        db.query(WhatsAppMessage).filter(WhatsAppMessage.lead_id.in_(chunk)).delete(synchronize_session=False)
        db.query(FollowUp).filter(FollowUp.lead_id.in_(chunk)).delete(synchronize_session=False)
        deleted += db.query(Lead).filter(Lead.id.in_(chunk)).delete(synchronize_session=False)
        db.commit()
    log_event("leads_purged", f"{deleted} lead(s) received before {cut} deleted by {user}", "warn")
    return {"ok": True, "deleted": deleted, "before": cut}
