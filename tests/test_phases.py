"""Phase 2-5 tests: spintax, custom fields, follow-up sequences, tracking,
seed placement, pagination, timezone dropdown, jitter interval.

Run: .venv/bin/python tests/test_phases.py
"""
import io
import os
import random
import sys
import tempfile
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["EMAILTOOL_NO_WORKER"] = "1"

TMP = tempfile.mkdtemp(prefix="emailtool-phases-")

from app import db, crypto  # noqa: E402
from app import mail as mailmod  # noqa: E402
from app import worker as workermod  # noqa: E402

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

# ---------- seeds ----------

def seed_account(email="sender@example.com"):
    def _do(conn):
        cur = conn.execute(
            """INSERT INTO accounts(label, email, smtp_host, smtp_port, smtp_user, smtp_pass_enc,
               smtp_enc, imap_host, imap_port, imap_user, imap_pass_enc, imap_enc, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'connected', ?)""",
            ("T", email, "smtp.example.com", 587, email, crypto.encrypt("secret"),
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
                """INSERT INTO contacts(list_id, email, first_name, company, custom_fields, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (lid, c["email"], c.get("first_name", ""), c.get("company", ""),
                 c.get("custom_fields", "{}"), utcnow_s()))
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


SENT = []


def fake_smtp_ok(account, msg, timeout=30):
    SENT.append(msg)
    return True, "250 OK"


def make_campaign(list_id, account_id, draft_id, extra_form=None):
    # isolate: earlier tests' campaigns must not send during this test
    db.tx(lambda c: c.execute("UPDATE campaigns SET status='completed'"))
    wd = str(datetime.now(timezone.utc).weekday())
    data = {"name": "C", "list_id": str(list_id), "account_id": str(account_id),
            "draft_id": str(draft_id), "ab_split": "50",
            "window_start": "00:00", "window_end": "23:59",
            "gap_min_sec": "60", "gap_max_sec": "60", "daily_cap": "100",
            "start_date": datetime.now(timezone.utc).date().isoformat(),
            "mailing_address": "123 St", "days": [wd]}
    if extra_form:
        data.update(extra_form)
    r = client.post("/campaigns", data=data, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text[:300])
    cid = int(r.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
    return cid


# ---------- spintax ----------

def t_spintax_basic():
    v = mailmod.process_spintax("{Hi|Hello|Hey}", random.Random(0))
    assert v in ("Hi", "Hello", "Hey"), v


def t_spintax_nested():
    v = mailmod.process_spintax("{a|{b|c}}", random.Random(1))
    assert v in ("a", "b", "c"), v


def t_spintax_preserves_vars_and_braces():
    assert mailmod.process_spintax("{{first_name}}", random.Random(0)) == "{{first_name}}"
    assert mailmod.process_spintax("{CSS} block", random.Random(0)) == "{CSS} block"
    out = mailmod.process_spintax("{Hi|Hello} {{first_name}}", random.Random(2))
    assert out in ("Hi {{first_name}}", "Hello {{first_name}}"), out


def t_spintax_preview():
    vs = mailmod.preview_variations("{a|b|c} x", 3, seed=7)
    assert len(vs) == 3 and len(set(vs)) == 3, vs
    assert all(v in ("a x", "b x", "c x") for v in vs), vs


def t_render_custom_fields():
    out = mailmod.render_template("Hi {{first_name}} from {{city}}, {{industry}}!",
                                  {"first_name": "Ali",
                                   "custom_fields": '{"city": "Lahore", "industry": "Real Estate"}'})
    assert out == "Hi Ali from Lahore, Real Estate!", out
    # unknown vars stay as-is
    out2 = mailmod.render_template("{{nope}}", {"custom_fields": "{}"})
    assert out2 == "{{nope}}", out2


# ---------- follow-up sequences ----------

def t_followup_steps_stored():
    acc = seed_account("f1@example.com")
    lid = seed_list("FL1", [{"email": "c1@x.com", "first_name": "C1"}])
    d1 = seed_draft("Step1", "S1", "<p>one {{first_name}}</p>")
    d2 = seed_draft("Step2", "S2", "<p>two</p>")
    cid = make_campaign(lid, acc, d1, {"fup_draft_0": str(d2), "fup_delay_0": "2",
                                       "fup_draft_1": str(d2), "fup_delay_1": "5"})
    rows = db.get_db().execute(
        "SELECT step_order, delay_days FROM campaign_steps WHERE campaign_id=? ORDER BY step_order",
        (cid,)).fetchall()
    assert [(r["step_order"], r["delay_days"]) for r in rows] == [(2, 2), (3, 5)], rows


def t_followup_send_and_thread():
    acc = seed_account("f2@example.com")
    lid = seed_list("FL2", [{"email": "c2@x.com", "first_name": "C2"}])
    d1 = seed_draft("S1b", "Sub1", "<p>one</p>")
    d2 = seed_draft("S2b", "Sub2", "<p>two</p>")
    cid = make_campaign(lid, acc, d1, {"fup_draft_0": str(d2), "fup_delay_0": "2"})
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        workermod.process_campaigns()
        assert len(SENT) == 1, len(SENT)
        cc = db.get_db().execute(
            "SELECT status, step, step_due_at FROM campaign_contacts WHERE campaign_id=?",
            (cid,)).fetchone()
        assert cc["status"] == "queued" and cc["step"] == 2 and cc["step_due_at"], dict(cc)
        # not due yet -> no second send
        workermod.process_campaigns()
        assert len(SENT) == 1, len(SENT)
        # make it due -> step 2 sends with In-Reply-To threading
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.tx(lambda c: (c.execute(
            "UPDATE campaign_contacts SET step_due_at=? WHERE campaign_id=?", (past, cid)),
            c.execute("UPDATE campaigns SET next_due_at=? WHERE id=?", (past, cid))))
        workermod.process_campaigns()
        assert len(SENT) == 2, len(SENT)
        first_mid = SENT[0]["Message-ID"]
        assert SENT[1]["In-Reply-To"] == first_mid, dict(SENT[1])
        assert SENT[1]["References"] == first_mid
        # step 2 used the follow-up draft's subject
        assert SENT[1]["Subject"] == "Sub2", SENT[1]["Subject"]
        cc = db.get_db().execute(
            "SELECT status, step FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()
        assert cc["status"] == "sent" and cc["step"] == 2, dict(cc)
    finally:
        mailmod.smtp_send = old


def t_followup_cancelled_on_reply():
    acc = seed_account("f3@example.com")
    lid = seed_list("FL3", [{"email": "c3@x.com", "first_name": "C3"}])
    d1 = seed_draft("S1c", "Sub1", "<p>one</p>")
    d2 = seed_draft("S2c", "Sub2", "<p>two</p>")
    cid = make_campaign(lid, acc, d1, {"fup_draft_0": str(d2), "fup_delay_0": "2"})
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        workermod.process_campaigns()
        assert len(SENT) == 1
        # contact replies -> follow-up must be cancelled
        workermod.handle_reply({"id": acc}, {
            "from": "c3@x.com", "subject": "Re: Sub1", "body": "not interested thanks",
            "in_reply_to": "", "references": "", "imap_num": 1,
            "message_id": "<r1>", "date": ""})
        cc = db.get_db().execute(
            "SELECT status FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()
        assert cc["status"] == "replied", dict(cc)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.tx(lambda c: c.execute(
            "UPDATE campaign_contacts SET step_due_at=? WHERE campaign_id=?", (past, cid)))
        workermod.process_campaigns()
        assert len(SENT) == 1, "follow-up was sent despite the reply"
    finally:
        mailmod.smtp_send = old


# ---------- tracking ----------

def t_open_tracking():
    acc = seed_account("f4@example.com")
    lid = seed_list("FL4", [{"email": "c4@x.com", "first_name": "C4"}])
    d1 = seed_draft("S1d", "Sub", '<p>hi <a href="https://example.com">x</a></p>')
    cid = make_campaign(lid, acc, d1, {"open_tracking": "1", "click_tracking": "1"})
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        workermod.process_campaigns()
        assert len(SENT) == 1
        # pixel was injected into the outgoing html (decode transfer encoding first)
        html = SENT[0].get_body(("html",)).get_content()
        assert "/t/o/" in html and ".png" in html, html[:200]
        assert "/t/c/" in html, html[:300]
        tok = db.get_db().execute(
            "SELECT unsub_token FROM sends WHERE campaign_id=?", (cid,)).fetchone()["unsub_token"]
        r = client.get(f"/t/o/{tok}.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        opened = db.get_db().execute(
            "SELECT opened_at FROM sends WHERE campaign_id=?", (cid,)).fetchone()["opened_at"]
        assert opened, "opened_at not set"
    finally:
        mailmod.smtp_send = old


def t_open_tracking_off():
    acc = seed_account("f5@example.com")
    lid = seed_list("FL5", [{"email": "c5@x.com", "first_name": "C5"}])
    d1 = seed_draft("S1e", "Sub", "<p>hi</p>")
    cid = make_campaign(lid, acc, d1)
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        workermod.process_campaigns()
        tok = db.get_db().execute(
            "SELECT unsub_token FROM sends WHERE campaign_id=?", (cid,)).fetchone()["unsub_token"]
        r = client.get(f"/t/o/{tok}.png")
        assert r.status_code == 200
        opened = db.get_db().execute(
            "SELECT opened_at FROM sends WHERE campaign_id=?", (cid,)).fetchone()["opened_at"]
        assert opened is None, "opened_at set even though tracking is off"
    finally:
        mailmod.smtp_send = old


def t_click_tracking():
    acc = seed_account("f6@example.com")
    lid = seed_list("FL6", [{"email": "c6@x.com", "first_name": "C6"}])
    d1 = seed_draft("S1f", "Sub", "<p>hi</p>")
    cid = make_campaign(lid, acc, d1, {"click_tracking": "1"})
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        workermod.process_campaigns()
        tok = db.get_db().execute(
            "SELECT unsub_token, id FROM sends WHERE campaign_id=?", (cid,)).fetchone()
        r = client.get(f"/t/c/{tok['unsub_token']}", params={"url": "https://example.com/offer"},
                       follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "https://example.com/offer", \
            (r.status_code, r.headers.get("location"))
        n = db.get_db().execute("SELECT COUNT(*) c FROM clicks WHERE send_id=?",
                                (tok["id"],)).fetchone()["c"]
        assert n == 1, n
        # bad target rejected
        r = client.get(f"/t/c/{tok['unsub_token']}", params={"url": "javascript:alert(1)"})
        assert r.status_code == 400, r.status_code
    finally:
        mailmod.smtp_send = old


# ---------- seed placement ----------

class FakeIMAP:
    def __init__(self, hits):
        self.hits = hits  # mailbox -> bool

    def select(self, mailbox, readonly=True):
        return ("ok", [b"1"]) if mailbox.strip('"') in self.hits else ("no", [])

    def search(self, *a):
        return ("ok", [b"1"])

    def logout(self):
        pass


def t_seed_placement_inbox():
    seed = {"imap_host": "h", "imap_port": 993, "imap_user": "u",
            "imap_pass_enc": crypto.encrypt("p"), "imap_enc": "SSL"}
    old = mailmod._imap_connect
    mailmod._imap_connect = lambda acct, pw, timeout=30: FakeIMAP({"INBOX": True})
    try:
        r = mailmod.check_seed_placement(seed, "<mid123>")
        assert r == {"placement": "inbox", "folder": "INBOX"}, r
    finally:
        mailmod._imap_connect = old


def t_seed_placement_spam():
    seed = {"imap_host": "h", "imap_port": 993, "imap_user": "u",
            "imap_pass_enc": crypto.encrypt("p"), "imap_enc": "SSL"}
    old = mailmod._imap_connect
    mailmod._imap_connect = lambda acct, pw, timeout=30: FakeIMAP({"[Gmail]/Spam": True})
    try:
        r = mailmod.check_seed_placement(seed, "<mid123>")
        assert r["placement"] == "spam" and r["folder"] == "[Gmail]/Spam", r
    finally:
        mailmod._imap_connect = old


def t_placement_score():
    s = mailmod.placement_score([{"placement": "inbox"}, {"placement": "inbox"},
                                 {"placement": "spam"}, {"placement": "missing"}])
    assert s == {"total": 4, "inbox_pct": 50.0, "spam_pct": 25.0, "missing_pct": 25.0}, s


# ---------- pagination / settings / misc UI ----------

def t_list_pagination():
    contacts = [{"email": f"u{i:02d}@x.com", "first_name": f"U{i:02d}"} for i in range(60)]
    lid = seed_list("BigList", contacts)
    r = client.get(f"/contacts/lists/{lid}", params={"per_page": 50})
    assert r.status_code == 200
    assert "u59@x.com" not in r.text and "u00@x.com" in r.text
    assert "page 1/2" in r.text, r.text[-500:]
    r = client.get(f"/contacts/lists/{lid}", params={"per_page": 50, "page": 2})
    assert "u59@x.com" in r.text and "u00@x.com" not in r.text
    # search narrows
    r = client.get(f"/contacts/lists/{lid}", params={"q": "u59@x.com"})
    assert "u59@x.com" in r.text and "u00@x.com" not in r.text
    # status filter
    r = client.get(f"/contacts/lists/{lid}", params={"status": "ok"})
    assert r.status_code == 200


def t_campaign_search_and_sends_page():
    r = client.get("/sends")
    assert r.status_code == 200 and "Send log" in r.text
    r = client.get("/campaigns", params={"q": "zzz-no-such-campaign"})
    assert r.status_code == 200 and "No campaigns yet" in r.text


def t_timezone_dropdown():
    r = client.get("/settings")
    assert r.status_code == 200
    assert "<select" in r.text and "Asia/Karachi" in r.text and "America/New_York" in r.text


def t_jitter_interval_setting():
    r = client.post("/settings", data={"timezone": "UTC", "base_url": "http://x",
                                       "physical_address": "", "reply_poll_minutes": "5-8"},
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    v = db.get_db().execute("SELECT value FROM settings WHERE key='reply_poll_minutes'").fetchone()["value"]
    assert v == "5-8", v
    for _ in range(20):
        assert 300 <= workermod.reply_interval_sec() <= 480
    r = client.post("/settings", data={"timezone": "UTC", "base_url": "http://x",
                                       "physical_address": "", "reply_poll_minutes": "7"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert workermod.reply_interval_sec() == 420


def t_slots_window():
    now = workermod.utcnow()
    wd = now.weekday()
    camp = {"schedule_mode": "slots",
            "send_slots": f'[{{"day": {wd}, "start": "00:00", "end": "23:59"}}]'}
    ok, _ = workermod.in_window(camp, now)
    assert ok
    camp2 = {"schedule_mode": "slots",
             "send_slots": f'[{{"day": {(wd + 1) % 7}, "start": "00:00", "end": "23:59"}}]'}
    ok2, _ = workermod.in_window(camp2, now)
    assert not ok2


def t_hourly_cap_enforced():
    acc = seed_account("f7@example.com")
    lid = seed_list("FL7", [{"email": f"h{i}@x.com"} for i in range(3)])
    d1 = seed_draft("S1g", "Sub", "<p>hi</p>")
    cid = make_campaign(lid, acc, d1, {"hourly_cap": "1"})
    old = mailmod.smtp_send
    mailmod.smtp_send = fake_smtp_ok
    try:
        SENT.clear()
        workermod.process_campaigns()
        assert len(SENT) == 1, f"hourly cap ignored, sent {len(SENT)}"
    finally:
        mailmod.smtp_send = old


if __name__ == "__main__":
    check("spintax basic", t_spintax_basic)
    check("spintax nested", t_spintax_nested)
    check("spintax preserves {{vars}} and braceless groups", t_spintax_preserves_vars_and_braces)
    check("spintax preview variations", t_spintax_preview)
    check("render_template custom_fields", t_render_custom_fields)
    check("follow-up steps stored", t_followup_steps_stored)
    check("follow-up send + In-Reply-To threading", t_followup_send_and_thread)
    check("follow-up cancelled on reply", t_followup_cancelled_on_reply)
    check("open tracking on", t_open_tracking)
    check("open tracking off", t_open_tracking_off)
    check("click tracking redirect + log", t_click_tracking)
    check("seed placement: inbox", t_seed_placement_inbox)
    check("seed placement: spam", t_seed_placement_spam)
    check("placement score math", t_placement_score)
    check("list pagination + search + status", t_list_pagination)
    check("campaign search + sends page", t_campaign_search_and_sends_page)
    check("timezone dropdown", t_timezone_dropdown)
    check("jitter reply interval", t_jitter_interval_setting)
    check("slots schedule window", t_slots_window)
    check("hourly cap enforced", t_hourly_cap_enforced)
    print(f"\nPASSED {len(PASS)} | FAILED {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
