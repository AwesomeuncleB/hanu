"""The bill pipeline: receive a bill, read it, apply the rules, and record decisions.

Two things can end a case: the rules approving a clean bill (only when shadow mode is
off), or a named reviewer approving or denying it. Both go through the same payment
code, and everything is written to the audit log.
"""
import hashlib
import io
import json
import secrets
from datetime import date, datetime, timedelta, timezone

from PIL import Image

from . import config, db, extract, rules

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
SESSION_DAYS = 7
MEDIA_TYPES = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}


class Problem(Exception):
    """A request that cannot be carried out; becomes an HTTP error with this message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def kobo(naira) -> int | None:
    return None if naira is None else round(float(naira) * 100)


def submit_bill(hanu_id: str, image_bytes: bytes) -> dict:
    media_type, ext = _check_image(image_bytes)
    sha = hashlib.sha256(image_bytes).hexdigest()
    hanu_id = hanu_id.strip().upper()

    conn = db.connect()
    try:
        member = _find_member(conn, hanu_id)
        if member:
            # The same photo sent twice (a WhatsApp retry, say) is the same case.
            again = conn.execute(
                "SELECT id FROM cases WHERE member_id = ? AND image_sha256 = ?", (member["id"], sha)
            ).fetchone()
            if again:
                return _receipt(conn, again["id"])
    finally:
        conn.close()

    image_file = f"{sha}.{ext}"
    (config.data_dir() / "uploads" / image_file).write_bytes(image_bytes)

    # The slow model call happens outside any database transaction.
    result = extract.extract_bill(image_bytes, media_type)
    ext_data = result.data

    with db.tx() as conn:
        clinic = None
        total_kobo = None
        items_kobo = 0
        bill_date = None
        if ext_data and ext_data["is_clinic_bill"]:
            clinic = conn.execute(
                "SELECT * FROM clinics WHERE name_key = ?", (db.name_key(ext_data["clinic_name"]),)
            ).fetchone()
            total_kobo = kobo(ext_data["total_naira"])
            items_kobo = sum(kobo(i["amount_naira"]) for i in ext_data["line_items"])
            bill_date = ext_data["bill_date"]

        duplicate = None
        if member and clinic and total_kobo and bill_date:
            duplicate = conn.execute(
                "SELECT id FROM cases WHERE member_id = ? AND clinic_id = ? AND bill_date = ? "
                "AND total_kobo = ? AND status != 'denied'",
                (member["id"], clinic["id"], bill_date, total_kobo),
            ).fetchone()

        balance = db.balance_kobo(conn, member["id"]) if member else 0
        verdict = rules.evaluate(rules.Context(
            extraction=ext_data,
            extraction_error=result.error,
            member_found=member is not None,
            member_hanu_key=db.name_key(member["hanu_id"]) if member else "",
            extracted_hanu_key=db.name_key(ext_data["hanu_id"]) if ext_data else "",
            clinic_is_partner=bool(clinic["is_partner"]) if clinic else None,
            total_kobo=total_kobo,
            line_items_kobo=items_kobo,
            balance_kobo=balance,
            duplicate_of=duplicate["id"] if duplicate else None,
            today=date.today(),
            min_confidence=config.min_confidence(),
            auto_approve_cap_kobo=config.auto_approve_cap_kobo(),
        ))

        case_id = conn.execute(
            "INSERT INTO cases (member_id, hanu_id_submitted, image_file, image_sha256, clinic_id, "
            "bill_date, total_kobo, extraction_json, extractor_model, rules_version, recommendation, "
            "flags_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'needs_review', ?)",
            (member["id"] if member else None, hanu_id, image_file, sha,
             clinic["id"] if clinic else None, bill_date, total_kobo,
             json.dumps(ext_data, ensure_ascii=False) if ext_data else None, result.model,
             verdict["version"], verdict["recommendation"],
             json.dumps(verdict["flags"], ensure_ascii=False), db.now()),
        ).lastrowid

        db.audit(conn, case_id, "member", "bill_received", {"hanu_id": hanu_id, "image_sha256": sha})
        db.audit(conn, case_id, f"reader:{result.model}", "bill_read",
                 {"extraction": ext_data, "error": result.error})
        db.audit(conn, case_id, f"rules:{verdict['version']}", "rules_applied",
                 {**verdict, "shadow_mode": config.shadow_mode()})

        if verdict["recommendation"] == "approve" and not config.shadow_mode():
            _pay(conn, case_id, member["id"], clinic["id"], total_kobo,
                 actor=f"rules:{verdict['version']}", reason="Passed every written rule.")

    conn = db.connect()
    try:
        return _receipt(conn, case_id)
    finally:
        conn.close()


def decide(case_id: int, action: str, reviewer: str, reason: str = "",
           pay_amount_naira: float | None = None, clinic_id: int | None = None) -> dict:
    reviewer, reason = reviewer.strip(), reason.strip()
    with db.tx() as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise Problem(404, "No such case.")
        if case["status"] != "needs_review":
            raise Problem(409, f"This case was already {case['status']} by {case['decided_by']}.")

        if action == "deny":
            if len(reason) < 5:
                raise Problem(422, "A denial needs a reason the member can be given.")
            conn.execute(
                "UPDATE cases SET status = 'denied', decided_by = ?, decision_reason = ?, "
                "decided_at = ? WHERE id = ?", (reviewer, reason, db.now(), case_id))
            db.audit(conn, case_id, f"reviewer:{reviewer}", "denied", {"reason": reason})
            return get_case(conn, case_id)

        if case["member_id"] is None:
            raise Problem(422, "No member is matched to this case, so there is no balance to pay from.")
        clinic = conn.execute(
            "SELECT * FROM clinics WHERE id = ?", (clinic_id or case["clinic_id"],)).fetchone()
        if not clinic or not clinic["is_partner"]:
            raise Problem(422, "Choose the partner clinic to pay.")
        amount = kobo(pay_amount_naira) if pay_amount_naira is not None else case["total_kobo"]
        if not amount or amount <= 0:
            raise Problem(422, "Enter the amount to pay the clinic.")
        balance = db.balance_kobo(conn, case["member_id"])
        if amount > balance:
            raise Problem(422, f"The member's balance is {rules.naira(balance)}. Pay that much or less; "
                               "the member covers the rest.")
        flags = json.loads(case["flags_json"])
        if (flags or amount != case["total_kobo"]) and len(reason) < 5:
            raise Problem(422, "This case has flags or a changed amount, so approving it needs a note.")

        _pay(conn, case_id, case["member_id"], clinic["id"], amount,
             actor=f"reviewer:{reviewer}", reason=reason)
        return get_case(conn, case_id)


def _pay(conn, case_id: int, member_id: int, clinic_id: int, amount_kobo: int, actor: str, reason: str):
    """Debit the member's balance for this case. The actual transfer to the clinic is
    not built yet: this records what is owed to the clinic."""
    conn.execute(
        "INSERT INTO ledger (member_id, kind, amount_kobo, case_id, clinic_id, created_at) "
        "VALUES (?, 'payment', ?, ?, ?, ?)", (member_id, -amount_kobo, case_id, clinic_id, db.now()))
    conn.execute(
        "UPDATE cases SET status = 'approved', clinic_id = ?, paid_kobo = ?, decided_by = ?, "
        "decision_reason = ?, decided_at = ? WHERE id = ?",
        (clinic_id, amount_kobo, actor, reason, db.now(), case_id))
    db.audit(conn, case_id, actor, "approved",
             {"paid_kobo": amount_kobo, "clinic_id": clinic_id, "reason": reason})


def get_case(conn, case_id: int) -> dict:
    row = conn.execute(
        "SELECT c.*, m.name AS member_name, m.hanu_id AS member_hanu_id, cl.name AS clinic_name, "
        "cl.is_partner AS clinic_is_partner FROM cases c LEFT JOIN members m ON m.id = c.member_id "
        "LEFT JOIN clinics cl ON cl.id = c.clinic_id WHERE c.id = ?", (case_id,)).fetchone()
    if not row:
        raise Problem(404, "No such case.")
    case = dict(row)
    case["extraction"] = json.loads(case.pop("extraction_json")) if case["extraction_json"] else None
    case["flags"] = json.loads(case.pop("flags_json"))
    case["balance_kobo"] = db.balance_kobo(conn, case["member_id"]) if case["member_id"] else None
    case["audit"] = [
        {"actor": a["actor"], "event": a["event"], "at": a["created_at"], "detail": json.loads(a["detail_json"])}
        for a in conn.execute("SELECT * FROM audit_log WHERE case_id = ? ORDER BY id", (case_id,))
    ]
    return case


def list_cases(conn, status: str | None) -> list[dict]:
    sql = ("SELECT c.id, c.status, c.recommendation, c.total_kobo, c.paid_kobo, c.created_at, "
           "c.flags_json, c.hanu_id_submitted, m.name AS member_name, cl.name AS clinic_name "
           "FROM cases c LEFT JOIN members m ON m.id = c.member_id "
           "LEFT JOIN clinics cl ON cl.id = c.clinic_id")
    args: tuple = ()
    if status == "decided":
        sql += " WHERE c.status != 'needs_review'"
    elif status:
        sql += " WHERE c.status = ?"
        args = (status,)
    # Oldest first in the queue so nobody waits behind newer bills; newest first for history.
    sql += " ORDER BY c.created_at " + ("ASC" if status == "needs_review" else "DESC") + ", c.id"
    out = []
    for row in conn.execute(sql, args):
        item = dict(row)
        item["flag_count"] = len(json.loads(item.pop("flags_json")))
        out.append(item)
    return out


def _receipt(conn, case_id: int) -> dict:
    """What the member is told. It never announces a denial: that message comes from a person."""
    case = get_case(conn, case_id)
    if case["status"] == "approved":
        message = (f"Paid {rules.naira(case['paid_kobo'])} to {case['clinic_name']}. "
                   f"Your balance is now {rules.naira(case['balance_kobo'])}.")
    elif case["status"] == "denied":
        message = "This bill has been reviewed. Please check your messages from the Hanu team."
    else:
        message = "We have your bill. A member of the Hanu team is checking it and will message you shortly."
    return {"case_id": case_id, "status": case["status"], "message": message}


def _check_image(data: bytes) -> tuple[str, str]:
    if not data:
        raise Problem(422, "No image was sent.")
    if len(data) > MAX_IMAGE_BYTES:
        raise Problem(413, "The image is larger than 8 MB.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            img.verify()
    except Exception:
        raise Problem(422, "That file is not a readable image.")
    if fmt not in MEDIA_TYPES:
        raise Problem(422, "Send the bill as a JPEG, PNG or WebP photo.")
    return MEDIA_TYPES[fmt]


# --- Members -----------------------------------------------------------------------

def _find_member(conn, hanu_id: str):
    return conn.execute(
        "SELECT * FROM members WHERE REPLACE(hanu_id, '-', '') = ?",
        (db.name_key(hanu_id).upper(),),
    ).fetchone()


def member_login(hanu_id: str, pin: str) -> dict:
    """Check a Hanu ID and PIN and start a session. Five wrong PINs lock the ID for a while.

    An unknown ID and a wrong PIN get the same answer, so the page cannot be used to
    find out which Hanu IDs exist.
    """
    now = datetime.now(timezone.utc)
    failure = Problem(401, "That Hanu ID and PIN do not match.")
    with db.tx() as conn:
        member = _find_member(conn, hanu_id)
        if member is None:
            outcome = failure
        elif member["locked_until"] and member["locked_until"] > now.isoformat(timespec="seconds"):
            outcome = Problem(429, f"Too many wrong PINs. Try again in {LOCKOUT_MINUTES} minutes "
                                   "or contact the Hanu team.")
        elif db.check_pin(member["pin_hash"], pin):
            token = secrets.token_urlsafe(32)
            conn.execute("UPDATE members SET failed_logins = 0, locked_until = NULL WHERE id = ?",
                         (member["id"],))
            conn.execute(
                "INSERT INTO member_sessions (token_hash, member_id, expires_at) VALUES (?,?,?)",
                (_token_hash(token), member["id"],
                 (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
            outcome = {"token": token, "name": member["name"]}
        else:
            failed = member["failed_logins"] + 1
            locked = None
            if failed >= MAX_FAILED_LOGINS:
                failed = 0
                locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
                db.audit(conn, None, f"member:{member['hanu_id']}", "login_locked", {})
            conn.execute("UPDATE members SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (failed, locked, member["id"]))
            outcome = failure
    # Raised only after the transaction commits, so the failed attempt is recorded.
    if isinstance(outcome, Problem):
        raise outcome
    return outcome


def member_from_token(token: str | None):
    if not token:
        raise Problem(401, "Please sign in.")
    conn = db.connect()
    try:
        member = conn.execute(
            "SELECT m.* FROM member_sessions s JOIN members m ON m.id = s.member_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?", (_token_hash(token), db.now())).fetchone()
    finally:
        conn.close()
    if member is None:
        raise Problem(401, "Please sign in again.")
    return member


def member_logout(token: str | None) -> None:
    if token:
        with db.tx() as conn:
            conn.execute("DELETE FROM member_sessions WHERE token_hash = ?", (_token_hash(token),))


def member_home(member) -> dict:
    """What a member sees: their balance and their bills. No flags, no reader output."""
    conn = db.connect()
    try:
        bills = []
        for row in conn.execute(
                "SELECT c.id, c.status, c.total_kobo, c.paid_kobo, c.created_at, c.decision_reason, "
                "cl.name AS clinic_name FROM cases c LEFT JOIN clinics cl ON cl.id = c.clinic_id "
                "WHERE c.member_id = ? ORDER BY c.id DESC LIMIT 50", (member["id"],)):
            bill = dict(row)
            if bill["status"] != "denied":
                bill["decision_reason"] = None  # reviewer notes on approvals are internal
            bills.append(bill)
        return {
            "name": member["name"],
            "hanu_id": member["hanu_id"],
            "lock_until": member["lock_until"],
            "balance_kobo": db.balance_kobo(conn, member["id"]),
            "bills": bills,
        }
    finally:
        conn.close()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
