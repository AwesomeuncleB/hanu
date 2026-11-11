"""HTTP API for bill intake and the human review queue."""
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, db, rules, service

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="Hanu bill review", lifespan=lifespan)


@app.exception_handler(service.Problem)
async def problem_handler(_request, exc: service.Problem):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


def reviewer_only(x_review_token: Optional[str] = Header(None)):
    token = config.review_token()
    if token and not secrets.compare_digest(x_review_token or "", token):
        raise HTTPException(401, "Review token required.")


def bearer(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def signed_in_member(token: Optional[str] = Depends(bearer)):
    return service.member_from_token(token)


class Login(BaseModel):
    hanu_id: str = Field(min_length=4, max_length=40)
    pin: str = Field(min_length=4, max_length=12)


class Decision(BaseModel):
    action: Literal["approve", "deny"]
    reviewer: str = Field(min_length=2, max_length=80)
    reason: str = Field("", max_length=1000)
    pay_amount_naira: Optional[float] = Field(None, gt=0)
    clinic_id: Optional[int] = None


@app.get("/")
def landing_page():
    return FileResponse(STATIC / "landing.html")


@app.get("/styles.css")
def landing_styles():
    return FileResponse(STATIC / "styles.css")


@app.get("/review")
def review_page():
    return FileResponse(STATIC / "review.html")


@app.get("/api/settings", dependencies=[Depends(reviewer_only)])
def settings():
    conn = db.connect()
    try:
        clinics = [dict(r) for r in conn.execute(
            "SELECT id, name FROM clinics WHERE is_partner = 1 ORDER BY name")]
    finally:
        conn.close()
    return {
        "shadow_mode": config.shadow_mode(),
        "storage_is_temporary": config.storage_is_temporary(),
        "extractor": config.extractor(),
        "rules_version": rules.RULES_VERSION,
        "auto_approve_cap_kobo": config.auto_approve_cap_kobo(),
        "partner_clinics": clinics,
    }


@app.post("/api/bills", dependencies=[Depends(reviewer_only)])
def submit_bill(hanu_id: str = Form(min_length=4, max_length=40), image: UploadFile = File(...)):
    return service.submit_bill(hanu_id, image.file.read(service.MAX_IMAGE_BYTES + 1))


@app.get("/api/cases", dependencies=[Depends(reviewer_only)])
def list_cases(status: Optional[Literal["needs_review", "approved", "denied", "decided"]] = None):
    conn = db.connect()
    try:
        return service.list_cases(conn, status)
    finally:
        conn.close()


@app.get("/api/cases/{case_id}", dependencies=[Depends(reviewer_only)])
def get_case(case_id: int):
    conn = db.connect()
    try:
        return service.get_case(conn, case_id)
    finally:
        conn.close()


@app.get("/api/cases/{case_id}/image", dependencies=[Depends(reviewer_only)])
def case_image(case_id: int):
    conn = db.connect()
    try:
        row = conn.execute("SELECT image_file FROM cases WHERE id = ?", (case_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "No such case.")
    return FileResponse(config.data_dir() / "uploads" / row["image_file"])


@app.post("/api/cases/{case_id}/decision", dependencies=[Depends(reviewer_only)])
def decide(case_id: int, body: Decision):
    return service.decide(case_id, body.action, body.reviewer, body.reason,
                          body.pay_amount_naira, body.clinic_id)


# --- Member page -------------------------------------------------------------------

@app.get("/member")
def member_page():
    return FileResponse(STATIC / "member.html")


@app.post("/api/member/login")
def member_login(body: Login):
    return service.member_login(body.hanu_id, body.pin)


@app.post("/api/member/logout")
def member_logout(token: Optional[str] = Depends(bearer)):
    service.member_logout(token)
    return {"ok": True}


@app.get("/api/member/me")
def member_home(member=Depends(signed_in_member)):
    return service.member_home(member)


@app.post("/api/member/bills")
def member_bill(image: UploadFile = File(...), member=Depends(signed_in_member)):
    receipt = service.submit_bill(member["hanu_id"], image.file.read(service.MAX_IMAGE_BYTES + 1))
    return {**receipt, **service.member_home(member)}
