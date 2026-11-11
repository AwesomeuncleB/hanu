# Hanu

Hanu is a healthcare savings and payment platform. It is not insurance. People save for
their own healthcare, alone or through a cooperative, and Hanu pays partner clinics
directly for consultations and lab tests.

AI agents read each bill and flag anything that looks wrong. Written rules judge the
result, and only a person can refuse a payment.

This repository holds an early prototype.

## What is here

One service (`backend/`) serves everything:

| Page | Path | Who it is for |
|---|---|---|
| Landing page | `/` | Everyone |
| Member page | `/member` | Members: create a Hanu ID, sign in, see balance, send a bill |
| Review queue | `/review` | Staff: sign in, see each bill, approve or deny |

Behind the pages:

- **Bill reader** (`hanu/extract.py`): a vision model on Groq turns a photo of a clinic
  bill into structured data. It only transcribes; it cannot approve, pay or deny.
- **Rules** (`hanu/rules.py`): written, versioned checks that either approve a bill or
  refer it to a person. The rules never deny.
- **Review and payment** (`hanu/service.py`): a signed-in member of staff approves or
  denies. A denial always needs a reason, which the member sees. Every step is written to
  an audit log.
- **Storage** (`hanu/db.py`): members, staff, clinics, the ledger, cases and bill photos.
  Postgres when `DATABASE_URL` is set, a local SQLite file otherwise.

## What is not built yet

- The price agent and the reminder agent shown on the landing page.
- Real payouts to clinics. Approving a bill debits the member's balance and records what the clinic is owed.
- Collecting contributions, cooperatives and groups. A new member starts with a zero balance.
- Phone verification at sign-up, PIN reset, and more than one staff account from the interface.
- WhatsApp. Members use the mobile page.

## Run it locally

```bash
pip install -r backend/requirements.txt
python backend/dev.py
```

Then open http://127.0.0.1:8000. This uses a local SQLite file and loads invented demo
members and clinics. The demo members' PIN is `DEMO_PIN` in `backend/hanu/db.py`, and the
local staff account is set at the top of `backend/dev.py`.

Settings go in `backend/.env` (not committed), one `KEY=value` per line. All of them are
listed at the top of `backend/hanu/config.py`. Without `GROQ_API_KEY` the service uses a
mock reader that returns the same canned bill for every photo.

By default the service runs in shadow mode: every bill goes to a person, and the rules
only recommend.

## Deploying

`pyproject.toml` points Vercel at the app. Set these environment variables in the project:

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Postgres connection string, for example from Neon. Tables are created on first start. |
| `GROQ_API_KEY` | Key for the bill reader. |
| `HANU_ADMIN_EMAIL`, `HANU_ADMIN_PASSWORD` | The first staff account, created on first start. |
| `HANU_SEED_DEMO` | Set to `1` only for a demo: it adds demo members whose PIN is public in this repository. |

Without `DATABASE_URL`, a Vercel deployment keeps its data on a temporary disk that is
wiped whenever the function restarts.

## Tests

```bash
cd backend
python -m pytest -q
```

Set `HANU_TEST_DATABASE_URL` to run the same tests on Postgres. Every table in that
database is dropped first, so point it at a throwaway database only.
