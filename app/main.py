"""Email Automation Tool — FastAPI app. Server-rendered UI (Jinja2) + worker thread."""
import csv
import io
import json
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from . import worker as worker_mod
from . import mail as mailmod
from . import auth as authmod
from .crypto import encrypt

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(os.path.dirname(BASE), "data", "uploads")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if os.environ.get("EMAILTOOL_NO_WORKER") != "1":
        worker_mod.start_worker()
    yield
    worker_mod.STOP.set()


app = FastAPI(title="Email Automation Tool", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


# ---------------- auth: first-run setup, session gate, sudo ----------------

PUBLIC_PREFIXES = ("/login", "/logout", "/setup", "/u/", "/t/", "/static")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    if authmod.needs_setup():
        if path != "/setup":
            return RedirectResponse("/setup", status_code=303)
        return await call_next(request)
    session = authmod.get_session(request.cookies.get(authmod.SESSION_COOKIE, ""))
    if not session:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)
    request.state.session = session
    return await call_next(request)


def current_session(request: Request):
    return getattr(request.state, "session", None)


def sudo_or_redirect(request: Request):
    """Returns None if sudo is active, else a redirect to the sudo page."""
    if authmod.has_sudo(current_session(request)):
        return None
    return RedirectResponse(f"/sudo?next={request.url.path}", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if not authmod.needs_setup():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"request": request})


@app.post("/setup")
def setup_create(username: str = Form(...), password: str = Form(...),
                 password2: str = Form(...)):
    if not authmod.needs_setup():
        return RedirectResponse("/login", status_code=303)
    username = (username or "").strip() or "admin"
    if len(password or "") < 8:
        return HTMLResponse("<h2>Password must be at least 8 characters.</h2>"
                            '<p><a href="/setup">Back</a></p>', status_code=400)
    if password != password2:
        return HTMLResponse("<h2>Passwords do not match.</h2>"
                            '<p><a href="/setup">Back</a></p>', status_code=400)
    authmod.set_setting("admin_username", username)
    authmod.set_setting("admin_password_hash", authmod.hash_password(password))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(authmod.SESSION_COOKIE, authmod.create_session(),
                    httponly=True, samesite="lax", max_age=7 * 86400)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not authmod.needs_setup():
        s = authmod.get_session(request.cookies.get(authmod.SESSION_COOKIE, ""))
        if s:
            return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"request": request, "next": next})


@app.post("/login")
def login_do(request: Request, username: str = Form(...), password: str = Form(...),
             next: str = Form("/")):
    if authmod.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if not authmod.authenticate(username, password):
        return templates.TemplateResponse(request, "login.html",
                                          {"request": request, "next": next,
                                           "error": "Invalid username or password."},
                                          status_code=401)
    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie(authmod.SESSION_COOKIE, authmod.create_session(),
                    httponly=True, samesite="lax", max_age=7 * 86400)
    return resp


