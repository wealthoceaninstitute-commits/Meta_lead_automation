import os, sys, tempfile, json, time
from datetime import datetime, timezone, timedelta

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "SCHEDULER_ENABLED": "false", "KEEP_ALIVE_ENABLED": "false",
    "WHATSAPP_ENABLED": "true", "WHATSAPP_PHONE_NUMBER_ID": "PHONE1",
    "META_APP_ID": "APP1", "META_APP_SECRET": "SECRET1",
    "ADMIN_PASSWORD": "pw", "JWT_SECRET": "test-secret-xyz",
    "META_PAGE_ACCESS_TOKEN": "", "WHATSAPP_ACCESS_TOKEN": "",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body
        self.text = json.dumps(body)
    def json(self):
        return self._b


class FakeMeta:
    """In-memory stand-in for graph.facebook.com."""
    def __init__(self):
        self.valid_meta = {"META_OK"}
        self.valid_wa = {"WA_OK"}
        self.forms = {"2216617835805846": "LIVE TRADING CLASS NEW", "F_NEW_FRI": "Seminar - Friday batch 2",
                      "F_NEW_X": "Weekend Special Offer"}
        self.leads = {}            # id -> obj
        self.form_leads = {}       # form id -> [ids]
        self.ads = {"AD1": {"id": "AD1", "name": "Ad One", "adset": {"id": "AS1", "name": "Adset"},
                            "campaign": {"id": "C1", "name": "Camp"}}}
        self.sent = []             # WhatsApp payloads accepted
        self.wa_error = None       # (status, error dict) to force
        self.calls = []

    def add_lead(self, lid, form, name="Ravi", phone="9876543210", age_hours=0, extra=None, ad="AD1"):
        ct = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        fd = [{"name": "full_name", "values": [name]}, {"name": "phone_number", "values": [phone]}]
        fd += extra or []
        self.leads[lid] = {"id": lid, "created_time": ct, "form_id": form, "ad_id": ad,
                           "platform": "fb", "is_organic": False, "field_data": fd,
                           "ad_name": "Ad One", "adset_name": "Adset", "campaign_name": "Camp"}
        self.form_leads.setdefault(form, []).append(lid)

    def _err(self, status, code, msg, sub=None):
        e = {"message": msg, "type": "OAuthException", "code": code}
        if sub: e["error_subcode"] = sub
        return FakeResp(status, {"error": e})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.split("/", 4)[4] if url.startswith("https://graph") else url
        path = path.split("?")[0]
        tok = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        params = params or {}
        self.calls.append((method, path))

        if path == "debug_token":
            t = params.get("input_token")
            is_user = t.startswith("USER_TOK") or t == "LL_USER"
            ok = t in self.valid_meta or t in self.valid_wa or is_user
            return FakeResp(200, {"data": {"is_valid": ok, "expires_at": int(time.time()) + 3600 if is_user else 0,
                                           "type": "USER" if is_user else "PAGE",
                                           "scopes": ["leads_retrieval", "pages_show_list", "pages_read_engagement"],
                                           "data_access_expires_at": int(time.time()) + 86400 * 80,
                                           **({} if ok else {"error": {"message": "Session has expired"}})}})
        if path == "oauth/access_token":
            return FakeResp(200, {"access_token": "LL_USER"})
        if path == "me/accounts":
            if tok not in {"LL_USER"} | self.valid_meta:
                return self._err(400, 190, "Invalid OAuth access token", 467)
            return FakeResp(200, {"data": [{"id": "PAGE1", "name": "WOI Page", "access_token": "PAGE_PERM"}]})
        # WhatsApp side
        if path == "PHONE1/messages":
            if tok not in self.valid_wa:
                return self._err(401, 190, "Error validating access token: Session has expired", 463)
            if self.wa_error:
                st, e = self.wa_error
                return FakeResp(st, {"error": e})
            self.sent.append(json)
            return FakeResp(200, {"messages": [{"id": f"wamid.{len(self.sent)}"}]})
        if path == "PHONE1":
            if tok not in self.valid_wa:
                return self._err(401, 190, "Error validating access token", 463)
            return FakeResp(200, {"display_phone_number": "+91 97413 08822", "verified_name": "WOI",
                                  "quality_rating": "GREEN", "whatsapp_business_account": {"id": "WABA1"}})
        if path == "WABA1/message_templates":
            return FakeResp(200, {"data": [{"name": params.get("name"), "status": "APPROVED"}]})
        # Meta side
        if tok not in self.valid_meta | {"PAGE_PERM"}:
            return self._err(400, 190, "Error validating access token: Session has expired", 463)
        if path == "me":
            return FakeResp(200, {"id": "PAGE1", "name": "WOI Page", "category": "Education"})
        if path == "PAGE1/subscribed_apps":
            return FakeResp(200, {"data": [{"id": "APP1", "subscribed_fields": ["leadgen"]}], "success": True})
        if path == "PAGE1/leadgen_forms":
            return FakeResp(200, {"data": [{"id": k, "name": v, "status": "ACTIVE"} for k, v in self.forms.items()]})
        if path.endswith("/leads"):
            fid = path.split("/")[0]
            return FakeResp(200, {"data": [self.leads[i] for i in self.form_leads.get(fid, [])]})
        if path in self.leads:
            return FakeResp(200, self.leads[path])
        if path in self.forms:
            return FakeResp(200, {"id": path, "name": self.forms[path], "status": "ACTIVE"})
        if path in self.ads:
            return FakeResp(200, self.ads[path])
        return self._err(400, 100, "Unsupported get request. Object does not exist", 33)


@pytest.fixture()
def fake(monkeypatch):
    f = FakeMeta()
    import app.graph as g
    monkeypatch.setattr(g.requests, "request", f.request)
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    return f


@pytest.fixture()
def env(monkeypatch, fake):
    """Fresh DB + token store seeded with working tokens."""
    from app import db, tokens, meta, alerts
    from app.db import Base, engine, init_db
    from app.form_config import seed_default_configs
    Base.metadata.drop_all(bind=engine)
    init_db()
    tokens._cache.clear(); tokens._bad.update({"meta": None, "wa": None}); meta._NAME_CACHE.clear()
    with db.SessionLocal() as s:
        seed_default_configs(s)
        from app.models import FormConfig   # templates are per-form config now: set test ones
        for r in s.query(FormConfig):
            r.wa_template = "tmpl_" + r.day.lower()
            r.wa_params = ("name,campaign,session_date,session_time,arrival_time,venue" if r.day == "Friday"
                           else "name,session_date,session_time,arrival_time,venue")
        s.commit()
    tokens.save_token("meta", "META_OK")
    tokens.save_token("wa", "WA_OK")
    sent_alerts = []
    monkeypatch.setattr(alerts, "_cooldown_ok", lambda k, h: True)
    monkeypatch.setattr("app.alerts.requests.post", lambda *a, **k: sent_alerts.append((a, k)) or type("R", (), {"ok": True})())
    monkeypatch.setattr(alerts.settings, "telegram_bot_token", "TG")
    monkeypatch.setattr(alerts.settings, "telegram_chat_id", "CHAT")
    fake.alerts = sent_alerts
    return fake


@pytest.fixture()
def client(env):
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "admin", "password": "pw"})
        c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
        yield c


@pytest.fixture()
def dbs(env):
    from app.db import SessionLocal
    with SessionLocal() as s:
        yield s
