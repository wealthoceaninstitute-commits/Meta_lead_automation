"""
System health: token validity / expiry, webhook subscription, template approval,
and a single `build_health()` that turns everything into a green/amber/red verdict
with plain-language "what to do" instructions for the System page.
"""
import json
from datetime import datetime, timezone, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import graph, tokens
from .alerts import channels_configured, notify, log_event, clear_cooldown
from .config import settings
from .models import Lead, FormConfig, SystemEvent

SNAP_KEY = "health_snapshot"
CYCLE_KEY = "last_cycle"
WEBHOOK_KEY = "last_webhook_at"
REQUIRED_META_SCOPES = {"leads_retrieval", "pages_show_list", "pages_read_engagement"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts) -> str:
    try:
        ts = int(ts)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts > 0 else ""
    except Exception:
        return ""


def _days_left(ts) -> float | None:
    try:
        ts = int(ts)
        return None if ts <= 0 else (ts - _now().timestamp()) / 86400
    except Exception:
        return None


# ── individual checks ───────────────────────────────────────────────────────

def _debug_token(tok: str) -> dict | None:
    app_id, secret = tokens.app_creds()
    if not (app_id and secret):
        return None
    try:
        return (graph.get("debug_token", params={"input_token": tok}, token=f"{app_id}|{secret}",
                          retries=1) or {}).get("data") or {}
    except graph.GraphError:
        return None


def check_meta() -> dict:
    tok = tokens.get_meta_token()
    out = {"configured": bool(tok), "source": tokens.token_source("meta"), "status": "unconfigured",
           "message": "No Meta (Facebook) access token is set.", "type": "", "expires_at": "",
           "never_expires": False, "days_left": None, "data_access_days_left": None,
           "scopes": [], "missing_scopes": [], "page_id": tokens.get_page_id(), "page_name": "",
           "webhook_subscribed": None, "can_auto_extend": all(tokens.app_creds())}
    if not tok:
        return out

    dbg = _debug_token(tok)
    if dbg is not None:
        out["type"] = dbg.get("type", "")
        out["scopes"] = dbg.get("scopes") or []
        exp = dbg.get("expires_at", 0)
        out["never_expires"] = (not exp)
        out["expires_at"] = _iso(exp)
        out["days_left"] = _days_left(exp)
        out["data_access_days_left"] = _days_left(dbg.get("data_access_expires_at", 0))
        if not dbg.get("is_valid", False):
            err = (dbg.get("error") or {}).get("message", "token is not valid")
            out.update(status="error", message=f"Token is invalid or expired: {err}")
            tokens.mark_bad("meta", out["message"])
            return out

    # live probe (also learns the page id when it's a Page token)
    try:
        me = graph.get("me", params={"fields": "id,name,category"}, retries=1)
    except graph.GraphError as e:
        out.update(status="error", message=f"Meta rejected the token: {e}")
        if e.kind == "token":
            tokens.mark_bad("meta", out["message"])
        return out
    tokens.mark_ok("meta")
    if "category" in me:                       # a PAGE token → /me is the page
        out["page_id"] = out["page_id"] or me.get("id", "")
        out["page_name"] = me.get("name", "")
        if out["page_id"] and not tokens.get_page_id():
            tokens.set_setting(tokens.PAGE_ID_KEY, out["page_id"])
        out["type"] = out["type"] or "PAGE"

    # webhook subscription
    if out["page_id"]:
        try:
            subs = graph.get(f"{out['page_id']}/subscribed_apps", retries=1).get("data") or []
            app_id = tokens.app_creds()[0]
            mine = [s for s in subs if not app_id or str(s.get("id")) == app_id]
            out["webhook_subscribed"] = any(
                ("leadgen" in (s.get("subscribed_fields") or [])) or not s.get("subscribed_fields")
                for s in mine) if mine else False
        except graph.GraphError:
            out["webhook_subscribed"] = None

    out["missing_scopes"] = sorted(REQUIRED_META_SCOPES - set(out["scopes"])) if out["scopes"] else []
    out["status"], out["message"] = "ok", "Token is valid."
    if out["days_left"] is not None and out["days_left"] < settings.warn_token_days:
        out.update(status="warn", message=f"Token expires in {max(out['days_left'], 0):.0f} day(s). "
                                          f"Replace it before then.")
    elif out["data_access_days_left"] is not None and out["data_access_days_left"] < settings.warn_token_days:
        out.update(status="warn", message=f"Data-access permission lapses in {out['data_access_days_left']:.0f} day(s).")
    elif out["webhook_subscribed"] is False:
        out.update(status="warn", message="The Page is not subscribed to lead notifications. "
                                          "Leads will still be picked up by the 10-minute sync.")
    elif out["missing_scopes"]:
        out.update(status="warn", message="Token is missing permissions: " + ", ".join(out["missing_scopes"]))
    return out