@app.get("/logout")
def logout(request: Request):
    authmod.destroy_session(request.cookies.get(authmod.SESSION_COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(authmod.SESSION_COOKIE)
    return resp


@app.get("/sudo", response_class=HTMLResponse)
def sudo_page(request: Request, next: str = "/accounts"):
    return templates.TemplateResponse(request, "sudo.html",
                                      {"request": request, "next": next})


@app.post("/sudo")
def sudo_do(request: Request, password: str = Form(...), next: str = Form("/accounts")):
    session = current_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    username = authmod.get_setting("admin_username", "admin")
    if not authmod.authenticate(username, password):
        return templates.TemplateResponse(request, "sudo.html",
                                          {"request": request, "next": next,
                                           "error": "Wrong password."},
                                          status_code=401)
    token = authmod._unsign(request.cookies.get(authmod.SESSION_COOKIE, ""))
    authmod.grant_sudo(token)
    return RedirectResponse(next or "/accounts", status_code=303)


def utcnow_s():
    return datetime.now(timezone.utc).isoformat()


def base_ctx(request: Request, active="dashboard", **kw):
    conn = db.get_db()
    paused = [dict(r) for r in conn.execute(
        "SELECT id, name, pause_reason FROM campaigns WHERE status='paused'").fetchall()]
    ctx = dict(request=request, active=active, paused_campaigns=paused,
               flash=request.query_params.get("flash"),
               flash_kind=request.query_params.get("kind", "info"))
    ctx.update(kw)
    return ctx


def redir(url, flash=None, kind="info"):
    if flash:
        url += ("&" if "?" in url else "?") + f"flash={flash}&kind={kind}"
    return RedirectResponse(url, status_code=303)


def page_params(request: Request, default_per=50):
    """?page=&per_page=&q=&status= — shared by all paginated list pages."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        per = int(request.query_params.get("per_page", str(default_per)))
    except (ValueError, TypeError):
        per = default_per
    per = max(10, min(200, per))
    return page, per, (request.query_params.get("q") or "").strip(), \
        (request.query_params.get("status") or "").strip()


def pager(total: int, page: int, per: int):
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)
    return {"total": total, "page": page, "per": per, "pages": pages,
            "offset": (page - 1) * per,
            "has_prev": page > 1, "has_next": page < pages}


# ---------------- dashboard ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = db.get_db()
    campaigns = []
    for c in conn.execute("SELECT id, name, status FROM campaigns ORDER BY id DESC").fetchall():
        campaigns.append({**dict(c), **_campaign_stats(c["id"])})
    usage = []
    for a in conn.execute("SELECT * FROM accounts").fetchall():
        a = dict(a)
        today = datetime.now(timezone.utc).date().isoformat()
        sent = worker_mod.sent_today_count(a["id"], today)
        cap, warming = worker_mod.effective_daily_cap(a, {"daily_cap": 80}, datetime.now(timezone.utc))
        usage.append({"label": a["label"], "email": a["email"], "sent": sent,
                      "cap": cap, "warming": warming,
                      "pct": min(100, int(sent / max(1, cap) * 100))})
    return templates.TemplateResponse(request, "dashboard.html",
                                      base_ctx(request, campaigns=campaigns, usage=usage))


def _campaign_stats(cid):
    conn = db.get_db()
    total = conn.execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?",
                         (cid,)).fetchone()["c"]
    sent = conn.execute("SELECT COUNT(*) c FROM sends WHERE campaign_id=? AND status='sent'",
                        (cid,)).fetchone()["c"]
    queued = conn.execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=? AND status='queued'",
                          (cid,)).fetchone()["c"]
    bounced = conn.execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=? AND status='bounced'",
                           (cid,)).fetchone()["c"]
    replies = conn.execute(
        "SELECT COUNT(*) c FROM replies r JOIN sends s ON s.id=r.send_id WHERE s.campaign_id=?",
        (cid,)).fetchone()["c"]
    unsub = conn.execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=? AND status='unsubscribed'",
                         (cid,)).fetchone()["c"]
    by_cat = {}
    for r in conn.execute(
            "SELECT r.category, COUNT(*) c FROM replies r JOIN sends s ON s.id=r.send_id "
            "WHERE s.campaign_id=? GROUP BY r.category", (cid,)).fetchall():
        by_cat[r["category"]] = r["c"]
    for k in ["Interested", "Not Interested", "Unsubscribe", "Out of Office",
              "Bounce", "Question", "Other"]:
        by_cat.setdefault(k, 0)
    return {"total": total, "sent": sent, "queued": queued, "bounced": bounced,
            "replies": replies, "unsub": unsub, "by_cat": by_cat,
            "reply_rate": round(replies / sent * 100, 1) if sent else 0}


# ---------------- campaigns ----------------

@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request):
    page, per, q, status = page_params(request, 20)
    where, params = [], []
    if q:
        where.append("c.name LIKE ?"); params.append(f"%{q}%")
    if status:
        where.append("c.status = ?"); params.append(status)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    conn = db.get_db()
    total = conn.execute(f"SELECT COUNT(*) c FROM campaigns c {w}", params).fetchone()["c"]
    pg = pager(total, page, per)
    rows = []
    for c in conn.execute(f"""SELECT c.*, l.name list_name, a.label account_label
                             FROM campaigns c JOIN contact_lists l ON l.id=c.list_id
                             JOIN accounts a ON a.id=c.account_id
                             {w} ORDER BY c.id DESC LIMIT ? OFFSET ?""",
                          params + [pg["per"], pg["offset"]]).fetchall():
        rows.append({**dict(c), **_campaign_stats(c["id"])})
    statuses = [r["status"] for r in conn.execute(
        "SELECT DISTINCT status FROM campaigns ORDER BY status").fetchall()]
    return templates.TemplateResponse(request, "campaigns.html", base_ctx(
        request, "campaigns", campaigns=rows, pg=pg, q=q, status=status,
        statuses=statuses))


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new(request: Request):
    conn = db.get_db()
    lists = [dict(r) for r in conn.execute(
        """SELECT l.*, (SELECT COUNT(*) FROM contacts WHERE list_id=l.id) count
           FROM contact_lists l ORDER BY l.id DESC""").fetchall()]
    accounts = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall():
        a = dict(a)
        cap, warming = worker_mod.effective_daily_cap(a, {"daily_cap": 80},
                                                      datetime.now(timezone.utc))
        a["warming"], a["warm_cap"] = warming, cap
        accounts.append(a)
    drafts = [dict(r) for r in conn.execute(
        """SELECT d.id, d.name, dv.version, dv.subject FROM drafts d
           JOIN draft_versions dv ON dv.draft_id=d.id
           WHERE dv.version=(SELECT MAX(version) FROM draft_versions WHERE draft_id=d.id)
           ORDER BY d.id DESC""").fetchall()]
    s = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    return templates.TemplateResponse(request, "campaign_new.html", base_ctx(
        request, "campaigns", lists=lists, accounts=accounts, drafts=drafts,
        today=date.today().isoformat(), physical_address=s.get("physical_address", "")))


@app.post("/campaigns")
async def campaign_create(request: Request):
    form = await request.form()
    def g(k, d=""):
        return form.get(k, d)
    days = ",".join(sorted(form.getlist("days"), key=int)) if form.getlist("days") else "0,1,2,3,4"
    dv = db.get_db().execute(
        "SELECT MAX(version) v FROM draft_versions WHERE draft_id=?", (g("draft_id"),)).fetchone()
    dv_b = None
    if g("draft_b_id"):
        dv_b = db.get_db().execute(
            "SELECT MAX(version) v FROM draft_versions WHERE draft_id=?", (g("draft_b_id"),)).fetchone()["v"]
    s = {r["key"]: r["value"] for r in db.get_db().execute("SELECT key, value FROM settings")}
    # --- schedule mode: continuous (days+window) or explicit time slots ---
    schedule_mode = g("schedule_mode", "continuous")
    if schedule_mode not in ("continuous", "slots"):
        schedule_mode = "continuous"
    slots = []
    if schedule_mode == "slots":
        for i in range(21):
            d_ = form.get(f"slot_day_{i}")
            if d_ is None or d_ == "":
                continue
            st_, en_ = form.get(f"slot_start_{i}", "09:00"), form.get(f"slot_end_{i}", "17:00")
            try:
                day_i = int(d_)
                if 0 <= day_i <= 6 and st_ < en_:
                    slots.append({"day": day_i, "start": st_, "end": en_})
            except (ValueError, TypeError):
                continue
        if not slots:
            schedule_mode = "continuous"  # no valid slots -> fall back
    try:
        hourly_cap = max(0, min(1000, int(g("hourly_cap") or 0)))
    except ValueError:
        hourly_cap = 0
    # --- follow-up steps: (draft_id, delay_days) pairs from the wizard ---
    followups = []
    for i in range(10):
        fd = form.get(f"fup_draft_{i}")
        if not fd:
            continue
        try:
            delay = max(1, min(90, int(form.get(f"fup_delay_{i}") or 3)))
        except ValueError:
            delay = 3
        fver = db.get_db().execute(
            "SELECT MAX(version) v FROM draft_versions WHERE draft_id=?", (fd,)).fetchone()["v"]
        if fver:
            followups.append({"draft_id": int(fd), "draft_version": fver, "delay_days": delay})
    def _do(conn):
        cur = conn.execute(
            """INSERT INTO campaigns(name, list_id, account_id, draft_id, draft_version,
               draft_b_id, draft_b_version, ab_split, days_of_week, window_start, window_end,
               timezone, gap_min_sec, gap_max_sec, daily_cap, start_date, status,
               mailing_address, open_tracking, click_tracking,
               schedule_mode, send_slots, hourly_cap, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?)""",
            (g("name"), int(g("list_id")), int(g("account_id")), int(g("draft_id")),
             dv["v"], int(g("draft_b_id")) if g("draft_b_id") else None, dv_b,
             int(g("ab_split") or 50), days, g("window_start", "09:00"),
             g("window_end", "17:00"), s.get("timezone", "Asia/Karachi"),
             int(g("gap_min_sec") or 60), int(g("gap_max_sec") or 180),
             int(g("daily_cap") or 80), g("start_date"), "active",
             g("mailing_address", ""),
             1 if g("open_tracking") else 0, 1 if g("click_tracking") else 0,
             schedule_mode, json.dumps(slots), hourly_cap,
             utcnow_s()))
        cid = cur.lastrowid
        # follow-up sequence: step 1 = main draft (implicit), steps 2+ stored here
        for order, fu in enumerate(followups, start=2):
            conn.execute(
                """INSERT INTO campaign_steps(campaign_id, step_order, draft_id,
                   draft_version, delay_days, created_at) VALUES(?,?,?,?,?,?)""",
                (cid, order, fu["draft_id"], fu["draft_version"],
                 fu["delay_days"], utcnow_s()))
        cid = cur.lastrowid
        # snapshot contacts
        conn.execute(
            """INSERT INTO campaign_contacts(campaign_id, contact_id, status, draft_version)
               SELECT ?, c.id, 'queued', ? FROM contacts c
               WHERE c.list_id=? AND c.valid=1 AND c.do_not_contact=0
               AND NOT EXISTS(SELECT 1 FROM suppression s WHERE lower(s.email)=lower(c.email))""",
            (cid, dv["v"], int(g("list_id"))))
        return cid
    cid = db.tx(_do)
    n = db.get_db().execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?",
                            (cid,)).fetchone()["c"]
    return redir(f"/campaigns/{cid}", f"Campaign launched — {n} contacts queued.", "info")


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_detail(request: Request, cid: int):
    conn = db.get_db()
    c = conn.execute("""SELECT c.*, l.name list_name, a.label account_label, a.email account_email,
                        d.name draft_name, db_.name draft_b_name
                        FROM campaigns c JOIN contact_lists l ON l.id=c.list_id
                        JOIN accounts a ON a.id=c.account_id
                        JOIN drafts d ON d.id=c.draft_id
                        LEFT JOIN drafts db_ ON db_.id=c.draft_b_id
                        WHERE c.id=?""", (cid,)).fetchone()
    if not c:
        raise HTTPException(404)
    c = dict(c)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    c["days"] = ", ".join(day_names[int(d)] for d in c["days_of_week"].split(",") if d != "")
    stats = _campaign_stats(cid)
    latest_ver = conn.execute("SELECT MAX(version) v FROM draft_versions WHERE draft_id=?",
                              (c["draft_id"],)).fetchone()["v"]
    newer = latest_ver if latest_ver and latest_ver > c["draft_version"] else None
    ab = None
    if c["draft_b_id"]:
        ab = [dict(r) for r in conn.execute(
            """SELECT cc.variant, dv.subject draft_name, cc.draft_version,
               (SELECT COUNT(*) FROM sends s WHERE s.campaign_id=? AND s.variant=cc.variant AND s.status='sent') sent,
               (SELECT COUNT(*) FROM replies r JOIN sends s ON s.id=r.send_id WHERE s.campaign_id=? AND s.variant=cc.variant) replies
               FROM campaign_contacts cc JOIN draft_versions dv ON dv.draft_id=
                 CASE cc.variant WHEN 'A' THEN ? ELSE ? END AND dv.version=cc.draft_version
               WHERE cc.campaign_id=? GROUP BY cc.variant""",
            (cid, cid, c["draft_id"], c["draft_b_id"], cid)).fetchall()]
    replies = [dict(r) for r in conn.execute(
        """SELECT r.* FROM replies r JOIN sends s ON s.id=r.send_id
           WHERE s.campaign_id=? ORDER BY r.received_at DESC LIMIT 50""", (cid,)).fetchall()]
    steps = [dict(r) for r in conn.execute(
        """SELECT cs.step_order, cs.delay_days, cs.draft_version, d.name draft_name
           FROM campaign_steps cs JOIN drafts d ON d.id=cs.draft_id
           WHERE cs.campaign_id=? ORDER BY cs.step_order""", (cid,)).fetchall()]
    # schedule summary for display
    sched = ""
    if c.get("schedule_mode") == "slots":
        try:
            slots = json.loads(c.get("send_slots") or "[]")
        except Exception:
            slots = []
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        sched = "; ".join(f"{day_names[s['day']]} {s['start']}-{s['end']}" for s in slots) or "no slots"
    else:
        sched = f"{c['days']} · {c['window_start']}-{c['window_end']} ({c['timezone']})"
    c["sched_summary"] = sched
    page, per, sq, sstatus = page_params(request, 50)
    sw, sp = [], []
    if sq:
        sw.append("c.email LIKE ?"); sp.append(f"%{sq}%")
    if sstatus:
        sw.append("s.status = ?"); sp.append(sstatus)
    swhere = ("AND " + " AND ".join(sw)) if sw else ""
    stotal = conn.execute(
        f"""SELECT COUNT(*) c FROM sends s JOIN contacts c ON c.id=s.contact_id
            WHERE s.campaign_id=? {swhere}""", [cid] + sp).fetchone()["c"]
    spg = pager(stotal, page, per)
    sends = [dict(r) for r in conn.execute(
        f"""SELECT s.sent_at, s.variant, s.status, s.smtp_status, c.email FROM sends s
           JOIN contacts c ON c.id=s.contact_id WHERE s.campaign_id=? {swhere}
           ORDER BY s.id DESC LIMIT ? OFFSET ?""",
        [cid] + sp + [spg["per"], spg["offset"]]).fetchall()]
    return templates.TemplateResponse(request, "campaign_detail.html", base_ctx(
        request, "campaigns", c=c, s=stats, ab=ab, replies=replies,
        sends=sends, newer_version=newer, steps=steps,
        spg=spg, sq=sq, sstatus=sstatus))


@app.post("/campaigns/{cid}/pause")
def campaign_pause(cid: int):
    db.tx(lambda c: c.execute(
        "UPDATE campaigns SET status='paused', pause_reason='Paused by user.' WHERE id=? AND status IN ('active','scheduled')",
        (cid,)))
    return redir(f"/campaigns/{cid}", "Campaign paused.", "info")


@app.post("/campaigns/{cid}/resume")
def campaign_resume(cid: int):
    db.tx(lambda c: c.execute(
        "UPDATE campaigns SET status='active', pause_reason=NULL, next_due_at=NULL WHERE id=? AND status='paused'",
        (cid,)))
    return redir(f"/campaigns/{cid}", "Campaign resumed — continues where it stopped.", "info")


@app.post("/campaigns/{cid}/stop")
def campaign_stop(cid: int):
    def _do(conn):
        conn.execute("UPDATE campaigns SET status='stopped' WHERE id=?", (cid,))
        conn.execute("UPDATE campaign_contacts SET status='cancelled' WHERE campaign_id=? AND status='queued'",
                     (cid,))
    db.tx(_do)
    return redir(f"/campaigns/{cid}", "Campaign stopped. Queued contacts cancelled.", "info")


@app.post("/campaigns/{cid}/launch")
def campaign_launch(cid: int):
    db.tx(lambda c: c.execute("UPDATE campaigns SET status='active' WHERE id=? AND status='draft'",
                              (cid,)))
    return redir(f"/campaigns/{cid}", "Campaign launched.", "info")


@app.post("/campaigns/{cid}/switch-draft")
def campaign_switch_draft(cid: int):
    conn = db.get_db()
    c = conn.execute("SELECT draft_id FROM campaigns WHERE id=?", (cid,)).fetchone()
    latest = conn.execute("SELECT MAX(version) v FROM draft_versions WHERE draft_id=?",
                          (c["draft_id"],)).fetchone()["v"]
    db.tx(lambda x: x.execute("UPDATE campaigns SET draft_version=? WHERE id=?", (latest, cid)))
    return redir(f"/campaigns/{cid}", f"Remaining sends will use draft v{latest}.", "info")


@app.get("/api/campaigns/{cid}/daily")
def api_daily(cid: int):
    conn = db.get_db()
    rows = conn.execute(
        """SELECT date(sent_at) day, COUNT(*) n FROM sends
           WHERE campaign_id=? AND status='sent' GROUP BY day ORDER BY day""",
        (cid,)).fetchall()
    return [{"day": r["day"], "n": r["n"]} for r in rows]


@app.get("/api/estimate")
def api_estimate(list_id: int):
    conn = db.get_db()
    total = conn.execute("SELECT COUNT(*) c FROM contacts WHERE list_id=?", (list_id,)).fetchone()["c"]
    emailable = conn.execute(
        """SELECT COUNT(*) c FROM contacts c WHERE c.list_id=? AND c.valid=1 AND c.do_not_contact=0
           AND NOT EXISTS(SELECT 1 FROM suppression s WHERE lower(s.email)=lower(c.email))""",
        (list_id,)).fetchone()["c"]
    return {"total": total, "emailable": emailable, "skipped": total - emailable}


@app.get("/api/checklist")
def api_checklist(account_id: int):
    conn = db.get_db()
    a = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not a:
        return {"items": []}
    a = dict(a)
    domain = a["email"].split("@")[-1]
    items = []
    try:
        st, det = mailmod.check_spf(domain)
        items.append({"label": f"SPF for {domain}", "status": st, "detail": det})
    except Exception as e:
        items.append({"label": f"SPF for {domain}", "status": "unknown", "detail": str(e)[:100]})
    try:
        st, det = mailmod.check_dmarc(domain)
        items.append({"label": f"DMARC for {domain}", "status": st, "detail": det})
    except Exception as e:
        items.append({"label": f"DMARC for {domain}", "status": "unknown", "detail": str(e)[:100]})
    try:
        st, det = mailmod.check_dkim(domain)
        items.append({"label": f"DKIM for {domain}", "status": st, "detail": det})
    except Exception as e:
        items.append({"label": f"DKIM for {domain}", "status": "unknown", "detail": str(e)[:100]})
    items.append({"label": "Unsubscribe handling",
                  "status": "pass",
                  "detail": "One-click unsubscribe link + footer in every email; replies auto-suppress."})
    items.append({"label": "Account health",
                  "status": "pass" if a["status"] == "connected" else "fail",
                  "detail": f"status: {a['status']}" + (f" — {a['last_error']}" if a["last_error"] else "")})
    cap, warming = worker_mod.effective_daily_cap(a, {"daily_cap": 80}, datetime.now(timezone.utc))
    if warming:
        items.append({"label": "Warm-up mode", "status": "unknown",
                      "detail": f"New account — capped at {cap}/day until warmed up (override on Accounts page)."})
    return {"items": items}


# ---------------- sends (global log) ----------------

@app.get("/sends", response_class=HTMLResponse)
def sends_page(request: Request):
    page, per, q, status = page_params(request, 50)
    where, params = [], []
    if q:
        where.append("(c.email LIKE ? OR cp.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if status:
        where.append("s.status = ?"); params.append(status)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    conn = db.get_db()
    total = conn.execute(
        f"""SELECT COUNT(*) c FROM sends s JOIN contacts c ON c.id=s.contact_id
            JOIN campaigns cp ON cp.id=s.campaign_id {w}""", params).fetchone()["c"]
    pg = pager(total, page, per)
    rows = [dict(r) for r in conn.execute(
        f"""SELECT s.id, s.sent_at, s.variant, s.status, s.smtp_status, s.opened_at,
                   c.email, cp.name campaign_name,
                   (SELECT COUNT(*) FROM clicks k WHERE k.send_id=s.id) clicks
            FROM sends s JOIN contacts c ON c.id=s.contact_id
            JOIN campaigns cp ON cp.id=s.campaign_id
            {w} ORDER BY s.id DESC LIMIT ? OFFSET ?""",
        params + [pg["per"], pg["offset"]]).fetchall()]
    statuses = [r["status"] for r in conn.execute(
        "SELECT DISTINCT status FROM sends ORDER BY status").fetchall()]
    return templates.TemplateResponse(request, "sends.html", base_ctx(
        request, "sends", rows=rows, pg=pg, q=q, status=status, statuses=statuses))


# ---------------- contacts ----------------

@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request):
    page, per, q, _ = page_params(request, 20)
    where, params = [], []
    if q:
        where.append("l.name LIKE ?"); params.append(f"%{q}%")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    conn = db.get_db()
    total = conn.execute(f"SELECT COUNT(*) c FROM contact_lists l {w}", params).fetchone()["c"]
    pg = pager(total, page, per)
    lists = [dict(r) for r in conn.execute(
        f"""SELECT l.*, (SELECT COUNT(*) FROM contacts WHERE list_id=l.id) count,
           (SELECT COUNT(*) FROM invalid_rows WHERE list_id=l.id) invalid
           FROM contact_lists l {w} ORDER BY l.id DESC LIMIT ? OFFSET ?""",
        params + [pg["per"], pg["offset"]]).fetchall()]
    return templates.TemplateResponse(request, "contacts.html", base_ctx(
        request, "contacts", lists=lists, pg=pg, q=q))


def _parse_rows(data: bytes, filename: str, sheet: str | None = None):
    """Parse CSV/XLSX bytes into (headers, rows). For XLSX, `sheet` selects the
    worksheet (defaults to the active sheet)."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        headers = rows[0].keys() if rows else []
        return list(headers), rows
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    if sheet:
        if sheet not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet}' not found (have: {', '.join(wb.sheetnames)})")
        ws = wb[sheet]
    else:
        ws = wb.active
    it = ws.iter_rows(values_only=True)
    try:
        header_row = next(it)
    except StopIteration:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in header_row]
    rows = []
    for r in it:
        if all(v is None or str(v).strip() == "" for v in r):
            continue
        rows.append({headers[i]: ("" if v is None else str(v).strip())
                     for i, v in enumerate(r) if i < len(headers)})
    return headers, rows


