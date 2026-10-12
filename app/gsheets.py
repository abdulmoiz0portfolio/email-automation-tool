"""Google Sheets import/sync for contact lists.

Two access modes:
1. API key mode (GOOGLE_SHEETS_API_KEY in settings): Sheets API v4 — works for
   any sheet the key can read (including private ones shared with the key's project).
2. Public mode (no key): gviz CSV export — works only for sheets shared as
   "Anyone with the link". Tab listing is unavailable without a key, so the
   caller must supply the tab name (defaults to the first sheet).

The API key itself is never logged; callers pass it in, this module only uses it
in the Authorization header / query param for the request at hand.
"""
import base64
import csv
import io
import json
import re
import time
import urllib.parse

import httpx

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# in-memory access-token cache: {client_email: (token, expires_at)}
_token_cache = {}

_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def parse_sheet_id(url_or_id: str) -> str | None:
    """Extract the spreadsheet ID from a share link, export URL, or bare ID."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    # bare spreadsheet ID (44 chars, url-safe base64-ish)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", s):
        return s
    return None


def _api_key() -> str:
    from . import auth as authmod  # lazy: avoids import cycle at module load
    from . import crypto
    enc = authmod.get_setting("google_sheets_api_key", "")
    return crypto.decrypt(enc) if enc else ""


def get_tabs(sheet_id: str, api_key: str | None = None) -> list[str]:
    """Return worksheet tab titles. Requires an API key (no public endpoint)."""
    key = api_key if api_key is not None else _api_key()
    if not key:
        return []
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{SHEETS_API}/{sheet_id}",
                  params={"fields": "sheets.properties.title", "key": key})
    if r.status_code != 200:
        raise RuntimeError(f"Sheets API error {r.status_code}: {r.text[:200]}")
    sheets = r.json().get("sheets", [])
    return [s["properties"]["title"] for s in sheets if "properties" in s]


def fetch_values(sheet_id: str, tab: str | None = None,
                 api_key: str | None = None) -> tuple[list[str], list[dict]]:
    """Return (headers, rows) for a worksheet tab. Tab None = first sheet."""
    key = api_key if api_key is not None else _api_key()
    if key:
        return _fetch_via_api(sheet_id, tab, key)
    return _fetch_via_gviz(sheet_id, tab)


def _fetch_via_api(sheet_id: str, tab: str | None, key: str):
    rng = f"'{tab}'" if tab else "A1:Z100000"
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{SHEETS_API}/{sheet_id}/values/{urllib.parse.quote(rng)}",
                  params={"key": key, "valueRenderOption": "UNFORMATTED_VALUE"})
    if r.status_code != 200:
        raise RuntimeError(f"Sheets API error {r.status_code}: {r.text[:200]}")
    values = r.json().get("values", [])
    return _values_to_rows(values)


def _fetch_via_gviz(sheet_id: str, tab: str | None):
    params = {"tqx": "out:csv"}
    if tab:
        params["sheet"] = tab
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(url, params=params,
                  headers={"User-Agent": "email-automation-tool/1.0"})
    if r.status_code != 200:
        raise RuntimeError(
            f"Could not fetch sheet (HTTP {r.status_code}). Is it shared as "
            f"'Anyone with the link'?")
    text = r.text
    if text.lstrip().startswith("<"):
        raise RuntimeError(
            "Google returned an HTML page instead of data — the sheet is not "
            "shared as 'Anyone with the link'.")
    reader = csv.reader(io.StringIO(text))
    values = list(reader)
    return _values_to_rows(values)


def _values_to_rows(values: list[list]) -> tuple[list[str], list[dict]]:
    """First row = headers; drop fully-empty rows; cells → stripped strings."""
    if not values:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in values[0]]
    # drop trailing empty header columns
    while headers and headers[-1] == "":
        headers.pop()
    rows = []
    for v in values[1:]:
        if all(c is None or str(c).strip() == "" for c in v):
            continue
        rows.append({headers[i]: ("" if c is None else str(c).strip())
                     for i, c in enumerate(v) if i < len(headers)})
    return headers, rows


# ---------- service-account auth (write access) ----------
# Sheets API *keys* are read-only. Writing needs OAuth — we use a service
# account (JSON key uploaded in Settings), signed JWT -> access token.

def service_account_info() -> dict | None:
    """Decrypted service-account JSON from settings, or None."""
    from . import auth as authmod  # lazy: avoids import cycle at module load
    from . import crypto
    enc = authmod.get_setting("google_service_account_json", "")
    if not enc:
        return None
    try:
        info = json.loads(crypto.decrypt(enc))
    except Exception:
        return None
    if not isinstance(info, dict) or "client_email" not in info or "private_key" not in info:
        return None
    return info


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sa_access_token(info: dict) -> str:
    """OAuth2 access token via service-account JWT assertion (cached)."""
    email = info["client_email"]
    cached = _token_cache.get(email)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({
        "iss": email,
        "scope": SHEETS_SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }).encode())
    signing_input = f"{header}.{claims}".encode()
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{header}.{claims}.{_b64url(sig)}"
    with httpx.Client(timeout=20) as c:
        r = c.post(TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        })
    if r.status_code != 200:
        raise RuntimeError(f"Service-account token failed ({r.status_code}): {r.text[:200]}")
    body = r.json()
    token = body["access_token"]
    _token_cache[email] = (token, now + int(body.get("expires_in", 3600)))
    return token


def _col_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _rng(tab: str | None, a1: str) -> str:
    return f"'{tab}'!{a1}" if tab else a1


def write_sent_via(sheet_id: str, tab: str | None,
                   email_to_account: dict[str, str],
                   sa_info: dict | None = None) -> int:
    """Write the sending account's email into a `sent_via` column, matched by
    contact email. Creates the column if missing. Exactly ONE
    values.batchUpdate call per invocation (plus one read, plus one header
    update only when the column must be created). Returns rows written."""
    sa = sa_info or service_account_info()
    if not sa:
        raise RuntimeError("No Google service account configured — write-back needs one.")
    token = _sa_access_token(sa)
    auth = {"Authorization": f"Bearer {token}"}
    mapping = {str(e).strip().lower(): a for e, a in email_to_account.items() if e and a}
    if not mapping:
        return 0

    with httpx.Client(timeout=60, headers=auth) as c:
        rng = _rng(tab, "A1:Z100000")
        r = c.get(f"{SHEETS_API}/{sheet_id}/values/{urllib.parse.quote(rng)}",
                  params={"valueRenderOption": "UNFORMATTED_VALUE"})
        if r.status_code != 200:
            raise RuntimeError(f"Sheets read failed ({r.status_code}): {r.text[:200]}")
        values = r.json().get("values", [])
        if not values:
            raise RuntimeError("Sheet tab is empty.")
        header = [str(h).strip() for h in values[0]]
        email_idx = next((i for i, h in enumerate(header) if "email" in h.lower()), None)
        if email_idx is None:
            raise RuntimeError("No email column found in the sheet tab.")
        sv_idx = next((i for i, h in enumerate(header) if h.lower() == "sent_via"), None)
        if sv_idx is None:
            # create the column: extend the header row
            sv_idx = len(header)
            col = _col_letter(sv_idx)
            rr = c.put(
                f"{SHEETS_API}/{sheet_id}/values/{urllib.parse.quote(_rng(tab, f'{col}1'))}",
                params={"valueInputOption": "RAW"},
                json={"values": [["sent_via"]]})
            if rr.status_code != 200:
                raise RuntimeError(f"Could not create sent_via column ({rr.status_code}).")
        col = _col_letter(sv_idx)
        data = []
        for rownum, row in enumerate(values[1:], start=2):
            if email_idx >= len(row):
                continue
            em = str(row[email_idx]).strip().lower()
            if em not in mapping:
                continue
            cur = str(row[sv_idx]).strip() if sv_idx < len(row) else ""
            if cur == mapping[em]:
                continue  # already correct — skip
            data.append({"range": _rng(tab, f"{col}{rownum}"),
                         "values": [[mapping[em]]]})
        if not data:
            return 0
        rb = c.post(f"{SHEETS_API}/{sheet_id}/values:batchUpdate",
                    json={"valueInputOption": "RAW", "data": data})
        if rb.status_code != 200:
            raise RuntimeError(f"batchUpdate failed ({rb.status_code}): {rb.text[:200]}")
        return len(data)
