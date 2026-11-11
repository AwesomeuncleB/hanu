# Hanu

Hanu is a healthcare savings and payment platform. It is not insurance. People save for
their own healthcare, alone or through a cooperative, and Hanu pays partner clinics
directly for consultations and lab tests.

This repository holds an early prototype.

## What is here

- `index.html`, `styles.css` — the public landing page.
- `backend/` — the bill review service:
  - **Ledger** (`hanu/db.py`): members, clinics, balances and an append-only audit log, in SQLite.
  - **Bill reader** (`hanu/extract.py`): a vision model on Groq turns a photo of a clinic
    bill into structured data. It only transcribes; it cannot approve, pay or deny.
  - **Rules** (`hanu/rules.py`): written, versioned checks that either approve a bill or
    refer it to a person. The rules never deny.
  - **Review queue** (`hanu/static/review.html`): where a person sees the bill, what was
    read, the flags, and approves or denies. A denial always needs a recorded reason.

## What is not built yet

- A member-facing channel (WhatsApp or a mobile page). Bills are submitted from the review page.
- Real payouts to clinics. Approving a bill debits the member's balance and records what the clinic is owed.
- Collecting contributions, cooperatives and groups, and savings locks.
- Reviewer accounts. An optional shared token protects the API.

## Run it

```bash
pip install -r backend/requirements.txt
python backend/dev.py
```

Then open http://127.0.0.1:8000. This loads invented demo members and clinics.

Set `GROQ_API_KEY` to use the real bill reader. Without it the service uses a mock reader
that returns the same canned bill for every photo. Other settings are listed at the top of
`backend/hanu/config.py`.

By default the service runs in shadow mode: every bill goes to a person, and the rules
only recommend.

## Tests

```bash
cd backend
python -m pytest -q
```
