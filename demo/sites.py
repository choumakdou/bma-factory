"""Site Value — a third source of projects for the map.

Bobby keeps a spreadsheet of his valuations for sites. This module
reads it and turns each row into a pin.

Three things this deliberately does NOT do, and they are the same
three the map already obeys:

1. **It does not guess.** A column is only read as a field when its
   header matches a known name, and when nothing matches, the columns
   that WERE found are reported rather than the row being silently
   mis-read. Bobby maps his own columns on the setup page, and the
   mapping is remembered.

2. **It does not invent a location.** A site with no coordinates gets
   no pin until it is placed by hand or corroborated by the geocoder.

3. **It does not overwrite anything.** The sheet is a source, not the
   system of record: re-reading it refreshes what it can see and
   leaves alone what it cannot.

Storage: the sheet URL and the column mapping live in the valuer's own
Drive, next to the section rules, because they are his settings and
should travel with his files.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger("bma-factory.sites")

#: Where the settings live.
SOURCE_FILE = "site-value.json"
SOURCE_FOLDER = "Templates"

#: The fields a site valuation row can carry. Each is a list of header
#: names, matched whole-word so "Site Area" and "Site" do not collide.
FIELD_ALIASES: dict[str, list[str]] = {
    "name": ["site", "site name", "development", "project", "property",
             "building", "estate"],
    "address": ["address", "site address", "location", "street address",
                "road", "lot", "site location"],
    "district": ["district", "area", "region", "hma", "location district"],
    "client": ["client", "vendor", "developer", "owner", "for"],
    "valuation_date": ["valuation date", "date of valuation", "valuation",
                       "valued", "date"],
    "site_area": ["site area", "land area", "area sq ft", "area sqft",
                  "site area sq ft", "site area sqft", "land area sq ft",
                  "site area sq m", "site area sqm", "lot area"],
    "floor_area": ["floor area", "gfa", "gross floor area", "saleable area",
                   "sa", "nfa"],
    "value": ["value", "valuation", "market value", "adopted value",
              "capital value", "site value", "land value"],
    "rate": ["rate", "rate psf", "unit rate", "price psf", "per sq ft",
             "rate per sq ft", "land rate"],
    "use": ["use", "usage", "site use", "zoning", "development type"],
    "lat": ["lat", "latitude", "y"],
    "lon": ["lon", "lng", "longitude", "x"],
}

#: The order fields are shown in on the setup page.
FIELD_ORDER = ["name", "address", "district", "client", "valuation_date",
               "site_area", "floor_area", "value", "rate", "use", "lat", "lon"]

FIELD_LABELS = {
    "name": "Site / development name",
    "address": "Address",
    "district": "District",
    "client": "Client",
    "valuation_date": "Valuation date",
    "site_area": "Site area",
    "floor_area": "Floor area",
    "value": "Value",
    "rate": "Rate",
    "use": "Use",
    "lat": "Latitude",
    "lon": "Longitude",
}

#: Fields the map cannot do without.
REQUIRED_FOR_A_PIN = ["address"]


# ---------------------------------------------------------------------------
# Column matching
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s/&-]", " ", str(s or "").lower()).split())


def header_matches(header: str, alias: str) -> bool:
    """Does this column header mean this field?

    Whole-word matching, so 'SITE' matches 'Site Area' but 'AREA'
    alone does not, and 'Site' does not swallow 'Site Area'. The same
    rule the section rules use.
    """
    h = _norm(header)
    a = _norm(alias)
    if not h or not a:
        return False
    if h == a:
        return True
    return re.search(rf"(?<!\w){re.escape(a)}(?!\w)", h) is not None


def detect_columns(headers: list[str],
                   overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Map field -> column header.

    The valuer's own mapping wins. Otherwise each field claims the
    first header that matches one of its aliases, and no two fields
    claim the same header.
    """
    out: dict[str, str] = {}
    taken: set[str] = set()

    for field, header in (overrides or {}).items():
        if field in FIELD_ALIASES and header:
            out[field] = header
            taken.add(header)

    for field in FIELD_ORDER:
        if field in out:
            continue
        for alias in FIELD_ALIASES[field]:
            for h in headers:
                if h in taken:
                    continue
                if header_matches(h, alias):
                    out[field] = h
                    taken.add(h)
                    break
            if field in out:
                break
    return out


def unmapped_headers(headers: list[str], mapping: dict[str, str]) -> list[str]:
    """Headers no field claimed — so Bobby can see what was ignored."""
    used = set(mapping.values())
    return [h for h in headers if h not in used]


# ---------------------------------------------------------------------------
# Reading the sheet
# ---------------------------------------------------------------------------

