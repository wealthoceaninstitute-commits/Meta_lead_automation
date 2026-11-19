"""Seminar feedback: pick attendee by phone or add as walk-in, configurable questions, export."""
from app.models import Lead, SeminarFeedback


def _fields(client):
    return {f["key"]: f for f in client.get("/feedback/fields").json()["rows"]}


def test_default_questions_match_printed_form(client):
    f = _fields(client)
    assert list(f) == ["profession", "seminar_experience", "interested_course", "time_to_join",
                       "eye_opener", "referrals", "comments"]
    assert f["seminar_experience"]["options"][0] == "Very Good"
    assert f["referrals"]["rows"] == 3


def test_pick_existing_lead_by_phone(client, dbs):
    lead = Lead(full_name="Ravi", phone="919876543210", status="Confirmed", source="meta")
    dbs.add(lead); dbs.commit()
    rows = client.get("/feedback/lookup", params={"phone": "98765"}).json()["rows"]
    assert rows and rows[0]["id"] == lead.id

    r = client.post("/feedback", json={
        "lead_id": lead.id, "seminar_date": "2026-11-15",
        "answers": {"profession": "Teacher", "seminar_experience": "Very Good",
                    "interested_course": ["PANCHAJANYA COURSE (Full Course)"],
                    "referrals": [{"name": "Anu", "phone": "9000000001"}, {"name": "", "phone": ""}],
                    "unknown_key": "dropped"},
        "add_referrals_as_leads": True,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["walk_in"] is False and d["full_name"] == "Ravi" and d["phone"] == "919876543210"
    assert d["answers"]["referrals"] == [{"name": "Anu", "phone": "919000000001"}]
    assert "unknown_key" not in d["answers"]
    assert d["referral_leads_added"] == 1
    dbs.expire_all()
    assert dbs.get(Lead, lead.id).status == "Attended"
    assert dbs.query(Lead).filter_by(phone="919000000001").one().campaign_name == "Seminar referral"


def test_walk_in_creates_lead(client, dbs):
    r = client.post("/feedback", json={"full_name": "Suma", "phone": "98450 12345",
                                       "answers": {"seminar_experience": "Good"}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["walk_in"] is True
    lead = dbs.query(Lead).filter_by(phone="919845012345").one()
    assert lead.id == d["lead_id"] and lead.source == "manual" and lead.status == "Attended"
    # walk-in needs name + valid mobile
    assert client.post("/feedback", json={"full_name": "X", "phone": "123"}).status_code == 400
    assert client.post("/feedback", json={"full_name": "", "phone": "9845012399"}).status_code == 400


def test_add_edit_hide_remove_questions(client):
    r = client.post("/feedback/fields", json={"label": "Annual income range", "field_type": "radio",
                                              "options": ["< 5L", "5-10L", "> 10L"], "required": True})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["key"] == "annual_income_range"
    assert client.post("/feedback/fields", json={"label": "Bad", "field_type": "radio", "options": ["one"]}).status_code == 400

    # required question is enforced
    assert client.post("/feedback", json={"full_name": "A", "phone": "9845000001"}).status_code == 400
    ok = client.post("/feedback", json={"full_name": "A", "phone": "9845000001",
                                        "answers": {"annual_income_range": "5-10L"}})
    assert ok.status_code == 200

    # hidden question is not enforced
    client.patch(f"/feedback/fields/{new['id']}", json={"is_active": False, "label": "Income"})
    assert client.post("/feedback", json={"full_name": "B", "phone": "9845000002"}).status_code == 200

    # reorder: new question first
    ids = [f["id"] for f in client.get("/feedback/fields").json()["rows"]]
    ids.remove(new["id"])
    client.post("/feedback/fields/reorder", json={"ids": [new["id"]] + ids})
    assert client.get("/feedback/fields").json()["rows"][0]["id"] == new["id"]

    # removing keeps old answers, export still shows them
    assert client.delete(f"/feedback/fields/{new['id']}").status_code == 200
    assert "annual_income_range" not in _fields(client)
    csv = client.get("/feedback/export").text
    assert "Annual income range" in csv and "5-10L" in csv

    # reset brings back the printed form
    client.post("/feedback/fields/reset")
    assert len(_fields(client)) == 7


def test_list_filter_update_delete(client, dbs):
    client.post("/feedback", json={"full_name": "C", "phone": "9845000003", "seminar_date": "2026-11-08"})
    client.post("/feedback", json={"full_name": "D", "phone": "9845000004", "seminar_date": "2026-11-15"})
    d = client.get("/feedback", params={"date_from": "2026-11-10"}).json()
    assert d["total"] == 1 and d["rows"][0]["full_name"] == "D"
    assert d["dates"][:2] == ["2026-11-15", "2026-11-08"]
    fid = d["rows"][0]["id"]
    r = client.patch(f"/feedback/{fid}", json={"answers": {"comments": "Great session"}})
    assert r.json()["answers"]["comments"] == "Great session"
    assert client.delete(f"/feedback/{fid}").status_code == 200
    assert dbs.query(SeminarFeedback).count() == 1
