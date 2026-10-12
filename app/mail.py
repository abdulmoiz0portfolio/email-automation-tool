"""Email plumbing: validation, templating, SMTP send, IMAP reply polling,
reply classification, and deliverability (SPF/DKIM/DMARC) lookups."""
import json
import re
import smtplib
import imaplib
import socket
import time
import uuid
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from email import policy
from email.parser import BytesParser

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


def validate_email(raw: str):
    """Returns (ok, reason). Quarantine logic uses the reason."""
    if raw is None:
        return False, "empty"
    email = raw.strip()
    if not email:
        return False, "empty"
    if len(email) > 254:
        return False, "too long (>254 chars)"
    if not EMAIL_RE.match(email):
        if "@" not in email:
            return False, "missing @"
        return False, "bad format"
    local, _, domain = email.rpartition("@")
    if ".." in email or email.startswith(".") or email.startswith("@"):
        return False, "bad format"
    if not local or not domain or "." not in domain:
        return False, "bad domain"
    return True, ""


# ---------- templating ----------

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# Single-brace spintax {a|b|c}, supporting nesting. The pattern requires a
# pipe and no inner braces, so it always matches the INNERMOST group first and
# never touches {{double-brace}} template variables (no pipe) or {CSS} blocks.
SPINTAX_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def process_spintax(text: str, rng=None) -> str:
    """Expand {opt1|opt2|{nested|deep}} spintax recursively. Braces without a
    pipe are left as-is (so stray {CSS} blocks survive)."""
    import random as _random
    rng = rng or _random
    if not text or "{" not in text:
        return text or ""
    out, protected = text, {}

    def _protect(m):
        key = f"\x00SP{len(protected)}\x00"
        protected[key] = m.group(0)
        return key

    # hide no-pipe single-brace groups so they can't loop forever
    out = re.sub(r"(?<!\{)\{([^{}]*)\}(?!\})",
                 lambda m: _protect(m) if "|" not in m.group(1) else m.group(0), out)
    for _ in range(50):  # nesting depth guard
        m = SPINTAX_RE.search(out)
        if not m:
            break
        out = out[:m.start()] + rng.choice(m.group(1).split("|")) + out[m.end():]
    for key, val in protected.items():
        out = out.replace(key, val)
    return out


def preview_variations(text: str, n: int = 5, seed: int = 0):
    """Deterministic-ish preview: n unique-ish variations for the draft editor."""
    import random as _random
    seen, out = set(), []
    for i in range(n * 4):
        v = process_spintax(text, _random.Random(seed + i))
        if v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) == n:
            break
    return out


def render_template(text: str, contact: dict, extra: dict | None = None,
                  rng=None) -> str:
    """Fill {{first_name}} etc. Unknown variables are left as-is.
    Spintax {a|b} is expanded first so every outgoing email is unique.
    Unlimited custom fields (contacts.custom_fields JSON) are merged in."""
    text = process_spintax(text or "", rng)
    ctx = {
        "first_name": contact.get("first_name", "") or "",
        "last_name": contact.get("last_name", "") or "",
        "company": contact.get("company", "") or "",
        "email": contact.get("email", "") or "",
        "custom_1": contact.get("custom_1", "") or "",
        "custom_2": contact.get("custom_2", "") or "",
        "custom_3": contact.get("custom_3", "") or "",
    }
    cf = contact.get("custom_fields")
    if cf:
        try:
            parsed = json.loads(cf) if isinstance(cf, str) else cf
            if isinstance(parsed, dict):
                ctx.update({str(k): (v or "") for k, v in parsed.items()})
        except Exception:
            pass
    if extra:
        ctx.update(extra)

    def repl(m):
        key = m.group(1)
        return str(ctx.get(key, m.group(0)))

    return VAR_RE.sub(repl, text or "")


# ---------- open / click tracking ----------

PIXEL_HTML = ('<img src="{base}/t/o/{token}.png" width="1" height="1" '
              'style="display:none" alt="">')

HREF_RE = re.compile(r'''(?i)<a(\s[^>]*?)href=(["'])(.*?)\2''', re.DOTALL)


