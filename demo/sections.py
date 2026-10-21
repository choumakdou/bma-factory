"""Section detection — what a workbook actually contains.

A workbook in the intake folder is not always one flat table. Bobby's
working files are a stack of sections: the subject record at the top,
the comparables with his adjustment ladder beside them, and the raw
Memfus Wong extract they were pulled from. Showing that as one long
table loses the structure he put there.

So a workbook is parsed into **sections** — blocks of a sheet that have
their own header row — and each section is recognised by the shape of
its header.

The registry mirrors the one in ``demo.intake``: a list of specs, each
with a ``detect`` function, so a new section type is a new entry rather
than another branch.

Section types, in the order they are tried:

  subject      the property being valued
  adopted-comps comparables with the adjustment ladder beside them
  mw-extract   the raw Memfus Wong transaction extract
  table        anything else

``adopted-comps`` is tried before ``mw-extract`` on purpose: a comps
sheet carries the Memfus Wong columns *and* the adjustment columns, and
the adjustment columns are the part worth showing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# The Memfus Wong column set — the baseline for "is this an extract?"
# ---------------------------------------------------------------------------

def _mw_columns() -> set[str]:
    """The Memfus Wong column set, normalised the same way as a header.

    Normalisation has to match: comparing ``"MEMNO"`` against
    ``"MEM_NO"`` finds no overlap, and then every column looks like an
    extra column and the extract is mistaken for an adjustments table.
    """
    from ingest.memfus import COLUMNS
    return {_norm(c) for c in COLUMNS}


#: Columns that belong to the adjustment group, whatever they are called.
#: "ADOPTED" and "WEIGHTED" are in here because the adopted output and
#: the weighted average are the end of the ladder, and in a comps sheet
#: they belong with it.
ADJUSTMENT_HINTS = (
    "ADJ", "ADJUST", "%", "FACTOR", "MULTIPLIER", "LADDER", "INDEX",
    "TIME ADJ", "FLOOR ADJ", "LOC ADJ", "SW ADJ", "QA ADJ", "AGE ADJ",
    "ADOPTED", "WEIGHTED",
)

#: Headers that name a property attribute — used to spot a subject block.
SUBJECT_HINTS = (
    "SUBJECT", "PROPERTY", "ADDRESS", "PREMISES", "LOT", "DISTRICT",
    "VALUATION DATE", "EFFECTIVE", "GROSS", "NET", "VALUER", "CLIENT",
    "CERTIFICATE", "MEM_NO", "MEM NO",
)


def _norm(v: Any) -> str:
    if v is None:
        return ""
    return re.sub(r"[\s_\-]+", "", str(v)).upper()


def _is_numeric(v: Any) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if isinstance(v, (datetime, date)):
        return True
    s = str(v).strip()
    if not s:
        return False
    s = s.replace(",", "").replace("%", "").replace("$", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    try:
        float(s)
        return True
    except ValueError:
        return False


def is_adjustment_column(header: str) -> bool:
    """Does this column carry an adjustment rather than raw data?"""
    h = str(header or "").upper()
    if not h:
        return False
    return any(hint in h for hint in ADJUSTMENT_HINTS)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

@dataclass
class Section:
    """One recognisable block of a workbook."""
    id: str
    label: str
    kind: str                       # subject | comps | extract | table
    sheet: str
    header_row: int
    #: The last row of the block, as a spreadsheet row number.
    end_row: int = 0
    headers: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    qc_counts: dict[str, int] = field(default_factory=dict)
    project: Optional[dict[str, Any]] = None
    #: Which of the valuer's rules claimed this block, if any.
    rule_id: str = ""
    rule_origin: str = ""

    @property
    def adjustment_columns(self) -> list[str]:
        """Headers of the columns that carry an adjustment."""
        return [h for h in self.headers if is_adjustment_column(h)]

    def as_dicts(self) -> list[dict[str, Any]]:
        out = []
        for r in self.rows:
            out.append({
                (self.headers[i] if i < len(self.headers) and self.headers[i] else f"COL{i+1}"): r[i] if i < len(r) else None
                for i in range(len(self.headers))
            })
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "sheet": self.sheet,
            "header_row": self.header_row,
            "end_row": self.end_row,
            "headers": self.headers,
            "rows": _jsonable_rows(self.rows),
            "notes": self.notes,
            "stats": self.stats,
            "qc_counts": self.qc_counts,
            "project": self.project,
            "adjustment_columns": self.adjustment_columns,
            "rule_id": self.rule_id,
            "rule_origin": self.rule_origin,
        }


def _jsonable_rows(rows: list[list[Any]]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for r in rows:
        row: list[Any] = []
        for v in r:
            if isinstance(v, datetime):
                row.append(v.isoformat())
            elif isinstance(v, date):
                row.append(v.isoformat())
            elif isinstance(v, (int, float, str, bool)) or v is None:
                row.append(v)
            else:
                row.append(str(v))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Splitting a sheet into blocks
# ---------------------------------------------------------------------------

def _is_header_row(
    row: list[Any],
    next_row: Optional[list[Any]],
    prev_row: Optional[list[Any]] = None,
) -> bool:
    """Is this row a header for the block below it?

    A header row has several non-empty cells, is mostly text, and does
    not repeat a value — data rows repeat values all the time, headers
    almost never do.

    It must also *start* a block: the row above it is blank, or it is
    the first row of the sheet. Without that, a data row of mostly-text
    values ("Tower A", "Tower A", "5") looks exactly like a header, and
    the sheet gets chopped in the wrong place.
    """
    if prev_row is not None:
        if any(c is not None and str(c).strip() != "" for c in prev_row):
            return False        # this row follows data, so it is data
    cells = [c for c in row if c is not None and str(c).strip() != ""]
    if len(cells) < 2:
        return False
    texty = sum(1 for c in cells if not _is_numeric(c))
    if texty / len(cells) < 0.6:
        return False
    seen = {_norm(c) for c in cells}
    if len(seen) < len(cells):
        return False        # repeated value — this is data
    if next_row is None:
        return False
    nxt = [c for c in next_row if c is not None and str(c).strip() != ""]
    return len(nxt) >= 1


def split_sheet(sheet_name: str, rows: list[list[Any]]) -> list[Section]:
    """Split a sheet into blocks, each with its own header row."""
    sections: list[Section] = []
    i = 0
    n = len(rows)

    def _is_header(idx: int) -> bool:
        row = rows[idx]
        nxt = rows[idx + 1] if idx + 1 < n else None
        prev = rows[idx - 1] if idx > 0 else None
        return _is_header_row(row, nxt, prev)

    while i < n:
        if not _is_header(i):
            i += 1
            continue
        headers = ["" if c is None else str(c).strip() for c in rows[i]]
        # The block runs until the next header row.
        j = i + 1
        body: list[list[Any]] = []
        while j < n:
            if _is_header(j):
                break
            if any(c is not None and str(c).strip() != "" for c in rows[j]):
                body.append(list(rows[j]))
            j += 1
        sections.append(Section(
            id="unknown",
            label="",
            kind="table",
            sheet=sheet_name,
            header_row=i + 1,
            end_row=max(i + 1, j),
            headers=headers,
            rows=body,
        ))
        i = j
    return sections


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

@dataclass
class SectionSpec:
    id: str
    label: str
    kind: str
    detect: Callable[[list[str]], bool]
    #: Applied after detection — fills in stats, flags and the project.
    enrich: Optional[Callable[[Section], None]] = None


#: Maps a usage string to a case category. Same table as the intake
#: module — one subject, one classification.
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


def _detect_adopted_comps(headers: list[str]) -> bool:
    """Comparables with an adjustment ladder beside them.

    Either it says so in the header names, or it is a Memfus Wong
    extract with extra columns beyond the extract's own — which is what
    Bobby's grid looks like: the extract verbatim in A–AA, the
    adjustment ladder from AB.
    """
    norm = {_norm(h) for h in headers}
    mw = _mw_columns()
    if not mw:
        return False
    has_mw = len(mw & norm) >= 5
    if not has_mw:
        return False
    named = any(is_adjustment_column(h) for h in headers)
    extra = len([h for h in headers if h.strip() and _norm(h) not in mw])
    return named or extra >= 2


def _detect_subject(headers: list[str]) -> bool:
    """A property record rather than a table of transactions."""
    norm = {_norm(h) for h in headers}
    if "MEMNO" in norm and ("INSTDATE" in norm or "CONSIDERTN" in norm):
        return False        # that is the extract, not a subject block
    hits = sum(1 for h in headers if any(k in _norm(h) for k in
                                         ("SUBJECT", "PROPERTY", "ADDRESS",
                                          "LOT", "DISTRICT", "VALUER",
                                          "VALUATIONDATE", "PREMISES")))
    return hits >= 2


def _detect_mw_extract(headers: list[str]) -> bool:
    norm = {_norm(h) for h in headers}
    return "MEMNO" in norm and "INSTDATE" in norm


def _detect_table(headers: list[str]) -> bool:
    return bool([h for h in headers if h.strip()])


# --- enrichment ------------------------------------------------------------

def _enrich_subject(section: Section) -> None:
    """Pull the subject's key facts out of a property record block."""
    rows = section.as_dicts()
    if not rows:
        return
    # A subject block is usually one record, read down the columns.
    record = {h: rows[0].get(h) for h in section.headers if h.strip()}
    section.stats = {
        "fields": len([h for h in section.headers if h.strip()]),
        "records": len(rows),
    }
    keys = {h: v for h, v in record.items() if v not in (None, "")}
    building = _first_str(keys, ("SUBJECT", "PROPERTY", "BUILDING", "PREMISES", "ADDRESS", "NAME"))
    district = _first_str(keys, ("DISTRICT", "D_CODE", "AREA", "LOCATION"))
    section.project = {
        # The same canonical slug the extract path uses, so the same
        # property is one project, not two.
        "id": project_slug(building, district),
        "title": building,
        "address": _first_str(keys, ("ADDRESS", "FULL ADDRESS", "PREMISES", "LOCATION")),
        "district": district,
        "valuation_date": _first_str(keys, ("VALUATION DATE", "VALUATION_DATE", "EFFECTIVE DATE", "DATE")),
        "adopted_value_hkd": _first_num(keys, ("ADOPTED VALUE", "ADOPTED_VALUE", "VALUE", "CONSIDERATION", "PRICE", "CAPITAL VALUE")),
        "nfa_sqft": _first_num(keys, ("NFA", "NET AREA", "NET FLOOR AREA", "NET AREA (SF)", "AREA (SF)")),
        "usage": _first_str(keys, ("USAGE", "PROPERTY TYPE", "TENURE USE", "LAND USE")),
        "category": _category_for_usage(_first_str(keys, ("USAGE", "PROPERTY TYPE", "LAND USE"))),
    }