def _read_upload(file: UploadFile):
    return _parse_rows(file.file.read(), file.filename or "")


def _sheet_names(data: bytes, filename: str):
    """Sheet names for XLSX; empty list for CSV (single sheet)."""
    if (filename or "").lower().endswith(".csv"):
        return []
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    return list(wb.sheetnames)


@app.post("/api/contacts/stage")
async def contacts_stage(file: UploadFile = File(...)):
    """Stage an upload and return its sheet names so the user can pick one
    before column mapping. The staged file is consumed by /contacts/upload-staged."""
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        return JSONResponse({"detail": "File too large (max 50MB)."}, status_code=413)
    try:
        sheets = _sheet_names(data, file.filename or "")
    except Exception as e:
        return JSONResponse({"detail": f"Could not read file: {e}"}, status_code=400)
    uid = uuid.uuid4().hex
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, f"stage_{uid}.bin"), "wb") as f:
        f.write(data)
    with open(os.path.join(UPLOAD_DIR, f"stage_{uid}.meta.json"), "w") as f:
        json.dump({"filename": file.filename}, f)
    return {"uid": uid, "sheets": sheets, "filename": file.filename}


@app.post("/contacts/upload-staged")
async def contacts_upload_staged(request: Request):
    """Finish a staged upload using the user-selected sheet, then go to mapping."""
    form = await request.form()
    uid = (form.get("uid") or "").strip()
    sheet = (form.get("sheet") or "").strip() or None
    name = (form.get("name") or "").strip()
    if not uid or not name or not re.fullmatch(r"[0-9a-f]{32}", uid):
        return JSONResponse({"detail": "Missing upload. Choose the file again."}, status_code=400)
    raw_path = os.path.join(UPLOAD_DIR, f"stage_{uid}.bin")
    meta_path = os.path.join(UPLOAD_DIR, f"stage_{uid}.meta.json")
    if not os.path.exists(raw_path) or not os.path.exists(meta_path):
        return JSONResponse({"detail": "Upload expired — choose the file again."}, status_code=400)
    meta = json.load(open(meta_path))
    with open(raw_path, "rb") as f:
        data = f.read()
    try:
        headers, rows = _parse_rows(data, meta["filename"], sheet)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": f"Could not read sheet: {e}"}, status_code=400)
    if not rows:
        return JSONResponse({"detail": "Selected sheet has no data rows."}, status_code=400)
    out_uid = uuid.uuid4().hex
    with open(os.path.join(UPLOAD_DIR, out_uid + ".json"), "w") as f:
        json.dump({"filename": meta["filename"], "list_name": name,
                   "headers": headers, "rows": rows[:100000],
                   "sheet": sheet or "(active)"}, f)
    for p in (raw_path, meta_path):
        try:
            os.remove(p)
        except OSError:
            pass
    return {"redirect": f"/contacts/mapping/{out_uid}"}


