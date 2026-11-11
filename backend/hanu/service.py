"""The bill pipeline and the accounts around it.

A bill is received, read, and judged by the written rules. Two things can end a case:
the rules approving a clean bill (only when shadow mode is off), or a signed-in member
of staff approving or denying it. Both go through the same payment code, and everything
is written to the audit log.
"""
import hashlib
import io
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from PIL import Image

from . import config, db, extract, rules

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
SESSION_DAYS = 7


class Problem(Exception):
    """A request that cannot be carried out; becomes an HTTP error with this message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def kobo(naira) -> int | None:
    return None if naira is None else round(float(naira) * 100)


# --- Bills -------------------------------------------------------------------------

def submit_bill(hanu_id: str, image_bytes: bytes) -> dict:
    media_type = _check_image(image_bytes)
    sha = hashlib.sha256(image_bytes).hexdigest()
    hanu_id = hanu_id.strip().upper()

    conn = db.connect()
    try:
        member = _find_member(conn, hanu_id)
        existing = _same_photo(conn, member, sha)
        if existing:
            return _receipt(conn, existing)
    finally:
        conn.close()

    # The slow model call happens outside any database transaction.
    result = extract.extract_bill(image_bytes, media_type)

    with db.tx() as conn:
        if member:
            db.lock_row(conn, "members", member["id"])  # one bill at a time per member
        case_id = _same_photo(conn, member, sha) or _open_case(conn, member, hanu_id, sha,
                                                               media_type, image_bytes, result)
    conn = db.connect()
    try:
        return _receipt(conn, case_id)
    finally:
        conn.close()


def _same_photo(conn, member, sha: str) -> int | None:
    """The same photo sent twice (a retry, say) is the same case."""
    if not member:
        return None
    row = conn.execute(
        "SELECT id FROM cases WHERE member_id = ? AND image_sha256 = ?", (member["id"], sha)
    ).fetchone()
    return row["id"] if row else None


def _open_case(conn, member, hanu_id, sha, media_type, image_bytes, result) -> int:
    ext = result.data
    conn.execute(
        "INSERT INTO bill_images (sha256, media_type, data) VALUES (?,?,?) "
        "ON CONFLICT (sha256) DO NOTHING", (sha, media_type, image_bytes))

    clinic, total_kobo, items_kobo, bill_date = None, None, 0, None
    if ext and ext["is_clinic_bill"]:
        clinic = conn.execute(
            "SELECT * FROM clinics WHERE name_key = ?", (db.name_key(ext["clinic_name"]),)
        ).fetchone()
        total_kobo = kobo(ext["total_naira"])
        items_kobo = sum(kobo(i["amount_naira"]) for i in ext["line_items"])
        bill_date = ext["bill_date"]

    duplicate = None
    if member and clinic and total_kobo and bill_date:
        duplicate = conn.execute(
            "SELECT id FROM cases WHERE member_id = ? AND clinic_id = ? AND bill_date = ? "
            "AND total_kobo = ? AND status != 'denied'",
            (member["id"], clinic["id"], bill_date, total_kobo),
        ).fetchone()

    verdict = rules.evaluate(rules.Context(
        extraction=ext,
        extraction_error=result.error,
        member_found=member is not None,
        member_hanu_key=db.name_key(member["hanu_id"]) if member else "",
        extracted_hanu_key=db.name_key(ext["hanu_id"]) if ext else "",
        clinic_is_partner=bool(clinic["is_partner"]) if clinic else None,
        total_kobo=total_kobo,
        line_items_kobo=items_kobo,
        balance_kobo=db.balance_kobo(conn, member["id"]) if member else 0,
        duplicate_of=duplicate["id"] if duplicate else None,
        today=date.today(),
        min_confidence=config.min_confidence(),
        auto_approve_cap_kobo=config.auto_approve_cap_kobo(),
    ))

    case_id = conn.execute(
        "INSERT INTO cases (member_id, hanu_id_submitted, image_sha256, clinic_id, bill_date, "
        "total_kobo, extraction_json, extractor_model, rules_version, recommendation, flags_json, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'needs_review', ?) RETURNING id",
        (member["id"] if member else None, hanu_id, sha, clinic["id"] if clinic else None,
         bill_date, total_kobo, json.dumps(ext, ensure_ascii=False) if ext else None, result.model,
         verdict["version"], verdict["recommendation"],
         json.dumps(verdict["flags"], ensure_ascii=False), db.now()),
    ).fetchone()["id"]

    db.audit(conn, case_id, "member", "bill_received", {"hanu_id": hanu_id, "image_sha256": sha})
    db.audit(conn, case_id, f"reader:{result.model}", "bill_read",
             {"extraction": ext, "error": result.error})
    db.audit(conn, case_id, f"rules:{verdict['version']}", "rules_applied",
             {**verdict, "shadow_mode": config.shadow_mode()})

    if verdict["recommendation"] == "approve" and not config.shadow_mode():
        _pay(conn, case_id, member["id"], clinic["id"], total_kobo,
             actor=f"rules:{verdict['version']}", reason="Passed every written rule.")
    return case_id


def decide(case_id: int, action: str, staff, reason: str = "",
           pay_amount_naira: float | None = None, clinic_id: int | None = None) -> dict:
    """A signed-in member of staff approves or denies a case. The name on the decision
    comes from their account, not from anything typed into the form."""
    actor = f"reviewer:{staff['name']}"
    reason = reason.strip()
    with db.tx() as conn:
        case = db.lock_row(conn, "cases", case_id)
        if not case:
            raise Problem(404, "No such case.")
        if case["status"] != "needs_review":
            raise Problem(409, f"This case was already {case['status']} by {case['decided_by']}.")

        if action == "deny":
            if len(reason) < 5:
                raise Problem(422, "A denial needs a reason the member can be given.")
            conn.execute(
                "UPDATE cases SET status = 'denied', decided_by = ?, decision_reason = ?, "
                "decided_at = ? WHERE id = ?", (actor, reason, db.now(), case_id))
            db.audit(conn, case_id, actor, "denied", {"reason": reason})
            return get_case(conn, case_id)

        if case["member_id"] is None:
            raise Problem(422, "No member is matched to this case, so there is no balance to pay from.")
        db.lock_row(conn, "members", case["member_id"])  # the balance cannot move under us
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

        _pay(conn, case_id, case["member_id"], clinic["id"], amount, actor=actor, reason=reason)
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
        for a in conn.execute("SELECT * FROM audit_log WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
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
    for row in conn.execute(sql, args).fetchall():
        item = dict(row)
        item["flag_count"] = len(json.loads(item.pop("flags_json")))
        out.append(item)
    return out


def case_image(case_id: int) -> tuple[bytes, str]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT i.data, i.media_type FROM cases c JOIN bill_images i ON i.sha256 = c.image_sha256 "
            "WHERE c.id = ?", (case_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise Problem(404, "No such case.")
    return bytes(row["data"]), row["media_type"]


def _receipt(conn, case_id: int) -> dict:
    """What the member is told. It never announces a denial: that message comes from a person."""
    case = get_case(conn, case_id)
    if case["status"] == "approved":
        message = (f"Paid {rules.naira(case['paid_kobo'])} to {case['clinic_name']}. "
                   f"Your balance is now {rules.naira(case['balance_kobo'])}.")
    elif case["status"] == "denied":
        message = "This bill has been reviewed. See the reason under your bills."
    else:
        message = "We have your bill. A member of the Hanu team is checking it and will message you shortly."
    return {"case_id": case_id, "status": case["status"], "message": message}


def _check_image(data: bytes) -> str:
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


# --- Accounts and sessions ---------------------------------------------------------

def _find_member(conn, hanu_id: str):
    return conn.execute(
        "SELECT * FROM members WHERE REPLACE(hanu_id, '-', '') = ?",
        (db.name_key(hanu_id).upper(),),
    ).fetchone()


def _sign_in(table: str, find, secret: str, mismatch: str) -> dict:
    """Check a secret and start a session. Five wrong tries lock the account for a while.

    An unknown account and a wrong secret get the same answer, so the page cannot be
    used to find out which accounts exist.
    """
    kind = {"members": "member", "staff": "staff"}[table]
    now = datetime.now(timezone.utc)
    failure = Problem(401, mismatch)
    with db.tx() as conn:
        found = find(conn)
        row = db.lock_row(conn, table, found["id"]) if found else None
        if row is None:
            outcome = failure
        elif row["locked_until"] and row["locked_until"] > now.isoformat(timespec="seconds"):
            outcome = Problem(429, f"Too many wrong tries. Try again in {LOCKOUT_MINUTES} minutes "
                                   "or contact the Hanu team.")
        elif db.check_secret(row["secret_hash"], secret):
            conn.execute(f"UPDATE {table} SET failed_logins = 0, locked_until = NULL WHERE id = ?",
                         (row["id"],))
            outcome = {"token": _start_session(conn, kind, row["id"]), "name": row["name"]}
        else:
            failed, locked = row["failed_logins"] + 1, None
            if failed >= MAX_FAILED_LOGINS:
                failed = 0
                locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
                db.audit(conn, None, f"{kind}:{row['id']}", "login_locked", {})
            conn.execute(f"UPDATE {table} SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (failed, locked, row["id"]))
            outcome = failure
    # Raised only after the transaction commits, so the failed try is recorded.
    if isinstance(outcome, Problem):
        raise outcome
    return outcome


def member_login(hanu_id: str, pin: str) -> dict:
    return _sign_in("members", lambda conn: _find_member(conn, hanu_id), pin,
                    "That Hanu ID and PIN do not match.")


def staff_login(email: str, password: str) -> dict:
    email = email.strip().lower()
    return _sign_in(
        "staff", lambda conn: conn.execute("SELECT * FROM staff WHERE email = ?", (email,)).fetchone(),
        password, "That email and password do not match.")


def member_signup(name: str, phone: str, pin: str, lock_months: int) -> dict:
    name = " ".join(name.split())
    digits = re.sub(r"\D", "", phone)
    if len(name) < 2:
        raise Problem(422, "Enter your full name.")
    if not 7 <= len(digits) <= 15:
        raise Problem(422, "Enter a valid phone number.")
    if not re.fullmatch(r"\d{4,6}", pin):
        raise Problem(422, "Choose a PIN of 4 to 6 digits.")
    if len(set(pin)) == 1 or pin in ("1234", "12345", "123456"):
        raise Problem(422, "That PIN is too easy to guess. Choose another.")
    if lock_months not in (6, 12):
        raise Problem(422, "Choose a lock of six months or one year.")

    today = date.today()
    years, month0 = divmod(today.month - 1 + lock_months, 12)
    lock_until = date(today.year + years, month0 + 1, min(today.day, 28)).isoformat()

    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM members WHERE phone = ?", (digits,)).fetchone():
            raise Problem(409, "This phone number already has a Hanu ID. Sign in instead.")
        while True:
            hanu_id = f"HN-{secrets.randbelow(10000):04d}-{secrets.randbelow(10000):04d}"
            if not _find_member(conn, hanu_id):
                break
        member_id = db.add_member(conn, hanu_id, name, lock_until=lock_until, pin=pin, phone=digits)
        db.audit(conn, None, f"member:{hanu_id}", "member_joined", {"lock_months": lock_months})
        return {"token": _start_session(conn, "member", member_id), "name": name, "hanu_id": hanu_id}


def _start_session(conn, kind: str, subject_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessions (token_hash, kind, subject_id, expires_at) VALUES (?,?,?,?)",
                 (_token_hash(token), kind, subject_id, expires))
    return token


def signed_in(kind: str, token: str | None):
    """The member or staff row for a session token, or a 401."""
    table = {"member": "members", "staff": "staff"}[kind]
    if not token:
        raise Problem(401, "Please sign in.")
    conn = db.connect()
    try:
        row = conn.execute(
            f"SELECT t.* FROM sessions s JOIN {table} t ON t.id = s.subject_id "
            "WHERE s.token_hash = ? AND s.kind = ? AND s.expires_at > ?",
            (_token_hash(token), kind, db.now())).fetchone()
    finally:
        conn.close()
    if row is None:
        raise Problem(401, "Please sign in again.")
    return row


def sign_out(token: str | None) -> None:
    if token:
        with db.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def member_home(member) -> dict:
    """What a member sees: their balance and their bills. No flags, no reader output."""
    conn = db.connect()
    try:
        bills = []
        for row in conn.execute(
                "SELECT c.id, c.status, c.total_kobo, c.paid_kobo, c.created_at, c.decision_reason, "
                "cl.name AS clinic_name FROM cases c LEFT JOIN clinics cl ON cl.id = c.clinic_id "
                "WHERE c.member_id = ? ORDER BY c.id DESC LIMIT 50", (member["id"],)).fetchall():
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
