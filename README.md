# Email Automation Tool (MVP)

Self-hosted cold/warm email outreach: upload a list, write drafts, set the pace —
the tool sends from YOUR email accounts via SMTP and tells you who replied.

Built to spec: `~/workspace/your_files/email-tool-build-spec.md` (§4.1–4.6).

## Run it

```bash
cd ~/workspace/email-tool
./.venv/bin/python run.py
# open http://localhost:8000
```

First run creates `data/emailtool.db` (SQLite) and `data/.key` (Fernet key, chmod 600).

Install deps (already done in `.venv`):
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Run without the background worker (for testing):
```bash
EMAILTOOL_NO_WORKER=1 .venv/bin/python run.py
```

Run the acceptance tests:
```bash
.venv/bin/python -m pytest tests/ -x -q
# or: .venv/bin/python tests/test_acceptance.py
```

## What's inside

- `app/main.py` — FastAPI app, all routes (server-rendered Jinja2 UI, no build step)
- `app/worker.py` — background thread: send queue (gap pacing, windows, daily caps,
  warm-up, auto-pause) + IMAP reply watcher (5-min poll, classification)
- `app/mail.py` — SMTP/IMAP, templating, reply classifier, SPF/DKIM/DMARC lookups
- `app/db.py` — SQLite schema + data layer (swappable for Postgres)
- `app/crypto.py` — Fernet encryption for stored credentials
- `app/templates/`, `app/static/` — UI

## Key behaviors

- **Idempotent sends**: a contact is claimed atomically; `UNIQUE(campaign_id, contact_id)`.
  Kill the server mid-send → on restart, stuck rows are failed and released; nobody is double-sent.
- **Auto-pause**: bounce rate > 8% or 2× SMTP auth failures → campaign pauses with a banner reason.
- **Suppression is global**: unsubscribe reply / one-click link / manual add → never emailed again by any campaign.
- **Warm-up**: accounts < 30 days old or < 50 sent are capped at 20/day +10/day unless overridden
  by typing `I UNDERSTAND`.
- Every email carries a one-click unsubscribe link + your physical mailing address in the footer.

## Not in MVP (per spec §9)

No list scraping, no AI copywriting, no teams, no mobile app. Follow-ups (§4.7) are
Phase 2 — the data model (`campaign_contacts.status`, `replies.category`) already supports them.