@app.post("/contacts/upload")
def contacts_upload(request: Request, file: UploadFile = File(...), name: str = Form(...)):
    try:
        headers, rows = _read_upload(file)
    except Exception as e:
        return redir("/contacts", f"Could not read file: {e}", "err")
    if not rows:
        return redir("/contacts", "File has no data rows.", "err")
    uid = uuid.uuid4().hex
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, uid + ".json"), "w") as f:
        json.dump({"filename": file.filename, "list_name": name,
                   "headers": headers, "rows": rows[:100000]}, f)
    return RedirectResponse(f"/contacts/mapping/{uid}", status_code=303)


FIELDS = [("email", "Email (required)"), ("first_name", "First name"),
          ("last_name", "Last name"), ("company", "Company"),
          ("custom_1", "Custom 1"), ("custom_2", "Custom 2"), ("custom_3", "Custom 3")]


def _guess(col):
    c = col.lower()
    if "email" in c or "e-mail" in c:
        return "email"
    if "first" in c or "fname" in c or "given" in c:
        return "first_name"
    if "last" in c or "lname" in c or "surname" in c or "family" in c:
        return "last_name"
    if "compan" in c or "organi" in c or "business" in c:
        return "company"
    return ""


@app.get("/contacts/mapping/{uid}", response_class=HTMLResponse)
def mapping_page(request: Request, uid: str):
    path = os.path.join(UPLOAD_DIR, uid + ".json")
    if not os.path.exists(path):
        return redir("/contacts", "Upload expired — please upload again.", "err")
    up = json.load(open(path))
    samples = {h: (up["rows"][0].get(h, "") if up["rows"] else "") for h in up["headers"]}
    guess = {h: _guess(h) for h in up["headers"]}
    return templates.TemplateResponse(request, "mapping.html", base_ctx(
        request, "contacts", upload_id=uid, filename=up["filename"],
        columns=up["headers"], samples=samples, fields=FIELDS,
        guess=guess, nrows=len(up["rows"]), list_name=up["list_name"]))


