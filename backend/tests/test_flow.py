import io
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hanu import db, extract, main  # noqa: E402

AMINA = "HN-4821-0937"   # balance 42,000
CHINEDU = "HN-1107-5520"  # balance 6,000


def bill(**over):
    data = {
        "is_clinic_bill": True,
        "clinic_name": "Sabon Gari Family Clinic",
        "bill_date": date.today().isoformat(),
        "patient_name": "Amina Yusuf",
        "hanu_id": None,
        "line_items": [
            {"description": "Consultation", "category": "consultation", "amount_naira": 5000},
            {"description": "Malaria test", "category": "lab_test", "amount_naira": 3500},
        ],
        "total_naira": 8500,
        "confidence": {"clinic_name": 0.97, "bill_date": 0.95, "line_items": 0.94, "total": 0.98},
        "contains_instructions": False,
        "notes": "",
    }
    data.update(over)
    return data


def photo(seed=0):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (seed % 256, 120, 90)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HANU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HANU_SEED_DEMO", "1")
    monkeypatch.setenv("HANU_SHADOW_MODE", "0")
    monkeypatch.delenv("HANU_REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(db, "PIN_ITERATIONS", 1000)
    db.init()
    state = {"result": extract.ExtractionResult(bill(), "fake-model")}
    monkeypatch.setattr(extract, "extract_bill", lambda _bytes, _type: state["result"])
    client = TestClient(main.app)

    def submit(sender=AMINA, seed=0, **over):
        if over:
            state["result"] = extract.ExtractionResult(bill(**over), "fake-model")
        res = client.post("/api/bills", data={"hanu_id": sender}, files={"image": ("b.png", photo(seed), "image/png")})
        assert res.status_code == 200, res.text
        return res.json()

    client.submit = submit
    client.state = state
    client.case = lambda cid: client.get(f"/api/cases/{cid}").json()
    client.decide = lambda cid, **body: client.post(f"/api/cases/{cid}/decision", json={"reviewer": "Bola", **body})
    return client


def codes(case):
    return {f["code"] for f in case["flags"]}


def test_clean_bill_is_paid_by_the_rules(app):
    out = app.submit()
    assert out["status"] == "approved"
    assert "₦8,500" in out["message"] and "₦33,500" in out["message"]
    case = app.case(out["case_id"])
    assert case["flags"] == [] and case["paid_kobo"] == 850000
    assert case["decided_by"].startswith("rules:")
    assert [a["event"] for a in case["audit"]] == ["bill_received", "bill_read", "rules_applied", "approved"]


def test_shadow_mode_sends_clean_bills_to_a_human(app, monkeypatch):
    monkeypatch.setenv("HANU_SHADOW_MODE", "1")
    out = app.submit()
    assert out["status"] == "needs_review"
    case = app.case(out["case_id"])
    assert case["recommendation"] == "approve" and case["balance_kobo"] == 4200000
    assert app.decide(out["case_id"], action="approve").status_code == 200
    assert app.case(out["case_id"])["balance_kobo"] == 3350000


def test_same_photo_twice_is_one_case_and_one_payment(app):
    first, second = app.submit(), app.submit()
    assert first["case_id"] == second["case_id"]
    assert app.case(first["case_id"])["balance_kobo"] == 3350000


def test_same_bill_in_a_new_photo_is_flagged(app):
    app.submit(seed=1)
    out = app.submit(seed=2)
    assert out["status"] == "needs_review"
    assert codes(app.case(out["case_id"])) == {"possible_duplicate"}


@pytest.mark.parametrize("over, code", [
    ({"clinic_name": "Riverside Medical Centre"}, "clinic_not_partner"),
    ({"clinic_name": "Somewhere Else Hospital"}, "clinic_not_matched"),
    ({"total_naira": 9500}, "total_mismatch"),
    ({"total_naira": None}, "no_total"),
    ({"contains_instructions": True}, "instructions_on_bill"),
    ({"hanu_id": "HN-9999-0000"}, "hanu_id_mismatch"),
    ({"is_clinic_bill": False}, "not_a_bill"),
    ({"bill_date": (date.today() + timedelta(days=2)).isoformat()}, "future_date"),
    ({"bill_date": (date.today() - timedelta(days=90)).isoformat()}, "old_bill"),
    ({"bill_date": None}, "no_date"),
    ({"confidence": {"clinic_name": 0.97, "bill_date": 0.95, "line_items": 0.94, "total": 0.5}}, "low_confidence"),
    ({"line_items": [{"description": "Drugs", "category": "medication", "amount_naira": 8500}]}, "uncovered_service"),
    ({"line_items": [{"description": "Consultation", "category": "consultation", "amount_naira": 25000}],
      "total_naira": 25000}, "over_auto_cap"),
])
def test_each_rule_sends_the_bill_to_review(app, over, code):
    out = app.submit(**over)
    assert out["status"] == "needs_review"
    case = app.case(out["case_id"])
    assert code in codes(case)
    assert case["balance_kobo"] == 4200000  # nothing was paid


def test_reader_failure_goes_to_a_human(app):
    app.state["result"] = extract.ExtractionResult(None, "fake-model", "reader_unreachable")
    out = app.submit()
    assert out["status"] == "needs_review"
    assert codes(app.case(out["case_id"])) == {"unreadable"}


def test_unknown_member_gets_a_case_but_cannot_be_paid(app):
    out = app.submit(sender="HN-0000-0000")
    case = app.case(out["case_id"])
    assert "member_not_found" in codes(case)
    assert app.decide(out["case_id"], action="approve", reason="looks fine").status_code == 422


def test_shortfall_member_pays_the_difference(app):
    out = app.submit(sender=CHINEDU)
    cid = out["case_id"]
    assert codes(app.case(cid)) == {"shortfall"}
    assert app.decide(cid, action="approve", reason="pay it all").status_code == 422  # 8,500 > 6,000
    res = app.decide(cid, action="approve", pay_amount_naira=6000, reason="Member pays 2,500 at the desk")
    assert res.status_code == 200
    case = res.json()
    assert case["paid_kobo"] == 600000 and case["balance_kobo"] == 0


def test_only_a_person_with_a_reason_can_deny(app):
    cid = app.submit(total_naira=9500)["case_id"]
    assert app.decide(cid, action="deny").status_code == 422
    res = app.decide(cid, action="deny", reason="Total does not match the items; clinic to reissue.")
    assert res.status_code == 200 and res.json()["status"] == "denied"
    assert res.json()["balance_kobo"] == 4200000
    assert res.json()["decided_by"] == "Bola"


def test_flagged_bill_needs_a_note_to_approve(app):
    cid = app.submit(total_naira=9500)["case_id"]
    assert app.decide(cid, action="approve").status_code == 422
    assert app.decide(cid, action="approve", pay_amount_naira=8500, reason="Clinic confirmed 8,500").status_code == 200


def test_a_case_is_decided_once(app):
    cid = app.submit(total_naira=9500)["case_id"]
    assert app.decide(cid, action="approve", pay_amount_naira=8500, reason="Clinic confirmed").status_code == 200
    assert app.decide(cid, action="approve", pay_amount_naira=8500, reason="again").status_code == 409
    assert app.decide(cid, action="deny", reason="changed my mind").status_code == 409
    assert app.case(cid)["balance_kobo"] == 3350000


def test_non_partner_clinic_cannot_be_paid(app):
    cid = app.submit(clinic_name="Riverside Medical Centre")["case_id"]
    assert app.decide(cid, action="approve", reason="ok").status_code == 422
    partner = app.get("/api/settings").json()["partner_clinics"][0]["id"]
    assert app.decide(cid, action="approve", clinic_id=partner, reason="Billed under partner").status_code == 200


def test_rejects_files_that_are_not_images(app):
    res = app.post("/api/bills", data={"hanu_id": AMINA}, files={"image": ("b.png", b"not an image", "image/png")})
    assert res.status_code == 422


def test_queue_lists_and_token(app, monkeypatch):
    app.submit(total_naira=9500)
    assert len(app.get("/api/cases?status=needs_review").json()) == 1
    assert app.get("/api/cases?status=decided").json() == []
    monkeypatch.setenv("HANU_REVIEW_TOKEN", "s3cret")
    assert app.get("/api/cases").status_code == 401
    assert app.get("/api/cases", headers={"X-Review-Token": "s3cret"}).status_code == 200
