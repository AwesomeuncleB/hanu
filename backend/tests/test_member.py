from hanu import db

from test_flow import AMINA, CHINEDU, app, photo  # noqa: F401  (app is the fixture)


def login(app, hanu_id=AMINA, pin=db.DEMO_PIN):
    return app.anonymous.post("/api/member/login", json={"hanu_id": hanu_id, "pin": pin})


def auth(app, hanu_id=AMINA):
    return {"Authorization": "Bearer " + login(app, hanu_id).json()["token"]}


def send(app, headers, seed=0):
    return app.post("/api/member/bills", headers=headers, files={"image": ("b.jpg", photo(seed), "image/png")})


def test_member_signs_in_and_sees_balance(app):
    res = login(app, "hn 4821 0937")  # typed without dashes, lower case
    assert res.status_code == 200 and res.json()["name"] == "Amina Yusuf"
    me = app.get("/api/member/me", headers={"Authorization": "Bearer " + res.json()["token"]}).json()
    assert me["balance_kobo"] == 4200000 and me["hanu_id"] == AMINA and me["bills"] == []


def test_wrong_pin_and_unknown_id_look_the_same(app):
    wrong, unknown = login(app, pin="0000"), login(app, "HN-0000-0000")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_five_wrong_pins_lock_the_id(app):
    for _ in range(5):
        assert login(app, pin="0000").status_code == 401
    assert login(app).status_code == 429  # even the right PIN waits out the lock
    assert login(app, CHINEDU).status_code == 200  # other members are unaffected


def test_member_pages_need_a_session(app):
    assert app.get("/api/member/me").status_code == 401  # a staff session is not a member session
    assert app.anonymous.get("/api/member/me").status_code == 401
    assert app.get("/api/member/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert send(app, {}).status_code == 401


def test_sign_out_ends_the_session(app):
    headers = auth(app)
    assert app.post("/api/logout", headers=headers).status_code == 200
    assert app.get("/api/member/me", headers=headers).status_code == 401


def test_member_sends_a_bill_and_sees_only_their_own(app):
    amina, chinedu = auth(app), auth(app, CHINEDU)
    out = send(app, amina).json()
    assert out["status"] == "approved" and out["balance_kobo"] == 3350000
    assert [b["status"] for b in out["bills"]] == ["approved"]
    assert "flags" not in out["bills"][0] and "extraction" not in out["bills"][0]
    assert app.get("/api/member/me", headers=chinedu).json()["bills"] == []


def test_member_sees_the_reason_for_a_denial_but_not_approval_notes(app):
    headers = auth(app, CHINEDU)  # 6,000 balance against an 8,500 bill: goes to review
    first = send(app, headers, seed=1).json()
    assert first["status"] == "needs_review"
    app.decide(first["case_id"], action="deny", reason="Clinic to reissue the bill.")
    second = send(app, headers, seed=2).json()
    app.decide(second["case_id"], action="approve", pay_amount_naira=6000, reason="internal note")
    bills = {b["id"]: b for b in app.get("/api/member/me", headers=headers).json()["bills"]}
    assert bills[first["case_id"]]["decision_reason"] == "Clinic to reissue the bill."
    assert bills[second["case_id"]]["decision_reason"] is None
    assert bills[second["case_id"]]["paid_kobo"] == 600000


def join(app, **over):
    body = {"name": "Halima Bello", "phone": "0803 555 0142", "pin": "7305", "lock_months": 6, **over}
    return app.anonymous.post("/api/member/signup", json=body)


def test_a_new_member_joins_and_gets_a_hanu_id(app):
    res = join(app)
    assert res.status_code == 200
    out = res.json()
    assert out["hanu_id"].startswith("HN-") and len(out["hanu_id"]) == 12
    me = app.get("/api/member/me", headers={"Authorization": "Bearer " + out["token"]}).json()
    assert me["name"] == "Halima Bello" and me["balance_kobo"] == 0 and me["lock_until"]
    assert login(app, out["hanu_id"], "7305").status_code == 200


def test_joining_checks_what_was_entered(app):
    assert join(app).status_code == 200
    assert join(app, name="Someone Else").status_code == 409  # same phone
    for bad in ({"pin": "12"}, {"pin": "1111"}, {"pin": "abcd"}, {"phone": "12"}, {"name": " "},
                {"lock_months": 3}):
        assert join(app, **{"phone": "0803 555 0199", **bad}).status_code == 422, bad
