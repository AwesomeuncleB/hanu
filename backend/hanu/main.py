"""HTTP API and pages: landing page, member page, and the staff review queue."""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import config, db, rules, service

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="Hanu", lifespan=lifespan)


@app.exception_handler(service.Problem)
async def problem_handler(_request, exc: service.Problem):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


def bearer(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def member_only(token: Optional[str] = Depends(bearer)):
    return service.signed_in("member", token)


def staff_only(token: Optional[str] = Depends(bearer)):
    return service.signed_in("staff", token)


class MemberLogin(BaseModel):
    hanu_id: str = Field(min_length=4, max_length=40)
    pin: str = Field(min_length=4, max_length=12)


class MemberSignup(BaseModel):
    name: str = Field(max_length=80)
    phone: str = Field(max_length=30)
    pin: str = Field(max_length=12)
    lock_months: int


class StaffLogin(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class Decision(BaseModel):
    action: Literal["approve", "deny"]
    reason: str = Field("", max_length=1000)
    pay_amount_naira: Optional[float] = Field(None, gt=0)
    clinic_id: Optional[int] = None


# --- Pages -------------------------------------------------------------------------

@app.get("/")
def landing_page():
    return FileResponse(STATIC / "landing.html")


@app.get("/styles.css")
def landing_styles():
    return FileResponse(STATIC / "styles.css")


@app.get("/member")
def member_page():
    return FileResponse(STATIC / "member.html")


@app.get("/review")
def review_page():
    return FileResponse(STATIC / "review.html")


@app.post("/api/logout")
def sign_out(token: Optional[str] = Depends(bearer)):
    service.sign_out(token)
    return {"ok": True}


# --- Staff -------------------------------------------------------------------------

@app.post("/api/staff/login")
def staff_login(body: StaffLogin):
    return service.staff_login(body.email, body.password)


@app.get("/api/settings")
def settings(staff=Depends(staff_only)):
    conn = db.connect()
    try:
        clinics = [dict(r) for r in conn.execute(
            "SELECT id, name FROM clinics WHERE is_partner = 1 ORDER BY name").fetchall()]
    finally:
        conn.close()
    return {
        "staff_name": staff["name"],
        "shadow_mode": config.shadow_mode(),
        "storage_is_temporary": config.storage_is_temporary(),
        "extractor": config.extractor(),
        "rules_version": rules.RULES_VERSION,
        "auto_approve_cap_kobo": config.auto_approve_cap_kobo(),
        "partner_clinics": clinics,
    }


@app.post("/api/bills", dependencies=[Depends(staff_only)])
def submit_bill(hanu_id: str = Form(min_length=4, max_length=40), image: UploadFile = File(...)):
    return service.submit_bill(hanu_id, image.file.read(service.MAX_IMAGE_BYTES + 1))


@app.get("/api/cases", dependencies=[Depends(staff_only)])
def list_cases(status: Optional[Literal["needs_review", "approved", "denied", "decided"]] = None):
    conn = db.connect()
    try:
        return service.list_cases(conn, status)
    finally:
        conn.close()


@app.get("/api/cases/{case_id}", dependencies=[Depends(staff_only)])
def get_case(case_id: int):
    conn = db.connect()
    try:
        return service.get_case(conn, case_id)
    finally:
        conn.close()


@app.get("/api/cases/{case_id}/image", dependencies=[Depends(staff_only)])
def case_image(case_id: int):
    data, media_type = service.case_image(case_id)
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/cases/{case_id}/decision")
def decide(case_id: int, body: Decision, staff=Depends(staff_only)):
    return service.decide(case_id, body.action, staff, body.reason, body.pay_amount_naira, body.clinic_id)


# --- Members -----------------------------------------------------------------------

@app.post("/api/member/login")
def member_login(body: MemberLogin):
    return service.member_login(body.hanu_id, body.pin)


@app.post("/api/member/signup")
def member_signup(body: MemberSignup):
    return service.member_signup(body.name, body.phone, body.pin, body.lock_months)


@app.get("/api/member/me")
def member_home(member=Depends(member_only)):
    return service.member_home(member)


@app.post("/api/member/bills")
def member_bill(image: UploadFile = File(...), member=Depends(member_only)):
    receipt = service.submit_bill(member["hanu_id"], image.file.read(service.MAX_IMAGE_BYTES + 1))
    return {**receipt, **service.member_home(member)}
