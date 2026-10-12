"""Upgrade v2 tests: sheet selection on import (Phase 5 import work).

Run: .venv/bin/python tests/test_upgrade.py
Uses a temp DATA_DIR so the real data/ is untouched.
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EMAILTOOL_NO_WORKER"] = "1"

TMP = tempfile.mkdtemp(prefix="emailtool-upg-")

from app import db, crypto  # noqa: E402

db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "emailtool.db")
db._local.conn = None
crypto.KEY_PATH = os.path.join(TMP, ".key")
crypto._fernet = None
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
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


def wait_import_job(job_id, timeout=30):
    """Poll the chunked import job until done; return the created list id."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        j = client.get(f"/api/import-jobs/{job_id}").json()
        if j["status"] == "done":
            assert j["list_id"], j
            return int(j["list_id"])
        assert j["status"] != "error", j.get("error")
        _time.sleep(0.2)
    raise AssertionError(f"import job {job_id} did not finish in {timeout}s")


def login():
    r = client.post("/setup", data={"username": "admin", "password": "testpass123",
                                    "password2": "testpass123"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code


login()


def make_xlsx(sheets):
    """sheets: list of (name, rows). Returns BytesIO."""
    import openpyxl
    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets:
        ws = wb.active if first else wb.create_sheet(name)
        if first:
            ws.title = name
        first = False
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def t_sheet_names_multi():
    buf = make_xlsx([("Leads-Q3", [["Email", "First"], ["a@x.com", "Ann"]]),
                     ("Leads-Q4", [["Email", "First"], ["b@x.com", "Bob"]])])
    r = client.post("/api/contacts/stage",
                    files={"file": ("list.xlsx", buf, XLSX_CT)})
    assert r.status_code == 200, r.status_code
    j = r.json()
    assert j["sheets"] == ["Leads-Q3", "Leads-Q4"], j


def t_sheet_names_csv():
    r = client.post("/api/contacts/stage",
                    files={"file": ("l.csv", io.BytesIO(b"Email,First\nc@x.com,Cat\n"),
                                    "text/csv")})
    assert r.status_code == 200, r.status_code
    assert r.json()["sheets"] == [], r.json()


def t_import_selected_sheet():
    buf = make_xlsx([("Leads-Q3", [["Email", "First"], ["a@x.com", "Ann"]]),
                     ("Leads-Q4", [["Email", "First"], ["b@x.com", "Bob"]])])
    r = client.post("/api/contacts/stage",
                    files={"file": ("list.xlsx", buf, XLSX_CT)})
    uid = r.json()["uid"]
    r = client.post("/contacts/upload-staged",
                    data={"uid": uid, "sheet": "Leads-Q4", "name": "Q4List"})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    mapping_url = r.json()["redirect"]
    r = client.get(mapping_url)
    assert r.status_code == 200 and "Bob" in r.text and "Ann" not in r.text
    # finish the mapping -> only Bob imported
    muid = mapping_url.split("/")[-1]
    r = client.post(f"/contacts/mapping/{muid}", follow_redirects=False,
                    data={"map_0": "email", "map_1": "first_name", "list_name": "Q4List"})
    assert r.status_code == 303, r.status_code
    job_id = r.headers["location"].rstrip("/").split("/")[-1]
    lid = wait_import_job(job_id)
    conn = db.get_db()
    emails = [x["email"] for x in conn.execute(
        "SELECT email FROM contacts WHERE list_id=?", (lid,)).fetchall()]
    assert emails == ["b@x.com"], emails


def t_import_bad_sheet():
    buf = make_xlsx([("Only", [["Email"], ["a@x.com"]])])
    r = client.post("/api/contacts/stage",
                    files={"file": ("list.xlsx", buf, XLSX_CT)})
    uid = r.json()["uid"]
    r = client.post("/contacts/upload-staged",
                    data={"uid": uid, "sheet": "Nope", "name": "X"})
    assert r.status_code == 400, r.status_code


def t_import_bad_uid():
    r = client.post("/contacts/upload-staged",
                    data={"uid": "0" * 32, "sheet": "", "name": "X"})
    assert r.status_code == 400, r.status_code


def t_stage_requires_auth():
    c2 = TestClient(app)
    r = c2.post("/api/contacts/stage",
                files={"file": ("l.csv", io.BytesIO(b"a"), "text/csv")})
    assert r.status_code == 401, r.status_code


def run_all():
    check("stage returns sheet names for multi-sheet xlsx", t_sheet_names_multi)
    check("stage returns [] for csv (single sheet)", t_sheet_names_csv)
    check("upload-staged imports ONLY the selected sheet", t_import_selected_sheet)
    check("upload-staged rejects unknown sheet (400)", t_import_bad_sheet)
    check("upload-staged rejects bad/expired uid (400)", t_import_bad_uid)
    check("stage endpoint requires auth (401)", t_stage_requires_auth)
    print()
    print(f"PASSED {len(PASS)} | FAILED {len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