def check_whatsapp() -> dict:
    tok = tokens.get_wa_token()
    pid = settings.whatsapp_phone_number_id
    out = {"configured": bool(tok and pid), "source": tokens.token_source("wa"),
           "enabled": settings.whatsapp_enabled, "status": "unconfigured",
           "message": "WhatsApp token or phone-number id is not set.", "type": "",
           "expires_at": "", "never_expires": False, "days_left": None,
           "number": "", "verified_name": "", "quality": "", "waba_id": settings.whatsapp_business_account_id}
    if not (tok and pid):
        return out
    dbg = _debug_token(tok)
    if dbg is not None:
        out["type"] = dbg.get("type", "")
        exp = dbg.get("expires_at", 0)
        out["never_expires"] = (not exp)
        out["expires_at"] = _iso(exp)
        out["days_left"] = _days_left(exp)
        if not dbg.get("is_valid", False):
            err = (dbg.get("error") or {}).get("message", "token is not valid")
            out.update(status="error", message=f"Token is invalid or expired: {err}")
            tokens.mark_bad("wa", out["message"])
            return out
    try:
        info = graph.get(pid, kind="wa", retries=1,
                         params={"fields": "display_phone_number,verified_name,quality_rating,whatsapp_business_account"})
    except graph.GraphError as e:
        out.update(status="error", message=f"WhatsApp API error: {e}")
        if e.kind == "token":
            tokens.mark_bad("wa", out["message"])
        return out
    tokens.mark_ok("wa")
    out["number"] = info.get("display_phone_number", "")
    out["verified_name"] = info.get("verified_name", "")
    out["quality"] = info.get("quality_rating", "")
    out["waba_id"] = out["waba_id"] or (info.get("whatsapp_business_account") or {}).get("id", "")
    out["status"], out["message"] = "ok", "Token is valid."
    if out["days_left"] is not None and out["days_left"] < settings.warn_token_days:
        out.update(status="warn", message=f"Token expires in {max(out['days_left'], 0):.0f} day(s).")
    elif not settings.whatsapp_enabled:
        out.update(status="warn", message="Token works, but WHATSAPP_ENABLED is off – nothing is being sent.")
    elif (out["quality"] or "").upper() == "RED":
        out.update(status="warn", message="Number quality rating is RED – WhatsApp may limit sending.")
    return out


def check_templates(db: Session, waba_id: str) -> list[dict]:
    """Are the templates used by active forms still APPROVED? (a paused/rejected
    template makes every send fail silently)."""
    res = []
    if not (waba_id and settings.whatsapp_enabled):
        return res
    names = sorted({r.wa_template for r in db.query(FormConfig).filter(FormConfig.is_active == True)  # noqa: E712
                    if r.wa_template})
    for n in names:
        try:
            d = graph.get(f"{waba_id}/message_templates", kind="wa", retries=1,
                          params={"name": n, "fields": "name,status,language"}).get("data") or []
            res.append({"name": n, "status": (d[0].get("status") if d else "NOT_FOUND")})
        except graph.GraphError:
            return res
    return res


