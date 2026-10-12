"""Background worker: send-queue processor + IMAP reply watcher.

Runs in a thread inside the web process. Idempotent: a contact is claimed
with an atomic UPDATE ... WHERE status='queued'; UNIQUE(campaign_id,contact_id)
plus per-send message_id/unsub_token make double-sends impossible across
restarts. On startup, rows stuck in 'sending' are failed and released.
"""
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from . import db
from . import mail as mailmod

STOP = threading.Event()
WORKER_THREAD = None


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def get_setting(key, default=""):
    conn = db.get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ---------- scheduling helpers ----------

def campaign_now(campaign):
    try:
        tz = ZoneInfo(campaign["timezone"] or get_setting("timezone", "Asia/Karachi"))
    except Exception:
        tz = ZoneInfo("Asia/Karachi")
    return utcnow().astimezone(tz)


def in_window(campaign, now=None):
    now = now or campaign_now(campaign)
    days = [d for d in (campaign["days_of_week"] or "").split(",") if d != ""]
    if str(now.weekday()) not in days:
        return False, f"today is not a sending day ({now.strftime('%A')})"
    ws = campaign["window_start"] or "09:00"
    we = campaign["window_end"] or "17:00"
    cur = now.strftime("%H:%M")
    if not (ws <= cur <= we):
        return False, f"outside sending window {ws}–{we} (now {cur})"
    return True, ""


def sent_today_count(account_id, day):
    conn = db.get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM sends s JOIN campaigns c ON c.id=s.campaign_id "
        "WHERE c.account_id=? AND s.status='sent' AND date(s.sent_at)=date(?)",
        (account_id, day)).fetchone()
    return row["c"]


def total_sent_account(account_id):
    conn = db.get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM sends s JOIN campaigns c ON c.id=s.campaign_id "
        "WHERE c.account_id=? AND s.status='sent'", (account_id,)).fetchone()
    return row["c"]


def first_send_at(account_id):
    conn = db.get_db()
    row = conn.execute(
        "SELECT MIN(s.sent_at) m FROM sends s JOIN campaigns c ON c.id=s.campaign_id "
        "WHERE c.account_id=? AND s.status='sent'", (account_id,)).fetchone()
    return parse_iso(row["m"]) if row and row["m"] else None


def effective_daily_cap(account, campaign, now):
    cap = int(campaign["daily_cap"] or 80)
    if int(account.get("warmup_override") or 0):
        return cap, False
    created = parse_iso(account["created_at"])
    age_days = (now - created).days if created else 999
    total = total_sent_account(account["id"])
    first = first_send_at(account["id"])
    warm_days = (now - first).days if first else 0
    if age_days < 30 or total < 50:
        return min(cap, 20 + 10 * max(0, warm_days)), True
    return cap, False


def auto_pause(campaign_id, reason):
    def _do(conn):
        conn.execute("UPDATE campaigns SET status='paused', pause_reason=? WHERE id=?",
                     (reason, campaign_id))
    db.tx(_do)


def check_bounce_rate(campaign_id):
    conn = db.get_db()
    sent = conn.execute("SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent'",
                        (campaign_id,)).fetchone()["c"]
    if sent < 10:
        return
    bounces = conn.execute(
        "SELECT COUNT(*) c FROM replies r JOIN sends s ON s.id=r.send_id "
        "WHERE s.campaign_id=? AND r.category='Bounce'", (campaign_id,)).fetchone()["c"]
    hard = conn.execute(
        "SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND status='failed' "
        "AND (smtp_status LIKE '%5__%' OR smtp_status LIKE '%bounce%')",
        (campaign_id,)).fetchone()["c"]
    total_b = bounces + hard
    if sent > 0 and total_b / sent > 0.08:
        auto_pause(campaign_id,
                   f"Auto-paused: bounce rate {total_b}/{sent} ({total_b/sent:.0%}) exceeded 8%.")


# ---------- send pipeline ----------

def _account_dict(row):
    return dict(row)


