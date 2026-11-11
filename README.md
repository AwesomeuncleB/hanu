# Hanu

Hanu is a healthcare savings and payment platform. It is not insurance. People save for
their own healthcare, alone or through a cooperative, and Hanu pays partner clinics
directly for consultations and lab tests.

This repository holds an early prototype.

## What is here

- `backend/` — one service that serves everything:
  - **Landing page** (`hanu/static/landing.html`, `styles.css`), at `/`.
  - **Ledger** (`hanu/db.py`): members, clinics, balances and an append-only audit log, in SQLite.
  - **Bill reader** (`hanu/extract.py`): a vision model on Groq turns a photo of a clinic
    bill into structured data. It only transcribes; it cannot approve, pay or deny.
  - **Rules** (`hanu/rules.py`): written, versioned checks that either approve a bill or
    refer it to a person. The rules never deny.
  - **Review queue** (`hanu/static/review.html`): where a person sees the bill, what was
    read, the flags, and approves or denies. A denial always needs a recorded reason.
  - **Member page** (`hanu/static/member.html`): a mobile page where a member signs in with
    their Hanu ID and PIN, sees their balance, sends a bill photo and follows their bills.

## What is not built yet

- WhatsApp. Members use the mobile page.
- A way to enrol members and set PINs. Only the demo members exist.
- Real payouts to clinics. Approving a bill debits the member's balance and records what the clinic is owed.
- Collecting contributions, cooperatives and groups, and savings locks.
- Reviewer accounts. An optional shared token protects the API.

## Run it

```bash
pip install -r backend/requirements.txt
python backend/dev.py
```

Then open http://127.0.0.1:8000 for the landing page, `/review` for the review queue and
`/member` for the member page. This loads invented demo members and clinics; their PIN is `DEMO_PIN` in
`backend/hanu/db.py`.

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

## Deploying

`pyproject.toml` points Vercel at the app. Set these environment variables in the project:
`GROQ_API_KEY`, `HANU_REVIEW_TOKEN` (protects the review queue) and, for a demo,
`HANU_SEED_DEMO=1`.

The service stores its database and bill photos on disk. On Vercel that disk is temporary:
data is wiped whenever the function restarts, and two running copies do not share it. That
is acceptable for a demo only. For real use, host it where the disk persists or move the
data to a hosted database first.
