"""Storage: members, staff, clinics, the ledger, bill cases, bill photos and the audit log.

Runs on Postgres when DATABASE_URL is set (Neon, for example) and on a local SQLite
file otherwise. Queries are written once, with "?" placeholders, and work on both.

Money is stored as integer kobo. A member's balance is the sum of their ledger rows.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA_VERSION = 2  # SQLite only: an older local file is set aside and a fresh one started

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id {pk},
    hanu_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT UNIQUE,
    lock_until TEXT,
    secret_hash TEXT,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff (
    id {pk},
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('member', 'staff')),
    subject_id BIGINT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clinics (
    id {pk},
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    is_partner INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bill_images (
    sha256 TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    data {blob} NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id {pk},
    member_id BIGINT REFERENCES members(id),
    hanu_id_submitted TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    clinic_id BIGINT REFERENCES clinics(id),
    bill_date TEXT,
    total_kobo BIGINT,
    extraction_json TEXT,
    extractor_model TEXT,
    rules_version TEXT NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN ('approve', 'needs_review')),
    flags_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('needs_review', 'approved', 'denied')),
    paid_kobo BIGINT,
    decided_by TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id {pk},
    member_id BIGINT NOT NULL REFERENCES members(id),
    kind TEXT NOT NULL CHECK (kind IN ('contribution', 'payment')),
    amount_kobo BIGINT NOT NULL,
    case_id BIGINT UNIQUE REFERENCES cases(id),
    clinic_id BIGINT REFERENCES clinics(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id {pk},
    case_id BIGINT REFERENCES cases(id),
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_status ON cases(status, created_at);
CREATE INDEX IF NOT EXISTS cases_member ON cases(member_id);
CREATE INDEX IF NOT EXISTS ledger_member ON ledger(member_id)
"""
# ledger.case_id is UNIQUE: one payment per case, so a retry can never pay twice.

# PIN for the invented demo members only (see seed_demo). Real members choose their own.
DEMO_PIN = "4821"
HASH_ITERATIONS = 200_000  # the tests lower this so they run quickly


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def name_key(text: str) -> str:
    """Normalise a clinic name or Hanu ID for matching: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# --- Connections -------------------------------------------------------------------

class Conn:
    """The small part of a database connection the app uses, the same on both engines."""

    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def execute(self, sql: str, args=()):
        return self.raw.execute(sql.replace("?", "%s") if self.pg else sql, args)

    def commit(self):
        self.raw.commit() if self.pg else self.raw.execute("COMMIT")

    def rollback(self):
        if self.pg:
            self.raw.rollback()
        elif self.raw.in_transaction:
            self.raw.execute("ROLLBACK")

    def close(self):
        if self.pg:
            self.raw.rollback()  # end any read transaction before the connection is reused
            _pool().putconn(self.raw)
        else:
            self.raw.close()


_pools: dict = {}
_pool_lock = threading.Lock()


def _pool():
    url = config.database_url()
    with _pool_lock:
        if url not in _pools:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            _pools[url] = ConnectionPool(
                url, min_size=0, max_size=5, open=True,
                kwargs={"row_factory": dict_row},
                # Hosted databases drop idle connections; test each one before handing it out.
                check=ConnectionPool.check_connection,
            )
        return _pools[url]


def connect() -> Conn:
    if config.database_url():
        return Conn(_pool().getconn(), pg=True)
    raw = sqlite3.connect(config.data_dir() / "hanu.db", isolation_level=None, timeout=10)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw, pg=False)


@contextmanager
def tx():
    """One write transaction, committed on success and rolled back on any error."""
    conn = connect()
    try:
        if not conn.pg:
            conn.execute("BEGIN IMMEDIATE")  # SQLite: writers queue up instead of racing
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def lock_row(conn: Conn, table: str, row_id: int):
    """Fetch a row and, on Postgres, hold it so two requests cannot change it at once.
    (SQLite's write transaction already locks the whole database.)"""
    assert table in ("members", "cases", "staff")
    return conn.execute(
        f"SELECT * FROM {table} WHERE id = ?" + (" FOR UPDATE" if conn.pg else ""), (row_id,)
    ).fetchone()


def init() -> None:
    if not config.database_url():
        _set_aside_old_sqlite()
    with tx() as conn:
        pk = "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY" if conn.pg else "INTEGER PRIMARY KEY"
        for statement in SCHEMA.format(pk=pk, blob="BYTEA" if conn.pg else "BLOB").split(";"):
            conn.execute(statement)
        if not conn.pg:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _ensure_admin(conn)
    if config.seed_demo():
        seed_demo()


def _set_aside_old_sqlite() -> None:
    path = config.data_dir() / "hanu.db"
    if not path.exists():
        return
    raw = sqlite3.connect(path)
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    raw.close()
    if version != SCHEMA_VERSION:
        path.replace(path.with_name(f"hanu-v{version}-{datetime.now():%Y%m%d%H%M%S}.db"))


# --- Small helpers -----------------------------------------------------------------

def balance_kobo(conn: Conn, member_id: int) -> int:
    row = conn.execute(
        "SELECT CAST(COALESCE(SUM(amount_kobo), 0) AS BIGINT) AS b FROM ledger WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    return int(row["b"])


def audit(conn: Conn, case_id, actor: str, event: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (case_id, actor, event, detail_json, created_at) VALUES (?,?,?,?,?)",
        (case_id, actor, event, json.dumps(detail, ensure_ascii=False), now()),
    )


def hash_secret(secret: str, salt: str | None = None, iterations: int | None = None) -> str:
    """Hash a PIN or password, stored as "iterations$salt$digest". A short PIN cannot be
    made strong by hashing; the login lockout is what protects it."""
    salt = salt or secrets.token_hex(16)
    iterations = iterations or HASH_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), iterations).hex()
    return f"{iterations}${salt}${digest}"


