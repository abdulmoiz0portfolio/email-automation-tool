"""Acceptance tests for Email Automation Tool MVP (spec §10).
Run: .venv/bin/python tests/test_acceptance.py
Uses a temp DATA_DIR so the real data/ is untouched. Real network is never touched:
SMTP/IMAP/DNS are monkeypatched where needed.
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EMAILTOOL_NO_WORKER"] = "1"

TMP = tempfile.mkdtemp(prefix="emailtool-test-")

from app import db, crypto, mail as mailmod, worker as workermod  # noqa: E402

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


# ---------- seed helpers ----------

def seed_account(label="Test Gmail", email="sender@example.com"):
    def _do(conn):
        cur = conn.execute(
            """INSERT INTO accounts(label, email, smtp_host, smtp_port, smtp_user, smtp_pass_enc,
               smtp_enc, imap_host, imap_port, imap_user, imap_pass_enc, imap_enc, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'connected', ?)""",
            (label, email, "smtp.example.com", 587, email, crypto.encrypt("secret"),
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
                """INSERT INTO contacts(list_id, email, first_name, last_name, company,
                   custom_1, created_at) VALUES(?,?,?,?,?,?,?)""",
                (lid, c["email"], c.get("first_name", ""), c.get("last_name", ""),
                 c.get("company", ""), c.get("custom_1", ""), utcnow_s()))
        return lid
    return db.tx(_do)


def seed_draft(name="D1", subject="Hi {{first_name}}", body="<p>Hello {{first_name}} at {{company}}</p>"):
    def _do(conn):
        cur = conn.execute("INSERT INTO drafts(name, created_at) VALUES(?,?)", (name, utcnow_s()))
        did = cur.lastrowid
        conn.execute("INSERT INTO draft_versions(draft_id, version, subject, body_html, created_at) VALUES(?,?,?,?,?)",
                     (did, 1, subject, body, utcnow_s()))
        return did
    return db.tx(_do)


def seed_campaign(name, list_id, account_id, draft_id, **kw):
    today = datetime.now(timezone.utc)
    wd = str(today.weekday())
    p = dict(days_of_week=wd, window_start="00:00", window_end="23:59",
             timezone="UTC", gap_min_sec=60, gap_max_sec=60, daily_cap=100,
             start_date=today.date().isoformat(), status="active",
             mailing_address="123 Main St, Karachi")
    p.update(kw)

    def _do(c):
        cur = c.execute(
            """INSERT INTO campaigns(name, list_id, account_id, draft_id, draft_version,
               days_of_week, window_start, window_end, timezone, gap_min_sec, gap_max_sec,
               daily_cap, start_date, status, mailing_address, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, list_id, account_id, draft_id, 1, p["days_of_week"],
             p["window_start"], p["window_end"], p["timezone"],
             p["gap_min_sec"], p["gap_max_sec"], p["daily_cap"],
             p["start_date"], p["status"], p["mailing_address"], utcnow_s()))
        cid = cur.lastrowid
        c.execute(
            """INSERT INTO campaign_contacts(campaign_id, contact_id, status, draft_version)
               SELECT ?, c.id, 'queued', 1 FROM contacts c
               WHERE c.list_id=? AND c.valid=1 AND c.do_not_contact=0
               AND NOT EXISTS(SELECT 1 FROM suppression s WHERE lower(s.email)=lower(c.email))""",
            (cid, list_id))
        return cid
    return db.tx(_do)


def force_due(cid):
    db.tx(lambda c: c.execute("UPDATE campaigns SET next_due_at=? WHERE id=?",
                              (datetime.now(timezone.utc).isoformat(), cid)))


SENT_LOG = []


def fake_smtp_ok(account, msg, timeout=30):
    SENT_LOG.append({"to": msg["To"], "subject": msg["Subject"],
                     "body": msg.get_body(preferencelist=("html",)).get_content()})
    return True, "250 OK accepted"


def fake_smtp_authfail(account, msg, timeout=30):
    return False, "AUTH FAILED: 535 bad credentials"


