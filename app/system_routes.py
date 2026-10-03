"""System page API: health, repair, token management, alert test."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import graph, health, tokens
from .alerts import log_event, notify, clear_cooldown
from .auth import require_user
from .config import settings
from .db import get_db
from .models import Lead

router = APIRouter(prefix="/system", tags=["System"])


@router.get("/health")
def get_health(db: Session = Depends(get_db), user: str = Depends(require_user)):
    return health.build_health(db)


@router.post("/check")
def check_now(db: Session = Depends(get_db), user: str = Depends(require_user)):
    health.run_checks(db)
    return health.build_health(db)


@router.post("/repair")
def repair_now(user: str = Depends(require_user)):
    """Re-check tokens, pull missed leads, retry everything that is stuck."""
    from .sync import run_cycle
    summary = run_cycle(force_check=True)
    log_event("manual_repair", f"Repair run by {user}: {summary}", "info")
    return summary


class TokenIn(BaseModel):
    token: str
    page_id: str = ""        # only needed when the pasted token owns several Pages


def _clean_token(t: str) -> str:
    return (t or "").strip().strip('"').strip("'").replace("\n", "").replace("\r", "").replace(" ", "")


def exchange_for_page_token(user_token: str, page_id: str = "") -> dict:
    """USER / SYSTEM-USER token → PAGE token for the right Page.
    A Page token derived this way never expires. Never guesses a Page: if the
    wanted Page is not reachable the error lists the Pages the token CAN see."""
    app_id, secret = tokens.app_creds()
    ll = user_token
    try:        # user tokens: make it long-lived first (system-user tokens simply don't need/allow this)
        ll = graph.get("oauth/access_token", token=user_token, retries=1, params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
            "fb_exchange_token": user_token}).get("access_token") or user_token
    except graph.GraphError:
        ll = user_token
    want = page_id or settings.meta_page_id or tokens.get_page_id()

    if want:    # direct lookup works for users with Page access and for system users with the Page assigned
        try:
            d = graph.get(str(want), token=ll, params={"fields": "id,name,access_token"}, retries=1)
            if d.get("access_token"):
                return {"token": d["access_token"], "page_id": str(d["id"]), "page_name": d.get("name", "")}
        except graph.GraphError:
            pass

    pages = (graph.get("me/accounts", token=ll, params={"fields": "id,name,access_token", "limit": 100})
             .get("data") or [])
    if not pages:
        raise HTTPException(400, "This token can access no Pages. Log in with an account that has access to the "
                                 "Page (or assign the Page to the system user) and allow the Page when asked.")
    visible = [f'{p.get("name", "?")} ({p["id"]})' for p in pages]
    if want:
        matched = [p for p in pages if str(p["id"]) == str(want)]
        if not matched:
            raise HTTPException(400, f"Page {want} is not one of the Pages this token can access. "
                                     f"Pages it CAN access: {', '.join(visible)}. Generate the token again with "
                                     f"that Page allowed (or assigned to the system user).")
        pages = matched
    if len(pages) > 1:
        raise HTTPException(409, {"message": "Several Pages found – pick one.",
                                  "pages": [{"id": p["id"], "name": p.get("name", "")} for p in pages]})
    p = pages[0]
    return {"token": p["access_token"], "page_id": str(p["id"]), "page_name": p.get("name", "")}


@router.post("/tokens/meta")
def set_meta_token(body: TokenIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    tok = _clean_token(body.token)
    if len(tok) < 20:
        raise HTTPException(400, "That does not look like an access token.")
    note = ""
    page_id = body.page_id
    app_id, secret = tokens.app_creds()
    if app_id and secret:
        dbg = health._debug_token(tok) or {}
        if dbg and not dbg.get("is_valid", False):
            raise HTTPException(400, "Meta says this token is invalid/expired: "
                                     + (dbg.get("error") or {}).get("message", "generate a new one"))
        if dbg.get("type") in ("USER", "SYSTEM_USER"):
            try:
                ex = exchange_for_page_token(tok, body.page_id)
            except graph.GraphError as e:
                raise HTTPException(400, f"Could not convert to a long-lived Page token: {e}")
            tok, page_id = ex["token"], ex["page_id"]
            note = f"Converted to a non-expiring Page token for “{ex['page_name']}”."
    else:
        note = ("Saved as-is. Add META_APP_ID and META_APP_SECRET in Render so the CRM can make tokens "
                "non-expiring and show the expiry date.")

    # validate before saving – never replace a token with a broken one
    try:
        me = health.probe_me(tok)
    except graph.GraphError as e:
        raise HTTPException(400, f"Meta rejected this token: {e}")
    if "category" in me and not page_id:
        page_id = me["id"]
    tokens.save_token("meta", tok)
    if page_id:
        tokens.set_setting(tokens.PAGE_ID_KEY, str(page_id))

    # make sure Meta will actually send us lead notifications
    sub = ""
    if page_id:
        try:
            graph.post(f"{page_id}/subscribed_apps", token=tok, params={"subscribed_fields": "leadgen"}, retries=1)
            sub = " Lead notifications are subscribed."
        except graph.GraphError as e:
            sub = f" (Could not auto-subscribe the Page to lead notifications: {e})"
    log_event("meta_token_saved", f"Meta token replaced by {user}. {note}{sub}", "info")
    snap = health.run_checks(db)
    clear_cooldown("meta_token_error")
    # recover anything that arrived while the token was dead
    from .sync import run_cycle
    run_cycle()
    return {"ok": True, "message": (note + sub).strip() or "Token saved.", "meta": snap["meta"]}


@router.post("/tokens/whatsapp")
def set_wa_token(body: TokenIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    tok = _clean_token(body.token)
    if len(tok) < 20:
        raise HTTPException(400, "That does not look like an access token.")
    if not settings.whatsapp_phone_number_id:
        raise HTTPException(400, "WHATSAPP_PHONE_NUMBER_ID is not set in Render.")
    try:
        info = graph.get(settings.whatsapp_phone_number_id, token=tok, retries=1,
                         params={"fields": "display_phone_number,verified_name"})
    except graph.GraphError as e:
        raise HTTPException(400, f"WhatsApp rejected this token: {e}")
    tokens.save_token("wa", tok)
    log_event("wa_token_saved", f"WhatsApp token replaced by {user} ({info.get('display_phone_number')})", "info")
    snap = health.run_checks(db)
    clear_cooldown("wa_token_error")
    from .sync import run_cycle
    run_cycle()
    return {"ok": True, "message": f"Saved. Connected to {info.get('verified_name', '')} {info.get('display_phone_number', '')}.",
            "whatsapp": snap["whatsapp"]}


@router.delete("/tokens/{kind}")
def clear_saved_token(kind: str, user: str = Depends(require_user)):
    """Forget the token saved in the CRM and fall back to the Render env var."""
    if kind not in ("meta", "whatsapp"):
        raise HTTPException(404, "unknown token kind")
    tokens.clear_token("meta" if kind == "meta" else "wa")
    log_event("token_cleared", f"{kind} token saved in CRM removed by {user}", "warn")
    return {"ok": True}


@router.post("/skip-old-leads")
def skip_old_leads(hours: int = 0, db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Stop retrying every lead that is still unfetched (hours=0 → all of them, i.e. 'new leads only')."""
    from .models import Lead
    from .sync import write_off_old
    if hours <= 0:
        n = 0
        for l in db.query(Lead).filter(Lead.sync_status.in_(("pending_fetch", "needs_config"))).all():
            l.sync_status = "unrecoverable"
            l.sync_error = "Skipped by admin – new leads only."
            n += 1
        db.commit()
        log_event("old_leads_skipped", f"{n} unfetched lead(s) skipped by {user}", "info")
    else:
        n = write_off_old(db, hours)
    return {"ok": True, "skipped": n}


@router.post("/test-alert")
def test_alert(user: str = Depends(require_user)):
    from .alerts import channels_configured
    ch = channels_configured()
    if not ch:
        raise HTTPException(400, "No alert channel configured (set TELEGRAM_* or SMTP_* / ALERT_EMAIL_TO in Render).")
    sent = notify(f"test_{__import__('time').time():.0f}", "Test alert",
                  "If you can read this, alerts are working.", level="info", cooldown_hours=0)
    return {"ok": bool(sent), "channels": ch}