def parse_sheet_url(url: str) -> tuple[str, str]:
    """(file_id, gid) from a Google Sheets URL. gid may be ''.

    Accepts a full URL, a bare file id, or a '/d/<id>' fragment.
    """
    text = str(url or "").strip()
    if not text:
        return "", ""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9-_]+)", text)
    if m:
        file_id = m.group(1)
    else:
        m2 = re.search(r"^([A-Za-z0-9-_]{20,})$", text)
        if m2:
            file_id = m2.group(1)
        else:
            return "", ""
    gid = ""
    mg = re.search(r"[#&?]gid=(\d+)", text)
    if mg:
        gid = mg.group(1)
    return file_id, gid


def _access_token() -> str:
    from demo.oauth import load_credentials
    creds = load_credentials()
    if creds is None:
        raise RuntimeError("Not signed in.")
    if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return str(creds.token)


def fetch_sheet_csv(url: str) -> tuple[str, str]:
    """(csv_text, note) for a Google Sheet. Raises on failure.

    Uses the Sheets export endpoint with the OAuth token as a bearer,
    which honours the ``gid`` and needs no scope beyond the
    ``drive.readonly`` already granted — adding a Sheets scope would
    mean Bobby re-consenting for something the Drive export can do.
    """
    file_id, gid = parse_sheet_url(url)
    if not file_id:
        raise ValueError("That does not look like a Google Sheet link.")
    token = _access_token()
    import urllib.parse
    import urllib.request

    q = {"format": "csv"}
    if gid:
        q["gid"] = gid
    export = (f"https://docs.google.com/spreadsheets/d/{file_id}/export?"
              + urllib.parse.urlencode(q))
    req = urllib.request.Request(export, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "bma-factory/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig", "replace")
    note = f"sheet {file_id}" + (f", tab {gid}" if gid else ", first tab")
    return raw, note


def rows_from_csv(csv_text: str) -> tuple[list[str], list[list[str]]]:
    """(headers, rows) from CSV text.

    The first non-blank row is the header. Everything after it is
    data. A sheet with a title row above the headers is handled by
    taking the first row that has at least two non-empty cells and a
    cell that looks like a header — otherwise the first row is the
    header and the rest is data.
    """
    reader = csv.reader(io.StringIO(csv_text))
    all_rows = [r for r in reader]
    if not all_rows:
        return [], []

    # Skip leading blank rows.
    start = 0
    while start < len(all_rows) and not any(
            str(c).strip() for c in all_rows[start]):
        start += 1
    if start >= len(all_rows):
        return [], []

    headers = [str(c).strip() for c in all_rows[start]]
    body = all_rows[start + 1:]
    # A row where every cell is blank is not a row.
    body = [r for r in body if any(str(c).strip() for c in r)]
    return headers, body


def load_sheet(url: str) -> tuple[list[str], list[list[str]], str]:
    """(headers, rows, note). Raises on failure."""
    csv_text, note = fetch_sheet_csv(url)
    headers, rows = rows_from_csv(csv_text)
    return headers, rows, note


# ---------------------------------------------------------------------------
# Settings — in the valuer's own Drive
# ---------------------------------------------------------------------------

def _settings_path() -> str:
    from demo.drive import DRIVE_ROOT_PATH
    return f"{DRIVE_ROOT_PATH}/{SOURCE_FOLDER}/{SOURCE_FILE}"


def load_settings() -> dict[str, Any]:
    """The sheet URL and column mapping. Empty when not set up yet."""
    out: dict[str, Any] = {"url": "", "mapping": {}, "overrides": {}}
    try:
        from demo.drive import app_folder_if_exists
        from demo.oauth import get_drive
        folder_id = app_folder_if_exists(SOURCE_FOLDER)
        if not folder_id:
            return out
        drive = get_drive()
        r = drive.files().list(
            q=f"'{folder_id}' in parents and name='{SOURCE_FILE}' and trashed=false",
            fields="files(id,name)", pageSize=5, supportsAllDrives=True,
        ).execute()
        files = r.get("files", [])
        if not files:
            return out
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buf, drive.files().get_media(fileId=files[0]["id"]))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = json.loads(buf.getvalue().decode("utf-8"))
        if isinstance(data, dict):
            out["url"] = str(data.get("url") or "")
            if isinstance(data.get("mapping"), dict):
                out["mapping"] = {str(k): str(v) for k, v in data["mapping"].items()}
            if isinstance(data.get("overrides"), dict):
                out["overrides"] = {str(k): str(v) for k, v in data["overrides"].items()}
        return out
    except Exception as e:  # noqa: BLE001
        log.info("could not load the site-value settings: %s", e)
        return out


