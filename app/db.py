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
    defaults = {
        "timezone": "Asia/Karachi",
        "base_url": "http://localhost:8000",
        "physical_address": "",
        "reply_poll_minutes": "5",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    conn.commit()


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
