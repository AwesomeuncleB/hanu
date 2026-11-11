"""Run Hanu locally:  python backend/dev.py

Reads settings from backend/.env if it exists (KEY=value per line; the file is not
committed). Loads demo members and clinics into an empty database, and creates a local
staff account for the review queue. With no GROQ_API_KEY it uses the mock bill reader,
which returns a canned result.
"""
import os
import sys
from pathlib import Path

import uvicorn

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

env_file = HERE / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

os.environ.setdefault("HANU_SEED_DEMO", "1")
# Local-only staff account. A deployment must set its own HANU_ADMIN_EMAIL and
# HANU_ADMIN_PASSWORD; this script is never what runs in production.
os.environ.setdefault("HANU_ADMIN_EMAIL", "reviewer@hanu.test")
os.environ.setdefault("HANU_ADMIN_PASSWORD", "local-review-only")
os.environ.setdefault("HANU_ADMIN_NAME", "Local Reviewer")
if not os.environ.get("GROQ_API_KEY"):
    os.environ.setdefault("HANU_EXTRACTOR", "mock")
    if os.environ["HANU_EXTRACTOR"] == "mock":
        print("No GROQ_API_KEY set: using the MOCK bill reader (canned results).")
print("Database:", "Postgres (DATABASE_URL)" if os.environ.get("DATABASE_URL") else "local SQLite file")

if __name__ == "__main__":
    uvicorn.run("hanu.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
