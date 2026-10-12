"""Email plumbing: validation, templating, SMTP send, IMAP reply polling,
reply classification, and deliverability (SPF/DKIM/DMARC) lookups."""
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


def render_template(text: str, contact: dict, extra: dict | None = None) -> str:
    """Fill {{first_name}} etc. Unknown variables are left as-is."""
    ctx = {
        "first_name": contact.get("first_name", "") or "",
        "last_name": contact.get("last_name", "") or "",
        "company": contact.get("company", "") or "",
        "email": contact.get("email", "") or "",
        "custom_1": contact.get("custom_1", "") or "",
        "custom_2": contact.get("custom_2", "") or "",
        "custom_3": contact.get("custom_3", "") or "",
    }
    if extra:
        ctx.update(extra)

    def repl(m):
        key = m.group(1)
        return str(ctx.get(key, m.group(0)))

    return VAR_RE.sub(repl, text or "")


def html_to_text(html: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def build_message(from_email: str, to_email: str, subject: str, body_html: str,
                  message_id: str, unsub_url: str, mailing_address: str,
                  reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
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