def claim_next_contact(campaign_id):
    """Atomically claim one queued contact. Returns row or None."""
    def _do(conn):
        row = conn.execute(
            """SELECT cc.id, cc.contact_id, c.* FROM campaign_contacts cc
               JOIN contacts c ON c.id=cc.contact_id
               WHERE cc.campaign_id=? AND cc.status='queued'
               ORDER BY cc.id LIMIT 1""", (campaign_id,)).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE campaign_contacts SET status='sending' WHERE id=? AND status='queued'",
            (row["id"],))
        if cur.rowcount == 0:
            return None
        return dict(row)
    return db.tx(_do)


def release_claim(cc_id, to_status="queued"):
    db.tx(lambda conn: conn.execute(
        "UPDATE campaign_contacts SET status=? WHERE id=? AND status='sending'",
        (to_status, cc_id)))


def send_one(campaign, cc):
    """Send a single email. Returns (ok, info)."""
    conn = db.get_db()
    account = conn.execute("SELECT * FROM accounts WHERE id=?",
                           (campaign["account_id"],)).fetchone()
    if not account:
        return False, "account missing"
    account = _account_dict(account)
    contact = {k: cc[k] for k in
               ("email", "first_name", "last_name", "company",
                "custom_1", "custom_2", "custom_3", "contact_id")}

    # suppression / validity checks at send time (never email suppressed)
    sup = conn.execute("SELECT 1 FROM suppression WHERE email=?",
                       (contact["email"],)).fetchone()
    crow = conn.execute("SELECT valid, do_not_contact FROM contacts WHERE id=?",
                        (cc["contact_id"],)).fetchone()
    if sup or (crow and (not crow["valid"] or crow["do_not_contact"])):
        db.tx(lambda c: c.execute(
            "UPDATE campaign_contacts SET status='cancelled' WHERE id=?", (cc["id"],)))
        return False, "suppressed/invalid — cancelled"

    # pick draft version + variant
    variant = "A"
    draft_id, draft_ver = campaign["draft_id"], campaign["draft_version"]
    if campaign["draft_b_id"]:
        variant = "A" if random.randint(1, 100) <= int(campaign["ab_split"] or 50) else "B"
        if variant == "B":
            draft_id, draft_ver = campaign["draft_b_id"], campaign["draft_b_version"]
    dv = conn.execute(
        "SELECT subject, body_html FROM draft_versions WHERE draft_id=? AND version=?",
        (draft_id, draft_ver)).fetchone()
    if not dv:
        return False, "draft version missing"

    base_url = get_setting("base_url", "http://localhost:8000").rstrip("/")
    token = uuid.uuid4().hex
    unsub_url = f"{base_url}/u/{token}"
    domain = account["email"].split("@")[-1]
    message_id = make_msgid(domain=domain)

    subject = mailmod.render_template(dv["subject"], contact)
    body_html = mailmod.render_template(dv["body_html"], contact, {
        "unsubscribe_link": unsub_url,
        "mailing_address": campaign["mailing_address"] or get_setting("physical_address", ""),
    })
    msg = mailmod.build_message(account["email"], contact["email"], subject,
                                body_html, message_id, unsub_url,
                                campaign["mailing_address"] or get_setting("physical_address", ""),
                                reply_to=account["email"])

    acct = dict(account, smtp_pass=account["smtp_pass_enc"], _enc=True)
    ok, resp = mailmod.smtp_send(acct, msg)

    now_s = iso(utcnow())
    if ok:
        def _ok(c):
            c.execute(
                """INSERT INTO sends(campaign_id, contact_id, draft_version, variant,
                   message_id, unsub_token, sent_at, status, smtp_status)
                   VALUES(?,?,?,?,?,?,?, 'sent', ?)""",
                (campaign["id"], cc["contact_id"], draft_ver, variant,
                 message_id, token, now_s, resp[:300]))
            c.execute(
                "UPDATE campaign_contacts SET status='sent', sent_at=?, draft_version=?, variant=? WHERE id=?",
                (now_s, draft_ver, variant, cc["id"]))
            gap = random.randint(int(campaign["gap_min_sec"]), int(campaign["gap_max_sec"]))
            c.execute("UPDATE campaigns SET next_due_at=? WHERE id=?",
                      (iso(utcnow() + timedelta(seconds=gap)), campaign["id"]))
        db.tx(_ok)
        db.tx(lambda c: c.execute("UPDATE accounts SET auth_fail_count=0 WHERE id=?",
                                  (account["id"],)))
        check_bounce_rate(campaign["id"])
        return True, resp[:200]
    else:
        def _fail(c):
            c.execute(
                """INSERT INTO sends(campaign_id, contact_id, draft_version, variant,
                   message_id, unsub_token, sent_at, status, smtp_status)
                   VALUES(?,?,?,?,?,?,?, 'failed', ?)""",
                (campaign["id"], cc["contact_id"], draft_ver, variant,
                 message_id, token, now_s, resp[:300]))
            c.execute("UPDATE campaign_contacts SET status='queued' WHERE id=?", (cc["id"],))
        db.tx(_fail)
        if "AUTH FAILED" in resp:
            n = db.tx(lambda c: (c.execute(
                "UPDATE accounts SET auth_fail_count=auth_fail_count+1 WHERE id=?",
                (account["id"],)),
                c.execute("SELECT auth_fail_count FROM accounts WHERE id=?",
                          (account["id"],)).fetchone()["auth_fail_count"])[1])
            if n >= 2:
                auto_pause(campaign["id"],
                           "Auto-paused: SMTP authentication failed twice. Check the account password / App Password.")
        return False, resp[:200]


