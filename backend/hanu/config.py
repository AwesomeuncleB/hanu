"""Settings, read from the environment at call time.

HANU_DATA_DIR                 where the database and uploaded bills live (default backend/data;
                              on Vercel /tmp/hanu, which is wiped whenever the function restarts)
HANU_SHADOW_MODE              "1" (default): every bill goes to a human, the rules only recommend.
                              "0": clean bills are approved by the rules without review.
HANU_AUTO_APPROVE_CAP_NAIRA   largest bill the rules may approve on their own (default 20000)
HANU_MIN_CONFIDENCE           lowest extraction confidence the rules accept (default 0.85)
HANU_EXTRACTOR                "groq" (default) or "mock" (canned result, no API call)
HANU_MODEL                    Groq vision model for the bill reader (default qwen/qwen3.8-27b)
HANU_REVIEW_TOKEN             if set, the API requires it in the X-Review-Token header
HANU_SEED_DEMO                "1" to load demo members and clinics into an empty database
"""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    # Vercel's disk is read-only apart from /tmp, and /tmp does not survive a restart.
    default = Path("/tmp/hanu") if os.environ.get("VERCEL") else BACKEND_DIR / "data"
    path = Path(os.environ.get("HANU_DATA_DIR", default))
    (path / "uploads").mkdir(parents=True, exist_ok=True)
    return path


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


def review_token() -> str | None:
    return os.environ.get("HANU_REVIEW_TOKEN") or None


def seed_demo() -> bool:
    return os.environ.get("HANU_SEED_DEMO", "0") == "1"


def storage_is_temporary() -> bool:
    return bool(os.environ.get("VERCEL")) and "HANU_DATA_DIR" not in os.environ