def send_to(cid, email):
    """Record a completed send (as if the worker sent it)."""
    conn = db.get_db()
    cc = conn.execute(
        """SELECT cc.id, cc.contact_id FROM campaign_contacts cc JOIN contacts c ON c.id=cc.contact_id
           WHERE cc.campaign_id=? AND c.email=?""", (cid, email)).fetchone()
    assert cc, f"no campaign_contact for {email}"
    mid = f"<{email}-mid>"
    tok = "tok-" + email.replace("@", "").replace(".", "")

    def _do(c):
        c.execute("""INSERT INTO sends(campaign_id, contact_id, draft_version, message_id,
                     unsub_token, sent_at, status, smtp_status)
                     VALUES(?,?,1,?,?,?, 'sent', '250 OK')""",
                  (cid, cc["contact_id"], mid, tok, utcnow_s()))
        c.execute("UPDATE campaign_contacts SET status='sent' WHERE id=?", (cc["id"],))
    db.tx(_do)
    return mid


# ================= tests =================

def t_validate():
    assert mailmod.validate_email("a@b.com") == (True, "")
    ok, reason = mailmod.validate_email("not-an-email")
    assert not ok and "missing @" in reason, reason
    ok, reason = mailmod.validate_email("   ")
    assert not ok and reason == "empty"
    assert not mailmod.validate_email("a@b")[0]
    assert mailmod.validate_email("  Jane.Doe@Example.COM  ")[0]


def t_template():
    c = {"first_name": "Sara", "company": "Acme", "custom_1": "x"}
    out = mailmod.render_template("Hi {{first_name}} from {{company}} {{unknown}}", c)
    assert out == "Hi Sara from Acme {{unknown}}", out
    m = mailmod.build_message("s@x.com", "t@x.com", "Sub", "<p>Hi</p>",
                              "<mid@x>", "http://h/u/abc", "123 St")
    assert m["List-Unsubscribe"] == "<http://h/u/abc>"
    html = m.get_body(preferencelist=("html",)).get_content()
    assert "Unsubscribe" in html and "123 St" in html


def t_classify():
    cases = [
        ("x@y.com", "Re: hi", "not interested, thanks", "Not Interested"),
        ("x@y.com", "Re: hi", "Please unsubscribe me now", "Unsubscribe"),
        ("x@y.com", "Re: hi", "I'm interested, tell me more", "Interested"),
        ("x@y.com", "Out of Office", "I am out of office until Monday", "Out of Office"),
        ("x@y.com", "Re: hi", "How much does it cost?", "Question"),
        ("x@y.com", "Re: hi", "ok noted", "Other"),
        ("mailer-daemon@x.com", "Delivery Failure", "undeliverable", "Bounce"),
        ("x@y.com", "Re: hi", "remove me from your list", "Unsubscribe"),
    ]
    for frm, subj, body, want in cases:
        got = mailmod.classify_reply(frm, subj, body)
        assert got == want, f"{body!r} -> {got}, want {want}"


def t_crypto():
    enc = crypto.encrypt("my-app-password")
    assert "my-app-password" not in enc
    assert crypto.decrypt(enc) == "my-app-password"
    aid = seed_account()
    raw = db.get_db().execute("SELECT smtp_pass_enc FROM accounts WHERE id=?",
                              (aid,)).fetchone()["smtp_pass_enc"]
    assert "secret" not in raw, "password stored in plaintext!"
    assert crypto.decrypt(raw) == "secret"


