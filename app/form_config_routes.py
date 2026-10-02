"""
CRUD API for FormConfig — lets frontend manage form_id → campaign mappings.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from .db import get_db
from .auth import require_user
from .models import FormConfig

router = APIRouter(prefix="/form-configs", tags=["Form Config"])


class FormConfigIn(BaseModel):
    form_id:       str
    form_name:     Optional[str] = ""
    campaign_name: str
    day:           str
    session_time:  Optional[str] = ""
    arrival_time:  Optional[str] = ""
    venue:         Optional[str] = ""
    wa_template:   Optional[str] = ""
    wa_language:   Optional[str] = "en"
    wa_params:     Optional[str] = ""
    is_active:     Optional[bool] = True
    notes:         Optional[str] = ""


class FormConfigOut(FormConfigIn):
    id:         int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


def _to_dict(row: FormConfig) -> dict:
    return {
        "id":            row.id,
        "form_id":       row.form_id,
        "form_name":     row.form_name or "",
        "campaign_name": row.campaign_name,
        "day":           row.day,
        "session_time":  row.session_time or "",
        "arrival_time":  row.arrival_time or "",
        "venue":         row.venue or "",
        "wa_template":   row.wa_template or "",
        "wa_language":   row.wa_language or "en",
        "wa_params":     row.wa_params or "",
        "is_active":     row.is_active,
        "auto_created":  bool(row.auto_created),
        "needs_review":  bool(row.needs_review),
        "notes":         row.notes or "",
        "created_at":    str(row.created_at) if row.created_at else "",
        "updated_at":    str(row.updated_at) if row.updated_at else "",
    }


@router.get("/defaults")
def form_defaults(user: str = Depends(require_user)):
    """Per-day defaults (time, arrival, template, variables) — used by the UI to pre-fill new forms."""
    from .form_config import day_defaults, PARAM_KEYS
    return {"days": day_defaults(), "param_keys": list(PARAM_KEYS)}


@router.get("")
def list_form_configs(db: Session = Depends(get_db), user: str = Depends(require_user)):
    rows = db.query(FormConfig).order_by(FormConfig.id.desc()).all()
    return {"rows": [_to_dict(r) for r in rows]}


@router.post("")
def create_form_config(data: FormConfigIn, background: BackgroundTasks, db: Session = Depends(get_db), user: str = Depends(require_user)):
    existing = db.query(FormConfig).filter(FormConfig.form_id == data.form_id).first()
    if existing:
        raise HTTPException(400, f"form_id {data.form_id} already exists. Use PATCH to update.")
    row = FormConfig(**data.model_dump(), needs_review=False, auto_created=False)
    db.add(row); db.commit(); db.refresh(row)
    from .sync import reprocess_needs_config
    background.add_task(reprocess_needs_config)
    return _to_dict(row)


@router.patch("/{config_id}")
def update_form_config(config_id: int, data: FormConfigIn, background: BackgroundTasks, db: Session = Depends(get_db), user: str = Depends(require_user)):
    row = db.get(FormConfig, config_id)
    if not row:
        raise HTTPException(404, "Form config not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    row.needs_review = False            # a human has looked at it
    if (row.day or "").lower() == "auto":
        row.needs_review = True
    db.commit(); db.refresh(row)
    from .sync import reprocess_needs_config
    background.add_task(reprocess_needs_config)
    return _to_dict(row)


@router.delete("/{config_id}")
def delete_form_config(config_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    row = db.get(FormConfig, config_id)
    if not row:
        raise HTTPException(404, "Form config not found")
    db.delete(row); db.commit()
    return {"success": True}