def _sanitize_custom_key(header: str) -> str:
    """Turn an arbitrary CSV column header into a safe {{variable}} name."""
    key = re.sub(r"[^a-z0-9_]+", "_", (header or "").strip().lower()).strip("_")
    return key[:60] or "field"


def _run_import_job(job_id: str, uid: str, col2field: dict, list_name: str):
    """Background chunked import: validates rows in chunks of 1000 and updates
    import_jobs.processed so the UI can show progress for 20k+ row files."""
    path = os.path.join(UPLOAD_DIR, uid + ".json")
    try:
        up = json.load(open(path))
        rows = up["rows"]
        db.tx(lambda c: c.execute(
            "UPDATE import_jobs SET status='running', total=? WHERE id=?",
            (len(rows), job_id)))

        def _do(conn):
            cur = conn.execute("INSERT INTO contact_lists(name, created_at) VALUES(?,?)",
                               (list_name, utcnow_s()))
            lid = cur.lastrowid
            seen = set()
            added = invalid = dupes = suppressed = 0
            for i, row in enumerate(rows):
                mapped = {f: (row.get(h) or "").strip() for h, f in col2field.items()}
                # auto-map: every unmapped column becomes a custom field
                extras = {}
                for h in up["headers"]:
                    if h not in col2field and (row.get(h) or "").strip():
                        extras[_sanitize_custom_key(h)] = (row.get(h) or "").strip()
                email = mapped.get("email", "").strip().lower()
                if not email:
                    conn.execute("INSERT INTO invalid_rows(list_id, row_data, reason, created_at) VALUES(?,?,?,?)",
                                 (lid, json.dumps(row)[:500], "missing email", utcnow_s()))
                    invalid += 1
                else:
                    ok, reason = mailmod.validate_email(email)
                    if not ok:
                        conn.execute("INSERT INTO invalid_rows(list_id, row_data, reason, created_at) VALUES(?,?,?,?)",
                                     (lid, json.dumps(row)[:500], f"invalid email: {reason}", utcnow_s()))
                        invalid += 1
                    elif email in seen:
                        dupes += 1
                    else:
                        seen.add(email)
                        dnc = 1 if conn.execute("SELECT 1 FROM suppression WHERE email=?",
                                               (email,)).fetchone() else 0
                        if dnc:
                            suppressed += 1
                        conn.execute(
                            """INSERT INTO contacts(list_id, email, first_name, last_name, company,
                               custom_1, custom_2, custom_3, custom_fields, valid, do_not_contact, created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                            (lid, email, mapped.get("first_name", ""), mapped.get("last_name", ""),
                             mapped.get("company", ""), mapped.get("custom_1", ""),
                             mapped.get("custom_2", ""), mapped.get("custom_3", ""),
                             json.dumps(extras), dnc, utcnow_s()))
                        added += 1
                if (i + 1) % 1000 == 0:
                    conn.execute("UPDATE import_jobs SET processed=? WHERE id=?", (i + 1, job_id))
                    conn.commit()
            return lid, added, invalid, dupes, suppressed
        lid, added, invalid, dupes, suppressed = db.tx(_do)
        summary = (f"Imported {added} contacts · {invalid} invalid quarantined · "
                   f"{dupes} duplicates merged · {suppressed} already suppressed.")
        db.tx(lambda c: c.execute(
            "UPDATE import_jobs SET status='done', processed=total, list_id=?, summary=? WHERE id=?",
            (lid, summary, job_id)))
    except Exception as e:
        db.tx(lambda c: c.execute(
            "UPDATE import_jobs SET status='error', error=? WHERE id=?", (str(e)[:500], job_id)))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/contacts/mapping/{uid}")
async def mapping_import(request: Request, uid: str):
    form = await request.form()
    path = os.path.join(UPLOAD_DIR, uid + ".json")
    if not os.path.exists(path):
        return redir("/contacts", "Upload expired — please upload again.", "err")
    up = json.load(open(path))
    headers = up["headers"]
    col2field = {}
    for i, h in enumerate(headers):
        f = form.get(f"map_{i}", "")
        if f:
            col2field[h] = f
    if "email" not in col2field.values():
        return redir(f"/contacts/mapping/{uid}", "You must map a column to Email.", "err")
    list_name = form.get("list_name") or up["list_name"]
    job_id = uuid.uuid4().hex[:12]
    db.tx(lambda c: c.execute(
        """INSERT INTO import_jobs(id, filename, list_name, status, processed, total,
           headers, rows_path, created_at) VALUES(?,?,?,?,0,?,?,?,?)""",
        (job_id, up.get("filename", ""), list_name, "queued",
         len(up["rows"]), json.dumps(headers), path, utcnow_s())))
    t = threading.Thread(target=_run_import_job, args=(job_id, uid, col2field, list_name),
                         daemon=True, name=f"import-{job_id}")
    t.start()
    return RedirectResponse(f"/contacts/import-progress/{job_id}", status_code=303)


@app.get("/contacts/import-progress/{job_id}", response_class=HTMLResponse)
def import_progress_page(request: Request, job_id: str):
    job = db.get_db().execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "import_progress.html",
                                      base_ctx(request, "contacts", job=dict(job)))


@app.get("/api/import-jobs/{job_id}")
def api_import_job(job_id: str):
    job = db.get_db().execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404)
    return dict(job)


@app.get("/contacts/lists/{lid}", response_class=HTMLResponse)
def list_detail(request: Request, lid: int):
    conn = db.get_db()
    lst = conn.execute("SELECT * FROM contact_lists WHERE id=?", (lid,)).fetchone()
    if not lst:
        raise HTTPException(404)
    page, per, q, status = page_params(request, 50)
    where, params = ["list_id=?"], [lid]
    if q:
        where.append("(email LIKE ? OR first_name LIKE ? OR last_name LIKE ? OR company LIKE ?)")
        params += [f"%{q}%"] * 4
    if status == "ok":
        where.append("valid=1 AND do_not_contact=0")
    elif status == "invalid":
        where.append("valid=0")
    elif status == "suppressed":
        where.append("do_not_contact=1")
    w = "WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) c FROM contacts {w}", params).fetchone()["c"]
    pg = pager(total, page, per)
    contacts = [dict(r) for r in conn.execute(
        f"SELECT * FROM contacts {w} ORDER BY id LIMIT ? OFFSET ?",
        params + [pg["per"], pg["offset"]]).fetchall()]
    invalid = [dict(r) for r in conn.execute(
        "SELECT * FROM invalid_rows WHERE list_id=? ORDER BY id LIMIT 200", (lid,)).fetchall()]
    return templates.TemplateResponse(request, "list_detail.html", base_ctx(
        request, "contacts", lst=dict(lst), contacts=contacts, invalid=invalid,
        pg=pg, q=q, status=status, lid=lid))


@app.post("/contacts/lists/{lid}/add")
def contact_add(lid: int, email: str = Form(...), first_name: str = Form(""),
                company: str = Form("")):
    email = email.strip().lower()
    ok, reason = mailmod.validate_email(email)
    if not ok:
        return redir(f"/contacts/lists/{lid}", f"Invalid email: {reason}", "err")
    conn = db.get_db()
    if conn.execute("SELECT 1 FROM contacts WHERE list_id=? AND lower(email)=lower(?)",
                    (lid, email)).fetchone():
        return redir(f"/contacts/lists/{lid}", "Already in this list.", "err")
    dnc = 1 if conn.execute("SELECT 1 FROM suppression WHERE email=?", (email,)).fetchone() else 0
    db.tx(lambda c: c.execute(
        "INSERT INTO contacts(list_id, email, first_name, company, do_not_contact, created_at) VALUES(?,?,?,?,?,?)",
        (lid, email, first_name.strip(), company.strip(), dnc, utcnow_s())))
    return redir(f"/contacts/lists/{lid}", "Contact added.", "info")


@app.get("/contacts/{cid}/edit", response_class=HTMLResponse)
def contact_edit_page(request: Request, cid: int):
    c = db.get_db().execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "contact_edit.html",
                                      base_ctx(request, "contacts", c=dict(c)))


@app.post("/contacts/{cid}/edit")
def contact_edit(cid: int, email: str = Form(...), first_name: str = Form(""),
                 last_name: str = Form(""), company: str = Form(""),
                 custom_1: str = Form(""), custom_2: str = Form(""),
                 custom_3: str = Form("")):
    email = email.strip().lower()
    ok, reason = mailmod.validate_email(email)
    if not ok:
        return redir(f"/contacts/{cid}/edit", f"Invalid email: {reason}", "err")
    conn = db.get_db()
    lid = conn.execute("SELECT list_id FROM contacts WHERE id=?", (cid,)).fetchone()["list_id"]
    db.tx(lambda c: c.execute(
        """UPDATE contacts SET email=?, first_name=?, last_name=?, company=?,
           custom_1=?, custom_2=?, custom_3=? WHERE id=?""",
        (email, first_name.strip(), last_name.strip(), company.strip(),
         custom_1.strip(), custom_2.strip(), custom_3.strip(), cid)))
    return redir(f"/contacts/lists/{lid}", "Contact updated.", "info")


@app.post("/contacts/lists/{lid}/delete")
def list_delete(request: Request, lid: int):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    db.tx(lambda c: (c.execute("DELETE FROM invalid_rows WHERE list_id=?", (lid,)),
                     c.execute("DELETE FROM contacts WHERE list_id=?", (lid,)),
                     c.execute("DELETE FROM contact_lists WHERE id=?", (lid,))))
    return redir("/contacts", "List deleted.", "info")


# ---------------- drafts ----------------

def _drafts_with_latest():
    conn = db.get_db()
    return [dict(r) for r in conn.execute(
        """SELECT d.id, d.name, dv.version, dv.subject FROM drafts d
           JOIN draft_versions dv ON dv.draft_id=d.id
           WHERE dv.version=(SELECT MAX(version) FROM draft_versions WHERE draft_id=d.id)
           ORDER BY d.id DESC""").fetchall()]


@app.get("/drafts", response_class=HTMLResponse)
def drafts_page(request: Request):
    return templates.TemplateResponse(request, "drafts.html",
                                      base_ctx(request, "drafts", drafts=_drafts_with_latest()))


def _sample_contacts():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM contacts ORDER BY id DESC LIMIT 5").fetchall()
    if rows:
        return [dict(r) for r in rows]
    return [{"id": 0, "first_name": "Ali", "last_name": "Khan",
             "email": "ali@example.com", "company": "Acme Realty",
             "custom_1": "", "custom_2": "", "custom_3": ""}]


@app.get("/drafts/new", response_class=HTMLResponse)
def draft_new_page(request: Request):
    return templates.TemplateResponse(request, "draft_edit.html", base_ctx(
        request, "drafts", draft=None, versions=[], sample_contacts=_sample_contacts()))


@app.post("/drafts")
def draft_create(name: str = Form(...), subject: str = Form(...), body_html: str = Form(...)):
    def _do(conn):
        cur = conn.execute("INSERT INTO drafts(name, created_at) VALUES(?,?)",
                           (name.strip(), utcnow_s()))
        did = cur.lastrowid
        conn.execute("INSERT INTO draft_versions(draft_id, version, subject, body_html, created_at) VALUES(?,?,?, ?,?)",
                     (did, 1, subject, body_html, utcnow_s()))
        return did
    did = db.tx(_do)
    return redir(f"/drafts/{did}/edit", "Draft saved (v1).", "info")


@app.get("/drafts/{did}/edit", response_class=HTMLResponse)
def draft_edit_page(request: Request, did: int):
    conn = db.get_db()
    d = conn.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404)
    latest = conn.execute(
        "SELECT * FROM draft_versions WHERE draft_id=? ORDER BY version DESC LIMIT 1",
        (did,)).fetchone()
    versions = [dict(r) for r in conn.execute(
        "SELECT version, created_at FROM draft_versions WHERE draft_id=? ORDER BY version DESC",
        (did,)).fetchall()]
    draft = {"id": d["id"], "name": d["name"], "version": latest["version"],
             "subject": latest["subject"], "body_html": latest["body_html"]}
    return templates.TemplateResponse(request, "draft_edit.html", base_ctx(
        request, "drafts", draft=draft, versions=versions,
        sample_contacts=_sample_contacts()))


@app.post("/drafts/{did}")
def draft_new_version(did: int, name: str = Form(...), subject: str = Form(...),
                      body_html: str = Form(...)):
    def _do(conn):
        conn.execute("UPDATE drafts SET name=? WHERE id=?", (name.strip(), did))
        v = conn.execute("SELECT MAX(version) m FROM draft_versions WHERE draft_id=?",
                         (did,)).fetchone()["m"] or 0
        conn.execute("INSERT INTO draft_versions(draft_id, version, subject, body_html, created_at) VALUES(?,?,?,?,?)",
                     (did, v + 1, subject, body_html, utcnow_s()))
        return v + 1
    v = db.tx(_do)
    return redir(f"/drafts/{did}/edit", f"Saved as v{v}. Running campaigns keep their old version unless you switch them.", "info")


@app.post("/drafts/{did}/delete")
def draft_delete(request: Request, did: int):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    conn = db.get_db()
    used = conn.execute("SELECT COUNT(*) c FROM campaigns WHERE draft_id=? OR draft_b_id=?",
                        (did, did)).fetchone()["c"]
    if used:
        return redir("/drafts", "Can't delete — a campaign uses this draft.", "err")
    db.tx(lambda c: (c.execute("DELETE FROM draft_versions WHERE draft_id=?", (did,)),
                     c.execute("DELETE FROM drafts WHERE id=?", (did,))))
    return redir("/drafts", "Draft deleted.", "info")


@app.get("/api/drafts/preview-variations")
def api_preview_variations(subject: str = "", body: str = "", n: int = 5):
    """Show N random spintax variations of the draft (draft editor button)."""
    n = max(1, min(10, n))
    return {"subjects": mailmod.preview_variations(subject, n),
            "bodies": mailmod.preview_variations(body, n)}


@app.get("/api/drafts/preview")
def api_preview(subject: str = "", body: str = "", contact_id: int = 0):
    conn = db.get_db()
    c = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    contact = dict(c) if c else {"first_name": "Ali", "last_name": "Khan",
                                 "email": "ali@example.com", "company": "Acme Realty",
                                 "custom_1": "", "custom_2": "", "custom_3": ""}
    return {"subject": mailmod.render_template(subject, contact),
            "body": mailmod.render_template(body, contact)}


# ---------------- accounts ----------------

@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    conn = db.get_db()
    accounts = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall():
        a = dict(a)
        cap, warming = worker_mod.effective_daily_cap(a, {"daily_cap": 80},
                                                      datetime.now(timezone.utc))
        a["warm_active"] = warming and not a["warmup_override"]
        accounts.append(a)
    return templates.TemplateResponse(request, "accounts.html",
                                      base_ctx(request, "accounts", accounts=accounts))


@app.get("/accounts/new", response_class=HTMLResponse)
def account_new_page(request: Request):
    return templates.TemplateResponse(request, "account_new.html", base_ctx(request, "accounts"))


@app.post("/accounts")
def account_create(request: Request, label: str = Form(...), email: str = Form(...),
                   smtp_host: str = Form(...), smtp_port: int = Form(587),
                   smtp_user: str = Form(...), smtp_pass: str = Form(...),
                   smtp_enc: str = Form("STARTTLS"),
                   imap_host: str = Form(...), imap_port: int = Form(993),
                   imap_user: str = Form(...), imap_pass: str = Form(...),
                   imap_enc: str = Form("SSL"),
                   imap_enabled: str = Form(""),
                   hourly_cap: int = Form(0)):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    def _do(conn):
        cur = conn.execute(
            """INSERT INTO accounts(label, email, smtp_host, smtp_port, smtp_user, smtp_pass_enc, smtp_enc,
               imap_host, imap_port, imap_user, imap_pass_enc, imap_enc, imap_enabled, hourly_cap, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unknown', ?)""",
            (label.strip(), email.strip().lower(), smtp_host.strip(), smtp_port,
             smtp_user.strip(), encrypt(smtp_pass), smtp_enc,
             imap_host.strip(), imap_port, imap_user.strip(), encrypt(imap_pass),
             imap_enc, 1 if imap_enabled else 0, max(0, min(1000, hourly_cap)), utcnow_s()))
        return cur.lastrowid
    aid = db.tx(_do)
    return redir("/accounts", "Account saved. Run 'Test connection' to verify.", "info")


@app.post("/accounts/{aid}/test")
def account_test(aid: int):
    conn = db.get_db()
    a = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not a:
        raise HTTPException(404)
    a = dict(a)
    acct = dict(a, smtp_pass=a["smtp_pass_enc"], _enc=True)
    res = mailmod.test_connection(acct)
    ok = res["smtp_ok"] and res["imap_ok"]
    status = "connected" if ok else "failed"
    err = None if ok else "; ".join(x for x in [res["smtp_msg"], res["imap_msg"]] if x)[:400]
    db.tx(lambda c: c.execute(
        "UPDATE accounts SET status=?, last_error=?, last_tested_at=? WHERE id=?",
        (status, err, utcnow_s(), aid)))
    if ok:
        return redir("/accounts", "✅ Test passed — email sent AND read back via IMAP.", "info")
    return redir("/accounts", f"❌ Test failed: {err}", "err")


@app.post("/accounts/{aid}/delete")
def account_delete(request: Request, aid: int):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    db.tx(lambda c: c.execute("DELETE FROM accounts WHERE id=?", (aid,)))
    return redir("/accounts", "Account deleted.", "info")


@app.get("/accounts/{aid}/warmup", response_class=HTMLResponse)
def warmup_page(request: Request, aid: int):
    a = db.get_db().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not a:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "warmup.html",
                                      base_ctx(request, "accounts", a=dict(a)))