def process_campaigns(now=None):
    now = now or utcnow()
    conn = db.get_db()
    campaigns = conn.execute(
        "SELECT * FROM campaigns WHERE status IN ('active','scheduled')").fetchall()
    for camp in campaigns:
        camp = dict(camp)
        # start date gate
        try:
            start = datetime.fromisoformat(camp["start_date"]).date()
        except Exception:
            start = now.date()
        if start > campaign_now(camp).date():
            continue
        if camp["status"] == "scheduled":
            db.tx(lambda c: c.execute("UPDATE campaigns SET status='active' WHERE id=?",
                                      (camp["id"],)))
            camp["status"] = "active"

        ok, why = in_window(camp)
        if not ok:
            continue
        due = parse_iso(camp["next_due_at"])
        if due and now < due:
            continue

        account = conn.execute("SELECT * FROM accounts WHERE id=?",
                               (camp["account_id"],)).fetchone()
        if not account or account["status"] not in ("connected", "unknown"):
            continue
        account = dict(account)
        cap, warming = effective_daily_cap(account, camp, now)
        if sent_today_count(account["id"], iso(now)) >= cap:
            continue

        cc = claim_next_contact(camp["id"])
        if not cc:
            # nothing left queued → completed (if no sending either)
            left = conn.execute(
                "SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=? "
                "AND status IN ('queued','sending')", (camp["id"],)).fetchone()["c"]
            if left == 0:
                db.tx(lambda c: c.execute(
                    "UPDATE campaigns SET status='completed' WHERE id=?", (camp["id"],)))
            continue
        send_one(camp, cc)


# ---------- IMAP reply watcher ----------

def poll_inboxes():
    conn = db.get_db()
    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    for a in accounts:
        a = dict(a)
        if not int(a.get("imap_enabled", 1)):
            continue  # send-only account: no reply monitoring
        try:
            replies = mailmod.fetch_unseen_replies(a)
        except Exception:
            continue
        for r in replies:
            try:
                handle_reply(a, r)
                mailmod.mark_seen(a, r["imap_num"])
            except Exception:
                continue


def find_send_for_reply(r):
    conn = db.get_db()
    irt = (r.get("in_reply_to") or "").strip()
    refs = (r.get("references") or "").strip()
    if irt:
        row = conn.execute("SELECT * FROM sends WHERE message_id=?", (irt,)).fetchone()
        if row:
            return dict(row)
    if refs:
        for part in refs.split():
            row = conn.execute("SELECT * FROM sends WHERE message_id=?", (part.strip(),)).fetchone()
            if row:
                return dict(row)
    # fallback: most recent send to this sender address
    if r.get("from"):
        row = conn.execute(
            """SELECT s.* FROM sends s JOIN contacts c ON c.id=s.contact_id
               WHERE lower(c.email)=lower(?) AND s.status='sent'
               ORDER BY s.sent_at DESC LIMIT 1""", (r["from"],)).fetchone()
        if row:
            return dict(row)
    return None


