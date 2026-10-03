"""
Self-healing background jobs (run every SYNC_INTERVAL_MINUTES, and on demand
from the System page):

  1. token health check          (every TOKEN_CHECK_MINUTES)
  2. discover new lead forms     → auto-map by day, flag for review
  3. pull recent leads from Meta → catches anything a missed webhook dropped
  4. retry leads whose fetch failed / that were waiting for a form config
  5. retry WhatsApp invites that failed for a temporary reason
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import graph, health, tokens
from .alerts import notify, log_event
from .config import settings
from .db import SessionLocal
from .form_config import infer_day, ensure_form_row
from .meta import upsert_lead_from_meta, is_fresh, LEAD_FIELDS_EXT, LEAD_FIELDS_BASE
from .models import Lead, FormConfig
from .whatsapp import send_template_for_lead

_lock = threading.Lock()
MAX_WA_ATTEMPTS = 5
MAX_FETCH_ATTEMPTS = 8


def _paged(path: str, params: dict, max_pages: int = 10):
    data = graph.get(path, params=params)
    for _ in range(max_pages):
        for row in data.get("data") or []:
            yield row
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            return
        data = graph.get(nxt)


# ── 2. forms ────────────────────────────────────────────────────────────────

def discover_forms(db: Session) -> dict:
    page_id = tokens.get_page_id()
    if not page_id:
        return {"skipped": "no page id yet"}
    known = {r.form_id for r in db.query(FormConfig.form_id).all()}
    auto, need = [], []
    try:
        for f in _paged(f"{page_id}/leadgen_forms", {"fields": "id,name,status", "limit": 100}):
            fid, name = str(f.get("id")), f.get("name", "")
            if fid in known or (f.get("status") and f["status"] != "ACTIVE"):
                continue
            day = infer_day(name)
            row = ensure_form_row(db, fid, name, day, campaign_name=name)
            (auto if row.is_active else need).append(name or fid)
    except graph.GraphError as e:
        return {"error": str(e)}
    if auto:
        notify("form_auto", "New lead form detected & mapped",
               "Mapped automatically from the form name: " + ", ".join(auto) +
               ".\nPlease confirm on the CRM → Form Config page.", level="warn", cooldown_hours=1)
    if need:
        notify("form_unmapped", "New lead form needs a session day",
               "Could not tell Friday/Sunday from the name: " + ", ".join(need) +
               ".\nOpen CRM → Form Config and set the day. Its leads are being kept safely.",
               level="error", cooldown_hours=6)
    return {"auto_mapped": auto, "needs_day": need}


# ── 3. pull ─────────────────────────────────────────────────────────────────

def pull_recent_leads(db: Session, hours: int | None = None) -> dict:
    hours = hours or settings.pull_lookback_hours
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    flt = json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": since}])
    forms = [r.form_id for r in db.query(FormConfig).filter(FormConfig.is_active == True)]  # noqa: E712
    new = recovered = errors = 0
    for fid in forms:
        try:
            try:
                rows = list(_paged(f"{fid}/leads", {"fields": LEAD_FIELDS_EXT, "filtering": flt, "limit": 100}))
            except graph.GraphError as e:
                if e.kind in ("token", "transient", "rate"):
                    raise
                rows = list(_paged(f"{fid}/leads", {"fields": LEAD_FIELDS_BASE, "filtering": flt, "limit": 100}))
        except graph.GraphError as e:
            errors += 1
            if e.kind == "token":
                break
            continue
        for obj in rows:
            lid = str(obj.get("id", ""))
            if not lid:
                continue
            existing = db.query(Lead).filter(Lead.meta_lead_id == lid).first()
            if existing and (existing.sync_status == "ok" or (existing.sync_status is None and existing.phone)):
                continue
            obj.setdefault("form_id", fid)
            try:
                upsert_lead_from_meta(db, lid, raw=obj, auto_send=True)
                recovered += 1 if existing else 0
                new += 0 if existing else 1
            except Exception as exc:
                db.rollback()
                errors += 1
                print(f"[sync] pull error {lid}: {exc}", flush=True)
    if new:
        log_event("sync_missed_leads", f"Recovered {new} lead(s) that never arrived by webhook", "warn")
    return {"forms": len(forms), "new": new, "recovered": recovered, "errors": errors}


# ── 4/5. retries ────────────────────────────────────────────────────────────

def write_off_old(db: Session, hours: int | None = None) -> int:
    """Leads still unfetched / unmapped after `hours` are given up on – no more API calls for them.
    One cheap UPDATE per cycle; the lead rows (and whatever data they have) are kept."""
    hours = hours or settings.retry_max_age_hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale = db.query(Lead).filter(
        or_(Lead.sync_status.in_(("pending_fetch", "needs_config")),
            (Lead.sync_status.is_(None)) & (Lead.meta_lead_id.isnot(None)) & or_(Lead.phone.is_(None), Lead.phone == "")),
        Lead.created_at < cutoff)
    n = 0
    for l in stale.limit(5000).all():
        l.sync_status = "unrecoverable"
        l.sync_error = "Too old to fetch automatically – skipped to save API usage."
        n += 1
    if n:
        db.commit()
        log_event("old_leads_written_off", f"{n} old lead(s) older than {hours}h were skipped (not fetched)", "info")
    return n


def retry_pending(db: Session, meta_ok: bool = True, wa_ok: bool = True) -> dict:
    out = {"fetched": 0, "fetch_failed": 0, "reprocessed": 0, "wa_sent": 0, "wa_failed": 0, "written_off": 0}
    out["written_off"] = write_off_old(db)

    # one-off sweep: blank leads created while the token was dead (before this version existed)
    blanks = (db.query(Lead).filter(
        Lead.meta_lead_id.isnot(None), Lead.sync_status.is_(None),
        or_(Lead.phone.is_(None), Lead.phone == ""), or_(Lead.fetch_attempts.is_(None), Lead.fetch_attempts < 3))
        .order_by(Lead.id.desc()).limit(150).all())
    for l in blanks:
        l.sync_status = "pending_fetch"
    if blanks:
        db.commit()

    if meta_ok:
        pend = (db.query(Lead).filter(Lead.sync_status == "pending_fetch",
                                      or_(Lead.fetch_attempts.is_(None), Lead.fetch_attempts < MAX_FETCH_ATTEMPTS))
                .order_by(Lead.id.desc()).limit(40).all())
        for l in pend:
            try:
                lead, res = upsert_lead_from_meta(db, l.meta_lead_id, auto_send=True, force_refetch=True)
                if lead.sync_status == "pending_fetch":
                    out["fetch_failed"] += 1
                    if res and res.get("kind") in ("token", "permission"):
                        break                      # token dead / cannot read leads – no point hammering
                else:
                    out["fetched"] += 1
            except Exception as exc:
                db.rollback()
                out["fetch_failed"] += 1
                print(f"[sync] retry fetch error {l.meta_lead_id}: {exc}", flush=True)

    # leads that were waiting for a form config
    for l in db.query(Lead).filter(Lead.sync_status == "needs_config").limit(150).all():
        try:
            lead, _ = upsert_lead_from_meta(db, l.meta_lead_id, auto_send=True) if l.meta_lead_id else (l, None)
            if lead.sync_status != "needs_config":
                out["reprocessed"] += 1
        except Exception:
            db.rollback()

    # WhatsApp retries (temporary failures only)
    if wa_ok and settings.whatsapp_enabled:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.auto_send_max_age_hours)
        cands = (db.query(Lead).filter(
            Lead.sync_status == "ok", Lead.phone.isnot(None), Lead.phone != "",
            or_(Lead.whatsapp_sent == False, Lead.whatsapp_sent.is_(None)),   # noqa: E712
            Lead.whatsapp_message_id.is_(None), or_(Lead.whatsapp_failed == False, Lead.whatsapp_failed.is_(None)),  # noqa: E712
            or_(Lead.wa_attempts.is_(None), Lead.wa_attempts < MAX_WA_ATTEMPTS),
            Lead.created_at >= cutoff).order_by(Lead.id.asc()).limit(50).all())
        for l in cands:
            if not is_fresh(l):
                continue
            try:
                r = send_template_for_lead(db, l)
                if r.get("ok"):
                    out["wa_sent"] += 1
                else:
                    out["wa_failed"] += 1
                    if r.get("kind") == "token":
                        break
            except Exception as exc:
                db.rollback()
                print(f"[sync] retry WA error {l.id}: {exc}", flush=True)
    return out


# ── orchestration ───────────────────────────────────────────────────────────

def run_cycle(force_check: bool = False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "a sync is already running"}
    t0 = time.time()
    summary: dict = {}
    try:
        with SessionLocal() as db:
            snap = health.maybe_check(db, force=force_check)
            meta_ok = (snap.get("meta") or {}).get("status") in ("ok", "warn") and not tokens.runtime_bad("meta")
            wa_ok = (snap.get("whatsapp") or {}).get("status") in ("ok", "warn") and not tokens.runtime_bad("wa")
            summary["meta_ok"], summary["wa_ok"] = meta_ok, wa_ok
            if meta_ok:
                summary["forms"] = discover_forms(db)
                summary["pull"] = pull_recent_leads(db)
            summary["retry"] = retry_pending(db, meta_ok=meta_ok, wa_ok=wa_ok)

            stuck = db.query(Lead).filter(
                Lead.sync_status == "needs_config",
                Lead.created_at <= datetime.now(timezone.utc) - timedelta(minutes=30)).count()
            if stuck:
                notify("needs_config", f"{stuck} lead(s) waiting for a form mapping",
                       "Open CRM → Form Config and set the session day for the new form.",
                       level="error", cooldown_hours=12)
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        log_event("sync_error", summary["error"], "error")
    finally:
        summary["seconds"] = round(time.time() - t0, 1)
        summary["at"] = datetime.now(timezone.utc).isoformat()
        try:
            tokens.set_setting(health.CYCLE_KEY, json.dumps(summary, default=str))
        except Exception:
            pass
        _lock.release()
    return summary


def reprocess_needs_config() -> int:
    """Called right after someone saves a Form Config: lead(s) that were waiting
    for it are processed immediately (no need to wait for the next cycle)."""
    n = 0
    with SessionLocal() as db:
        for l in db.query(Lead).filter(Lead.sync_status == "needs_config").limit(300).all():
            try:
                if l.meta_lead_id:
                    lead, _ = upsert_lead_from_meta(db, l.meta_lead_id, auto_send=True)
                    n += 1 if lead.sync_status != "needs_config" else 0
            except Exception as exc:
                db.rollback()
                print(f"[sync] reprocess error {l.id}: {exc}", flush=True)
    if n:
        log_event("form_config_applied", f"{n} waiting lead(s) processed after form config was saved", "info")
    return n
