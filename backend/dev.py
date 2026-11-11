"""Run the review service locally:  python backend/dev.py

Loads demo members and clinics into an empty database. With no GROQ_API_KEY
in the environment it uses the mock bill reader, which returns a canned result.
"""
import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("HANU_SEED_DEMO", "1")
if not os.environ.get("GROQ_API_KEY"):
    os.environ.setdefault("HANU_EXTRACTOR", "mock")
    if os.environ["HANU_EXTRACTOR"] == "mock":
        print("No GROQ_API_KEY set: using the MOCK bill reader (canned results).")

if __name__ == "__main__":
    uvicorn.run("hanu.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
