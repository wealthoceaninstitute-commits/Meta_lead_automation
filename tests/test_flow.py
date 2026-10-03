from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import text

from app.form_config import infer_day, next_session_date, parse_start_time
from app.models import Lead, FormConfig
from app import tokens, sync
from app.meta import upsert_lead_from_meta, classify_webhook_and_handle

IST = ZoneInfo("Asia/Kolkata")


def webhook(lid, form):
    return {"entry": [{"changes": [{"field": "leadgen", "value": {"leadgen_id": lid, "form_id": form, "created_time": 1}}]}]}


# ── pure logic ──────────────────────────────────────────────────────────────
def test_infer_day():
    assert infer_day("Free Seminar - Friday batch") == "Friday"
    assert infer_day("SUN morning") == "Sunday"
    assert infer_day("Sunrise Fridays and Sundays") is None      # two different days → ambiguous
    assert infer_day("Sunrise capital") is None                  # 'sun' inside a word is not a day
    assert infer_day("Please choose: Sunday 10:30 AM") == "Sunday"


def test_session_date_rules():
    fri_10am = datetime(2026, 10, 2, 10, 0, tzinfo=IST)          # a Friday
    assert next_session_date("Friday", "6:00 PM to 8:00 PM", fri_10am) == "Friday, 02 October 2026"   # still today
    fri_530pm = datetime(2026, 10, 2, 17, 30, tzinfo=IST)
    assert next_session_date("Friday", "6:00 PM to 8:00 PM", fri_530pm) == "Friday, 09 October 2026"  # too late
    assert next_session_date("Sunday", "10:30 AM to 12:30 PM", fri_10am) == "Sunday, 04 October 2026"
    assert parse_start_time("10:30 AM to 12:30 PM").hour == 10
    assert parse_start_time("12:00 AM").hour == 0


# ── 1. two ads (Friday + Sunday) ────────────────────────────────────────────
def test_two_ads_map_to_their_own_day(env, dbs):
    env.add_lead("L1", "2216617835805846", name="Asha")
    env.add_lead("L2", "1063861336132383", name="Bhat")
    for lid in ("L1", "L2"):
        classify_webhook_and_handle(dbs, webhook(lid, env.leads[lid]["form_id"]))
    a = dbs.query(Lead).filter_by(meta_lead_id="L1").one()
    b = dbs.query(Lead).filter_by(meta_lead_id="L2").one()
    assert a.session_day == "Friday" and a.sync_status == "ok"
    assert b.session_day == "Sunday" and b.sync_status == "ok" and b.whatsapp_sent
    assert a.whatsapp_sent
    # friday template has the campaign param (6), sunday 5
    counts = sorted(len(p["template"]["components"][0]["parameters"]) for p in env.sent)
    assert counts == [5, 6]
    assert a.platform == "FB" and a.ad_name == "Ad One"


def test_new_ad_form_named_with_day_is_auto_mapped(env, dbs):
    env.add_lead("L3", "F_NEW_FRI")
    lead, wa = upsert_lead_from_meta(dbs, "L3")
    assert lead.session_day == "Friday" and lead.sync_status == "ok"
    assert wa["reason"] == "no_template" and not env.sent          # no template configured → no invite
    row = dbs.query(FormConfig).filter_by(form_id="F_NEW_FRI").one()
    assert row.auto_created and row.needs_review and row.is_active


def test_answer_picks_the_day_for_shared_form(env, dbs):
    env.forms["SHARED"] = "Stock market seminar"
    env.add_lead("L4", "SHARED", extra=[{"name": "which_day_works_for_you", "values": ["Sunday"]}])
    lead, wa = upsert_lead_from_meta(dbs, "L4")
    assert lead.session_day == "Sunday" and lead.sync_status == "ok" and wa["reason"] == "no_template"