def _enrich_comps(section: Section) -> None:
    """Statistics for a comparables table, and the subject row if present."""
    from ingest import memfus

    dicts = section.as_dicts()
    comps = memfus.from_rows(dicts, first_row=section.header_row + 1)
    subject, evidence = memfus.split_subject(comps)
    qc: dict[str, int] = {}
    for c in comps:
        for f in c.flags:
            qc[f.code] = qc.get(f.code, 0) + 1
    section.qc_counts = qc
    dates = [c.date for c in comps if c.date]
    section.stats = {
        "comps": len(evidence),
        "subject_found": subject is not None,
        "adjustment_columns": section.adjustment_columns,
        "date_min": min(dates).isoformat() if dates else None,
        "date_max": max(dates).isoformat() if dates else None,
    }
    if subject is not None:
        section.project = project_from_subject(
            subject, category=_category_for_usage(subject.usage)
        )


def _enrich_extract(section: Section) -> None:
    _enrich_comps(section)


def _enrich_table(section: Section) -> None:
    section.stats = {
        "rows": len(section.rows),
        "columns": len([h for h in section.headers if h.strip()]),
    }


def project_slug(building: str, district: str = "") -> str:
    """The canonical project id for a subject.

    One derivation, used everywhere. The same subject must not become
    two projects because one path read it from the extract and another
    read it from a section.
    """
    bits = [b for b in (building or "", district or "") if b]
    stem = re.sub(r"[^a-z0-9]+", "-", " ".join(bits).lower()).strip("-")
    return f"mw-{stem or 'extract'}"[:60]