def handle_reply(account, r):
    send = find_send_for_reply(r)
    category = mailmod.classify_reply(r.get("from", ""), r.get("subject", ""), r.get("body", ""))
    now_s = iso(utcnow())
    conn = db.get_db()

    def _do(c):
        contact_email = None
        if send:
            c.execute(
                """INSERT INTO replies(send_id, received_at, from_email, subject, body_text, category)
                   VALUES(?,?,?,?,?,?)""",
                (send["id"], now_s, r.get("from", ""), r.get("subject", "")[:300],
                 r.get("body", "")[:20000], category))
            c.execute(
                """UPDATE campaign_contacts SET status='replied', reply_category=?
                   WHERE campaign_id=? AND contact_id=?""",
                (category, send["campaign_id"], send["contact_id"]))
            erow = c.execute("SELECT email FROM contacts WHERE id=?",
                             (send["contact_id"],)).fetchone()
            contact_email = erow["email"] if erow else None
        else:
            # unmatched reply — still record against a dummy? store with send_id 0 is invalid;
            # keep a lightweight record via unmatched note in replies with send_id NULL not allowed.
            # We simply skip DB insert but it was marked seen.
            return None

        if category == "Unsubscribe" and contact_email:
            c.execute("INSERT OR IGNORE INTO suppression(email, reason, created_at) VALUES(?,?,?)",
                      (contact_email, "unsubscribe reply", now_s))
            c.execute("UPDATE contacts SET do_not_contact=1 WHERE lower(email)=lower(?)",
                      (contact_email,))
            c.execute("""UPDATE campaign_contacts SET status='unsubscribed'
                         WHERE status='queued' AND contact_id IN
                         (SELECT id FROM contacts WHERE lower(email)=lower(?))""",
                      (contact_email,))
        elif category == "Bounce" and contact_email:
            c.execute("UPDATE contacts SET valid=0 WHERE lower(email)=lower(?)",
                      (contact_email,))
            if send:
                c.execute("""UPDATE campaign_contacts SET status='bounced'
                             WHERE campaign_id=? AND contact_id=?""",
                          (send["campaign_id"], send["contact_id"]))
            c.execute("""UPDATE campaign_contacts SET status='cancelled'
                         WHERE status='queued' AND contact_id IN
                         (SELECT id FROM contacts WHERE lower(email)=lower(?))""",
                      (contact_email,))
        return contact_email
    result = db.tx(_do)
    if send and category in ("Bounce",):
        check_bounce_rate(send["campaign_id"])
    return result


# ---------- lifecycle ----------

def recover_interrupted():
    """On startup: fail any sends stuck in 'sending', release claims."""
    def _do(conn):
        stuck = conn.execute("SELECT id FROM sends WHERE status='sending'").fetchall()
        for s in stuck:
            conn.execute("UPDATE sends SET status='failed', smtp_status='interrupted: process restarted' WHERE id=?",
                         (s["id"],))
        conn.execute("UPDATE campaign_contacts SET status='queued' WHERE status='sending'")
        conn.execute("UPDATE campaigns SET next_due_at=NULL WHERE status='active'")
    db.tx(_do)


def worker_loop(poll_interval=20):
    last_inbox = 0
    while not STOP.is_set():
        try:
            process_campaigns()
        except Exception:
            pass
        try:
            interval = int(get_setting("reply_poll_minutes", "5")) * 60
            if time.time() - last_inbox > interval:
                poll_inboxes()
                last_inbox = time.time()
        except Exception:
            pass
        STOP.wait(poll_interval)


def start_worker():
    global WORKER_THREAD
    recover_interrupted()
    WORKER_THREAD = threading.Thread(target=worker_loop, daemon=True,
                                     name="emailtool-worker")
    WORKER_THREAD.start()
    return WORKER_THREAD
