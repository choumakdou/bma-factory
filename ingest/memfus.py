"""Memfus Wong transaction extract — schema, QC gates, and address assembly.

The extract's column set is fixed and already present in the CHFT Map workbook
(`Sheet1` = selected comps, `Sheet2` = the pool, both 27 columns A..AA). This
module maps that schema into the comparables the relevancy scorer consumes,
and applies the QC gates the data itself demands.

Four traps in this schema, each of which produces a number that looks fine:

* `CONSIDERTN` is in **millions**. 3.58 means HK$3,580,000. Reading it as
  dollars understates every unit rate by a factor of a million.
* `NAT_INST = "news (unverified)"` marks a press-sourced price, not a Land
  Registry one. His own rows carry the note "transacted price not sure".
* `REMARK2 = "Connected Transaction"` marks a related-party deal. Not arm's
  length, so not evidence of market value without adjustment or exclusion.
* `TOTGFA = 0` (basement) makes `GROSS_PSF` meaningless, and `TOTNFA = 0`
  makes a unit rate impossible.

None of these raise. They just produce a plausible number, which is the failure
mode worth engineering against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

# --- the extract's columns --------------------------------------------------

COLUMNS = [
    "MEM_NO", "NAT_INST", "INST_DATE", "PUSAGE", "CP_PT", "UNIT", "FLOOR",
    "BLOCK", "PHASE", "BLD_E_NAME", "BLD_C_NAME", "EST_E_NAME", "EST_C_NAME",
    "ST_E_NAME", "ST_C_NAME", "ST_NO_FRM", "ST_NO_TO", "LOT_NO1", "D_CODE",
    "OP_DATE", "CONSIDERTN", "TOTGFA", "TOTNFA", "GROSS_PSF", "NET_PSF",
    "REMARK", "REMARK2",
]

# the workbook marks the subject by writing these into the transaction fields
SUBJECT_MARKERS = {"subject", "s", "subject property"}

PRESS_SOURCED = re.compile(r"news|unverified|press|信報|報導", re.IGNORECASE)
CONNECTED = re.compile(r"connected transaction|connected|related party|關聯", re.IGNORECASE)
APPROX = re.compile(r"approx|approximate|estimated|unconfirmed|待confirm", re.IGNORECASE)

# Nominatim/Photon do better with spelled-out streets; the tool's own review
# sheet asks for abbreviations to be fixed by hand for exactly this reason.
ABBREV = {
    "RD": "ROAD", "ST": "STREET", "AVE": "AVENUE", "LN": "LANE",
    "PL": "PLACE", "GD": "GARDEN", "GDNS": "GARDENS", "CT": "COURT",
    "BLK": "BLOCK", "BLDG": "BUILDING", "CTR": "CENTRE", "B/F": "BASEMENT",
    "G/F": "GROUND FLOOR", "1/F": "1ST FLOOR", "1ST": "1ST",
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
    "HKT": "", "HK": "",
}


# --- coercion ---------------------------------------------------------------

def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return None if v != v else float(v)   # NaN guard
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").replace("HK$", "").strip()
    if not s or s in ("-", "—", "N/A", "n/a"):
        return None
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
                "%b %Y", "%B %Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def consideration_hkd(v: Any) -> float | None:
    """CONSIDERTN is in millions. This is the single most dangerous field."""
    n = _num(v)
    return n * 1_000_000.0 if n is not None else None


def expand_abbrev(addr: str) -> str:
    """A best-effort pre-pass. The workbook still asks a human to fix the
    yellow column; this just reduces how often they have to."""
    if not addr:
        return ""
    out = addr
    for k, v in sorted(ABBREV.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"(?<![A-Z]){re.escape(k)}(?![A-Z])", v, out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip(" ,").strip()


# --- QC ---------------------------------------------------------------------

@dataclass
class Flag:
    level: str          # "error" | "warning" | "info"
    code: str
    message: str


def qc_flags(row: dict) -> list[Flag]:
    out: list[Flag] = []
    nat = str(row.get("NAT_INST") or "")
    rem2 = f"{row.get('REMARK') or ''} {row.get('REMARK2') or ''}"
    tnf, tgf = _num(row.get("TOTNFA")), _num(row.get("TOTGFA"))
    cons = consideration_hkd(row.get("CONSIDERTN"))
    inst = _date(row.get("INST_DATE"))

    if PRESS_SOURCED.search(nat) or PRESS_SOURCED.search(rem2):
        out.append(Flag("error", "press_sourced",
                        f"NAT_INST={nat!r} — price is from a press report, not the Land Registry; "
                        "not evidence of market value without independent verification"))
    if CONNECTED.search(rem2):
        out.append(Flag("error", "connected_transaction",
                        "REMARK flags a connected/related-party transaction — not arm's length, "
                        "so not market value evidence without adjustment or exclusion"))
    if cons is None:
        out.append(Flag("error", "no_consideration", "CONSIDERTN is missing or unreadable"))
    if tnf is None or tnf <= 0:
        out.append(Flag("error", "no_net_area",
                        "TOTNFA is zero or missing — no unit rate can be computed"))
    if tgf is None or tgf <= 0:
        out.append(Flag("info", "no_gross_area",
                        "TOTGFA is zero (basement or car park) — GROSS_PSF is unusable, "
                        "compare on net area only"))
    if APPROX.search(rem2):
        out.append(Flag("warning", "approximate",
                        f"remarks flag the figure as approximate: {rem2.strip()[:60]!r}"))
    if inst is None:
        out.append(Flag("warning", "no_date", "INST_DATE unreadable — recency cannot be scored"))
    return out


# --- row → comparable -------------------------------------------------------

@dataclass
class Comp:
    row: dict
    address: str
    building: str
    district: str
    usage: str
    area_sf: float | None
    price_hkd: float | None
    date: date | None
    floor: str
    flat: str
    mem_no: str
    src_row: int
    is_subject: bool = False
    flags: list[Flag] = field(default_factory=list)

    @property
    def rate_net(self) -> float | None:
        if self.area_sf and self.price_hkd:
            return self.price_hkd / self.area_sf
        return None

    @property
    def usable(self) -> bool:
        return not any(f.level == "error" for f in self.flags)

    def to_scoring_dict(self) -> dict:
        return {
            "address": self.address,
            "building": self.building,
            "district": self.district,
            "usage": self.usage,
            "area_sf": self.area_sf,
            "date": self.date.isoformat() if self.date else None,
            "floor": self.floor,
            "flat": self.flat,
            "mem_no": self.mem_no,
        }


def _street_number(row: dict) -> str:
    frm, to = str(row.get("ST_NO_FRM") or "").strip(), str(row.get("ST_NO_TO") or "").strip()
    if frm and to and frm != to:
        return f"{frm}-{to}"
    return frm or to


def build_address(row: dict) -> str:
    """The geocode query. Street number + street, then city — the shape
    Nominatim resolves best, and the same shape the review sheet asks for."""
    parts = []
    n = _street_number(row)
    if n:
        parts.append(n)
    street = str(row.get("ST_E_NAME") or "").strip()
    if street:
        parts.append(street)
    if not parts:
        parts.append(str(row.get("BLD_E_NAME") or "").strip())
    return ", ".join(p for p in parts if p) + ", Hong Kong"


def display_name(row: dict) -> str:
    return str(row.get("BLD_E_NAME") or row.get("EST_E_NAME") or
               row.get("ST_E_NAME") or row.get("MEM_NO") or "?").strip()


def from_row(row: dict, src_row: int) -> Comp:
    mem = str(row.get("MEM_NO") or "").strip()
    nat = str(row.get("NAT_INST") or "").strip()
    is_subj = mem.lower() in SUBJECT_MARKERS or nat.lower() in SUBJECT_MARKERS
    c = Comp(
        row=row,
        address=build_address(row),
        building=str(row.get("BLD_E_NAME") or "").strip(),
        district=str(row.get("D_CODE") or "").strip(),
        usage=str(row.get("PUSAGE") or "").strip(),
        area_sf=_num(row.get("TOTNFA")),
        price_hkd=consideration_hkd(row.get("CONSIDERTN")),
        date=_date(row.get("INST_DATE")),
        floor=str(row.get("FLOOR") or "").strip(),
        flat=str(row.get("UNIT") or "").strip(),
        mem_no=mem,
        src_row=src_row,
        is_subject=is_subj,
    )
    if not is_subj:
        c.flags = qc_flags(row)
    return c


def from_rows(rows: Iterable[dict], first_row: int = 2) -> list[Comp]:
    return [from_row(r, i) for i, r in enumerate(rows, start=first_row)]


def split_subject(comps: Sequence[Comp]) -> tuple[Comp | None, list[Comp]]:
    subs = [c for c in comps if c.is_subject]
    rest = [c for c in comps if not c.is_subject]
    # last subject row wins, matching the workbook's own convention
    return (subs[-1] if subs else None), rest


# --- txnText ----------------------------------------------------------------

def txn_text(c: Comp, label: str) -> str:
    """The pipe-delimited blob the generated HTML already parses."""
    return "|".join([
        label,
        c.flat or str(c.row.get("CP_PT") or ""),
        str(c.row.get("NAT_INST") or ""),
        c.date.strftime("%-d/%-m/%Y") if c.date else "",
        f"HK${c.price_hkd / 1_000_000:g}mil" if c.price_hkd else "",
        f"{c.area_sf:,.0f}sf" if c.area_sf else "",
        str(c.row.get("CP_PT") or ""),
    ])


def price_label(v: float | None) -> str:
    if not v:
        return ""
    return f"HK${v:,.0f}/sqft"


def price_range_label(vals: Sequence[float | None]) -> str:
    vals = [v for v in vals if v]
    if not vals:
        return "N/A"
    lo, hi = min(vals), max(vals)
    if abs(hi - lo) < 1:
        return price_label(lo)
    return f"{price_label(lo)} – {price_label(hi)}"


def suggest_geocode(c: Comp) -> tuple[str, bool]:
    """A suggested geocode query for the review sheet, and whether it changed.

    Deliberately a *suggestion*, not an automatic rewrite. The workbook already
    asks a human to review the yellow column, and abbreviation expansion gets
    it wrong on anything that merely looks like a direction or a unit — which
    is exactly the case where a silently wrong address is worst. This reduces
    the reviewer's work; it does not replace the review.
    """
    raw = build_address(c.row)
    fixed = expand_abbrev(raw)
    return fixed, fixed != raw


# --- candidate filtering ----------------------------------------------------
# Ranking a full market extract is meaningless: the paid source returns every
# transaction in the query, so a 194-row pool spanning Mong Kok street shops
# and a 13,800 sq ft office will always rank *something* in the top five. The
# filters below cut the pool to plausible evidence BEFORE relevancy is scored,
# and each one records what it removed so the cut is auditable.

@dataclass
class FilterResult:
    kept: list[Comp]
    removed: list[tuple[Comp, str]]


def filter_candidates(
    comps: Sequence[Comp],
    subject: Comp,
    same_district_only: bool = True,
    area_band: tuple[float, float] = (0.25, 4.0),
    max_distance_m: float | None = None,
    subject_coords: tuple[float, float] | None = None,
) -> FilterResult:
    """Cut the pool to plausible comparables, recording every exclusion.

    The band is deliberately wide. This is a *relevance* screen, not a
    comparability ruling — a 3x size difference may still be the best evidence
    available, and saying so is useful. It is not the place to decide
    comparability; that is the adjustment ladder's job.
    """
    kept: list[Comp] = []
    removed: list[tuple[Comp, str]] = []
    s_dist = (subject.district or "").strip().upper()
    s_area = subject.area_sf or 0.0

    for c in comps:
        if not c.usable:
            removed.append((c, f"qc:{c.flags[0].code}"))
            continue
        if same_district_only and s_dist:
            cd = (c.district or "").strip().upper()
            if cd and cd != s_dist:
                removed.append((c, f"district {cd or '—'} ≠ {s_dist}"))
                continue
        if s_area and c.area_sf:
            ratio = c.area_sf / s_area
            if not (area_band[0] <= ratio <= area_band[1]):
                removed.append((c, f"area {c.area_sf:,.0f}sf is {ratio:.2f}× the subject"))
                continue
        if max_distance_m and subject_coords and (c.row.get("_lat") and c.row.get("_lon")):
            from .relevance import haversine_m
            d = haversine_m(subject_coords[0], subject_coords[1],
                            float(c.row["_lat"]), float(c.row["_lon"]))
            if d > max_distance_m:
                removed.append((c, f"{d:,.0f}m away"))
                continue
        kept.append(c)
    return FilterResult(kept, removed)
