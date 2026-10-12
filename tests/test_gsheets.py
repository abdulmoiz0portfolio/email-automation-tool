"""Google Sheets import/sync tests.

Run: .venv/bin/python tests/test_gsheets.py
Uses a temp DATA_DIR so the real data/ is untouched. Network calls to Google
are stubbed out — no real HTTP happens.
"""
import io
import json
import os
import sys
import tempfile
import time as _time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EMAILTOOL_NO_WORKER"] = "1"

TMP = tempfile.mkdtemp(prefix="emailtool-gsheets-")

from app import db, crypto  # noqa: E402

db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "emailtool.db")
db._local.conn = None
crypto.KEY_PATH = os.path.join(TMP, ".key")
crypto._fernet = None
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from app import gsheets  # noqa: E402
from app import auth as authmod  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
PASS = []
FAIL = []


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


def wait_job(job_id, timeout=30):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        j = client.get(f"/api/import-jobs/{job_id}").json()
        if j["status"] == "done":
            return j
        assert j["status"] != "error", j.get("error")
        _time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def login():
    r = client.post("/setup", data={"username": "admin", "password": "testpass123",
                                    "password2": "testpass123"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code


login()

SID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"


# ---------- URL -> ID parsing ----------

def t_parse_edit_url():
    assert gsheets.parse_sheet_id(
        f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0") == SID


def t_parse_sharing_url():
    assert gsheets.parse_sheet_id(
        f"https://docs.google.com/spreadsheets/d/{SID}?usp=sharing") == SID


def t_parse_export_url():
    assert gsheets.parse_sheet_id(
        f"https://docs.google.com/spreadsheets/d/{SID}/export?format=csv") == SID


def t_parse_bare_id():
    assert gsheets.parse_sheet_id(SID) == SID


def t_parse_garbage():
    assert gsheets.parse_sheet_id("not a sheet") is None
    assert gsheets.parse_sheet_id("") is None


# ---------- tabs endpoint (mocked API) ----------

def _set_key():
    authmod.set_setting("google_sheets_api_key", crypto.encrypt("fake-test-key"))


def t_tabs_with_key():
    _set_key()
    real = gsheets.get_tabs
    gsheets.get_tabs = lambda sid, api_key=None: ["Leads", "Archive"]
    try:
        r = client.post("/api/contacts/gsheet/tabs",
                        json={"url": f"https://docs.google.com/spreadsheets/d/{SID}/edit"})
        assert r.status_code == 200, r.status_code
        j = r.json()
        assert j["sheet_id"] == SID and j["tabs"] == ["Leads", "Archive"], j
        assert j["has_key"] is True
    finally:
        gsheets.get_tabs = real


def t_tabs_bad_url():
    r = client.post("/api/contacts/gsheet/tabs", json={"url": "hello"})
    assert r.status_code == 400, r.status_code


def t_tabs_requires_auth():
    anon = TestClient(app)
    r = anon.post("/api/contacts/gsheet/tabs", json={"url": "x"})
    assert r.status_code == 401, r.status_code


# ---------- fetch -> mapping -> import (mocked fetch) ----------

def _stub_fetch(headers_rows):
    real = gsheets.fetch_values
    gsheets.fetch_values = lambda sid, tab=None, api_key=None: headers_rows
    return real


LID = [None]


# ---------- sync: upsert by email, preserve do_not_contact ----------

def t_gsheet_sync():
    lid = LID[0]
    assert lid, "import flow must run first"
    # mark one contact as unsubscribed; give it an old name to prove update still happens
    db.tx(lambda c: c.execute(
        "UPDATE contacts SET do_not_contact=1, first_name='OldName' WHERE email='n1@x.com'"))
    real = _stub_fetch((["Email", "First", "City"],
                        [{"Email": "n1@x.com", "First": "Nora-New", "City": "Karachi"},
                         {"Email": "n3@x.com", "First": "Nia", "City": "London"}]))
    try:
        r = client.post(f"/contacts/lists/{lid}/gsheet-sync", follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])
        job_id = r.headers["location"].rsplit("/contacts/import-progress/", 1)[1]
        job = wait_job(job_id)
        assert job["list_id"] == lid, job
        assert "2 added" not in (job["summary"] or ""), job["summary"]
        assert "1 added" in (job["summary"] or "") and "1 updated" in (job["summary"] or ""), job["summary"]
        c1 = db.get_db().execute(
            "SELECT * FROM contacts WHERE email='n1@x.com'").fetchone()
        assert c1["first_name"] == "Nora-New", dict(c1)          # name updated
        assert c1["do_not_contact"] == 1, dict(c1)               # unsubscribe preserved
        assert json.loads(c1["custom_fields"])["city"] == "Karachi", c1["custom_fields"]
        c3 = db.get_db().execute(
            "SELECT * FROM contacts WHERE email='n3@x.com'").fetchone()
        assert c3 is not None and c3["first_name"] == "Nia", "new row not added"
        lst = db.get_db().execute(
            "SELECT gsheet_synced_at FROM contact_lists WHERE id=?", (lid,)).fetchone()
        assert lst["gsheet_synced_at"], "synced_at not stamped"
    finally:
        gsheets.fetch_values = real


def t_sync_requires_auth():
    anon = TestClient(app)
    r = anon.post("/contacts/lists/1/gsheet-sync", follow_redirects=False)
    assert r.status_code in (303, 401), r.status_code
    if r.status_code == 303:
        assert "/login" in r.headers["location"], r.headers["location"]


def t_sync_unlinked_list():
    lid = db.tx(lambda c: c.execute(
        "INSERT INTO contact_lists(name, created_at) VALUES(?,?)",
        ("Plain", utcnow_s())).lastrowid)
    r = client.post(f"/contacts/lists/{lid}/gsheet-sync", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert r.headers["location"].startswith("/contacts?"), r.headers["location"]


check("parse edit URL", t_parse_edit_url)
check("parse sharing URL", t_parse_sharing_url)
check("parse export URL", t_parse_export_url)
check("parse bare ID", t_parse_bare_id)
check("parse garbage -> None", t_parse_garbage)
check("tabs endpoint (mocked API)", t_tabs_with_key)
check("tabs endpoint bad URL -> 400", t_tabs_bad_url)
check("tabs endpoint requires auth", t_tabs_requires_auth)


def run_import_and_store():
    real = _stub_fetch((["Email", "First", "City"],
                        [{"Email": "n1@x.com", "First": "Nora", "City": "Lahore"},
                         {"Email": "n2@x.com", "First": "Ned", "City": "Dubai"}]))
    try:
        r = client.post("/contacts/gsheet/fetch",
                        data={"url": f"https://docs.google.com/spreadsheets/d/{SID}/edit",
                              "tab": "Leads", "name": "Sheet Leads"},
                        follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])
        uid = r.headers["location"].rsplit("/contacts/mapping/", 1)[1]
        r = client.post(f"/contacts/mapping/{uid}",
                        data={"map_0": "email", "map_1": "first_name",
                              "list_name": "Sheet Leads"}, follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])
        job_id = r.headers["location"].rsplit("/contacts/import-progress/", 1)[1]
        job = wait_job(job_id)
        lid = int(job["list_id"])
        lst = db.get_db().execute("SELECT * FROM contact_lists WHERE id=?", (lid,)).fetchone()
        assert lst["gsheet_id"] == SID, dict(lst)
        assert lst["gsheet_tab"] == "Leads", dict(lst)
        n = db.get_db().execute(
            "SELECT COUNT(*) c FROM contacts WHERE list_id=?", (lid,)).fetchone()["c"]
        assert n == 2, n
        c = db.get_db().execute(
            "SELECT custom_fields FROM contacts WHERE email='n1@x.com'").fetchone()
        assert json.loads(c["custom_fields"])["city"] == "Lahore", c["custom_fields"]
        LID[0] = lid
    finally:
        gsheets.fetch_values = real


check("gsheet import flow links list to sheet", run_import_and_store)
check("gsheet sync upserts + preserves unsubscribe", t_gsheet_sync)
check("sync requires auth", t_sync_requires_auth)
check("sync on unlinked list redirects", t_sync_unlinked_list)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed", flush=True)
sys.exit(1 if FAIL else 0)
