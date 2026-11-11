"""SQLite storage: members, clinics, the ledger, bill cases and the audit log.

Money is stored as integer kobo. A member's balance is the sum of their ledger rows.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    hanu_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT,
    lock_until TEXT
);
CREATE TABLE IF NOT EXISTS clinics (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    is_partner INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    member_id INTEGER REFERENCES members(id),
    hanu_id_submitted TEXT NOT NULL,
    image_file TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    clinic_id INTEGER REFERENCES clinics(id),
    bill_date TEXT,
    total_kobo INTEGER,
    extraction_json TEXT,
    extractor_model TEXT,
    rules_version TEXT NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN ('approve', 'needs_review')),
    flags_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('needs_review', 'approved', 'denied')),
    paid_kobo INTEGER,
    decided_by TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
-- One payment per case: the UNIQUE on case_id is what stops a retry paying twice.
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id),
    kind TEXT NOT NULL CHECK (kind IN ('contribution', 'payment')),
    amount_kobo INTEGER NOT NULL,
    case_id INTEGER UNIQUE REFERENCES cases(id),
    clinic_id INTEGER REFERENCES clinics(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    case_id INTEGER REFERENCES cases(id),
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_sessions (
    token_hash TEXT PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_status ON cases(status, created_at);
CREATE INDEX IF NOT EXISTS ledger_member ON ledger(member_id);
"""

# Columns added to members after the first version; init() adds any that are missing.
MEMBER_COLUMNS = {
    "pin_hash": "TEXT",
    "failed_logins": "INTEGER NOT NULL DEFAULT 0",
    "locked_until": "TEXT",
}

# PIN for the invented demo members only (see seed_demo). Real members get their own.
DEMO_PIN = "4821"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def name_key(text: str) -> str:
    """Normalise a clinic name or Hanu ID for matching: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.data_dir() / "hanu.db", isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    """One write transaction. BEGIN IMMEDIATE makes concurrent decisions queue up, not race."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    have = {row["name"] for row in conn.execute("PRAGMA table_info(members)")}
    for column, kind in MEMBER_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE members ADD COLUMN {column} {kind}")
    conn.close()
    if config.seed_demo():
        seed_demo()


def balance_kobo(conn, member_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_kobo), 0) AS b FROM ledger WHERE member_id = ?", (member_id,)
    ).fetchone()
    return row["b"]


def audit(conn, case_id, actor: str, event: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (case_id, actor, event, detail_json, created_at) VALUES (?,?,?,?,?)",
        (case_id, actor, event, json.dumps(detail, ensure_ascii=False), now()),
    )


PIN_ITERATIONS = 200_000  # the tests lower this so they run quickly


def hash_pin(pin: str, salt: str | None = None, iterations: int | None = None) -> str:
    """Stored as "iterations$salt$digest". A short PIN cannot be made strong by hashing;
    the login lockout is what protects it."""
    salt = salt or secrets.token_hex(16)
    iterations = iterations or PIN_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), iterations).hex()
    return f"{iterations}${salt}${digest}"


def check_pin(stored: str | None, pin: str) -> bool:
    try:
        iterations, salt, _digest = stored.split("$")
        return hmac.compare_digest(stored, hash_pin(pin, salt, int(iterations)))
    except (AttributeError, ValueError):
        return False  # no PIN set, or a hash in an unknown format


def add_member(conn, hanu_id: str, name: str, contribution_naira: int = 0, lock_until=None,
               pin: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO members (hanu_id, name, lock_until, pin_hash) VALUES (?,?,?,?)",
        (hanu_id, name, lock_until, hash_pin(pin) if pin else None),
    )
    if contribution_naira:
        conn.execute(
            "INSERT INTO ledger (member_id, kind, amount_kobo, created_at) VALUES (?,?,?,?)",
            (cur.lastrowid, "contribution", contribution_naira * 100, now()),
        )
    return cur.lastrowid


def add_clinic(conn, name: str, is_partner: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO clinics (name, name_key, is_partner) VALUES (?,?,?)",
        (name, name_key(name), int(is_partner)),
    )
    return cur.lastrowid


def seed_demo() -> None:
    """Invented members and clinics for local testing. Adds nothing if any member exists."""
    demo_ids = ("HN-4821-0937", "HN-1107-5520")
    with tx() as conn:
        if conn.execute("SELECT 1 FROM members LIMIT 1").fetchone():
            # A demo database made before PINs existed: give the demo members theirs.
            conn.execute(
                "UPDATE members SET pin_hash = ? WHERE hanu_id IN (?, ?) AND "
                "(pin_hash IS NULL OR LENGTH(pin_hash) - LENGTH(REPLACE(pin_hash, '$', '')) != 2)",
                (hash_pin(DEMO_PIN), *demo_ids))
            return
        add_member(conn, demo_ids[0], "Amina Yusuf", 42000, "2027-05-31", DEMO_PIN)
        add_member(conn, demo_ids[1], "Chinedu Okafor", 6000, "2027-02-28", DEMO_PIN)
        add_clinic(conn, "Sabon Gari Family Clinic")
        add_clinic(conn, "Unity Diagnostics")
        add_clinic(conn, "Riverside Medical Centre", is_partner=False)
