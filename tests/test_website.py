"""Website → CRM: events, form leads, WhatsApp tickets and the website report."""
import pytest

from app.models import Lead, FollowUp, FormConfig, WebsiteEvent
from app.config import settings

KEY = "web-test-key"
VIS = {"visitor_id": "v1", "session_id": "s1", "page": "/", "device": "mobile",
       "utm_source": "facebook", "utm_medium": "paid", "utm_campaign": "nov_freeclass",
       "referrer": "https://m.facebook.com/", "landed": "2026-11-18T10:00:00Z"}


def form(stage, lid="f1", phone="9876501234", tag="FreeClass", looking="Free class · Monday 5–7 PM", **kw):
    return {"type": "expert_form", "lead_id": lid, "stage": stage, "tag": tag, "looking_for": looking,
            "section": "Talk to an expert", "name": "Ravi Kumar", "phone": phone, "area": "Mandya",
            "mode": "", "amount": "", "investing": "-", "call_time": "Any time", "language": "Kannada",
            "note": "", **VIS, **kw}


@pytest.fixture()
def web(client, dbs, monkeypatch):
    monkeypatch.setattr(settings, "website_ingest_key", KEY)
    # seat-confirmation template for the Monday website class
    row = dbs.query(FormConfig).filter_by(form_id="website-freeclass-monday").one()
    row.wa_template = "web_seat"
    row.wa_params = "name,session_date,session_time,arrival_time,venue"
    dbs.commit()

    def post(*events, key=KEY):
        return client.post("/public/website/events", json={"events": list(events)},
                           headers={"X-Website-Key": key})
    return post


def test_website_form_configs_are_seeded(client, dbs):
    ids = {r.form_id for r in dbs.query(FormConfig)}
    assert {"website-freeclass-monday", "website-freeclass-friday", "website-freeclass-sunday",
            "website-enquiry"} <= ids
    fri = dbs.query(FormConfig).filter_by(form_id="website-freeclass-friday").one()
    assert fri.day == "Friday" and fri.session_time == "1:00 PM to 3:00 PM"


def test_ingest_needs_the_key(client, monkeypatch):
    monkeypatch.setattr(settings, "website_ingest_key", "")
    assert client.post("/public/website/events", json={"type": "page_view"}).status_code == 503
    monkeypatch.setattr(settings, "website_ingest_key", KEY)
    r = client.post("/public/website/events", json={"type": "page_view"}, headers={"X-Website-Key": "nope"})
    assert r.status_code == 401


def test_clicks_are_stored(web, dbs):
    r = web({"type": "page_view", **VIS},
            {"type": "whatsapp_click", "tag": "Course", "topic": "Panchajanya", "section": "Courses",
             "button": "Ask about fees", **VIS},
            {"type": "call_click", "section": "Contact", "button": "Call 97413 08822", **VIS},
            {"type": "not_a_real_type", **VIS})
    assert r.status_code == 200 and r.json()["stored"] == 3
    assert dbs.query(WebsiteEvent).count() == 3
    assert not dbs.query(Lead).count()


def test_half_filled_then_sent_is_one_lead_and_template_goes_once(web, dbs, env):
    web({"type": "expert_open", "topic": "Book a free class", "section": "Free classes", **VIS})
    r = web(form("Filled form, did not press Send"))
    out = r.json()["leads"][0]
    assert out["action"] == "created" and out["stage"] == "incomplete"
    lead = dbs.query(Lead).one()
    assert lead.source == "website" and lead.platform == "Website"
    assert lead.sync_status == "web_incomplete" and not lead.whatsapp_sent
    assert lead.session_day == "Monday" and lead.session_time == "5:00 PM to 7:00 PM"
    assert lead.form_id == "website-freeclass-monday" and lead.phone == "919876501234"
    assert "[Website]" in lead.notes and "Mandya" in lead.notes
    assert not env.sent                                     # half-filled → never messaged

    # the retry loop must not message half-filled website leads either
    from app.sync import retry_pending
    retry_pending(dbs)
    assert not env.sent

    r = web(form("Pressed Send (opened WhatsApp)"))
    assert r.json()["leads"][0]["action"] == "updated"
    dbs.expire_all()
    assert dbs.query(Lead).count() == 1
    lead = dbs.query(Lead).one()
    assert lead.web_stage == "whatsapp" and lead.sync_status == "ok" and lead.whatsapp_sent
    assert len(env.sent) == 1 and env.sent[0]["template"]["name"] == "web_seat"
    # journey: the expert_open click before the form is linked to the lead
    assert dbs.query(WebsiteEvent).filter(WebsiteEvent.lead_id == lead.id).count() == 3

    # a later partial event from the same session never downgrades the stage
    web(form("Filled form, did not press Send"))
    dbs.expire_all()
    assert dbs.query(Lead).one().web_stage == "whatsapp" and len(env.sent) == 1


