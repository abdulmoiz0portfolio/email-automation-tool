"""Batched 'sent via' write-back to Google Sheets.

After a campaign send (with the campaign's "Mark sent account in Google Sheet"
toggle ON), we queue (contact email -> sending account email) pairs in memory.
flush() writes them in ONE values.batchUpdate call per (sheet, tab) — never
one API call per email. Flush triggers: 50 queued entries or 120s elapsed.

Requires a service-account credential in Settings (Sheets API keys are
read-only and cannot write). Queueing is a no-op without one.
"""
import threading
import time

from . import db

FLUSH_MAX_ENTRIES = 50
FLUSH_MAX_AGE_SEC = 120

_lock = threading.Lock()
_buffer = []          # list of (sheet_id, tab, email, account_email, send_id)
_last_flush = time.time()


def _sheet_for_campaign(campaign_id):
    conn = db.get_db()
    row = conn.execute(
        """SELECT c.mark_sent_in_sheet, l.gsheet_id, l.gsheet_tab
           FROM campaigns c JOIN contact_lists l ON l.id=c.list_id
           WHERE c.id=?""", (campaign_id,)).fetchone()
    if not row:
        return None
    if not int(row["mark_sent_in_sheet"] or 0):
        return None
    if not row["gsheet_id"]:
        return None
    return row["gsheet_id"], row["gsheet_tab"] or None


def queue(campaign_id, contact_email, account_email, send_id=None):
    """Queue one write-back. Cheap no-op unless the campaign opted in."""
    try:
        sheet = _sheet_for_campaign(campaign_id)
    except Exception:
        return
    if not sheet:
        return
    from . import gsheets
    if not gsheets.service_account_info():
        return  # toggle is gated in the UI; never write without a service account
    sheet_id, tab = sheet
    with _lock:
        _buffer.append((sheet_id, tab, (contact_email or "").strip().lower(),
                        account_email or "", send_id))


def pending_count():
    with _lock:
        return len(_buffer)


def flush(force=False):
    """Write queued entries. Returns (flushed_entries, sheets_touched)."""
    global _buffer, _last_flush
    with _lock:
        if not _buffer:
            return 0, 0
        age = time.time() - _last_flush
        if not force and len(_buffer) < FLUSH_MAX_ENTRIES and age < FLUSH_MAX_AGE_SEC:
            return 0, 0
        batch = _buffer
        _buffer = []
        _last_flush = time.time()
    from . import gsheets
    sa = gsheets.service_account_info()
    if not sa:
        return 0, 0
    # group by (sheet_id, tab) -> one batchUpdate per sheet
    groups = {}
    for sheet_id, tab, email, account_email, send_id in batch:
        groups.setdefault((sheet_id, tab), []).append((email, account_email, send_id))
    flushed, touched = 0, 0
    marked_ids = []
    failed = []
    for (sheet_id, tab), entries in groups.items():
        mapping = {e: a for e, a, _ in entries if e and a}
        if not mapping:
            continue
        try:
            n = gsheets.write_sent_via(sheet_id, tab, mapping, sa)
        except Exception:
            failed.extend(entries)  # re-queue; retried on the next flush
            continue
        flushed += n
        touched += 1
        marked_ids.extend(sid for _, _, sid in entries if sid)
    if failed:
        with _lock:
            _buffer.extend(failed)
    if marked_ids:
        def _do(conn):
            conn.executemany("UPDATE sends SET sheet_marked=1 WHERE id=?",
                             [(sid,) for sid in marked_ids])
        try:
            db.tx(_do)
        except Exception:
            pass
    return flushed, touched