def run_checks(db: Session) -> dict:
    meta = check_meta()
    wa = check_whatsapp()
    snap = {"checked_at": _now().isoformat(), "meta": meta, "whatsapp": wa,
            "templates": check_templates(db, wa.get("waba_id", "")) if wa["status"] in ("ok", "warn") else []}
    tokens.set_setting(SNAP_KEY, json.dumps(snap))

    # alerts for *upcoming* problems (hard failures alert from tokens.mark_bad)
    for kind, label, d in (("meta", "Meta lead-ads token", meta), ("wa", "WhatsApp token", wa)):
        if d["status"] == "warn" and d.get("days_left") is not None:
            notify(f"{kind}_token_expiring", f"{label} expires soon", d["message"] +
                   " Open the CRM → System page to replace it.", level="warn", cooldown_hours=24)
    return snap


def load_snapshot() -> dict:
    try:
        return json.loads(tokens.get_setting(SNAP_KEY) or "{}")
    except ValueError:
        return {}


def maybe_check(db: Session, force: bool = False) -> dict:
    snap = load_snapshot()
    try:
        age = _now() - datetime.fromisoformat(snap.get("checked_at", "1970-01-01T00:00:00+00:00"))
    except ValueError:
        age = timedelta(days=999)
    if force or age > timedelta(minutes=settings.token_check_minutes):
        return run_checks(db)
    return snap


# ── the verdict ─────────────────────────────────────────────────────────────

