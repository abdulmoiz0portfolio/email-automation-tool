"""Admin authentication: session cookies + scrypt password hashing + sudo re-auth.

- Sessions are server-side: a random token lives in a signed-ish cookie and is
  validated against the `sessions` table (expiry + sliding renewal).
- Passwords hashed with hashlib.scrypt (stdlib, memory-hard).
- Sudo mode: after re-entering the admin password, `sudo_until` grants a
  10-minute window for credential viewing/modification and destructive actions.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from . import db

SESSION_COOKIE = "emailtool_session"
SESSION_DAYS = 7
SUDO_MINUTES = 10

SESSION_SECRET = os.environ.get("SESSION_SECRET", "")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# ---------- password hashing (scrypt) ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ---------- admin account state ----------

def get_setting(key, default=None):
    row = db.get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db.tx(lambda c: c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
                              (key, value)))


def needs_setup() -> bool:
    return not get_setting("admin_password_hash")


# ---------- sessions ----------

def _sign(token: str) -> str:
    """Bind the token to SESSION_SECRET so a stolen DB alone can't forge cookies."""
    if not SESSION_SECRET:
        return token
    sig = hmac.new(SESSION_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{token}.{sig}"


def _unsign(value: str):
    if not SESSION_SECRET:
        return value
    if "." not in value:
        return None
    token, sig = value.rsplit(".", 1)
    want = hmac.new(SESSION_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, want):
        return None
    return token


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.tx(lambda c: c.execute(
        "INSERT INTO sessions(token, created_at, expires_at, sudo_until) VALUES(?,?,?,NULL)",
        (token, iso(now), iso(now + timedelta(days=SESSION_DAYS)))))
    return _sign(token)


def get_session(cookie_value: str):
    """Returns the session row dict or None. Slides expiry on use."""
    token = _unsign(cookie_value or "")
    if not token:
        return None
    row = db.get_db().execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if exp < utcnow():
        db.tx(lambda c: c.execute("DELETE FROM sessions WHERE token=?", (token,)))
        return None
    # sliding renewal
    db.tx(lambda c: c.execute("UPDATE sessions SET expires_at=? WHERE token=?",
                              (iso(utcnow() + timedelta(days=SESSION_DAYS)), token)))
    return dict(row)


def destroy_session(cookie_value: str):
    token = _unsign(cookie_value or "")
    if token:
        db.tx(lambda c: c.execute("DELETE FROM sessions WHERE token=?", (token,)))


def grant_sudo(token: str):
    db.tx(lambda c: c.execute("UPDATE sessions SET sudo_until=? WHERE token=?",
                              (iso(utcnow() + timedelta(minutes=SUDO_MINUTES)), token)))


def has_sudo(session: dict) -> bool:
    if not session or not session.get("sudo_until"):
        return False
    try:
        until = datetime.fromisoformat(session["sudo_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > utcnow()
    except Exception:
        return False


def authenticate(username: str, password: str) -> bool:
    stored_user = get_setting("admin_username", "admin")
    stored_hash = get_setting("admin_password_hash")
    if not stored_hash:
        return False
    if not hmac.compare_digest(username or "", stored_user or ""):
        return False
    return verify_password(password or "", stored_hash)