def project_from_subject(
    subject: Any,
    category: str = "Residential",
    source: str = "memfus-mw",
) -> dict[str, Any]:
    """The canonical project dict for a Memfus Wong subject row."""
    price = int(subject.price_hkd) if subject.price_hkd else 0
    area = int(subject.area_sf) if subject.area_sf else 0
    rate = round(price / area) if price and area else 0
    return {
        "id": project_slug(subject.building, subject.district),
        "title": subject.building or subject.address or "Memfus Wong extract",
        "category": category,
        "case_ref": subject.mem_no or "",
        "address": subject.address,
        "address_raw": getattr(subject, "address", ""),
        "district": subject.district,
        "valuation_date": subject.date.isoformat() if subject.date else "",
        "adopted_value_hkd": price,
        "adopted_rate_psf_n": rate,
        "nfa_sqft": area,
        "usage": subject.usage or "",
        "summary": "",
        "source_sheet": "",
        "source_format": source,
        "floor": subject.floor,
        "unit": subject.flat,
    }


def _slug_from_record(keys: dict[str, Any]) -> str:
    bits = []
    for k in keys:
        if any(t in _norm(k) for t in ("ADDRESS", "BUILDING", "PREMISES", "SUBJECT", "PROPERTY", "LOT")):
            v = str(keys[k])
            if v:
                bits.append(v)
            break
    stem = re.sub(r"[^a-z0-9]+", "-", " ".join(bits).lower()).strip("-")
    return stem[:50]