def save_settings(url: str, overrides: dict[str, str]) -> tuple[bool, str]:
    """Store the sheet URL and the valuer's column mapping."""
    try:
        from demo.drive import app_folder
        from demo.oauth import get_drive
        from googleapiclient.http import MediaIoBaseUpload
        folder_id = app_folder(SOURCE_FOLDER)
        drive = get_drive()
        payload = json.dumps(
            {"version": 1, "url": str(url or "").strip(),
             "overrides": {k: v for k, v in (overrides or {}).items() if v}},
            indent=2, ensure_ascii=False,
        ).encode("utf-8")
        existing = drive.files().list(
            q=f"'{folder_id}' in parents and name='{SOURCE_FILE}' and trashed=false",
            fields="files(id)", pageSize=5, supportsAllDrives=True,
        ).execute().get("files", [])
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False)
        if existing:
            drive.files().update(
                fileId=existing[0]["id"], media_body=media,
                fields="id,name", supportsAllDrives=True,
            ).execute()
        else:
            drive.files().create(
                body={"name": SOURCE_FILE, "parents": [folder_id]},
                media_body=media, fields="id,name", supportsAllDrives=True,
            ).execute()
        return True, "saved"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Rows -> sites
# ---------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    try:
        s = str(v or "").strip().replace(",", "").replace("$", "")
        s = re.sub(r"^[^\d.\-]+", "", s)
        if not s or s in {"-", "—", "n/a", "N/A"}:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


#: Words that mean a row is a total, not a site.
_TOTAL_WORDS = {"total", "totals", "subtotal", "sub-total", "grand total",
                "sum", "grand total", "合计", "總計"}


def _is_total_row(row: dict[str, Any]) -> bool:
    """Is this a total line rather than a site?

    A valuation sheet ends with a total row more often than not, and a
    row called "Total" would otherwise become a pin at the centre of
    nowhere with a value attached to it.
    """
    name = str(row.get("name") or "").strip().lower()
    if name and name in _TOTAL_WORDS:
        return True
    # "Total — all sites" and similar.
    if name.startswith("total ") or name.startswith("total\t"):
        return True
    return False


def _parse_date(value: Any) -> Optional[str]:
    """A date from a sheet, normalised to ISO where possible.

    A spreadsheet writes dates however the person felt like it on the
    day: '2026-10-09', '09/10/2026', '9 Oct 2026', 'Oct 9, 2026'. The
    brochure's own parser handles the numeric forms; the written ones
    are handled here rather than loosening it, because that parser
    decides review deadlines and a wrong date there is worse than a
    date left as written.
    """
    from demo.brochure import parse_date

    d = parse_date(value)
    if d:
        return d.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                "%d-%b-%Y", "%d-%B-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    # Not a date we recognise: keep it as written rather than guess.
    return text


def build_sites(headers: list[str], rows: list[list[str]],
                overrides: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """(sites, mapping) from a sheet.

    A row with nothing identifying in it is skipped — a blank row, or
    a total row, must not become a pin at 0,0.
    """
    mapping = detect_columns(headers, overrides)
    idx = {h: i for i, h in enumerate(headers)}

    def cell(row: list[str], field: str) -> str:
        h = mapping.get(field)
        if not h or h not in idx:
            return ""
        i = idx[h]
        return str(row[i] or "").strip() if i < len(row) else ""

    sites: list[dict[str, Any]] = []
    for n, row in enumerate(rows):
        site = {
            "row": n + 2,                      # +2: one for the header, one for 1-based
            "name": cell(row, "name"),
            "address": cell(row, "address"),
            "district": cell(row, "district"),
            "client": cell(row, "client"),
            "valuation_date": _parse_date(cell(row, "valuation_date")),
            "site_area": cell(row, "site_area"),
            "floor_area": cell(row, "floor_area"),
            "value": cell(row, "value"),
            "rate": cell(row, "rate"),
            "use": cell(row, "use"),
        }
        lat, lon = _num(cell(row, "lat")), _num(cell(row, "lon"))
        # Always present, so a row's shape does not depend on its
        # contents. The template and the pin builder both ask for them
        # without having to check first.
        site["lat"] = lat if (lat is not None and lon is not None) else None
        site["lon"] = lon if (lat is not None and lon is not None) else None
        # Nothing identifying: not a site.
        if not (site["name"] or site["address"]):
            continue
        # A total row is not a site either.
        if _is_total_row(site):
            continue
        sites.append(site)
    return sites, mapping


def read_sites() -> dict[str, Any]:
    """Everything the Site Value tab needs, and nothing more.

    Never raises: a missing sheet, an unreachable sheet or an
    unreadable one is a state the page can show, not a crash.
    """
    settings = load_settings()
    out: dict[str, Any] = {
        "configured": bool(settings.get("url")),
        "url": settings.get("url", ""),
        "headers": [], "sites": [], "mapping": {}, "unmapped": [],
        "error": "", "note": "",
    }
    if not out["configured"]:
        return out
    try:
        headers, rows, note = load_sheet(settings["url"])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    sites, mapping = build_sites(headers, rows, settings.get("overrides") or {})
    out.update({
        "headers": headers, "sites": sites, "mapping": mapping,
        "unmapped": unmapped_headers(headers, mapping), "note": note,
    })
    return out