@app.post("/accounts/{aid}/warmup-override")
def warmup_override(aid: int, confirm: str = Form("")):
    if confirm.strip() != "I UNDERSTAND":
        return redir(f"/accounts/{aid}/warmup", "Type exactly: I UNDERSTAND", "err")
    db.tx(lambda c: c.execute("UPDATE accounts SET warmup_override=1 WHERE id=?", (aid,)))
    return redir("/accounts", "Warm-up overridden.", "info")


# ---------------- suppression ----------------

@app.get("/suppression", response_class=HTMLResponse)
def suppression_page(request: Request):
    items = [dict(r) for r in db.get_db().execute(
        "SELECT * FROM suppression ORDER BY created_at DESC").fetchall()]
    return templates.TemplateResponse(request, "suppression.html",
                                      base_ctx(request, "suppression", items=items))


@app.post("/suppression/add")
def suppression_add(email: str = Form(...), reason: str = Form("")):
    email = email.strip().lower()
    ok, why = mailmod.validate_email(email)
    if not ok:
        return redir("/suppression", f"Invalid email: {why}", "err")

    def _do(conn):
        conn.execute("INSERT OR IGNORE INTO suppression(email, reason, created_at) VALUES(?,?,?)",
                     (email, reason.strip() or "manual", utcnow_s()))
        conn.execute("UPDATE contacts SET do_not_contact=1 WHERE lower(email)=lower(?)", (email,))
        conn.execute("""UPDATE campaign_contacts SET status='cancelled'
                        WHERE status='queued' AND contact_id IN
                        (SELECT id FROM contacts WHERE lower(email)=lower(?))""", (email,))
    db.tx(_do)
    return redir("/suppression", f"{email} suppressed globally.", "info")


