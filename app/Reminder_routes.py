"""CRM -> Reminders: settings, upcoming list, log, run-now and test-send."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import require_user
from .db import get_db
from .models import Lead, ReminderLog, WhatsAppMessage, WhatsAppTemplate, TemplateHeaderImage
from . import reminders
from . import whatsapp as wa

router = APIRouter(prefix="/reminders", tags=["reminders"])


class RuleIn(BaseModel):
    template: str | None = None
    language: str | None = None
    params: str | None = None


class SettingsIn(BaseModel):
    enabled: bool | None = None
    days: list[str] | None = None
    rules: dict[str, RuleIn] | None = None


class TestIn(BaseModel):
    lead_id: int
    kind: str = "4hr"


def _tmpl_info(db: Session, t: WhatsAppTemplate) -> dict:
    shape = wa._template_shape(t)
    needs_image = shape["header"] in ("IMAGE", "VIDEO", "DOCUMENT")
    img = db.get(TemplateHeaderImage, t.name)
    return {"name": t.name, "language": t.language, "variables": shape["named"] or shape["positional"],
            "needs_image": needs_image, "has_image": bool(img and img.url) or not needs_image,
            "header": shape["header"]}


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: str = Depends(require_user)):
    cfg = reminders.load_settings()
    tmpls = (db.query(WhatsAppTemplate).filter(WhatsAppTemplate.status.in_(["APPROVED", "approved"]))
             .order_by(WhatsAppTemplate.name).all())
    return {
        "settings": cfg,
        "templates": [_tmpl_info(db, t) for t in tmpls],
        "now": datetime.now(reminders.tz()).isoformat(),
        "timezone": str(reminders.tz()),
    }


@router.put("/settings")
def put_settings(data: SettingsIn, user: str = Depends(require_user)):
    payload = data.model_dump(exclude_none=True) if hasattr(data, "model_dump") else data.dict(exclude_none=True)
    if "rules" in payload:
        payload["rules"] = {k: {f: v for f, v in r.items() if v is not None} for k, r in payload["rules"].items()}
    return {"settings": reminders.save_settings(payload)}


@router.get("/upcoming")
def get_upcoming(db: Session = Depends(get_db), user: str = Depends(require_user)):
    return {"rows": reminders.upcoming(db), "enabled": reminders.load_settings()["enabled"]}


@router.get("/log")
def get_log(limit: int = 100, db: Session = Depends(get_db), user: str = Depends(require_user)):
    rows = db.query(ReminderLog).order_by(ReminderLog.id.desc()).limit(min(max(limit, 1), 300)).all()
    leads = {l.id: l for l in db.query(Lead).filter(Lead.id.in_([r.lead_id for r in rows] or [0])).all()}
    wamids = [r.wamid for r in rows if r.wamid]
    msgs = {m.wa_message_id: m for m in db.query(WhatsAppMessage)
            .filter(WhatsAppMessage.wa_message_id.in_(wamids or ["-"])).all()}
    out = []
    for r in rows:
        l = leads.get(r.lead_id)
        m = msgs.get(r.wamid)
        out.append({
            "id": r.id, "lead_id": r.lead_id, "name": l.full_name if l else "", "phone": l.phone if l else "",
            "kind": r.kind, "session_date": r.session_date, "template": r.template, "status": r.status,
            "delivery": (m.status if m else None), "attempts": r.attempts, "error": r.error,
            "at": (r.updated_at or r.created_at).isoformat() if (r.updated_at or r.created_at) else None,
        })
    return {"rows": out}


@router.post("/run")
def run_now(db: Session = Depends(get_db), user: str = Depends(require_user)):
    return reminders.run_due(db)


@router.post("/test")
def test_send(data: TestIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Send a reminder to ONE lead right now (ignores timing). Does not block the real one."""
    if data.kind not in reminders.KINDS:
        raise HTTPException(400, "kind must be 4hr or 1hr")
    lead = db.get(Lead, data.lead_id)
    if not lead or not lead.phone:
        raise HTTPException(404, "Lead not found or has no phone")
    rule = reminders.load_settings()["rules"][data.kind]
    if not rule["template"]:
        raise HTTPException(400, "No template chosen for this reminder")
    res = reminders.send_reminder(db, lead, rule["template"], rule["language"], rule["params"])
    return res