def _first_str(keys: dict[str, Any], names: tuple[str, ...]) -> str:
    for want in names:
        for k, v in keys.items():
            if _norm(k) == _norm(want):
                return str(v)
    for want in names:
        for k, v in keys.items():
            if _norm(want) in _norm(k):
                return str(v)
    return ""


def _first_num(keys: dict[str, Any], names: tuple[str, ...]) -> int:
    raw = _first_str(keys, names)
    if not raw:
        return 0
    s = str(raw).replace(",", "").replace("$", "").replace("HK$", "").strip()
    mult = 1
    if s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    elif s.endswith("K"):
        mult, s = 1_000, s[:-1]
    s = s.replace("%", "").strip()
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


SECTION_SPECS: list[SectionSpec] = [
    SectionSpec(
        id="adopted-comps",
        label="Adopted comparables, with adjustments",
        kind="comps",
        detect=_detect_adopted_comps,
        enrich=_enrich_comps,
    ),
    SectionSpec(
        id="subject",
        label="Subject",
        kind="subject",
        detect=_detect_subject,
        enrich=_enrich_subject,
    ),
    SectionSpec(
        id="mw-extract",
        label="Memfus Wong data extraction",
        kind="extract",
        detect=_detect_mw_extract,
        enrich=_enrich_extract,
    ),
    SectionSpec(
        id="table",
        label="Table",
        kind="table",
        detect=_detect_table,
        enrich=_enrich_table,
    ),
]


def detect_section(headers: list[str]) -> SectionSpec:
    for spec in SECTION_SPECS:
        try:
            if spec.detect(headers):
                return spec
        except Exception:  # noqa: BLE001
            continue
    return SECTION_SPECS[-1]


def classify(sections: list[Section], rules: Optional[list[Any]] = None) -> list[Section]:
    """Give every block a type, a label and its stats.

    The valuer's own rules are tried first — a rule that matches wins
    outright. The built-in detection is the fallback, not the authority.
    """
    if rules:
        from demo import rules as R
        return R.apply_rules(sections, rules)
    for section in sections:
        spec = detect_section(section.headers)
        section.id = spec.id
        section.label = spec.label
        section.kind = spec.kind
        if spec.enrich is not None:
            try:
                spec.enrich(section)
            except Exception as e:  # noqa: BLE001
                section.notes.append(f"could not read this section fully: {e}")
    return sections


def sections_from_sheet(
    sheet_name: str,
    rows: list[list[Any]],
    rules: Optional[list[Any]] = None,
) -> list[Section]:
    """Blocks of one sheet, classified."""
    return classify(split_sheet(sheet_name, rows), rules)
