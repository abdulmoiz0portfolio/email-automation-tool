"""SQLite data layer. Swappable: all access goes through the functions here,
so a Postgres backend can replace this module without touching routes/worker."""
import os
import sqlite3
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "emailtool.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    email TEXT NOT NULL,
    smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL DEFAULT 587,
    smtp_user TEXT NOT NULL, smtp_pass_enc TEXT NOT NULL, smtp_enc TEXT NOT NULL DEFAULT 'STARTTLS',
    imap_host TEXT NOT NULL, imap_port INTEGER NOT NULL DEFAULT 993,
    imap_user TEXT NOT NULL, imap_pass_enc TEXT NOT NULL, imap_enc TEXT NOT NULL DEFAULT 'SSL',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_error TEXT, last_tested_at TEXT,
    created_at TEXT NOT NULL,
    warmup_override INTEGER NOT NULL DEFAULT 0,
    auth_fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contact_lists(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES contact_lists(id) ON DELETE CASCADE,
    email TEXT NOT NULL, first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
    company TEXT DEFAULT '', custom_1 TEXT DEFAULT '', custom_2 TEXT DEFAULT '', custom_3 TEXT DEFAULT '',
    valid INTEGER NOT NULL DEFAULT 1, do_not_contact INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_list ON contacts(list_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE TABLE IF NOT EXISTS invalid_rows(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES contact_lists(id) ON DELETE CASCADE,
    row_data TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, subject TEXT NOT NULL, body_html TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(draft_id, version)
);
CREATE TABLE IF NOT EXISTS campaigns(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    list_id INTEGER NOT NULL REFERENCES contact_lists(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    draft_version INTEGER NOT NULL,
    draft_b_id INTEGER REFERENCES drafts(id),
    draft_b_version INTEGER, ab_split INTEGER NOT NULL DEFAULT 50,
    days_of_week TEXT NOT NULL DEFAULT '0,1,2,3,4',
    window_start TEXT NOT NULL DEFAULT '09:00', window_end TEXT NOT NULL DEFAULT '17:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Karachi',
    gap_min_sec INTEGER NOT NULL DEFAULT 60, gap_max_sec INTEGER NOT NULL DEFAULT 180,
    daily_cap INTEGER NOT NULL DEFAULT 80,
    start_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    pause_reason TEXT,
    mailing_address TEXT NOT NULL DEFAULT '',
    next_due_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_contacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    draft_version INTEGER, variant TEXT NOT NULL DEFAULT 'A',
    sent_at TEXT, reply_category TEXT,
    UNIQUE(campaign_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_cc_campaign ON campaign_contacts(campaign_id, status);
CREATE TABLE IF NOT EXISTS sends(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    draft_version INTEGER NOT NULL, variant TEXT NOT NULL DEFAULT 'A',
    message_id TEXT UNIQUE, unsub_token TEXT UNIQUE,
    sent_at TEXT, status TEXT NOT NULL DEFAULT 'sending', smtp_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_sends_campaign ON sends(campaign_id);
CREATE TABLE IF NOT EXISTS replies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    send_id INTEGER NOT NULL REFERENCES sends(id) ON DELETE CASCADE,
    received_at TEXT NOT NULL, from_email TEXT NOT NULL,
    subject TEXT, body_text TEXT, category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppression(
    email TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
-- v2: admin auth sessions
CREATE TABLE IF NOT EXISTS sessions(
    token TEXT PRIMARY KEY, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, sudo_until TEXT
);
-- v2: deliverability seed inboxes
CREATE TABLE IF NOT EXISTS seed_accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL, imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL DEFAULT 993,
    imap_user TEXT NOT NULL, imap_pass_enc TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'other',
    created_at TEXT NOT NULL
);
-- v2: click tracking
CREATE TABLE IF NOT EXISTS clicks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    send_id INTEGER NOT NULL REFERENCES sends(id) ON DELETE CASCADE,
    url TEXT NOT NULL, clicked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_send ON clicks(send_id);
-- v2: multi-step follow-up sequences
CREATE TABLE IF NOT EXISTS campaign_steps(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL, draft_id INTEGER NOT NULL REFERENCES drafts(id),
    draft_version INTEGER NOT NULL, delay_days INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, step_order)
);
CREATE INDEX IF NOT EXISTS idx_steps_campaign ON campaign_steps(campaign_id, step_order);
-- v2: chunked CSV import jobs
CREATE TABLE IF NOT EXISTS import_jobs(
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, list_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'parsing', processed INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0, headers TEXT, rows_path TEXT,
    error TEXT, created_at TEXT NOT NULL
);
-- v2: deliverability test runs
CREATE TABLE IF NOT EXISTS deliverability_tests(
    id TEXT PRIMARY KEY, draft_id INTEGER, account_id INTEGER,
    status TEXT NOT NULL DEFAULT 'running', results TEXT,
    created_at TEXT NOT NULL
);
"""


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db():
    """One connection per thread."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    defaults = {
        "timezone": "Asia/Karachi",
        "base_url": "http://localhost:8000",
        "physical_address": "",
        "reply_poll_minutes": "5",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    conn.commit()


# v2 column migrations for pre-existing tables (fresh DBs get them via... the
# ALTERs below run for every DB, guarded by PRAGMA checks, so both cases work).
_MIGRATIONS = [
    ("accounts", "imap_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("contacts", "custom_fields", "TEXT NOT NULL DEFAULT '{}'"),
    ("sends", "opened_at", "TEXT"),
    ("campaigns", "open_tracking", "INTEGER NOT NULL DEFAULT 0"),
    ("campaigns", "click_tracking", "INTEGER NOT NULL DEFAULT 0"),
    ("campaigns", "schedule_mode", "TEXT NOT NULL DEFAULT 'continuous'"),
    ("campaigns", "send_slots", "TEXT NOT NULL DEFAULT '[]'"),
    ("campaigns", "hourly_cap", "INTEGER NOT NULL DEFAULT 0"),
    ("campaign_contacts", "step", "INTEGER NOT NULL DEFAULT 1"),
    ("campaign_contacts", "step_due_at", "TEXT"),
    ("accounts", "hourly_cap", "INTEGER NOT NULL DEFAULT 0"),
    ("import_jobs", "list_id", "INTEGER"),
    ("import_jobs", "summary", "TEXT"),
    ("import_jobs", "kind", "TEXT NOT NULL DEFAULT 'import'"),
    ("contact_lists", "gsheet_id", "TEXT"),
    ("contact_lists", "gsheet_tab", "TEXT"),
    ("contact_lists", "gsheet_url", "TEXT"),
    ("contact_lists", "gsheet_synced_at", "TEXT"),
    ("contact_lists", "gsheet_mapping", "TEXT"),
    ("contacts", "mail_sent", "TEXT NOT NULL DEFAULT ''"),
    ("contacts", "sent_by_account", "TEXT NOT NULL DEFAULT ''"),
    ("campaigns", "update_sheet_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("campaigns", "account_ids", "TEXT NOT NULL DEFAULT ''"),
    ("sends", "account_id", "INTEGER"),
]


def _migrate(conn):
    for table, column, ddl in _MIGRATIONS:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


_db_lock = threading.RLock()


def tx(fn, *args, **kwargs):
    """Run fn(conn, ...) inside a locked transaction. Commits on success."""
    with _db_lock:
        conn = get_db()
        try:
            result = fn(conn, *args, **kwargs)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