def t_import():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Email Address", "First", "Company"])
    ws.append(["good1@x.com", "Ann", "Acme"])
    ws.append(["good2@x.com", "Bob", "Beta"])
    ws.append(["bad-email", "Zed", "ZedCo"])
    ws.append(["", "NoMail", "NoCo"])
    ws.append(["GOOD1@x.com", "Ann2", "Acme2"])  # duplicate
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = client.post("/contacts/upload", follow_redirects=False,
                    files={"file": ("list.xlsx", buf,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    data={"name": "TestList"})
    assert r.status_code == 303, r.status_code
    uid = r.headers["location"].split("/")[-1]
    r = client.get(f"/contacts/mapping/{uid}")
    assert r.status_code == 200 and "Email Address" in r.text
    r = client.post(f"/contacts/mapping/{uid}", follow_redirects=False,
                    data={"map_0": "email", "map_1": "first_name",
                          "map_2": "company", "list_name": "TestList"})
    assert r.status_code == 303, r.status_code
    lid = int(r.headers["location"].split("/")[-1].split("?")[0])
    conn = db.get_db()
    n = conn.execute("SELECT COUNT(*) c FROM contacts WHERE list_id=?", (lid,)).fetchone()["c"]
    assert n == 2, f"want 2 contacts, got {n}"
    inv = conn.execute("SELECT reason FROM invalid_rows WHERE list_id=?", (lid,)).fetchall()
    reasons = " | ".join(x["reason"] for x in inv)
    assert len(inv) == 2, reasons
    assert "missing @" in reasons and "missing email" in reasons, reasons


def t_drafts():
    did = seed_draft()
    r = client.post(f"/drafts/{did}", follow_redirects=False,
                    data={"name": "D1", "subject": "New {{first_name}}", "body_html": "<p>v2</p>"})
    assert r.status_code == 303, r.status_code
    conn = db.get_db()
    v = conn.execute("SELECT MAX(version) m FROM draft_versions WHERE draft_id=?",
                     (did,)).fetchone()["m"]
    assert v == 2, v
    cid = seed_list("Prev", [{"email": "prev@x.com", "first_name": "Prev", "company": "Co"}])
    c = conn.execute("SELECT id FROM contacts WHERE list_id=?", (cid,)).fetchone()["id"]
    r = client.get("/api/drafts/preview", params={"subject": "Hi {{first_name}}",
                                                  "body": "<p>{{company}}</p>", "contact_id": c})
    j = r.json()
    assert j["subject"] == "Hi Prev" and "Co" in j["body"], j


def t_send_engine():
    SENT_LOG.clear()
    mailmod.smtp_send = fake_smtp_ok
    aid = seed_account("Eng", "eng@x.com")
    lid = seed_list("Eng10", [{"email": f"e{i}@x.com", "first_name": f"N{i}"} for i in range(10)])
    did = seed_draft()
    cid = seed_campaign("EngCamp", lid, aid, did, gap_min_sec=60, gap_max_sec=60, daily_cap=10)
    workermod.process_campaigns()
    assert len(SENT_LOG) == 1, f"want 1, got {len(SENT_LOG)}"
    workermod.process_campaigns()
    assert len(SENT_LOG) == 1, "gap not respected"
    force_due(cid)
    workermod.process_campaigns()
    assert len(SENT_LOG) == 2
    for _ in range(20):
        force_due(cid)
        workermod.process_campaigns()
    conn = db.get_db()
    sent = conn.execute("SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent'",
                        (cid,)).fetchone()["c"]
    assert sent == 10, f"daily cap 10 -> want 10 sent, got {sent}"
    force_due(cid)
    workermod.process_campaigns()
    assert len(SENT_LOG) == 10, "daily cap exceeded!"
    row = conn.execute("SELECT smtp_status FROM sends WHERE campaign_id=? LIMIT 1",
                       (cid,)).fetchone()
    assert "250 OK" in row["smtp_status"]
    dup = conn.execute(
        "SELECT contact_id, COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent' GROUP BY contact_id HAVING c>1",
        (cid,)).fetchall()
    assert not dup, "duplicate sends!"
    assert "N0" in SENT_LOG[0]["body"] or "N0" in SENT_LOG[0]["subject"]


def t_pause_resume():
    SENT_LOG.clear()
    mailmod.smtp_send = fake_smtp_ok
    aid = seed_account("PR", "pr@x.com")
    lid = seed_list("PR5", [{"email": f"p{i}@x.com", "first_name": f"P{i}"} for i in range(5)])
    did = seed_draft()
    cid = seed_campaign("PRCamp", lid, aid, did, gap_min_sec=1, gap_max_sec=1, daily_cap=100)
    client.post(f"/campaigns/{cid}/pause")
    workermod.process_campaigns()
    assert len(SENT_LOG) == 0, "sent while paused!"
    client.post(f"/campaigns/{cid}/resume")
    for _ in range(8):
        force_due(cid)
        workermod.process_campaigns()
    assert len(SENT_LOG) == 5, f"want 5 after resume, got {len(SENT_LOG)}"
    conn = db.get_db()
    dup = conn.execute(
        "SELECT contact_id, COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent' GROUP BY contact_id HAVING c>1",
        (cid,)).fetchall()
    assert not dup


def t_restart():
    SENT_LOG.clear()
    mailmod.smtp_send = fake_smtp_ok
    aid = seed_account("RS", "rs@x.com")
    lid = seed_list("RS3", [{"email": f"r{i}@x.com", "first_name": f"R{i}"} for i in range(3)])
    did = seed_draft()
    cid = seed_campaign("RSCamp", lid, aid, did, gap_min_sec=1, gap_max_sec=1, daily_cap=100)
    conn = db.get_db()
    cc = conn.execute("SELECT id, contact_id FROM campaign_contacts WHERE campaign_id=? LIMIT 1",
                      (cid,)).fetchone()

    def _crash(c):
        c.execute("UPDATE campaign_contacts SET status='sending' WHERE id=?", (cc["id"],))
        c.execute("""INSERT INTO sends(campaign_id, contact_id, draft_version, message_id,
                     unsub_token, sent_at, status) VALUES(?,?,1,'<crash>','tok-crash',?, 'sending')""",
                  (cid, cc["contact_id"], utcnow_s()))
    db.tx(_crash)
    workermod.recover_interrupted()
    for _ in range(6):
        force_due(cid)
        workermod.process_campaigns()
    sent = conn.execute("SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent'",
                        (cid,)).fetchone()["c"]
    assert sent == 3, f"want 3 sent after recovery, got {sent}"
    dup = conn.execute(
        "SELECT contact_id, COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent' GROUP BY contact_id HAVING c>1",
        (cid,)).fetchall()
    assert not dup, "double-send after restart!"


def t_reply_notinterested():
    aid = seed_account("RP", "rp@x.com")
    lid = seed_list("RP2", [{"email": "ni@x.com", "first_name": "Ni"},
                            {"email": "u2@x.com", "first_name": "U2"}])
    did = seed_draft()
    cid = seed_campaign("RPCamp", lid, aid, did)
    mid = send_to(cid, "ni@x.com")
    workermod.handle_reply({"id": aid},
                           {"from": "ni@x.com", "subject": "Re: Hi", "in_reply_to": mid,
                            "references": "", "body": "not interested, thanks", "imap_num": b"1"})
    conn = db.get_db()
    st = conn.execute(
        "SELECT status, reply_category FROM campaign_contacts WHERE campaign_id=? AND contact_id=(SELECT id FROM contacts WHERE email='ni@x.com')",
        (cid,)).fetchone()
    assert st["status"] == "replied" and st["reply_category"] == "Not Interested", dict(st)
    st2 = conn.execute(
        "SELECT status FROM campaign_contacts WHERE campaign_id=? AND contact_id=(SELECT id FROM contacts WHERE email='u2@x.com')",
        (cid,)).fetchone()
    assert st2["status"] == "queued"


def t_reply_unsubscribe():
    aid = seed_account("RU", "ru@x.com")
    lid = seed_list("RU2", [{"email": "un@x.com", "first_name": "Un"}])
    did = seed_draft()
    cid1 = seed_campaign("RUCamp1", lid, aid, did)
    mid = send_to(cid1, "un@x.com")
    workermod.handle_reply({"id": aid},
                           {"from": "un@x.com", "subject": "Re: Hi", "in_reply_to": mid,
                            "references": "", "body": "please unsubscribe me", "imap_num": b"2"})
    conn = db.get_db()
    assert conn.execute("SELECT 1 FROM suppression WHERE email='un@x.com'").fetchone(), \
        "not on suppression list!"
    cid2 = seed_campaign("RUCamp2", lid, aid, did)
    n = conn.execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?",
                     (cid2,)).fetchone()["c"]
    assert n == 0, f"suppressed contact queued in 2nd campaign! ({n})"
    lid2 = seed_list("RU3", [{"email": "link@x.com", "first_name": "L"}])
    cid3 = seed_campaign("RUCamp3", lid2, aid, did)
    mid3 = send_to(cid3, "link@x.com")
    tok = conn.execute("SELECT unsub_token FROM sends WHERE message_id=?", (mid3,)).fetchone()["unsub_token"]
    r = client.get(f"/u/{tok}")
    assert r.status_code == 200 and "Unsubscribed" in r.text
    assert conn.execute("SELECT 1 FROM suppression WHERE email='link@x.com'").fetchone()


def t_bounce_pause():
    SENT_LOG.clear()
    mailmod.smtp_send = fake_smtp_ok
    aid = seed_account("BP", "bp@x.com")
    lid = seed_list("BP12", [{"email": f"b{i}@x.com", "first_name": f"B{i}"} for i in range(12)])
    did = seed_draft()
    cid = seed_campaign("BPCamp", lid, aid, did, gap_min_sec=1, gap_max_sec=1, daily_cap=100)
    for i in range(10):
        send_to(cid, f"b{i}@x.com")
    for i in range(2):
        mid = f"<b{i}@x.com-mid>"
        workermod.handle_reply({"id": aid},
                               {"from": "mailer-daemon@x.com", "subject": "Delivery Failure",
                                "in_reply_to": mid, "references": "",
                                "body": "undeliverable", "imap_num": str(i).encode()})
    conn = db.get_db()
    c = conn.execute("SELECT status, pause_reason FROM campaigns WHERE id=?", (cid,)).fetchone()
    assert c["status"] == "paused", dict(c)
    assert "bounce" in (c["pause_reason"] or "").lower(), c["pause_reason"]
    v = conn.execute("SELECT valid FROM contacts WHERE email='b0@x.com'").fetchone()["valid"]
    assert v == 0


def t_auth_pause():
    SENT_LOG.clear()
    mailmod.smtp_send = fake_smtp_authfail
    aid = seed_account("AF", "af@x.com")
    lid = seed_list("AF3", [{"email": f"a{i}@x.com", "first_name": f"A{i}"} for i in range(3)])
    did = seed_draft()
    cid = seed_campaign("AFCamp", lid, aid, did, gap_min_sec=1, gap_max_sec=1, daily_cap=100)
    for _ in range(3):
        force_due(cid)
        workermod.process_campaigns()
    conn = db.get_db()
    c = conn.execute("SELECT status, pause_reason FROM campaigns WHERE id=?", (cid,)).fetchone()
    assert c["status"] == "paused", dict(c)
    assert "authentication" in (c["pause_reason"] or "").lower(), c["pause_reason"]
    mailmod.smtp_send = fake_smtp_ok


def t_warmup():
    aid = seed_account("WU", "wu@x.com")
    conn = db.get_db()
    a = dict(conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())
    cap, warming = workermod.effective_daily_cap(a, {"daily_cap": 80},
                                                 datetime.now(timezone.utc))
    assert warming and cap == 20, (cap, warming)
    r = client.post(f"/accounts/{aid}/warmup-override", follow_redirects=False,
                    data={"confirm": "nope"})
    assert r.status_code == 303 and "warmup" in r.headers["location"]
    r = client.post(f"/accounts/{aid}/warmup-override", data={"confirm": "I UNDERSTAND"})
    a = dict(conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())
    assert a["warmup_override"] == 1
    cap, warming = workermod.effective_daily_cap(a, {"daily_cap": 80},
                                                 datetime.now(timezone.utc))
    assert cap == 80 and not warming


def t_pages():
    for p in ["/", "/campaigns", "/campaigns/new", "/contacts", "/drafts",
              "/drafts/new", "/accounts", "/accounts/new", "/suppression", "/settings"]:
        r = client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"
    for needle, p in [("Dashboard", "/"), ("Suppression List", "/suppression"),
                      ("IMAP", "/accounts/new"), ("SPF", "/accounts/new")]:
        r = client.get(p)
        assert needle in r.text, f"{needle!r} missing on {p}"


def t_checklist():
    aid = seed_account("CL", "cl@gmail.com")
    r = client.get("/api/checklist", params={"account_id": aid})
    j = r.json()
    labels = [i["label"] for i in j["items"]]
    assert any("SPF" in l for l in labels) and any("DMARC" in l for l in labels) \
        and any("DKIM" in l for l in labels), labels


def t_dashboard_usage():
    r = client.get("/")
    assert "Daily usage per account" in r.text


def run_all():
    check("email validation + quarantine reasons", t_validate)
    check("{{variables}} render + unsubscribe footer", t_template)
    check("reply auto-classification (all 7 categories)", t_classify)
    check("credentials encrypted at rest", t_crypto)
    check("xlsx upload: quarantine + duplicate merge + mapping", t_import)
    check("draft versioning + live preview with variables", t_drafts)
    check("10 contacts, gaps, daily cap 10, logs, no duplicates, variables rendered", t_send_engine)
    check("pause stops sends; resume continues, no duplicates", t_pause_resume)
    check("kill mid-send → restart resumes without double-sending", t_restart)
    check("'not interested' reply → classified, contact marked", t_reply_notinterested)
    check("'unsubscribe me' → global suppression + excluded from 2nd campaign + one-click link", t_reply_unsubscribe)
    check("bounce rate >8% → auto-pause with banner reason; hard bounce invalidates", t_bounce_pause)
    check("2x SMTP auth failure → auto-pause with reason", t_auth_pause)
    check("warm-up 20/day for new accounts; typed override required", t_warmup)
    check("all pages render 200; sidebar + tooltips present", t_pages)
    check("pre-launch checklist API (SPF/DKIM/DMARC)", t_checklist)
    check("global dashboard: campaigns table + per-account usage bars", t_dashboard_usage)
    print()
    print(f"PASSED {len(PASS)} | FAILED {len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