def inject_open_pixel(body_html: str, base_url: str, token: str) -> str:
    """Append an invisible 1x1 tracking pixel to the HTML body."""
    pixel = PIXEL_HTML.format(base=base_url.rstrip("/"), token=token)
    if re.search(r"(?i)</body\s*>", body_html or ""):
        return re.sub(r"(?i)</body\s*>", pixel + "</body>", body_html, count=1)
    return (body_html or "") + pixel


def rewrite_links(body_html: str, base_url: str, token: str) -> str:
    """Wrap every <a href> through the click-tracking redirect."""
    import urllib.parse

    def _wrap(m):
        attrs, quote, url = m.group(1), m.group(2), m.group(3)
        if not url or url.startswith(("mailto:", "#", "javascript:")):
            return m.group(0)
        if f"/t/c/{token}" in url:
            return m.group(0)  # already wrapped
        target = f"{base_url.rstrip('/')}/t/c/{token}?url={urllib.parse.quote(url, safe='')}"
        return f"<a{attrs}href={quote}{target}{quote}"

    return HREF_RE.sub(_wrap, body_html or "")


TRANSPARENT_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def html_to_text(html: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def build_message(from_email: str, to_email: str, subject: str, body_html: str,
                  message_id: str, unsub_url: str, mailing_address: str,
                  reply_to: str | None = None, in_reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        # follow-up steps thread into the same conversation
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    footer_html = (
        f'<br><br><hr><p style="font-size:11px;color:#888">'
        f'You received this email because you are on our mailing list.<br>'
        f'<a href="{unsub_url}">Unsubscribe</a> instantly — no questions asked.<br>'
        f'{mailing_address}</p>'
    )
    full_html = (body_html or "") + footer_html
    msg.set_content(html_to_text(full_html))
    msg.add_alternative(full_html, subtype="html")
    return msg


# ---------- SMTP ----------

def smtp_send(account: dict, msg: EmailMessage, timeout: int = 30):
    """account: dict with smtp_host/port/user/pass(enc or plain flag)/enc.
    Returns (ok, response_text). Raises nothing."""
    from .crypto import decrypt
    password = account.get("smtp_pass")
    if account.get("_enc"):
        password = decrypt(password)
    enc = (account.get("smtp_enc") or "STARTTLS").upper()
    try:
        if enc == "SSL":
            server = smtplib.SMTP_SSL(account["smtp_host"], int(account["smtp_port"]), timeout=timeout)
        else:
            server = smtplib.SMTP(account["smtp_host"], int(account["smtp_port"]), timeout=timeout)
            if enc == "STARTTLS":
                server.ehlo()
                server.starttls()
                server.ehlo()
        try:
            code, resp = server.login(account["smtp_user"], password)
        except smtplib.SMTPAuthenticationError as e:
            server.quit()
            return False, f"AUTH FAILED: {e.smtp_error.decode(errors='replace') if hasattr(e, 'smtp_error') else e}"
        try:
            errors = server.send_message(msg)
            server.quit()
            if errors:
                return False, f"rejected recipients: {errors}"
            return True, "250 OK accepted"
        except smtplib.SMTPException as e:
            server.quit()
            return False, f"send error: {e}"
    except (socket.timeout, socket.gaierror, ConnectionError, OSError) as e:
        return False, f"connection error: {e}"
    except Exception as e:
        return False, f"unexpected: {e}"


def test_connection(account: dict, timeout: int = 20):
    """Send a test email to self, then read it back via IMAP. Returns dict."""
    from .crypto import decrypt
    result = {"smtp_ok": False, "imap_ok": False, "smtp_msg": "", "imap_msg": ""}
    test_subject = f"Email Tool connection test {uuid.uuid4().hex[:8]}"
    msg = EmailMessage()
    msg["From"] = account["email"]
    msg["To"] = account["email"]
    msg["Subject"] = test_subject
    msg["Message-ID"] = make_msgid(domain=account["email"].split("@")[-1])
    msg.set_content("This is a connection test from your Email Automation Tool. If you can read this, SMTP and IMAP both work.")

    acct = dict(account, _enc=True)
    ok, resp = smtp_send(acct, msg, timeout=timeout)
    result["smtp_ok"] = ok
    result["smtp_msg"] = resp
    if not ok:
        return result

    # IMAP read-back: poll for the test subject (skipped for send-only accounts)
    if not int(account.get("imap_enabled", 1)):
        result["imap_ok"] = True
        result["imap_msg"] = "IMAP monitoring disabled for this account (send-only)"
        return result
    password = decrypt(account["imap_pass_enc"])
    try:
        mail = _imap_connect(account, password, timeout)
        mail.select("INBOX")
        deadline = time.time() + 45
        found = False
        while time.time() < deadline and not found:
            typ, data = mail.search(None, "UNSEEN", "SUBJECT", f'"{test_subject}"')
            if typ == "ok" and data and data[0]:
                found = True
                break
            time.sleep(5)
            mail.noop()
        mail.logout()
        result["imap_ok"] = found
        result["imap_msg"] = "test email found in inbox" if found else "sent OK but test email not found in inbox within 45s"
    except Exception as e:
        result["imap_msg"] = f"IMAP error: {e}"
    return result


def _imap_connect(account: dict, password: str, timeout: int = 30):
    enc = (account.get("imap_enc") or "SSL").upper()
    if enc == "SSL":
        mail = imaplib.IMAP4_SSL(account["imap_host"], int(account["imap_port"]))
    else:
        mail = imaplib.IMAP4(account["imap_host"], int(account["imap_port"]))
        mail.starttls()
    mail.login(account["imap_user"], password)
    return mail


# ---------- IMAP reply polling ----------

def fetch_unseen_replies(account: dict, timeout: int = 30, limit: int = 50):
    """Returns list of dicts: from, subject, body, in_reply_to, references, date."""
    from .crypto import decrypt
    out = []
    password = decrypt(account["imap_pass_enc"])
    mail = _imap_connect(account, password, timeout)
    try:
        mail.select("INBOX")
        typ, data = mail.search(None, "UNSEEN")
        if typ != "ok" or not data or not data[0]:
            return out
        ids = data[0].split()[-limit:]
        for num in ids:
            typ, parts = mail.fetch(num, "(RFC822)")
            if typ != "ok":
                continue
            raw = parts[0][1]
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            body = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    if part.get_content_type() == "text/plain" and not part.get_filename():
                        try:
                            body = part.get_content()
                        except Exception:
                            body = ""
                        break
            else:
                try:
                    body = parsed.get_content()
                except Exception:
                    body = ""
            out.append({
                "imap_num": num,
                "from": parseaddr(str(parsed.get("From", "")))[1],
                "subject": str(parsed.get("Subject", "")),
                "in_reply_to": str(parsed.get("In-Reply-To", "") or "").strip(),
                "references": str(parsed.get("References", "") or "").strip(),
                "date": str(parsed.get("Date", "")),
                "body": (body or "")[:20000],
                "message_id": str(parsed.get("Message-ID", "") or "").strip(),
            })
        return out
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def mark_seen(account: dict, imap_num, timeout: int = 30):
    from .crypto import decrypt
    password = decrypt(account["imap_pass_enc"])
    mail = _imap_connect(account, password, timeout)
    try:
        mail.select("INBOX")
        mail.store(imap_num, "+FLAGS", "\\Seen")
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ---------- reply classification (keyword rules) ----------

def classify_reply(from_email: str, subject: str, body: str) -> str:
    s = (subject or "").lower()
    b = (body or "").lower()
    text = s + "\n" + b
    frm = (from_email or "").lower()

    # Bounce: mailer daemons / delivery failure notices
    if ("mailer-daemon" in frm or "postmaster" in frm or "mail delivery" in frm
            or "delivery status notification" in s or "undeliverable" in s
            or "delivery failure" in s or "message not delivered" in s):
        return "Bounce"
    if any(p in text for p in ["unsubscribe", "remove me", "stop emailing",
                               "opt me out", "opt-out", "do not contact",
                               "take me off", "no more emails"]):
        return "Unsubscribe"
    if any(p in text for p in ["not interested", "no thanks", "not a fit",
                               "not for us", "please don't", "don't contact"]):
        return "Not Interested"
    if any(p in text for p in ["out of office", "out-of-office", "ooo:", "on vacation",
                               "on leave", "away from", "automatic reply",
                               "auto-reply", "auto reply", "will respond when i return"]):
        return "Out of Office"
    if any(p in text for p in ["interested", "tell me more", "let's talk", "lets talk",
                               "book a call", "schedule a call", "sounds good",
                               "yes please", "call me", "send me more", "count me in",
                               "let's do it"]):
        return "Interested"
    if text.strip().endswith("?") or any(p in text for p in [
            "how much", "pricing", "price", "cost", "how does",
            "what does", "can you explain", "more info", "more details"]):
        return "Question"
    return "Other"


# ---------- deliverability seed testing ----------

SPAM_FOLDERS = ["[Gmail]/Spam", "Junk", "Bulk Mail", "Spam", "Junk E-Mail"]


def _find_by_msgid(mail, mailbox: str, message_id: str) -> bool:
    """True if a message with this Message-ID exists in the mailbox."""
    try:
        typ, _ = mail.select(f'"{mailbox}"', readonly=True)
    except Exception:
        return False
    if typ != "ok":
        return False
    mid = message_id.strip().strip("<>")
    try:
        typ, data = mail.search(None, "HEADER", "Message-ID", mid)
    except Exception:
        return False
    return typ == "ok" and bool(data) and bool(data[0])


def check_seed_placement(seed: dict, message_id: str, timeout: int = 30):
    """Log into a seed inbox and report where the test email landed.

    Returns {"placement": "inbox"|"spam"|"missing", "folder": str|None}.
    Pure IMAP logic — unit-testable with a fake IMAP object via monkeypatching
    _imap_connect (it takes the same account-shaped dict)."""
    from .crypto import decrypt
    password = decrypt(seed["imap_pass_enc"])
    acct = {"imap_host": seed["imap_host"], "imap_port": seed.get("imap_port", 993),
            "imap_user": seed["imap_user"], "imap_enc": seed.get("imap_enc", "SSL")}
    mail = _imap_connect(acct, password, timeout)
    try:
        if _find_by_msgid(mail, "INBOX", message_id):
            return {"placement": "inbox", "folder": "INBOX"}
        for folder in SPAM_FOLDERS:
            if _find_by_msgid(mail, folder, message_id):
                return {"placement": "spam", "folder": folder}
        return {"placement": "missing", "folder": None}
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def placement_score(results):
    """results: list of {"placement": ...}. Returns dict with inbox/spam/missing %."""
    n = len(results) or 1
    inbox = sum(1 for r in results if r.get("placement") == "inbox")
    spam = sum(1 for r in results if r.get("placement") == "spam")
    return {"total": len(results),
            "inbox_pct": round(inbox / n * 100, 1),
            "spam_pct": round(spam / n * 100, 1),
            "missing_pct": round((len(results) - inbox - spam) / n * 100, 1)}


# ---------- deliverability lookups ----------

def _txt_records(name: str, timeout: int = 5):
    import dns.resolver
    r = dns.resolver.Resolver()
    r.lifetime = timeout
    try:
        answers = r.resolve(name, "TXT")
        return [b"".join(a.strings).decode(errors="replace") for a in answers]
    except Exception:
        return []


def check_spf(domain: str):
    recs = _txt_records(domain)
    spf = [r for r in recs if r.lower().startswith("v=spf1")]
    if spf:
        return "pass", spf[0][:200]
    return "fail", "no SPF record found"


def check_dmarc(domain: str):
    recs = _txt_records(f"_dmarc.{domain}")
    dm = [r for r in recs if r.lower().startswith("v=dmarc1")]
    if dm:
        return "pass", dm[0][:200]
    return "fail", "no DMARC record found"


DKIM_SELECTORS = ["google", "default", "selector1", "selector2", "k1", "k2",
                  "mail", "dkim", "everlytickey1", "s1", "s2"]


def check_dkim(domain: str):
    found = []
    for sel in DKIM_SELECTORS:
        recs = _txt_records(f"{sel}._domainkey.{domain}")
        dkim = [r for r in recs if "v=dkim1" in r.lower() or "k=rsa" in r.lower()]
        if dkim:
            found.append(sel)
    if found:
        return "pass", f"DKIM key found (selector: {found[0]})"
    return "unknown", "no DKIM key found with common selectors (may still exist)"