def test_unmapped_form_is_kept_not_messaged_then_recovers(env, dbs, client):
    env.add_lead("L5", "F_NEW_X")
    lead, wa = upsert_lead_from_meta(dbs, "L5")
    assert lead.sync_status == "needs_config" and not env.sent and not lead.whatsapp_sent
    assert "Form Config" in lead.sync_error
    # human maps it in the CRM
    row = dbs.query(FormConfig).filter_by(form_id="F_NEW_X").one()
    assert row.is_active is False and row.needs_review
    r = client.patch(f"/form-configs/{row.id}", json={
        "form_id": "F_NEW_X", "campaign_name": "Weekend", "day": "Sunday", "session_time": "10:30 AM to 12:30 PM",
        "arrival_time": "10:15 AM", "venue": "V", "wa_template": "woi_seminar_registration_followup",
        "wa_params": "name,session_date,session_time,arrival_time,venue", "is_active": True})
    assert r.status_code == 200
    dbs.expire_all()
    lead = dbs.query(Lead).filter_by(meta_lead_id="L5").one()
    assert lead.sync_status == "ok" and lead.session_day == "Sunday" and lead.whatsapp_sent   # background reprocess ran


# ── 2. expired tokens ───────────────────────────────────────────────────────
def test_expired_meta_token_loses_nothing_and_self_heals(env, dbs, client):
    env.valid_meta.clear()                                  # token dies
    env.add_lead("L6", "1063861336132383", name="Kiran")
    classify_webhook_and_handle(dbs, webhook("L6", "1063861336132383"))
    lead = dbs.query(Lead).filter_by(meta_lead_id="L6").one()
    assert lead.sync_status == "pending_fetch" and not lead.full_name and not env.sent
    assert tokens.runtime_bad("meta")
    assert any("Meta" in str(k) for _, k in env.alerts)      # telegram alert fired
    h = client.get("/system/health").json()
    assert h["level"] == "error" and h["counts"]["pending_fetch"] == 1

    # admin pastes a short-lived USER token from Graph Explorer → CRM converts it to a non-expiring Page token
    assert client.post("/system/tokens/meta", json={"token": "short"}).status_code == 400
    r = client.post("/system/tokens/meta", json={"token": "EXPIRED_TOKEN_1234567890abc"})
    assert r.status_code == 400 and "invalid" in r.text.lower() or "rejected" in r.text.lower()
    still_bad = client.get("/system/health").json()
    assert still_bad["counts"]["pending_fetch"] == 1                 # a bad token never replaces anything
    env.valid_meta.add("PAGE_PERM")
    r = client.post("/system/tokens/meta", json={"token": "USER_TOK_graph_explorer_123456"})
    assert r.status_code == 200, r.text
    assert "non-expiring" in r.json()["message"] and tokens.get_meta_token() == "PAGE_PERM"
    assert tokens.get_page_id() == "PAGE1"
    assert ("POST", "PAGE1/subscribed_apps") in env.calls          # leadgen webhook re-subscribed
    dbs.expire_all()
    lead = dbs.query(Lead).filter_by(meta_lead_id="L6").one()
    assert lead.sync_status == "ok" and lead.full_name == "Kiran" and lead.whatsapp_sent   # recovered + invited
    assert not tokens.runtime_bad("meta")
    assert client.get("/system/health").json()["counts"]["pending_fetch"] == 0


def test_user_token_is_exchanged_for_permanent_page_token(env, client):
    tok = "USER_TOK"
    env.valid_meta.add("USER_TOK")
    # 'USER_TOK' is 8 chars → pad the real value but register it as user token in the fake
    long = "USER_TOK_" + "a" * 20
    env.valid_meta.add(long)
    import app.system_routes as sr
    ex = sr.exchange_for_page_token(long)
    assert ex["token"] == "PAGE_PERM" and ex["page_id"] == "PAGE1"


