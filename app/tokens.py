"""
Token store.

Resolution order for every token:  CRM-saved (DB, encrypted)  →  environment variable.

That means a token can be rotated from the CRM "System" page in 10 seconds,
without touching Render or redeploying. The env var stays as a fallback.
"""
import base64
import hashlib
import os
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .db import SessionLocal
from .models import AppSetting

META_KEY = "meta_page_token"
WA_KEY = "wa_token"
PAGE_ID_KEY = "meta_page_id"

_cache: dict = {}
_TTL = 30  # seconds
# Runtime flags raised by real API calls (instant, no waiting for the next check)
_bad: dict = {"meta": None, "wa": None}


def _fernet() -> Fernet:
    secret = (os.getenv("SECRET_KEY") or settings.jwt_secret or "x").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def _enc(value: str) -> str:
    return "enc:" + _fernet().encrypt(value.encode()).decode()


def _dec(value: str) -> str:
    if not value:
        return ""
    if value.startswith("enc:"):
        try:
            return _fernet().decrypt(value[4:].encode()).decode()
        except InvalidToken:
            return ""
    return value


# ── generic settings ────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, key)
            val = (row.value or "") if row else ""
    except Exception:
        val = ""
    _cache[key] = (time.time(), val)
    return val or default


def set_setting(key: str, value: Optional[str]) -> None:
    with SessionLocal() as db:
        row = db.get(AppSetting, key)
        if value is None:
            if row:
                db.delete(row)
        else:
            if not row:
                row = AppSetting(key=key)
                db.add(row)
            row.value = value
        db.commit()
    _cache.pop(key, None)


# ── tokens ──────────────────────────────────────────────────────────────────

def get_meta_token() -> str:
    t = _dec(get_setting(META_KEY)).strip()
    if t:
        return t
    for env in ("META_PAGE_ACCESS_TOKEN", "META_ACCESS_TOKEN", "PAGE_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN"):
        v = (os.getenv(env) or getattr(settings, env.lower(), "") or "").strip()
        if v:
            return v
    return (settings.meta_page_access_token or settings.meta_access_token or "").strip()


def get_wa_token() -> str:
    t = _dec(get_setting(WA_KEY)).strip()
    return t or (settings.whatsapp_access_token or "").strip()


def get_page_id() -> str:
    return (get_setting(PAGE_ID_KEY) or settings.meta_page_id or "").strip()


def save_token(kind: str, token: str) -> None:
    set_setting(META_KEY if kind == "meta" else WA_KEY, _enc(token.strip()))
    _bad[kind] = None


def clear_token(kind: str) -> None:
    set_setting(META_KEY if kind == "meta" else WA_KEY, None)
    _bad[kind] = None


def token_source(kind: str) -> str:
    key = META_KEY if kind == "meta" else WA_KEY
    if _dec(get_setting(key)).strip():
        return "crm"
    return "env" if (get_meta_token() if kind == "meta" else get_wa_token()) else "none"


def app_creds() -> tuple[str, str]:
    return (settings.meta_app_id or "").strip(), (settings.meta_app_secret or "").strip()


# ── runtime failure flags (set by graph.py on real calls) ───────────────────

def mark_bad(kind: str, message: str) -> None:
    first = _bad.get(kind) is None
    _bad[kind] = message
    if first:
        from .alerts import notify
        label = "Meta (lead ads) token" if kind == "meta" else "WhatsApp token"
        notify(f"{kind}_token_error", f"{label} stopped working",
               f"{message}\n\nOpen the CRM → System page and paste a fresh token. "
               f"New leads are being kept safely and will be processed automatically afterwards.",
               level="error", cooldown_hours=12)


def mark_ok(kind: str) -> None:
    if _bad.get(kind) is not None:
        _bad[kind] = None
        from .alerts import clear_cooldown, log_event
        clear_cooldown(f"{kind}_token_error")
        log_event(f"{kind}_token_recovered", f"{kind} token is working again", "info")


def runtime_bad(kind: str) -> Optional[str]:
    return _bad.get(kind)