def build_health(db: Session) -> dict:
    snap = load_snapshot()
    meta = dict(snap.get("meta") or {})
    wa = dict(snap.get("whatsapp") or {})
    issues: list[dict] = []

    def issue(sev, title, detail, action=""):
        issues.append({"severity": sev, "title": title, "detail": detail, "action": action})

    # runtime flags beat the (possibly 30-min-old) snapshot
    mb, wb = tokens.runtime_bad("meta"), tokens.runtime_bad("wa")
    if mb:
        meta.update(status="error", message=mb)
    if wb:
        wa.update(status="error", message=wb)

    if not snap and not (mb or wb):
        issue("warn", "Health not checked yet", "Click “Check now”.")
    if meta.get("status") in ("error", "unconfigured"):
        issue("error", "Facebook / Instagram lead token is not working", meta.get("message", ""),
              "Paste a new token in the “Facebook / Instagram lead-ads token” box below. Leads that arrive meanwhile are kept and processed afterwards.")
    elif meta.get("status") == "warn":
        issue("warn", "Meta token needs attention", meta.get("message", ""), "")
    if meta.get("status") in ("ok", "warn") and not meta.get("page_id"):
        issue("warn", "Facebook Page ID is not known",
              "The token works but the CRM cannot tell which Page to sync lead forms from, so missed-lead recovery is off.",
              "Enter the Page ID next to the token box (or set META_PAGE_ID in Render).")
    if wa.get("status") in ("error", "unconfigured"):
        issue("error", "WhatsApp token is not working", wa.get("message", ""),
              "Paste a new System-User token in the “WhatsApp token” box below. Un-sent invites go out automatically after.")
    elif wa.get("status") == "warn":
        issue("warn", "WhatsApp needs attention", wa.get("message", ""), "")
    for t in snap.get("templates") or []:
        if t["status"] != "APPROVED":
            issue("error", f"WhatsApp template “{t['name']}” is {t['status']}",
                  "Invites using this template will fail until it is approved again.",
                  "Fix it under Templates, or point the form to another approved template in Form Config.")

    # lead pipeline counts
    cnt = lambda *c: db.query(func.count(Lead.id)).filter(*c).scalar() or 0  # noqa: E731
    pending = cnt(Lead.sync_status == "pending_fetch")
    needs_cfg = cnt(Lead.sync_status == "needs_config")
    unrecov = cnt(Lead.sync_status == "unrecoverable")
    two_days = (_now() - timedelta(days=2)).isoformat()
    wa_failed = cnt(Lead.whatsapp_failed == True, Lead.whatsapp_failed_at >= two_days)  # noqa: E712
    unsent_recent = cnt(Lead.sync_status == "ok", Lead.phone.isnot(None), Lead.phone != "",
                        or_(Lead.whatsapp_sent == False, Lead.whatsapp_sent.is_(None)),   # noqa: E712
                        Lead.whatsapp_message_id.is_(None), Lead.whatsapp_failed != True,  # noqa: E712
                        Lead.created_at >= _now() - timedelta(hours=settings.auto_send_max_age_hours))
    review = db.query(func.count(FormConfig.id)).filter(FormConfig.needs_review == True).scalar() or 0  # noqa: E712

    if pending:
        issue("warn", f"{pending} lead(s) waiting to be fetched from Meta",
              "Their details could not be downloaded (usually an expired token). Nothing is lost.",
              "Fix the token – they are retried every few minutes. Or press “Repair now”.")
    if needs_cfg:
        issue("error", f"{needs_cfg} lead(s) from a form with no session day",
              "A new ad form arrived that the system could not map to Friday/Sunday. No WhatsApp was sent to them.",
              "Open Form Config → fill the day for the highlighted form. They are processed automatically.")
    if review:
        issue("info", f"{review} form(s) to review",
              "New forms were detected and mapped automatically (or need a day).",
              "Open Form Config and confirm the day, time and template.")
    if wa_failed:
        issue("warn", f"{wa_failed} WhatsApp message(s) failed in the last 2 days",
              "See the red note under each lead (e.g. number not on WhatsApp).")
    if unsent_recent and wa.get("status") in ("ok", "warn"):
        issue("info", f"{unsent_recent} recent lead(s) still without an invite", "They are retried automatically.")

    # config hygiene
    if settings.admin_password in ("admin123", "") or settings.jwt_secret == "dev-secret-change-me":
        issue("warn", "Default admin password / JWT secret in use",
              "Anyone who knows the default can log in.", "Set ADMIN_PASSWORD and JWT_SECRET in Render.")
    if not channels_configured():
        issue("info", "No alert channel configured",
              "You will only see problems here. Add Telegram or e-mail to be told the moment a token dies.",
              "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (or SMTP_* + ALERT_EMAIL_TO) in Render.")
    if not all(tokens.app_creds()):
        issue("info", "META_APP_ID / META_APP_SECRET not set",
              "Without them the CRM cannot convert a pasted token into a non-expiring one or read its expiry date.",
              "Add both in Render (Meta Developer → App settings → Basic).")

    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    level = "error" if any(i["severity"] == "error" for i in issues) else \
            "warn" if any(i["severity"] == "warn" for i in issues) else "ok"

    last_lead = db.query(func.max(Lead.created_at)).filter(Lead.meta_lead_id.isnot(None)).scalar()
    utc = lambda d: (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat() if d else ""  # noqa: E731
    events = [{"id": e.id, "kind": e.kind, "level": e.level, "message": e.message,
               "at": utc(e.created_at)}
              for e in db.query(SystemEvent).order_by(SystemEvent.id.desc()).limit(25)]
    try:
        cycle = json.loads(tokens.get_setting(CYCLE_KEY) or "{}")
    except ValueError:
        cycle = {}

    return {
        "level": level, "issues": issues, "meta": meta, "whatsapp": wa,
        "templates": snap.get("templates") or [], "checked_at": snap.get("checked_at"),
        "counts": {"pending_fetch": pending, "needs_config": needs_cfg, "unrecoverable": unrecov,
                   "wa_failed_2d": wa_failed, "unsent_recent": unsent_recent, "forms_to_review": review},
        "last_lead_at": utc(last_lead) or None,
        "last_webhook_at": tokens.get_setting(WEBHOOK_KEY) or None,
        "last_cycle": cycle,
        "scheduler": {"enabled": settings.scheduler_enabled, "interval_minutes": settings.sync_interval_minutes},
        "alert_channels": channels_configured(),
        "can_auto_extend_token": all(tokens.app_creds()),
        "events": events,
    }