def test_expired_whatsapp_token_keeps_invite_pending_then_sends(env, dbs, client):
    env.valid_wa.clear()
    env.add_lead("L7", "1063861336132383")
    lead, wa = upsert_lead_from_meta(dbs, "L7")
    assert lead.sync_status == "ok" and lead.phone and not lead.whatsapp_sent
    assert wa["ok"] is False and "190" in lead.whatsapp_error and tokens.runtime_bad("wa")
    assert client.get("/system/health").json()["whatsapp"]["status"] == "error"
    env.valid_wa.add("WA_NEW_TOKEN_1234567890")
    r = client.post("/system/tokens/whatsapp", json={"token": "WA_NEW_TOKEN_1234567890"})
    assert r.status_code == 200, r.text
    dbs.expire_all()
    lead = dbs.query(Lead).filter_by(meta_lead_id="L7").one()
    assert lead.whatsapp_sent and lead.whatsapp_error is None and len(env.sent) == 1


def test_permanent_whatsapp_error_is_not_retried(env, dbs):
    env.wa_error = (400, {"message": "Recipient not on WhatsApp", "type": "OAuthException", "code": 131026})
    env.add_lead("L8", "1063861336132383")
    lead, wa = upsert_lead_from_meta(dbs, "L8")
    assert lead.whatsapp_failed and "131026" in lead.whatsapp_error
    n = len(env.calls)
    sync.retry_pending(dbs)
    dbs.refresh(lead)
    assert lead.wa_attempts == 1


# ── 3. self healing: missed webhooks, old leads ─────────────────────────────
def test_pull_recovers_missed_webhook_leads_only_messages_fresh_ones(env, dbs):
    env.add_lead("M1", "1063861336132383", age_hours=2)       # webhook never arrived
    env.add_lead("M2", "1063861336132383", age_hours=60)      # old → saved, but NOT messaged
    res = sync.pull_recent_leads(dbs, hours=96)
    assert res["new"] == 2
    m1 = dbs.query(Lead).filter_by(meta_lead_id="M1").one()
    m2 = dbs.query(Lead).filter_by(meta_lead_id="M2").one()
    assert m1.whatsapp_sent and not m2.whatsapp_sent and m2.sync_status == "ok"
    # idempotent
    assert sync.pull_recent_leads(dbs, hours=96)["new"] == 0
    assert dbs.query(Lead).count() == 2


def test_blank_legacy_leads_are_repaired_without_messaging(env, dbs):
    old = Lead(meta_lead_id="OLD1", status="New")             # exactly what the screenshot shows: "No name", no phone
    dbs.add(old); dbs.commit()
    env.add_lead("OLD1", "1063861336132383", name="Late Lead", age_hours=24 * 40)
    sync.retry_pending(dbs)
    dbs.refresh(old)
    assert old.full_name == "Late Lead" and old.phone and old.sync_status == "ok"
    assert not old.whatsapp_sent and not env.sent
    assert old.campaign_name == "Free Seminar" and old.session_day == "Sunday"


def test_duplicate_webhook_does_not_double_send(env, dbs):
    env.add_lead("D1", "1063861336132383")
    for _ in range(3):
        classify_webhook_and_handle(dbs, webhook("D1", "1063861336132383"))
    assert len(env.sent) == 1 and dbs.query(Lead).count() == 1


def test_form_discovery_auto_maps_and_flags(env, dbs):
    tokens.set_setting(tokens.PAGE_ID_KEY, "PAGE1")
    res = sync.discover_forms(dbs)
    assert "Seminar - Friday batch 2" in res["auto_mapped"]
    assert "Weekend Special Offer" in res["needs_day"]
    assert any("form" in str(k).lower() for _, k in env.alerts)


# ── 4. API surface that was broken before ───────────────────────────────────
def test_previously_broken_endpoints_work(env, dbs, client):
    env.add_lead("R1", "1063861336132383")
    upsert_lead_from_meta(dbs, "R1")
    assert client.get("/reports/summary").status_code == 200
    assert client.get("/reports/seminar").json()["rows"]
    assert client.get("/leads?day=Sunday").json()["total"] == 1
    assert client.get("/leads?sync=ok").json()["total"] == 1
    r = client.post("/leads", json={"full_name": "Manual", "phone": "9000000000", "preferred_day": "Friday"})
    assert r.status_code == 200 and r.json()["session_day"] == "Friday"
    lid = client.get("/leads").json()["rows"][0]["id"]
    assert client.post(f"/leads/{lid}/followups", json={"response": "ok", "confirmed": "Confirmed",
                                                         "next_followup_date": "2026-10-03"}).status_code == 200
    assert client.get(f"/leads/{lid}/followups").status_code == 200
    assert client.get("/followups/due?bucket=all").status_code == 200
    assert client.get("/health").json()["status"] == "ok"


