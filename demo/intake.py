"""Drive intake — turn an Excel file dropped in Drive into app content.

The rule for this whole project is that there is one implementation of
everything and the front doors call it. This module is the "read" side of
the same pattern the engine has on the "write" side:

    Drive folder  ->  detect format  ->  parse  ->  content  ->  project

Three pieces, deliberately separated:

  1. ``detect_format`` / ``parse_rows`` are pure functions over rows.
     They have no idea Drive exists, which is what makes them testable
     without a Google account and reusable from any future front door.

  2. ``list_drive_workbooks`` and ``download_workbook`` are the thin
     Drive layer. They only move bytes.

  3. ``build_project`` turns a parsed Memfus Wong extract into a case
     (a "project") so the console and the project page pick it up.

Formats are registered in ``FORMATS`` rather than hard-coded into a
branch, so a new agency extract is a new entry, not a new if-statement.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("bma-factory.intake")

# Folders we treat as input. The first one is the landing zone the app
# creates; the second is where ORO workbooks already live.
INTAKE_FOLDERS = ("Intake", "ORO")

# The app's own name, so we only pick up what it put there or what the
# valuer clearly put in an intake folder.
EXCEL_MIME = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
}

MAX_PREVIEW_ROWS = 200
#: Rows kept in the cache. The cache is the source of truth for re-deriving
#: comparables, so it holds every row, not the slice the page shows.
MAX_CACHED_ROWS = 5000


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------

@dataclass
class FormatSpec:
    """One recognised workbook shape."""
    id: str
    label: str
    #: Returns True when the header row matches this format.
    detect: Callable[[list[str]], bool]
    #: Turns rows into the content dict the pages render.
    parse: Callable[[list[list[Any]], list[str]], "ParsedWorkbook"]
    #: True if the format carries a subject row and therefore defines a project.
    yields_project: bool = False


@dataclass
class ParsedWorkbook:
    """The result of parsing a workbook — what the content page renders."""
    format_id: str
    format_label: str
    sheet_name: str
    sheets: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False
    stats: dict[str, Any] = field(default_factory=dict)
    qc_counts: dict[str, int] = field(default_factory=dict)
    project: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None
    #: The workbook split into recognised blocks. Each is rendered in its
    #: own way: a subject record reads as key/value pairs, comparables
    #: with an adjustment ladder get the adjustment columns marked, the
    #: raw extract gets the QC flags.
    sections: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id,
            "format_label": self.format_label,
            "sheet_name": self.sheet_name,
            "sheets": self.sheets,
            "headers": self.headers,
            "rows": self.rows,
            "total_rows": self.total_rows,
            "truncated": self.truncated,
            "stats": self.stats,
            "qc_counts": self.qc_counts,
            "project": self.project,
            "notes": self.notes,
            "error": self.error,
            "sections": self.sections,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParsedWorkbook":
        return cls(
            format_id=d.get("format_id", "unknown"),
            format_label=d.get("format_label", "Unknown"),
            sheet_name=d.get("sheet_name", ""),
            sheets=d.get("sheets", []),
            headers=d.get("headers", []),
            rows=d.get("rows", []),
            total_rows=d.get("total_rows", 0),
            truncated=d.get("truncated", False),
            stats=d.get("stats", {}),
            qc_counts=d.get("qc_counts", {}),
            project=d.get("project"),
            notes=d.get("notes", []),
            error=d.get("error"),
            sections=d.get("sections", []),
        )


# --- helpers ---------------------------------------------------------------

def _norm_header(v: Any) -> str:
    """Header cell -> a comparable token."""
    if v is None:
        return ""
    return re.sub(r"[\s_\-]+", "", str(v)).upper()


def _read_workbook(data: bytes) -> tuple[Any, list[str]]:
    """Open an .xlsx/.xlsm from bytes. Returns (workbook, sheet names)."""
    import openpyxl

    wb = openpyxl.load_workbook(
        io.BytesIO(data), data_only=True, read_only=True
    )
    return wb, list(wb.sheetnames)


def _sheet_rows(ws: Any, limit: int | None = None) -> tuple[list[str], list[list[Any]]]:
    """First non-empty row is the header; the rest are data rows."""
    header: list[str] = []
    rows: list[list[Any]] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            candidate = ["" if c is None else str(c).strip() for c in row]
            if any(candidate):
                header = candidate
            else:
                # Blank first row — keep looking for the header.
                continue
            continue
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        rows.append(list(row))
        if limit is not None and len(rows) >= limit:
            break
    return header, rows


def _rows_as_dicts(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        d: dict[str, Any] = {}
        for i, h in enumerate(headers):
            key = h or f"COL{i+1}"
            d[key] = r[i] if i < len(r) else None
        out.append(d)
    return out


def _first_sheet_with_data(wb: Any) -> tuple[str, Any]:
    """The first sheet that has a non-empty first row."""
    for name in wb.sheetnames:
        ws = wb[name]
        for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
            if any(c is not None and str(c).strip() != "" for c in row):
                return name, ws
    return wb.sheetnames[0], wb[wb.sheetnames[0]]


# ---------------------------------------------------------------------------
# Memfus Wong transaction extract
# ---------------------------------------------------------------------------

def _detect_memfus(headers: list[str]) -> bool:
    norm = {_norm_header(h) for h in headers}
    # MEM_NO is the marker column; INST_DATE and CONSIDERTN confirm it.
    return "MEMNO" in norm and "INSTDATE" in norm


def _parse_memfus(sheet_name: str, headers: list[str], rows: list[list[Any]]) -> ParsedWorkbook:
    from ingest import memfus

    dicts = _rows_as_dicts(headers, rows)
    comps = memfus.from_rows(dicts, first_row=2)
    subject, evidence = memfus.split_subject(comps)

    qc_counts: dict[str, int] = {}
    for c in comps:
        for f in c.flags:
            qc_counts[f.code] = qc_counts.get(f.code, 0) + 1

    stats: dict[str, Any] = {
        "comps": len(evidence),
        "subject_found": subject is not None,
        "date_min": min((c.date for c in comps if c.date), default=None),
        "date_max": max((c.date for c in comps if c.date), default=None),
    }
    if stats["date_min"] is not None:
        stats["date_min"] = stats["date_min"].isoformat()
        stats["date_max"] = stats["date_max"].isoformat()

    notes: list[str] = []
    if subject is None:
        notes.append(
            "No row is marked as the subject (MEM_NO = 'Subject'). "
            "The extract will be shown as evidence only — it will not "
            "create a project."
        )

    project = None
    if subject is not None:
        project = _project_from_memfus(subject, evidence, sheet_name)

    return ParsedWorkbook(
        format_id="memfus-mw",
        format_label="Memfus Wong transaction extract",
        sheet_name=sheet_name,
        headers=headers,
        rows=_clean_rows(dicts),
        total_rows=len(dicts),
        truncated=len(dicts) > MAX_CACHED_ROWS,
        stats={k: v for k, v in stats.items() if v is not None},
        qc_counts=qc_counts,
        project=project,
        notes=notes,
    )


#: Maps the usage string on a Memfus Wong row to a case category.
#: Categories are derived from the data, not declared, so a new usage
#: string still produces a project — it just lands in the catch-all.
USAGE_CATEGORY = [
    (("office", "commercial", "godown"), "Office / Commercial"),
    (("shop", "retail", "store"), "Shop / Retail"),
    (("industrial", "warehouse", "factory", "godown"), "Industrial"),
    (("car park", "carpark", "parking"), "Industrial"),
    (("residence", "residential", "flat", "house"), "Residential"),
    (("land", "site"), "Land"),
]


def _category_for_usage(usage: str) -> str:
    u = (usage or "").lower()
    for keys, cat in USAGE_CATEGORY:
        if any(k in u for k in keys):
            return cat
    return "Residential"


#: Street and building suffixes, expanded for display only. The compass
#: letters are deliberately absent — a standalone "N" or "S" in an
#: address is far more often a floor or a unit than a direction.
STREET_SUFFIX = {
    "RD": "Road", "ST": "Street", "AVE": "Avenue", "LN": "Lane",
    "PL": "Place", "GD": "Garden", "GDNS": "Gardens", "CT": "Court",
    "BLK": "Block", "BLDG": "Building", "CTR": "Centre",
    "B/F": "Basement", "G/F": "Ground Floor", "1/F": "1st Floor",
}

#: Acronyms that keep their capitals. An explicit list, not a length
#: heuristic — HK street names are romanised Chinese words, so "HANG LOK"
#: and "SUN YIP" are full of short all-caps tokens that are not acronyms.
ACRONYMS = {
    "AIA", "HKT", "HSBC", "KCRC", "MTR", "LHT", "PCCW", "HKPC", "CITIC",
    "CKH", "SHK", "NWS", "SPL", "HKEX", "CWCH", "HKG", "HKRI", "HKTEL",
}

#: Words that stay lowercase in a title-cased address. Deliberately short:
#: "a" and "an" are left out because "Shop A" and "Block B" are unit
#: designations, not articles.
SMALL_WORDS = {"of", "and", "the", "for", "to"}


def _title_word(w: str, first: bool) -> str:
    """Title-case one word without breaking possessives."""
    if not w:
        return w
    low = w.lower()
    if not first and low in SMALL_WORDS:
        return low
    all_caps = w.isupper()
    parts = w.split("'")
    out = []
    for i, p in enumerate(parts):
        if not p:
            out.append(p)
            continue
        if i > 0:
            # The "'s" tail. "KING'S" -> "King's", not "KingS".
            out.append(p.lower() if all_caps else p)
        else:
            out.append(p.capitalize())
    return "'".join(out)


def _pretty_address(addr: str) -> str:
    """Put a street address into the house style.

    The extract gives us "510 KING'S RD, Hong Kong" — all caps and
    abbreviated. The house style is title case with the abbreviation
    spelled out: "510 King's Road, Hong Kong".

    Token-based on purpose. ``ingest.memfus.expand_abbrev`` is a geocoding
    pre-pass and rewrites single letters, which turns "KING'S" into
    "KING' SOUTH". This only expands a suffix when it stands alone as a
    word, so apostrophes and possessives survive.
    """
    if not addr:
        return ""
    out_parts: list[str] = []
    for chunk in addr.split(", "):
        words = chunk.split(" ")
        rebuilt: list[str] = []
        for i, w in enumerate(words):
            bare = w.strip(",.")
            key = bare.upper()
            # Only a standalone token is a suffix.
            if bare and key in STREET_SUFFIX and bare == w.strip(",. ") and len(bare) > 1:
                rebuilt.append(STREET_SUFFIX[key])
                continue
            # A known acronym keeps its capitals: "AIA", "HKT".
            if bare in ACRONYMS and bare == w:
                rebuilt.append(bare)
                continue
            rebuilt.append(_title_word(w, i == 0))
        out_parts.append(" ".join(rebuilt))
    return ", ".join(out_parts)


def _project_from_memfus(subject: Any, evidence: list[Any], sheet_name: str) -> dict[str, Any]:
    """Derive a project (case) from the subject row of a Memfus Wong extract."""
    # A stable slug from the subject's identity, not from the file name, so
    # re-uploading the same extract does not create a second project.
    bits = [
        subject.building or subject.address or "",
        subject.district or "",
    ]
    stem = re.sub(r"[^a-z0-9]+", "-", " ".join(b for b in bits if b).lower()).strip("-")
    slug = f"mw-{stem or 'extract'}"[:60]

    price = int(subject.price_hkd) if subject.price_hkd else 0
    area = int(subject.area_sf) if subject.area_sf else 0
    rate = round(price / area) if price and area else 0

    title = subject.building or subject.address or "Memfus Wong extract"
    return {
        "id": slug,
        "title": _pretty_address(title),
        "category": _category_for_usage(subject.usage),
        "case_ref": subject.mem_no or "",
        "address": _pretty_address(subject.address),
        "address_raw": subject.address,
        "district": subject.district,
        "valuation_date": subject.date.isoformat() if subject.date else "",
        "adopted_value_hkd": price,
        "adopted_rate_psf_n": rate,
        "nfa_sqft": area,
        "usage": subject.usage or "",
        "summary": (
            f"{len(evidence)} comparables from a Memfus Wong extract "
            f"({sheet_name})."
        ),
        "source_sheet": sheet_name,
        "source_format": "memfus-mw",
        "floor": subject.floor,
        "unit": subject.flat,
    }


def _clean_rows(dicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make each row JSON-safe, keeping every row (not just the preview).

    The cached parse is the source of truth for re-deriving comparables,
    so it must hold every row, not the slice the page shows.
    """
    out: list[dict[str, Any]] = []
    for d in dicts[:MAX_CACHED_ROWS]:
        clean: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, datetime):
                clean[k] = v.isoformat()
            elif isinstance(v, date):
                clean[k] = v.isoformat()
            elif isinstance(v, (int, float, str, bool)) or v is None:
                clean[k] = v
            else:
                clean[k] = str(v)
        out.append(clean)
    return out