def check_secret(stored: str | None, secret: str) -> bool:
    try:
        iterations, salt, _digest = stored.split("$")
        return hmac.compare_digest(stored, hash_secret(secret, salt, int(iterations)))
    except (AttributeError, ValueError):
        return False  # nothing set, or a hash in an unknown format


def add_member(conn: Conn, hanu_id: str, name: str, contribution_naira: int = 0, lock_until=None,
               pin: str | None = None, phone: str | None = None) -> int:
    member_id = conn.execute(
        "INSERT INTO members (hanu_id, name, phone, lock_until, secret_hash, created_at) "
        "VALUES (?,?,?,?,?,?) RETURNING id",
        (hanu_id, name, phone, lock_until, hash_secret(pin) if pin else None, now()),
    ).fetchone()["id"]
    if contribution_naira:
        conn.execute(
            "INSERT INTO ledger (member_id, kind, amount_kobo, created_at) VALUES (?,?,?,?)",
            (member_id, "contribution", contribution_naira * 100, now()),
        )
    return member_id


def add_clinic(conn: Conn, name: str, is_partner: bool = True) -> int:
    return conn.execute(
        "INSERT INTO clinics (name, name_key, is_partner) VALUES (?,?,?) RETURNING id",
        (name, name_key(name), int(is_partner)),
    ).fetchone()["id"]


def add_staff(conn: Conn, email: str, name: str, password: str) -> int:
    return conn.execute(
        "INSERT INTO staff (email, name, secret_hash, created_at) VALUES (?,?,?,?) RETURNING id",
        (email.strip().lower(), name, hash_secret(password), now()),
    ).fetchone()["id"]


def _ensure_admin(conn: Conn) -> None:
    """Create the first staff account from HANU_ADMIN_EMAIL / HANU_ADMIN_PASSWORD, once."""
    admin = config.admin_account()
    if not admin:
        return
    email, password, name = admin
    if not conn.execute("SELECT 1 FROM staff WHERE email = ?", (email,)).fetchone():
        add_staff(conn, email, name, password)


def seed_demo() -> None:
    """Invented members and clinics for trying the product. Adds nothing if any member exists."""
    with tx() as conn:
        if conn.execute("SELECT 1 FROM members LIMIT 1").fetchone():
            return
        add_member(conn, "HN-4821-0937", "Amina Yusuf", 42000, "2027-05-31", DEMO_PIN)
        add_member(conn, "HN-1107-5520", "Chinedu Okafor", 6000, "2027-02-28", DEMO_PIN)
        add_clinic(conn, "Sabon Gari Family Clinic")
        add_clinic(conn, "Unity Diagnostics")
        add_clinic(conn, "Riverside Medical Centre", is_partner=False)