@app.post("/suppression/remove")
def suppression_remove(request: Request, email: str = Form(...)):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    db.tx(lambda c: c.execute("DELETE FROM suppression WHERE email=?", (email.strip().lower(),)))
    return redir("/suppression", "Removed from suppression list.", "info")


# ---------------- one-click unsubscribe (public) ----------------

@app.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe(token: str):
    conn = db.get_db()
    s = conn.execute("SELECT * FROM sends WHERE unsub_token=?", (token,)).fetchone()
    if not s:
        return HTMLResponse("<h2>Link expired or invalid.</h2>", status_code=404)
    s = dict(s)
    c = conn.execute("SELECT email FROM contacts WHERE id=?", (s["contact_id"],)).fetchone()
    email = c["email"]

    def _do(conn):
        conn.execute("INSERT OR IGNORE INTO suppression(email, reason, created_at) VALUES(?,?,?)",
                     (email, "one-click unsubscribe link", utcnow_s()))
        conn.execute("UPDATE contacts SET do_not_contact=1 WHERE lower(email)=lower(?)", (email,))
        conn.execute("""UPDATE campaign_contacts SET status='unsubscribed'
                        WHERE status='queued' AND contact_id IN
                        (SELECT id FROM contacts WHERE lower(email)=lower(?))""", (email,))
        conn.execute("""UPDATE campaign_contacts SET status='replied', reply_category='Unsubscribe'
                        WHERE campaign_id=? AND contact_id=? AND status IN ('sent')""",
                     (s["campaign_id"], s["contact_id"]))
    db.tx(_do)
    return HTMLResponse(
        f"""<html><body style="font-family:sans-serif;max-width:600px;margin:60px auto;text-align:center">
        <h2>✅ Unsubscribed</h2><p><strong>{email}</strong> will never be emailed again.</p>
        <p style="color:#888">No questions asked — you're off the list.</p></body></html>""")


# ---------------- open & click tracking (public) ----------------

@app.get("/t/o/{token}.png")
def track_open(token: str):
    conn = db.get_db()
    s = conn.execute("SELECT id, opened_at, campaign_id FROM sends WHERE unsub_token=?",
                     (token,)).fetchone()
    if s and not s["opened_at"]:
        # only count opens for campaigns with tracking enabled
        c = conn.execute("SELECT open_tracking FROM campaigns WHERE id=?",
                         (s["campaign_id"],)).fetchone()
        if c and c["open_tracking"]:
            db.tx(lambda x: x.execute("UPDATE sends SET opened_at=? WHERE id=?",
                                      (utcnow_s(), s["id"])))
    from fastapi.responses import Response
    return Response(content=mailmod.TRANSPARENT_PNG, media_type="image/png")