def test_call_back_request_also_gets_the_template(web, dbs, env):
    web(form("Asked for a call back (no WhatsApp)", lid="cb1", phone="9988776655"))
    lead = dbs.query(Lead).one()
    assert lead.web_stage == "callback" and lead.whatsapp_sent


def test_enquiry_without_template_is_saved_and_not_messaged(web, dbs, env):
    web(form("Pressed Send (opened WhatsApp)", lid="inv1", tag="Premium10L",
             looking="Invest ₹10 lakh or more", amount="₹10 lakh – ₹25 lakh", investing="Yes, in mutual funds"))
    lead = dbs.query(Lead).one()
    assert lead.campaign_name == "Website · Premium10L" and lead.form_id == "website-enquiry"
    assert not lead.session_day and not env.sent and lead.experience == "Yes, in mutual funds"


def test_existing_number_gets_a_follow_up_not_a_duplicate(web, dbs, env):
    env.add_lead("M1", "1063861336132383", name="Asha", phone="9876501234")
    from app.meta import upsert_lead_from_meta
    upsert_lead_from_meta(dbs, "M1")
    sent_before = len(env.sent)
    web(form("Filled form, did not press Send", lid="f9"))
    web(form("Pressed Send (opened WhatsApp)", lid="f9"))
    dbs.expire_all()
    assert dbs.query(Lead).count() == 1
    lead = dbs.query(Lead).one()
    assert lead.source == "meta" and lead.web_stage == "whatsapp"
    fus = dbs.query(FollowUp).filter_by(lead_id=lead.id).all()
    assert len(fus) == 1 and "Sent the form" in fus[0].response      # same session → one follow-up, updated
    assert len(env.sent) == sent_before                               # no second invite


def test_bad_phone_makes_no_lead(web, dbs):
    r = web(form("Pressed Send (opened WhatsApp)", phone="12345"))
    assert r.json()["stored"] == 1 and not r.json()["leads"]
    assert not dbs.query(Lead).count()


def wa_msg(phone, text, mid="wamid.in1"):
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "contacts": [{"wa_id": phone, "profile": {"name": "Suma"}}],
        "messages": [{"from": phone, "id": mid, "type": "text", "timestamp": "1",
                      "text": {"body": text}}]}}]}]}


TICKET = ("Hi Wealth Ocean Institute,\n#FreeClass · Friday · Stock market trading awareness\n"
          "I would like a free seat on Fri, 20 Nov, 1:00 – 3:00 PM.\n\nName: Suma\nCity / town: Hassan\n\n"
          "(sent from the website · Free classes)")


def test_whatsapp_ticket_from_new_number_becomes_website_lead(web, dbs, env):
    fri = dbs.query(FormConfig).filter_by(form_id="website-freeclass-friday").one()
    fri.wa_template, fri.wa_params = "web_seat", "name,session_date,session_time,arrival_time,venue"
    dbs.commit()
    from app.meta import classify_webhook_and_handle
    classify_webhook_and_handle(dbs, wa_msg("919900112233", TICKET))
    lead = dbs.query(Lead).one()
    assert lead.source == "website" and lead.web_stage == "whatsapp_direct" and lead.status == "New"
    assert lead.session_day == "Friday" and lead.city == "Hassan" and lead.full_name == "Suma"
    assert lead.whatsapp_sent and env.sent[-1]["template"]["name"] == "web_seat"
    ev = dbs.query(WebsiteEvent).filter_by(type="whatsapp_message").one()
    assert ev.tag == "FreeClass" and ev.section == "Free classes" and ev.lead_id == lead.id


def test_ordinary_whatsapp_message_is_unchanged(dbs, env):
    from app.meta import classify_webhook_and_handle
    classify_webhook_and_handle(dbs, wa_msg("919900112244", "hello, fees?", mid="wamid.x"))
    lead = dbs.query(Lead).one()
    assert lead.status == "WhatsApp" and lead.source == "whatsapp"
    assert not dbs.query(WebsiteEvent).count()


