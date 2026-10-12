"""Multi-account campaign sending + 'sent via' sheet write-back tests.

Run: .venv/bin/python tests/test_multiaccount.py
"""
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EMAILTOOL_NO_WORKER"] = "1"

TMP = tempfile.mkdtemp(prefix="emailtool-multi-")

from app import db, crypto  # noqa: E402
from app import mail as mailmod  # noqa: E402
from app import worker as workermod  # noqa: E402
from app import gsheets  # noqa: E402
from app import sheetwrite  # noqa: E402
from app import auth as authmod  # noqa: E402

db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "emailtool.db")
db._local.conn = None
crypto.KEY_PATH = os.path.join(TMP, ".key")
crypto._fernet = None
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS: {name}", flush=True)
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  FAIL: {name}: {e}", flush=True)
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  FAIL: {name}: {type(e).__name__}: {e}", flush=True)


def utcnow_s():
    return datetime.now(timezone.utc).isoformat()


def login():
    r = client.post("/setup", data={"username": "admin", "password": "testpass123",
                                    "password2": "testpass123"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code


login()

SENT = []


def fake_smtp_ok(account, msg, timeout=30):
    SENT.append((account["email"], msg))
    return True, "250 OK"


# ---------- seeds ----------

def seed_account(email):
    def _do(conn):
        cur = conn.execute(
            """INSERT INTO accounts(label, email, smtp_host, smtp_port, smtp_user, smtp_pass_enc,
               smtp_enc, imap_host, imap_port, imap_user, imap_pass_enc, imap_enc,
               status, created_at, warmup_override)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'connected', ?, 1)""",
            ("T-" + email, email, "smtp.example.com", 587, email, crypto.encrypt("secret"),
             "STARTTLS", "imap.example.com", 993, email, crypto.encrypt("secret"),
             "SSL", utcnow_s()))
        return cur.lastrowid
    return db.tx(_do)


def seed_list(name, contacts):
    def _do(conn):
        cur = conn.execute("INSERT INTO contact_lists(name, created_at) VALUES(?,?)",
                           (name, utcnow_s()))
        lid = cur.lastrowid
        for c in contacts:
            conn.execute(
                """INSERT INTO contacts(list_id, email, first_name, company, custom_fields,
                   do_not_contact, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (lid, c["email"], c.get("first_name", ""), c.get("company", ""),
                 c.get("custom_fields", "{}"), c.get("do_not_contact", 0), utcnow_s()))
        return lid
    return db.tx(_do)


def seed_draft(name, subject="Hi", body="<p>Hi</p>"):
    def _do(conn):
        cur = conn.execute("INSERT INTO drafts(name, created_at) VALUES(?,?)", (name, utcnow_s()))
        did = cur.lastrowid
        conn.execute("INSERT INTO draft_versions(draft_id, version, subject, body_html, created_at)"
                     " VALUES(?,?,?,?,?)", (did, 1, subject, body, utcnow_s()))
        return did
    return db.tx(_do)


def make_campaign_multi(list_id, account_ids, draft_id, extra_form=None):
    db.tx(lambda c: c.execute("UPDATE campaigns SET status='completed'"))
    wd = str(datetime.now(timezone.utc).weekday())
    data = {"name": "MC", "list_id": str(list_id),
            "account_ids": [str(a) for a in account_ids],
            "draft_id": str(draft_id), "ab_split": "50",
            "window_start": "00:00", "window_end": "23:59",
            "gap_min_sec": "0", "gap_max_sec": "0", "daily_cap": "1000",
            "start_date": datetime.now(timezone.utc).date().isoformat(),
            "mailing_address": "123 St", "days": [wd]}
    if extra_form:
        data.update(extra_form)
    r = client.post("/campaigns", data=data, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text[:300])
    return int(r.headers["location"].split("?")[0].rstrip("/").split("/")[-1])


def fresh_camp(cid):
    return dict(db.get_db().execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone())


# ---------- Feature A: multi-account sending ----------

def t_multi_account_create():
    a1, a2 = seed_account("m1@x.com"), seed_account("m2@x.com")
    lid = seed_list("ML1", [{"email": "t1@x.com"}])
    d1 = seed_draft("MD1")
    cid = make_campaign_multi(lid, [a1, a2], d1)
    rows = db.get_db().execute(
        "SELECT account_id FROM campaign_accounts WHERE campaign_id=? ORDER BY account_id",
        (cid,)).fetchall()
    assert [r["account_id"] for r in rows] == sorted([a1, a2]), rows
    c = fresh_camp(cid)
    assert c["account_id"] == a1  # primary = first selected
    # all contacts queued once
    n = db.get_db().execute(
        "SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()["c"]
    assert n == 1, n


def t_no_duplicate_concurrent():
    a1, a2 = seed_account("c1@x.com"), seed_account("c2@x.com")
    lid = seed_list("ML2", [{"email": "solo@x.com"}])
    d1 = seed_draft("MD2")
    cid = make_campaign_multi(lid, [a1, a2], d1)
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        errs = []

        def hammer():
            try:
                camp = fresh_camp(cid)
                workermod._process_one_campaign(camp, workermod.utcnow())
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errs, errs[:1]
        sent = db.get_db().execute(
            "SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent'",
            (cid,)).fetchone()["c"]
        assert sent == 1, f"expected exactly 1 sent row, got {sent} (SENT={len(SENT)})"
        assert len(SENT) == 1, len(SENT)
    finally:
        mailmod.smtp_send = old


def t_round_robin_distribution():
    a1, a2 = seed_account("r1@x.com"), seed_account("r2@x.com")
    contacts = [{"email": f"rr{i}@x.com"} for i in range(10)]
    lid = seed_list("ML3", contacts)
    d1 = seed_draft("MD3")
    cid = make_campaign_multi(lid, [a1, a2], d1)
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        for _ in range(10):
            db.tx(lambda c: c.execute("UPDATE campaigns SET next_due_at=NULL WHERE id=?", (cid,)))
            workermod._process_one_campaign(fresh_camp(cid), workermod.utcnow())
        rows = db.get_db().execute(
            "SELECT account_id, COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent' "
            "GROUP BY account_id", (cid,)).fetchall()
        counts = {r["account_id"]: r["c"] for r in rows}
        assert sum(counts.values()) == 10, counts
        assert set(counts) == {a1, a2}, counts
        assert abs(counts[a1] - counts[a2]) <= 1, counts
        # every send records its account; every contact got exactly one
        nulls = db.get_db().execute(
            "SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND account_id IS NULL",
            (cid,)).fetchone()["c"]
        assert nulls == 0
        dupes = db.get_db().execute(
            """SELECT contact_id, COUNT(*) c FROM sends
               WHERE campaign_id=? AND status='sent' GROUP BY contact_id HAVING c>1""",
            (cid,)).fetchall()
        assert not dupes, [dict(d) for d in dupes]
    finally:
        mailmod.smtp_send = old


def t_partial_index_blocks_double_sent():
    # DB-level backstop: two 'sent' rows for same (campaign, contact, step) fail;
    # a 'failed' row does NOT block a later 'sent' (retries still work).
    a1 = seed_account("px@x.com")
    lid = seed_list("MLX", [{"email": "px1@x.com"}])
    d1 = seed_draft("MDX")
    cid = make_campaign_multi(lid, [a1], d1)
    ccid = db.get_db().execute(
        "SELECT contact_id FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()["contact_id"]
    db.tx(lambda c: c.execute(
        "INSERT INTO sends(campaign_id, contact_id, account_id, step, draft_version, sent_at, status)"
        " VALUES(?,?,?,?,1,?, 'failed')", (cid, ccid, a1, 1, utcnow_s())))
    db.tx(lambda c: c.execute(
        "INSERT INTO sends(campaign_id, contact_id, account_id, step, draft_version, sent_at, status)"
        " VALUES(?,?,?,?,1,?, 'sent')", (cid, ccid, a1, 1, utcnow_s())))
    try:
        db.tx(lambda c: c.execute(
            "INSERT INTO sends(campaign_id, contact_id, account_id, step, draft_version, sent_at, status)"
            " VALUES(?,?,?,?,1,?, 'sent')", (cid, ccid, a1, 1, utcnow_s())))
        raise AssertionError("duplicate 'sent' row was NOT blocked")
    except Exception as e:
        assert "idx_sends_nodup" in str(e) or "UNIQUE" in str(e), str(e)[:200]


# ---------- Feature B: sent-via write-back ----------

class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSheetsClient:
    """Records calls; serves canned values for read, ok for writes."""
    instances = []
    canned_values = [["email", "name"],
                     ["a@x.com", "Ann"], ["b@x.com", "Bob"], ["c@x.com", "Cat"]]

    def __init__(self, *a, **k):
        self.calls = []
        FakeSheetsClient.instances.append(self)
        # tab without sent_via column yet
        self.values = [list(r) for r in FakeSheetsClient.canned_values]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, **k):
        self.calls.append(("GET", url))
        return FakeResp(200, {"values": [list(r) for r in self.values]})

    def put(self, url, params=None, json=None, **k):
        self.calls.append(("PUT", url, json))
        # simulate header extension
        self.values[0].append("sent_via")
        return FakeResp(200, {})

    def post(self, url, json=None, data=None, **k):
        self.calls.append(("POST", url, json or data))
        if "oauth2" in url:
            return FakeResp(200, {"access_token": "tok", "expires_in": 3600})
        return FakeResp(200, {})


def t_sent_via_batch_write():
    FakeSheetsClient.instances.clear()
    real_client, real_token = gsheets.httpx.Client, gsheets._sa_access_token
    gsheets.httpx.Client = FakeSheetsClient
    gsheets._sa_access_token = lambda info: "tok"
    try:
        sa = {"client_email": "sa@p.iam.gserviceaccount.com", "private_key": "x"}
        n = gsheets.write_sent_via("SID1", "Tab1",
                                   {"a@x.com": "s1@gmail.com", "b@x.com": "s2@gmail.com"}, sa)
        assert n == 2, n
        inst = FakeSheetsClient.instances[-1]
        posts = [c for c in inst.calls if c[0] == "POST" and "batchUpdate" in c[1]]
        assert len(posts) == 1, f"expected ONE batchUpdate, got {len(posts)}"
        data = posts[0][2]["data"]
        assert len(data) == 2, data
        ranges = sorted(d["range"] for d in data)
        assert ranges == ["'Tab1'!C2", "'Tab1'!C3"], ranges
        vals = sorted(d["values"][0][0] for d in data)
        assert vals == ["s1@gmail.com", "s2@gmail.com"], vals
        puts = [c for c in inst.calls if c[0] == "PUT"]
        assert len(puts) == 1, "sent_via column should be created once"
    finally:
        gsheets.httpx.Client = real_client
        gsheets._sa_access_token = real_token


def t_sheet_writeback_gated_without_sa():
    # service account absent -> toggle blocked at campaign creation
    a1 = seed_account("g1@x.com")
    lid = seed_list("MLG", [{"email": "g1c@x.com"}])
    db.tx(lambda c: c.execute(
        "UPDATE contact_lists SET gsheet_id='GS1', gsheet_tab='T', gsheet_mapping='{}' WHERE id=?",
        (lid,)))
    d1 = seed_draft("MDG")
    db.tx(lambda c: c.execute("UPDATE campaigns SET status='completed'"))
    wd = str(datetime.now(timezone.utc).weekday())
    data = {"name": "G", "list_id": str(lid), "account_ids": [str(a1)],
            "draft_id": str(d1), "ab_split": "50",
            "window_start": "00:00", "window_end": "23:59",
            "gap_min_sec": "0", "gap_max_sec": "0", "daily_cap": "100",
            "start_date": datetime.now(timezone.utc).date().isoformat(),
            "mailing_address": "123 St", "days": [wd], "mark_sent_in_sheet": "1"}
    r = client.post("/campaigns", data=data, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    n = db.get_db().execute(
        "SELECT COUNT(*) c FROM campaigns WHERE name='G'").fetchone()["c"]
    assert n == 0, "campaign with sheet toggle must be blocked without a service account"
    # now configure a service account -> allowed
    authmod.set_setting("google_service_account_json", crypto.encrypt(json.dumps(
        {"client_email": "sa@test.iam.gserviceaccount.com", "private_key": "fake"})))
    data["name"] = "G2"
    r = client.post("/campaigns", data=data, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text[:300])
    cid = int(r.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
    c = fresh_camp(cid)
    assert int(c["mark_sent_in_sheet"]) == 1
    # cleanup so other tests aren't affected
    db.tx(lambda c_: c_.execute("DELETE FROM settings WHERE key='google_service_account_json'"))
    db.tx(lambda c_: c_.execute("UPDATE campaigns SET status='completed' WHERE id=?", (cid,)))


def t_sync_ignores_sent_via_and_unsubscribed():
    # sent_via column in the sheet is read-only metadata; unsubscribed contacts
    # are never re-added or resurrected by sync.
    from app.main import _run_gsheet_sync
    lid = seed_list("MLS", [{"email": "keep@x.com", "first_name": "Keep",
                             "do_not_contact": 1}])
    db.tx(lambda c: c.execute(
        """UPDATE contact_lists SET gsheet_id='GS9', gsheet_tab='T',
           gsheet_mapping='{"Email":"email","Name":"first_name"}' WHERE id=?""", (lid,)))
    headers_rows = (["Email", "Name", "sent_via"],
                    [{"Email": "keep@x.com", "Name": "KeepUpdated", "sent_via": "s1@gmail.com"},
                     {"Email": "new@x.com", "Name": "New", "sent_via": ""}])
    real = gsheets.fetch_values
    gsheets.fetch_values = lambda sid, tab=None, api_key=None: headers_rows
    try:
        jid = "synctest1"
        db.tx(lambda c: c.execute(
            "INSERT INTO import_jobs(id, filename, list_name, status, created_at, kind)"
            " VALUES(?,?,?,?,?, 'sync')", (jid, "f", "L", "queued", utcnow_s())))
        _run_gsheet_sync(jid, lid)
        keep = db.get_db().execute(
            "SELECT first_name, do_not_contact, custom_fields FROM contacts "
            "WHERE list_id=? AND email='keep@x.com'", (lid,)).fetchone()
        assert keep["do_not_contact"] == 1, dict(keep)
        assert "sent_via" not in (keep["custom_fields"] or ""), keep["custom_fields"]
        new = db.get_db().execute(
            "SELECT custom_fields FROM contacts WHERE list_id=? AND email='new@x.com'",
            (lid,)).fetchone()
        assert new, "new contact should be added"
        assert "sent_via" not in (new["custom_fields"] or ""), new["custom_fields"]
    finally:
        gsheets.fetch_values = real


def t_sheetwrite_buffer_flushes_once():
    FakeSheetsClient.instances.clear()
    FakeSheetsClient.canned_values = [["email", "name"],
                                      ["w1c@x.com", "W1"], ["w2c@x.com", "W2"]]
    real_client, real_token = gsheets.httpx.Client, gsheets._sa_access_token
    real_sa = gsheets.service_account_info
    gsheets.httpx.Client = FakeSheetsClient
    gsheets._sa_access_token = lambda info: "tok"
    gsheets.service_account_info = lambda: {"client_email": "sa@x", "private_key": "y"}
    try:
        a1 = seed_account("w1@x.com")
        lid = seed_list("MLW", [{"email": "w1c@x.com"}, {"email": "w2c@x.com"}])
        db.tx(lambda c: c.execute(
            "UPDATE contact_lists SET gsheet_id='GSW', gsheet_tab='T' WHERE id=?", (lid,)))
        d1 = seed_draft("MDW")
        db.tx(lambda c: c.execute("UPDATE campaigns SET status='completed'"))
        wd = str(datetime.now(timezone.utc).weekday())
        cid_holder = {}
        # create campaign with the toggle via direct SQL (SA gating already tested)
        def _do(conn):
            cur = conn.execute(
                """INSERT INTO campaigns(name, list_id, account_id, draft_id, draft_version,
                   days_of_week, window_start, window_end, timezone, gap_min_sec, gap_max_sec,
                   daily_cap, start_date, status, mark_sent_in_sheet, created_at)
                   VALUES('W',?,?,?,?, '0,1,2,3,4,5,6','00:00','23:59','Asia/Karachi',0,0,100,?, 'active', 1, ?)""",
                (lid, a1, d1, 1, datetime.now(timezone.utc).date().isoformat(), utcnow_s()))
            cid_holder["cid"] = cur.lastrowid
            conn.execute("INSERT INTO campaign_accounts(campaign_id, account_id) VALUES(?,?)",
                         (cid_holder["cid"], a1))
        db.tx(_do)
        cid = cid_holder["cid"]
        db.tx(lambda c: c.execute(
            """INSERT INTO sends(campaign_id, contact_id, account_id, step, draft_version,
               sent_at, status) SELECT ?, id, ?, 1, 1, ?, 'sent' FROM contacts WHERE list_id=?""",
            (cid, a1, utcnow_s(), lid)))
        rows = db.get_db().execute(
            "SELECT id, contact_id FROM sends WHERE campaign_id=?", (cid,)).fetchall()
        for r in rows:
            em = db.get_db().execute("SELECT email FROM contacts WHERE id=?",
                                     (r["contact_id"],)).fetchone()["email"]
            sheetwrite.queue(cid, em, "w1@x.com", r["id"])
        assert sheetwrite.pending_count() == 2
        flushed, touched = sheetwrite.flush(force=True)
        assert flushed == 2 and touched == 1, (flushed, touched)
        inst = FakeSheetsClient.instances[-1]
        posts = [c for c in inst.calls if c[0] == "POST" and "batchUpdate" in c[1]]
        assert len(posts) == 1, f"one batchUpdate per flush, got {len(posts)}"
        marked = db.get_db().execute(
            "SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND sheet_marked=1",
            (cid,)).fetchone()["c"]
        assert marked == 2, marked
        db.tx(lambda c: c.execute("UPDATE campaigns SET status='completed' WHERE id=?", (cid,)))
    finally:
        gsheets.httpx.Client = real_client
        gsheets._sa_access_token = real_token
        gsheets.service_account_info = real_sa
        FakeSheetsClient.canned_values = [["email", "name"],
                                          ["a@x.com", "Ann"], ["b@x.com", "Bob"], ["c@x.com", "Cat"]]
        with sheetwrite._lock:
            sheetwrite._buffer.clear()


for _name, _fn in sorted([(k, v) for k, v in globals().items() if k.startswith("t_")]):
    check(_name, _fn)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed", flush=True)
sys.exit(1 if FAIL else 0)
