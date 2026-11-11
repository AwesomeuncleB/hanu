"""Settings, read from the environment at call time.

DATABASE_URL                  Postgres connection string (Neon, for example). When unset, the
                              service uses a local SQLite file in HANU_DATA_DIR.
HANU_DATA_DIR                 where the SQLite file lives (default backend/data; on Vercel
                              /tmp/hanu, which is wiped whenever the function restarts)
HANU_ADMIN_EMAIL              with HANU_ADMIN_PASSWORD, creates the first staff account at
HANU_ADMIN_PASSWORD           startup if it does not exist yet. HANU_ADMIN_NAME is optional.
HANU_SHADOW_MODE              "1" (default): every bill goes to a human, the rules only recommend.
                              "0": clean bills are approved by the rules without review.
HANU_AUTO_APPROVE_CAP_NAIRA   largest bill the rules may approve on their own (default 20000)
HANU_MIN_CONFIDENCE           lowest extraction confidence the rules accept (default 0.85)
HANU_EXTRACTOR                "groq" (default) or "mock" (canned result, no API call)
HANU_MODEL                    Groq vision model for the bill reader (default qwen/qwen3.8-27b)
GROQ_API_KEY                  key for the bill reader
HANU_SEED_DEMO                "1" to load demo members and clinics into an empty database
"""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def data_dir() -> Path:
    # Vercel's disk is read-only apart from /tmp, and /tmp does not survive a restart.
    default = Path("/tmp/hanu") if os.environ.get("VERCEL") else BACKEND_DIR / "data"
    path = Path(os.environ.get("HANU_DATA_DIR", default))
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_is_temporary() -> bool:
    return bool(os.environ.get("VERCEL")) and not database_url() and "HANU_DATA_DIR" not in os.environ


def admin_account() -> tuple[str, str, str] | None:
    email = os.environ.get("HANU_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("HANU_ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    return email, password, os.environ.get("HANU_ADMIN_NAME", "").strip() or email.split("@")[0]


def shadow_mode() -> bool:
    return os.environ.get("HANU_SHADOW_MODE", "1") != "0"


def auto_approve_cap_kobo() -> int:
    return int(os.environ.get("HANU_AUTO_APPROVE_CAP_NAIRA", "20000")) * 100


def min_confidence() -> float:
    return float(os.environ.get("HANU_MIN_CONFIDENCE", "0.85"))


def extractor() -> str:
    return os.environ.get("HANU_EXTRACTOR", "groq")


def model() -> str:
    return os.environ.get("HANU_MODEL", "qwen/qwen3.8-27b")


def seed_demo() -> bool:
    return os.environ.get("HANU_SEED_DEMO", "0") == "1"