def test_migration_adds_columns_to_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect
    from app.db import Base
    from app.migrate import add_missing_columns
    import app.models  # noqa
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE leads (id INTEGER PRIMARY KEY, meta_lead_id VARCHAR(100), full_name VARCHAR(255), phone VARCHAR(40))"))
        c.execute(text("INSERT INTO leads (meta_lead_id, full_name, phone) VALUES ('X','Old','911')"))
    added = add_missing_columns(eng, Base)
    cols = {c["name"] for c in inspect(eng).get_columns("leads")}
    assert {"sync_status", "whatsapp_error", "wa_attempts", "session_day"} <= cols
    assert "leads.sync_status" in added
    with eng.begin() as c:
        assert c.execute(text("SELECT full_name FROM leads")).scalar() == "Old"


def test_token_is_stored_encrypted(env):
    from app.db import SessionLocal
    from app.models import AppSetting
    with SessionLocal() as s:
        raw = s.get(AppSetting, tokens.META_KEY).value
    assert raw.startswith("enc:") and "META_OK" not in raw
    assert tokens.get_meta_token() == "META_OK"


def test_error_classification():
    from app.graph import classify
    assert classify(400, {"code": 190, "error_subcode": 463, "message": "Session has expired"}) == "token"
    assert classify(400, {"code": 100, "error_subcode": 33,
                          "message": "Object does not exist, cannot be loaded due to missing permissions"}) == "permission"
    assert classify(400, {"code": 100, "error_subcode": 33, "message": "Object does not exist"}) == "gone"
    assert classify(429, {"code": 4, "message": "limit"}) == "rate"
    assert classify(503, {"message": "down"}) == "transient"
    assert classify(400, {"code": 131026, "message": "undeliverable"}) == "other"


def test_no_template_means_no_invite_and_no_hidden_default(env, dbs):
    from app.models import FormConfig
    row = dbs.query(FormConfig).filter_by(form_id="1063861336132383").one()
    row.wa_template = ""; dbs.commit()
    env.add_lead("T1", "1063861336132383")
    lead, wa = upsert_lead_from_meta(dbs, "T1")
    assert wa["reason"] == "no_template" and not env.sent and not lead.wa_attempts and lead.sync_status == "ok"
    # setting a template later + manual invite works, with the configured variables only
    row.wa_template = "brand_new_tpl"; row.wa_params = "name,venue"; dbs.commit()
    from app.whatsapp import send_template_for_lead
    assert send_template_for_lead(dbs, lead)["ok"]
    p = env.sent[-1]["template"]
    assert p["name"] == "brand_new_tpl" and len(p["components"][0]["parameters"]) == 2
    # a template with no variables sends no body component
    row.wa_params = ""; dbs.commit(); dbs.refresh(lead)
    assert send_template_for_lead(dbs, lead, force=True)["ok"] and env.sent[-1]["template"]["components"] == []


def test_old_unfetched_leads_are_written_off():
    from datetime import datetime, timedelta, timezone
    from app.db import SessionLocal
    from app.models import Lead
    from app.sync import write_off_old
    with SessionLocal() as db:
        old = Lead(meta_lead_id="OLD1", sync_status="pending_fetch",
                   created_at=datetime.now(timezone.utc) - timedelta(days=40))
        new = Lead(meta_lead_id="NEW1", sync_status="pending_fetch",
                   created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        db.add_all([old, new]); db.commit()
        assert write_off_old(db) >= 1
        db.refresh(old); db.refresh(new)
        assert old.sync_status == "unrecoverable"
        assert new.sync_status == "pending_fetch"