@app.get("/t/c/{token}")
def track_click(token: str, url: str = ""):
    import urllib.parse
    conn = db.get_db()
    s = conn.execute("SELECT id, campaign_id FROM sends WHERE unsub_token=?", (token,)).fetchone()
    dest = (url or "").strip()
    ok_url = dest.startswith(("http://", "https://"))
    if s and ok_url:
        c = conn.execute("SELECT click_tracking FROM campaigns WHERE id=?",
                         (s["campaign_id"],)).fetchone()
        if c and c["click_tracking"]:
            db.tx(lambda x: x.execute(
                "INSERT INTO clicks(send_id, url, clicked_at) VALUES(?,?,?)",
                (s["id"], dest[:2000], utcnow_s())))
    if not ok_url:
        raise HTTPException(400, "bad target url")
    return RedirectResponse(dest, status_code=302)


# ---------------- deliverability: seed accounts + placement tests ----------------

@app.get("/deliverability", response_class=HTMLResponse)
def deliverability_page(request: Request):
    conn = db.get_db()
    seeds = [dict(r) for r in conn.execute("SELECT * FROM seed_accounts ORDER BY id").fetchall()]
    tests = [dict(r) for r in conn.execute(
        "SELECT * FROM deliverability_tests ORDER BY created_at DESC LIMIT 10").fetchall()]
    for t in tests:
        t["results_parsed"] = json.loads(t["results"]) if t["results"] else None
    drafts = [dict(r) for r in conn.execute("SELECT id, name FROM drafts ORDER BY id DESC").fetchall()]
    accounts = [dict(r) for r in conn.execute("SELECT id, label, email FROM accounts ORDER BY id").fetchall()]
    return templates.TemplateResponse(request, "deliverability.html", base_ctx(
        request, "deliverability", seeds=seeds, tests=tests, drafts=drafts, accounts=accounts))


@app.post("/seed-accounts")
def seed_add(request: Request, email: str = Form(...), imap_host: str = Form(...),
             imap_port: int = Form(993), imap_user: str = Form(...),
             imap_pass: str = Form(...), provider: str = Form("other")):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    db.tx(lambda c: c.execute(
        """INSERT INTO seed_accounts(email, imap_host, imap_port, imap_user,
           imap_pass_enc, provider, created_at) VALUES(?,?,?,?,?,?,?)""",
        (email.strip().lower(), imap_host.strip(), imap_port, imap_user.strip(),
         encrypt(imap_pass), provider, utcnow_s())))
    return redir("/deliverability", "Seed inbox added.", "info")


@app.post("/seed-accounts/{sid}/delete")
def seed_delete(request: Request, sid: int):
    gate = sudo_or_redirect(request)
    if gate:
        return gate
    db.tx(lambda c: c.execute("DELETE FROM seed_accounts WHERE id=?", (sid,)))
    return redir("/deliverability", "Seed inbox removed.", "info")


def _run_deliverability_test(test_id: str, draft_id: int, account_id: int, wait_sec: int = 45):
    """Background: send the draft to every seed, wait, then check placement."""
    from email.utils import make_msgid
    conn = db.get_db()
    try:
        seeds = [dict(r) for r in conn.execute("SELECT * FROM seed_accounts ORDER BY id").fetchall()]
        account = dict(conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())
        dv = conn.execute("SELECT subject, body_html FROM draft_versions WHERE draft_id=? "
                          "ORDER BY version DESC LIMIT 1", (draft_id,)).fetchone()
        sample = {"first_name": "Seed", "last_name": "Test", "email": "",
                  "company": "SeedCheck", "custom_1": "", "custom_2": "", "custom_3": ""}
        subject = mailmod.render_template(dv["subject"], sample)
        body = mailmod.render_template(dv["body_html"], sample)
        results = []
        for seed in seeds:
            sample["email"] = seed["email"]
            message_id = make_msgid(domain=account["email"].split("@")[-1])
            msg = mailmod.build_message(account["email"], seed["email"], subject, body,
                                        message_id,
                                        f"{db_worker_base_url()}/u/seedtest", "",
                                        reply_to=account["email"])
            acct = dict(account, smtp_pass=account["smtp_pass_enc"], _enc=True)
            ok, resp = mailmod.smtp_send(acct, msg)
            if not ok:
                results.append({"seed": seed["email"], "provider": seed["provider"],
                                "placement": "missing", "folder": None,
                                "detail": f"send failed: {resp[:120]}"})
                continue
            time.sleep(wait_sec)
            try:
                placed = mailmod.check_seed_placement(seed, message_id)
            except Exception as e:
                placed = {"placement": "missing", "folder": None,
                          "detail": f"imap error: {e}"[:120]}
            results.append({"seed": seed["email"], "provider": seed["provider"], **placed})
        score = mailmod.placement_score(results)
        db.tx(lambda c: c.execute(
            "UPDATE deliverability_tests SET status='done', results=? WHERE id=?",
            (json.dumps({"results": results, "score": score}), test_id)))
    except Exception as e:
        db.tx(lambda c: c.execute(
            "UPDATE deliverability_tests SET status='error', results=? WHERE id=?",
            (json.dumps({"error": str(e)[:300]}), test_id)))


def db_worker_base_url():
    row = db.get_db().execute("SELECT value FROM settings WHERE key='base_url'").fetchone()
    return (row["value"] if row else "http://localhost:8000").rstrip("/")


@app.post("/deliverability/test")
def deliverability_test(request: Request, draft_id: int = Form(...),
                        account_id: int = Form(...)):
    tid = uuid.uuid4().hex[:12]
    db.tx(lambda c: c.execute(
        "INSERT INTO deliverability_tests(id, draft_id, account_id, status, created_at) VALUES(?,?,?,?,?)",
        (tid, draft_id, account_id, "running", utcnow_s())))
    t = threading.Thread(target=_run_deliverability_test, args=(tid, draft_id, account_id),
                         daemon=True, name=f"seedtest-{tid}")
    t.start()
    return redir("/deliverability", "Placement test started — results appear below shortly.", "info")


# ---------------- settings ----------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from zoneinfo import available_timezones
    s = {r["key"]: r["value"] for r in db.get_db().execute("SELECT key, value FROM settings")}
    return templates.TemplateResponse(request, "settings.html", base_ctx(
        request, "settings", s=s,
        timezones=sorted(available_timezones())))


REPLY_INTERVAL_RE = re.compile(r"^\s*(\d{1,3})\s*(?:-\s*(\d{1,3}))?\s*$")


def normalize_reply_interval(raw: str) -> str:
    """Accept '5' or '5-8' (jitter range, minutes). Returns a clean string."""
    m = REPLY_INTERVAL_RE.match(raw or "")
    if not m:
        return "5"
    lo = max(2, min(120, int(m.group(1))))
    hi = max(2, min(120, int(m.group(2) or m.group(1))))
    lo, hi = min(lo, hi), max(lo, hi)
    return str(lo) if lo == hi else f"{lo}-{hi}"


@app.post("/settings")
def settings_save(request: Request, timezone: str = Form(...), base_url: str = Form(...),
                  physical_address: str = Form(""), reply_poll_minutes: str = Form("5")):
    vals = {"timezone": timezone.strip(), "base_url": base_url.strip().rstrip("/"),
            "physical_address": physical_address.strip(),
            "reply_poll_minutes": normalize_reply_interval(reply_poll_minutes)}
    db.tx(lambda c: [c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (k, v))
                     for k, v in vals.items()])
    return redir("/settings", "Settings saved.", "info")