# ---------------------------------------------------------------------------
# ORO HOS case workbook
# ---------------------------------------------------------------------------

def _detect_oro(headers: list[str]) -> bool:
    norm = {_norm_header(h) for h in headers}
    # The ORO registry is keyed on MEM_NO + the report's own field names.
    return "MEMNO" in norm and ("CASEID" in norm or "VALUATIONDATE" in norm)


def _parse_oro(sheet_name: str, headers: list[str], rows: list[list[Any]]) -> ParsedWorkbook:
    """Best-effort read of an ORO case workbook.

    The full ORO parse lives in ``ingest.oro_ingest`` and wants the
    88-field master registry. Without that registry we still show the
    content, which is the point of this page.
    """
    dicts = _rows_as_dicts(headers, rows)
    notes = [
        "Read as a plain table. Supply the ORO master registry to get the "
        "full field-level parse with cross-document checks."
    ]
    stats = {"rows": len(dicts), "columns": len(headers)}
    return ParsedWorkbook(
        format_id="oro-case",
        format_label="ORO HOS case workbook",
        sheet_name=sheet_name,
        headers=headers,
        rows=_clean_rows(dicts),
        total_rows=len(dicts),
        truncated=len(dicts) > MAX_CACHED_ROWS,
        stats=stats,
        qc_counts={},
        project=None,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Anything else
# ---------------------------------------------------------------------------

def _detect_generic(headers: list[str]) -> bool:
    return bool(headers)


def _parse_generic(sheet_name: str, headers: list[str], rows: list[list[Any]]) -> ParsedWorkbook:
    dicts = _rows_as_dicts(headers, rows)
    return ParsedWorkbook(
        format_id="generic",
        format_label="Unrecognised workbook",
        sheet_name=sheet_name,
        headers=headers,
        rows=_clean_rows(dicts),
        total_rows=len(dicts),
        truncated=len(dicts) > MAX_CACHED_ROWS,
        stats={"rows": len(dicts), "columns": len(headers)},
        qc_counts={},
        project=None,
        notes=[
            "No known format matched, so this is shown as a plain table. "
            "Add a format spec in demo/intake.py to teach the app this shape."
        ],
    )


# Ordered: the first match wins. Detection is cheap and order matters —
# the Memfus Wong signature is more specific than the generic one.
FORMATS: list[FormatSpec] = [
    FormatSpec(
        id="memfus-mw",
        label="Memfus Wong transaction extract",
        detect=_detect_memfus,
        parse=_parse_memfus,
        yields_project=True,
    ),
    FormatSpec(
        id="oro-case",
        label="ORO HOS case workbook",
        detect=_detect_oro,
        parse=_parse_oro,
    ),
    FormatSpec(
        id="generic",
        label="Unrecognised workbook",
        detect=_detect_generic,
        parse=_parse_generic,
    ),
]


# ---------------------------------------------------------------------------
# The pure part: bytes -> ParsedWorkbook
# ---------------------------------------------------------------------------

def detect_format(headers: list[str]) -> FormatSpec:
    for spec in FORMATS:
        try:
            if spec.detect(headers):
                return spec
        except Exception:  # noqa: BLE001
            continue
    return FORMATS[-1]


def _extract_sections(wb: Any, sheet_names: list[str]) -> list[dict[str, Any]]:
    """Every sheet in the workbook, split into recognised blocks."""
    from demo import sections as S

    out: list[dict[str, Any]] = []
    for name in sheet_names:
        try:
            ws = wb[name]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except Exception as e:  # noqa: BLE001
            out.append({
                "id": "table", "label": name, "kind": "table",
                "sheet": name, "header_row": 0, "headers": [], "rows": [],
                "notes": [f"could not read this sheet: {e}"],
                "stats": {}, "qc_counts": {}, "project": None,
                "adjustment_columns": [],
            })
            continue
        for section in S.sections_from_sheet(name, rows):
            out.append(section.to_dict())
    return out


def parse_workbook_bytes(data: bytes) -> ParsedWorkbook:
    """Bytes of an .xlsx/.xlsm -> ParsedWorkbook.

    Never raises: a broken workbook comes back as a ParsedWorkbook with
    an ``error`` set, so one bad file in Drive cannot break the page.
    """
    try:
        wb, sheet_names = _read_workbook(data)
    except Exception as e:  # noqa: BLE001
        return ParsedWorkbook(
            format_id="error",
            format_label="Unreadable file",
            sheet_name="",
            error=f"{type(e).__name__}: {e}",
        )
    try:
        sheet_name, ws = _first_sheet_with_data(wb)
        headers, rows = _sheet_rows(ws)
        spec = detect_format(headers)
        parsed = spec.parse(sheet_name, headers, rows)
        parsed.sheets = sheet_names
        parsed.sections = _extract_sections(wb, sheet_names)
        return parsed
    except Exception as e:  # noqa: BLE001
        return ParsedWorkbook(
            format_id="error",
            format_label="Unreadable file",
            sheet_name=sheet_names[0] if sheet_names else "",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The Drive layer — bytes in, bytes out
# ---------------------------------------------------------------------------

@dataclass
class DriveFile:
    """A workbook sitting in an intake folder."""
    id: str
    name: str
    folder: str
    modified_time: str
    size: int
    web_view_link: str
    mime_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "folder": self.folder,
            "modified_time": self.modified_time,
            "size": self.size,
            "web_view_link": self.web_view_link,
            "mime_type": self.mime_type,
        }


def _drive():
    from demo.oauth import get_drive
    return get_drive()


_FOLDER_LIST_OBJECT = "bma-factory/drive/folder-list.json"
_FOLDER_LIST_TTL = 900          # 15 minutes
_folder_list_cache: list[dict[str, str]] = []
_folder_list_at: float = 0.0
_folder_list_lock = threading.Lock()


def list_drive_folders(force: bool = False) -> list[dict[str, str]]:
    """Every folder in the valuer's Drive, for the folder picker.

    Cached in Cloud Storage for fifteen minutes. The query is a heavy
    one — every folder in a personal Drive — and the list barely moves,
    so re-running it on every page render is pure latency.
    """
    global _folder_list_cache, _folder_list_at
    _load_folder_list_cache()
    now = time.time()
    if not force and _folder_list_cache and now - _folder_list_at < _FOLDER_LIST_TTL:
        return _folder_list_cache

    try:
        drive = _drive()
    except Exception as e:  # noqa: BLE001
        log.info("not signed in, cannot list Drive folders: %s", e)
        return _folder_list_cache

    try:
        # Paginate. Bobby's Drive has more folders than one page holds,
        # and a truncated picker is worse than no picker — it looks
        # complete and is not.
        raw: list[dict[str, Any]] = []
        page_token = None
        for _ in range(20):          # 20 pages x 200 = 4000 folders
            kwargs: dict[str, Any] = {
                "q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
                "fields": "files(id,name,nextPageToken)",
                "pageSize": 200,
                "orderBy": "name",
                "supportsAllDrives": True,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            r = drive.files().list(**kwargs).execute()
            raw.extend(r.get("files", []))
            page_token = r.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:  # noqa: BLE001
        log.warning("cannot list Drive folders: %s", e)
        return _folder_list_cache

    folders = [{"id": f["id"], "name": f["name"]} for f in raw]
    with _folder_list_lock:
        _folder_list_cache = folders
        _folder_list_at = now
    try:
        from demo.oauth import _gcs_write_to
        _gcs_write_to(json.dumps({"at": now, "folders": folders}),
                      _FOLDER_LIST_OBJECT)
    except Exception:  # noqa: BLE001
        pass
    return folders


def _load_folder_list_cache() -> None:
    """Warm the folder list from Cloud Storage on a cold start.

    Locked: the file listing runs this on a worker thread while the
    folder picker may call it on another.
    """
    global _folder_list_cache, _folder_list_at
    with _folder_list_lock:
        if _folder_list_cache:
            return
        try:
            from demo.oauth import _gcs_read_from
            blob = _gcs_read_from(_FOLDER_LIST_OBJECT)
            if not blob:
                return
            data = json.loads(blob)
            at = float(data.get("at", 0) or 0)
            folders = data.get("folders")
            if isinstance(folders, list) and time.time() - at < _FOLDER_LIST_TTL:
                _folder_list_cache = folders
                _folder_list_at = at
                log.info("loaded %d Drive folders from cache", len(folders))
        except Exception as e:  # noqa: BLE001
            log.warning("could not load the Drive folder list cache: %s", e)


def list_drive_workbooks(
    folders: tuple[str, ...] = INTAKE_FOLDERS,
    folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[DriveFile]:
    """List Excel files in the intake folders. Empty list if not signed in.

    ``folder_ids`` points at folders by Drive id, so the valuer can aim
    the app at wherever they actually put the file instead of guessing.
    ``whole_drive`` scans everything, which is the fallback for a file
    that went into the Drive root rather than a named folder.
    """
    from demo.drive import _find_or_create_folder

    try:
        drive = _drive()
    except Exception as e:  # noqa: BLE001
        log.info("not signed in, cannot list Drive: %s", e)
        return []

    _load_folder_list_cache()
    out: list[DriveFile] = []
    targets: list[tuple[str, str]] = []   # (label, folder id)

    if whole_drive:
        targets.append(("My Drive", "root"))
    for folder in folders:
        try:
            from demo.drive import app_folder_if_exists
            fid = app_folder_if_exists(folder)
            if fid is None:
                # Not there yet. Do not create it just because we looked.
                continue
            targets.append((folder, fid))
        except Exception as e:  # noqa: BLE001
            log.warning("cannot resolve folder %s: %s", folder, e)
    for fid in folder_ids:
        targets.append((fid, fid))

    def _list_one(label: str, folder_id: str) -> list[DriveFile]:
        """One folder's Excel files."""
        if folder_id == "root":
            q = "trashed=false and 'root' in parents"
        else:
            q = f"'{folder_id}' in parents and trashed=false"
        try:
            r = drive.files().list(
                q=q,
                fields=(
                    "files(id,name,size,modifiedTime,webViewLink,mimeType)"
                ),
                pageSize=100,
                orderBy="modifiedTime desc",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("cannot list folder %s: %s", label, e)
            return []
        found: list[DriveFile] = []
        for f in r.get("files", []):
            mime = f.get("mimeType", "")
            name = f.get("name", "")
            if mime not in EXCEL_MIME and not name.lower().endswith((".xlsx", ".xlsm", ".xls")):
                continue
            found.append(DriveFile(
                id=f["id"],
                name=name,
                folder=label,
                modified_time=f.get("modifiedTime", ""),
                size=int(f.get("size", 0) or 0),
                web_view_link=f.get("webViewLink", ""),
                mime_type=mime,
            ))
        return found

    # The folders are independent, so list them at the same time rather
    # than one after the other. Each call is a round trip to Google and
    # they add up directly to the page's load time.
    if len(targets) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            for found in pool.map(lambda t: _list_one(t[0], t[1]), targets):
                out.extend(found)
    else:
        for label, folder_id in targets:
            out.extend(_list_one(label, folder_id))
    # Newest first across all folders, not per folder. A re-uploaded
    # extract supersedes the older one for the same subject, and the
    # project derivation relies on that order.
    out.sort(key=lambda f: f.modified_time, reverse=True)
    return out
    # Newest first across all folders, not per folder. A re-uploaded
    # extract supersedes the older one for the same subject, and the
    # project derivation relies on that order.
    out.sort(key=lambda f: f.modified_time, reverse=True)
    return out


def download_workbook(file_id: str) -> bytes:
    """Fetch the bytes of a Drive file."""
    from googleapiclient.http import MediaIoBaseDownload

    drive = _drive()
    import io as _io
    buf = _io.BytesIO()
    request = drive.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Cache — parsed content in Cloud Storage, keyed by file id + mtime
# ---------------------------------------------------------------------------

#: One object holds every parsed workbook. One read per container start
#: instead of one read per file, which is what made the console slow.
_PARSED_OBJECT = "bma-factory/intake/parsed.json"
_parsed_cache: dict[str, Any] = {}
_parsed_loaded = False
_parsed_lock = threading.Lock()


def _parse_key(file_id: str, modified_time: str) -> str:
    stamp = (modified_time or "").replace(":", "").replace("-", "").replace(".", "")
    return f"{file_id}-{stamp}"


def _load_parsed_cache() -> None:
    global _parsed_loaded
    if _parsed_loaded:
        return
    _parsed_loaded = True
    try:
        from demo.oauth import _gcs_read_from
        blob = _gcs_read_from(_PARSED_OBJECT)
        if not blob:
            return
        data = json.loads(blob)
        if isinstance(data, dict):
            with _parsed_lock:
                _parsed_cache.update(data)
            log.info("loaded %d parsed workbooks from cache", len(_parsed_cache))
    except Exception as e:  # noqa: BLE001
        log.warning("could not load the parsed-workbook cache: %s", e)


def _save_parsed_cache() -> None:
    try:
        from demo.oauth import _gcs_write_to
        with _parsed_lock:
            payload = json.dumps(_parsed_cache, ensure_ascii=False)
        _gcs_write_to(payload, _PARSED_OBJECT)
    except Exception as e:  # noqa: BLE001
        log.warning("could not save the parsed-workbook cache: %s", e)


def load_cached(file_id: str, modified_time: str) -> Optional[ParsedWorkbook]:
    """A previously parsed workbook, if the cache has it."""
    _load_parsed_cache()
    key = _parse_key(file_id, modified_time)
    with _parsed_lock:
        blob = _parsed_cache.get(key)
    if not blob:
        return None
    return ParsedWorkbook.from_dict(blob)


def store_cached(file_id: str, modified_time: str, parsed: ParsedWorkbook) -> bool:
    """Cache a parsed workbook so the console does not re-parse on every page view."""
    _load_parsed_cache()
    key = _parse_key(file_id, modified_time)
    with _parsed_lock:
        _parsed_cache[key] = parsed.to_dict()
    _save_parsed_cache()
    return True


def parse_drive_file(f: DriveFile) -> ParsedWorkbook:
    """Cached parse of a Drive workbook."""
    cached = load_cached(f.id, f.modified_time)
    if cached is not None:
        return cached
    try:
        data = download_workbook(f.id)
    except Exception as e:  # noqa: BLE001
        return ParsedWorkbook(
            format_id="error",
            format_label="Unreadable file",
            sheet_name="",
            error=f"{type(e).__name__}: {e}",
        )
    parsed = parse_workbook_bytes(data)
    store_cached(f.id, f.modified_time, parsed)
    return parsed


def find_file(file_id: str, folders: tuple[str, ...] = INTAKE_FOLDERS) -> Optional[DriveFile]:
    for f in list_drive_workbooks(folders):
        if f.id == file_id:
            return f
    return None


# --- the folder the valuer is aiming the app at -----------------------------

#: Cookie holding the extra folders the valuer picked, or "all".
SCAN_COOKIE = "bma_intake_scan"


def parse_scan_setting(value: str | None) -> tuple[tuple[str, ...], bool]:
    """Cookie value -> (extra folder ids, whole drive?).

    ``all`` means scan the whole of Drive rather than named folders.
    """
    if not value:
        return (), False
    v = value.strip()
    if v == "all":
        return (), True
    ids = tuple(x for x in (part.strip() for part in v.split(",")) if x)
    return ids, False


def scan_setting_cookie(folder_ids: tuple[str, ...], whole_drive: bool) -> str:
    return "all" if whole_drive else ",".join(folder_ids)


# ---------------------------------------------------------------------------
# Memfus Wong rows -> the shape the relevancy engine reads
# ---------------------------------------------------------------------------

def comps_to_relevance(comps: list[Any]) -> list[dict[str, Any]]:
    """Shape Memfus Wong comparables for ``ingest.relevance.rank()``.

    Mirrors ``demo.case_data.to_relevance_input`` so both sources feed
    the same scorer. The keys the scorer reads are: building, district,
    area_sf, usage, date. The rest is for display and QC.
    """
    from ingest import memfus

    out: list[dict[str, Any]] = []
    for c in comps:
        nfa = float(c.area_sf) if c.area_sf else 0.0
        price = float(c.price_hkd) if c.price_hkd else 0.0
        psf = round(price / nfa, 2) if nfa and price else 0.0
        out.append({
            "src_row": c.src_row,
            "name": _pretty_address(memfus.display_name(c.row)),
            "address": _pretty_address(c.address),
            "address_raw": c.address,
            "building": c.building,
            "district": c.district,
            "floor": c.floor,
            "unit": c.flat,
            "nfa_sqft": nfa,
            "gfa_sqft": 0.0,
            "area_sf": nfa,
            "price_hkd": price,
            "psf_gross": psf,
            "psf_net": psf,
            "inst_date": c.date.isoformat() if c.date else "",
            "date": c.date.isoformat() if c.date else "",
            "nat_inst": str(c.row.get("NAT_INST") or ""),
            "op_date": str(c.row.get("OP_DATE") or ""),
            "usage": c.usage,
            "grade": "",
            "qc_flags": [f.code for f in c.flags],
        })
    return out


def subject_to_relevance(subject: Any) -> dict[str, Any]:
    """The subject row in the shape the relevancy engine reads."""
    nfa = float(subject.area_sf) if subject.area_sf else 0.0
    return {
        "name": subject.building,
        "building": subject.building,
        "address": _pretty_address(subject.address),
        "nfa_sqft": nfa,
        "area_sf": nfa,
        "usage": subject.usage,
        "case_ref": subject.mem_no,
        "district": subject.district,
    }


def build_case_data(parsed: ParsedWorkbook) -> Optional[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """(subject, comps) for the relevancy engine, from a parsed workbook.

    Prefers the section holding the comparables with their adjustment
    ladder — Bobby's own working evidence — and falls back to the raw
    Memfus Wong extract.

    None when the workbook carries no subject row.
    """
    from ingest import memfus

    # Which block holds the comparables, and which is the raw extract.
    comps_section = None
    extract_section = None
    for s in parsed.sections:
        if s.get("kind") == "comps" and comps_section is None:
            comps_section = s
        elif s.get("kind") == "extract" and extract_section is None:
            extract_section = s
    if comps_section is None and extract_section is None:
        if parsed.format_id != "memfus-mw" or not parsed.project:
            return None
        comps_section = {
            "headers": parsed.headers,
            "rows": [[d.get(h) for h in parsed.headers] for d in parsed.rows],
            "header_row": 1,
        }
    source = comps_section or extract_section or {}

    headers = source.get("headers") or []
    rows = source.get("rows") or []
    if not headers:
        return None
    dicts = _rows_as_dicts(headers, rows)
    comps = memfus.from_rows(dicts, first_row=(source.get("header_row", 1) or 1) + 1)
    subject, evidence = memfus.split_subject(comps)
    if subject is None:
        return None
    subject_dict = subject_to_relevance(subject)
    subject_dict.update({
        "valuer": "BMA",
        "valuation_date": subject.date.isoformat() if subject.date else "",
        "adopted_value_hkd": int(subject.price_hkd) if subject.price_hkd else 0,
        "adopted_rate_psf_n": (
            round(subject.price_hkd / subject.area_sf)
            if subject.price_hkd and subject.area_sf else 0
        ),
        "purpose": "Memfus Wong extract",
        "lot": str(subject.row.get("LOT_NO1") or ""),
    })
    return subject_dict, comps_to_relevance(evidence)