def test_website_report_and_journey(web, client, dbs):
    web({"type": "page_view", **VIS},
        {"type": "section_view", "section": "Free classes", **VIS},
        {"type": "whatsapp_click", "tag": "FreeClass", "topic": "Monday", "section": "Free classes", **VIS},
        {"type": "page_view", **{**VIS, "visitor_id": "v2", "session_id": "s2", "utm_source": "",
                                 "referrer": "https://www.google.com/", "device": "desktop"}},
        {"type": "expert_open", "topic": "", "section": "Top of page", **VIS},
        {"type": "course_open", "topic": "panchajanya", "section": "Courses", **VIS},
        {"type": "whatsapp_click", "tag": "Course", "topic": "Panchajanya · fee and next batch", "section": "Courses", **VIS},
        {"type": "form_step", "topic": "kind", **VIS},
        {"type": "form_step", "topic": "you", **VIS},
        form("Filled form, did not press Send", lid="r1"),
        form("Pressed Send (opened WhatsApp)", lid="r1"),
        form("Filled form, did not press Send", lid="r2", phone="9123456789"))
    r = client.get("/reports/website")
    assert r.status_code == 200
    d = r.json()
    t = d["totals"]
    assert t["visitors"] == 2 and t["page_views"] == 2 and t["whatsapp_clicks"] == 2 and t["form_opens"] == 1
    assert d["course_opens"] == {"panchajanya": 1} and d["course_whatsapp"] == {"Panchajanya": 1}
    assert t["new_leads"] == 2 and t["forms_submitted"] == 1 and t["forms_incomplete"] == 1
    assert d["sources"] == {"Facebook": 1, "Google": 1}
    assert d["whatsapp_by_tag"] == {"FreeClass": 1, "Course": 1}
    assert d["form_funnel"]["steps"][0]["reached"] == 1
    assert sum(x["leads"] for x in d["by_day"]) == 2
    lead = dbs.query(Lead).filter_by(web_ref="r1").one()
    j = client.get(f"/website/journey/{lead.id}").json()
    assert [e["type"] for e in j["events"]][:3] == ["page_view", "section_view", "whatsapp_click"]


def test_leads_source_filter(web, client, dbs, env):
    env.add_lead("M2", "1063861336132383", name="Meta person", phone="9000000001")
    from app.meta import upsert_lead_from_meta
    upsert_lead_from_meta(dbs, "M2")
    web(form("Pressed Send (opened WhatsApp)", lid="s9", phone="9000000002"))
    w = client.get("/leads?source=website").json()
    m = client.get("/leads?source=meta").json()
    assert w["total"] == 1 and w["rows"][0]["source"] == "website" and w["rows"][0]["web_stage"] == "whatsapp"
    assert m["total"] == 1 and m["rows"][0]["full_name"] == "Meta person"
    assert client.get("/leads?source=bogus").status_code == 400


def test_contact_and_course_page_forms(web, dbs, env):
    web({"type": "expert_form", "form": "contact", "lead_id": "c1", "stage": "submitted", "tag": "Contact",
         "looking_for": "Contact page message", "section": "Contact page", "name": "Kiran", "phone": "9845012345",
         "email": "kiran@example.com", "note": "Fees for Panchajanya?", **VIS},
        {"type": "expert_form", "form": "mf_course", "lead_id": "m1", "stage": "checkout", "tag": "MFCourse",
         "looking_for": "Wealth Class Mysore · ₹99 checkout", "section": "Mutual fund course page · hero",
         "name": "Latha", "phone": "9845012346", "email": "latha@example.com", **VIS})
    c = dbs.query(Lead).filter_by(full_name="Kiran").one()
    m = dbs.query(Lead).filter_by(full_name="Latha").one()
    assert c.form_name == "Website · Contact page" and c.email == "kiran@example.com" and c.web_stage == "submitted"
    assert "Fees for Panchajanya?" in c.notes and c.sync_status == "ok"
    assert m.campaign_name == "Website · MFCourse" and m.web_stage == "checkout"
    assert not env.sent          # website-enquiry has no template → nobody messaged


def test_journey_keeps_two_people_on_one_phone_apart(web, client, dbs):
    web({"type": "page_view", **VIS}, form("Pressed Send (opened WhatsApp)", lid="a1", phone="9800000011"))
    web(form("Pressed Send (opened WhatsApp)", lid="b1", phone="9800000022", name="Second"))
    a = dbs.query(Lead).filter_by(web_ref="a1").one()
    b = dbs.query(Lead).filter_by(web_ref="b1").one()
    ja = [e["type"] for e in client.get(f"/website/journey/{a.id}").json()["events"]]
    jb = [e["type"] for e in client.get(f"/website/journey/{b.id}").json()["events"]]
    assert ja == ["page_view", "expert_form"] and jb == ["expert_form"]
