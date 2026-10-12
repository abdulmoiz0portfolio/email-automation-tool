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
import csv
import io
import re
import urllib.parse

import httpx

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

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
