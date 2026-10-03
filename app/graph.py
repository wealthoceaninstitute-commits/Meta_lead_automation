"""
One place that talks to graph.facebook.com.

* reads the CURRENT token on every call (so a token pasted in the CRM works at once)
* retries transient failures (5xx, timeouts, rate-limits) with back-off
* classifies errors → GraphError.kind in {token, permission, rate, transient, gone, other}
* flags the token as bad/ok for the System page + alerts
"""
import time
from typing import Any, Optional

import requests

from .config import settings
from . import tokens

BASE = "https://graph.facebook.com"

TOKEN_CODES = {190, 102, 463, 467, 460, 459, 458, 452}
PERMISSION_CODES = {10, 200, 299, 3, 100_000}
RATE_CODES = {4, 17, 32, 613, 80007, 130429, 131048, 131056}


def version() -> str:
    return settings.meta_graph_version or "v25.0"


class GraphError(Exception):
    def __init__(self, message: str, status: int = 0, code: Optional[int] = None,
                 subcode: Optional[int] = None, kind: str = "other", body: Any = None):
        super().__init__(message)
        self.message, self.status, self.code, self.subcode = message, status, code, subcode
        self.kind, self.body = kind, body

    @property
    def retryable(self) -> bool:
        return self.kind in ("token", "rate", "transient")

    def __str__(self):
        c = f"#{self.code}" + (f"/{self.subcode}" if self.subcode else "") if self.code else f"HTTP {self.status}"
        return f"({c}) {self.message}"


def classify(status: int, err: dict) -> str:
    code, sub = err.get("code"), err.get("error_subcode")
    msg = (err.get("message") or "").lower()
    if code in TOKEN_CODES or err.get("type") == "OAuthException" and ("access token" in msg or "session" in msg):
        return "token"
    if code in RATE_CODES:
        return "rate"
    if status >= 500:
        return "transient"
    # Meta uses the SAME error (100/33 "does not exist … or missing permissions") for a deleted/expired
    # lead and for a token without permission. Only call it "gone" when permissions are not mentioned.
    if code in PERMISSION_CODES or 200 <= (code or 0) <= 299 or "permission" in msg:
        return "permission"
    if (code == 100 and sub in (33, 2018001)) or ("does not exist" in msg and code in (100, 803)):
        return "gone"
    return "other"


def call(method: str, path: str, *, kind: str = "meta", params: dict | None = None,
         json: dict | None = None, token: str | None = None, retries: int = 2,
         timeout: int = 20) -> dict:
    """kind: 'meta' | 'wa' → decides which stored token is used & flagged."""
    tok = token if token is not None else (tokens.get_meta_token() if kind == "meta" else tokens.get_wa_token())
    if not tok:
        raise GraphError(f"No {'Meta' if kind == 'meta' else 'WhatsApp'} access token configured",
                         kind="token")
    url = path if path.startswith("http") else f"{BASE}/{version()}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {tok}"}
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.request(method, url, params=params, json=json, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            if attempt <= retries:
                time.sleep(attempt * 1.5)
                continue
            raise GraphError(f"Network error: {exc}", kind="transient")
        try:
            body = r.json()
        except ValueError:
            body = {"_text": r.text[:500]}
        if r.status_code < 400 and not (isinstance(body, dict) and "error" in body):
            if token is None:
                tokens.mark_ok(kind)
            return body
        err = (body or {}).get("error") or {}
        gk = classify(r.status_code, err)
        ge = GraphError(err.get("message") or r.text[:300], r.status_code, err.get("code"),
                        err.get("error_subcode"), gk, body)
        if gk in ("rate", "transient") and attempt <= retries:
            time.sleep(attempt * 2)
            continue
        if gk == "token" and token is None:
            tokens.mark_bad(kind, f"Graph API rejected the token: {ge}")
        raise ge


def get(path, **kw):
    return call("GET", path, **kw)


def post(path, **kw):
    return call("POST", path, **kw)
