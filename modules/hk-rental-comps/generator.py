"""HK rental comps + Term & Reversion workbook generator.

Produces a live-formula .xlsx -- not values pasted into cells. Every derived
number stays auditable in Excel: change the adopted rate and the MR moves; change
a comparable and the adjusted price moves.

Formula set follows the CHFT rental workback convention:
    effective SA       = saleable SA + platform x weighting
    unit price $/sqm   = ROUND(rent / effective SA, 0)
    unit price $/sqft  = ROUND(unit price / 10.76391, 0)
    time adjustment    = (subject index / comparable index) - 1
    total adjustment   = SUM(time : location)
    adjusted price     = ROUND(rent / effective SA x (1 + total), 0)
    YP (annuity)       = ROUND((1 - (1+r)^-n) / r, 4)
    YP (perpetuity)    = ROUND(1 / r, 4)
    PV reversion       = ROUND(ROUND(reversion x YP perp, 0) x PV factor, 0)

Colour convention: blue font = input, black = formula, green = cross-sheet
reference, yellow fill = headline, pink fill = sub-header, blue fill = banner.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core.ledger import pin_document_properties  # noqa: E402
from app.core.schemas import (  # noqa: E402
    Field,
    Issue,
    Schema,
    Severity,
)
from app.core.schemas import validate as validate_fields  # noqa: E402

SQM_PER_SQFT = 10.76391

# --- style constants --------------------------------------------------------
INPUT_FONT = Font(name="Calibri", size=10, color="0000FF")
FORMULA_FONT = Font(name="Calibri", size=10, color="000000")
XREF_FONT = Font(name="Calibri", size=10, color="008000")
HEAD_FONT = Font(name="Calibri", size=10, bold=True, color="000000")
HEADLINE_FONT = Font(name="Calibri", size=10, bold=True, color="000000")
TITLE_FONT = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
YELLOW = PatternFill("solid", fgColor="FFFF99")
PINK = PatternFill("solid", fgColor="FCE4D6")
BANNER = PatternFill("solid", fgColor="D9E1F2")
THIN = Side(style="thin", color="B0B0B0")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

COMPS_HEADERS = [
    "#", "Address", "Year", "Date", "Rent (HK$)", "SA (sqm)",
    "Unit Px $/sqm", "Unit Px $/sqft", "Floor", "Aspect",
    "AC Platform", "AC %", "Effective SA", "Px Index",
    "Time %", "Floor %", "QA %", "SW %", "Age & Qual %", "Location %",
    "Total %", "Adjusted $/sqm", "Unrounded", "Remarks",
]
# column letters for the fields we reference from other sheets
C = {name: get_column_letter(i + 1) for i, name in enumerate(COMPS_HEADERS)}


# --- comp row schema --------------------------------------------------------

COMP_FIELDS = [
    Field("address", "Address", "str", section="Comparable"),
    Field("rent", "Rent (HK$)", "currency", section="Comparable", minimum=0),
    Field("sa", "Saleable area (sqm)", "float", section="Comparable", minimum=1),
    Field("price_index", "Price index", "float", section="Comparable", minimum=1),
    Field("transaction_date", "Transaction date", "date", required=False, section="Comparable"),
    Field("year", "Year", "int", required=False, section="Comparable"),
    Field("floor", "Floor", "str", required=False, section="Comparable"),
    Field("aspect", "Aspect", "str", required=False, section="Comparable"),
    Field("ac_platform", "Ancillary platform (sqm)", "float", required=False, section="Comparable"),
    Field("ac_pct", "Platform weighting", "pct", required=False, section="Comparable"),
    Field("adj_floor", "Floor adjustment", "pct", required=False, section="Comparable"),
    Field("adj_qa", "Quality adjustment", "pct", required=False, section="Comparable"),
    Field("adj_sw", "Sun / weather / view adjustment", "pct", required=False, section="Comparable"),
    Field("adj_age", "Age & quality adjustment", "pct", required=False, section="Comparable"),
    Field("adj_location", "Location adjustment", "pct", required=False, section="Comparable"),
    Field("remarks", "Remarks", "str", required=False, section="Comparable"),
]

COMP_SCHEMA = Schema(fields=COMP_FIELDS, name="comparable")


def validate(values: dict[str, Any]) -> list[Issue]:
    """Module-level business rules.

    The framework has already type-checked the scalars; this checks the things
    only a valuer would know -- most importantly whether the comparables are
    actually comparable with the subject.
    """
    issues: list[Issue] = []
    comps = values.pop("comps", None) or []

    if not comps:
        issues.append(Issue("comps", "at least one comparable is required", Severity.ERROR))
        return issues

    if len(comps) < 3:
        issues.append(
            Issue(
                "comps",
                f"only {len(comps)} comparable(s) supplied; a direct comparison with fewer than "
                "three is not supportable",
                Severity.WARNING,
            )
        )

    sub_pct = values.get("subject_ac_pct")
    for idx, comp in enumerate(comps, start=1):
        if not isinstance(comp, dict):
            issues.append(Issue(f"comps[{idx}]", "comparable must be an object", Severity.ERROR))
            continue
        res = validate_fields(COMP_SCHEMA, comp)
        for i in res.errors:
            issues.append(Issue(f"comps[{idx}].{i.field}", i.message, i.severity))
        for i in res.warnings:
            issues.append(Issue(f"comps[{idx}].{i.field}", i.message, i.severity))
        # continue with the *coerced* values, not the raw strings, so every
        # downstream check compares numbers
        comp = res.values
        comps[idx - 1] = comp

        # platform given but no weighting -> the weighting silently defaults to
        # zero and the comparable is understated
        if comp.get("ac_platform") and not comp.get("ac_pct"):
            issues.append(
                Issue(
                    f"comps[{idx}].ac_pct",
                    "ancillary platform given with no weighting; it will count as zero",
                    Severity.WARNING,
                )
            )

        # adjustment magnitudes outside a sane band
        for key, label in (
            ("adj_floor", "floor"),
            ("adj_qa", "quality"),
            ("adj_sw", "sun/weather/view"),
            ("adj_age", "age & quality"),
            ("adj_location", "location"),
        ):
            v = comp.get(key)
            if v is not None and abs(v) > 0.30:
                issues.append(
                    Issue(
                        f"comps[{idx}].{key}",
                        f"{label} adjustment of {v:+.1%} exceeds 30%; check it is not a percentage "
                        "point figure",
                        Severity.WARNING,
                    )
                )

        pi = comp.get("price_index")
        if pi and values.get("subject_price_index"):
            drift = abs(values["subject_price_index"] / pi - 1)
            if drift > 0.40:
                issues.append(
                    Issue(
                        f"comps[{idx}].price_index",
                        f"time adjustment of {drift:+.1%} is large; confirm the comparable is "
                        "still representative at the valuation date",
                        Severity.WARNING,
                    )
                )

    values["comps"] = comps

    if sub_pct is None and values.get("subject_ac_platform"):
        issues.append(
            Issue("subject_ac_pct", "ancillary platform given with no weighting", Severity.WARNING)
        )

    adr = values.get("adopted_unit_rate")
    if adr and comps:
        lows = [c.get("rent") / (c.get("sa", 0) or 1) for c in comps if c.get("sa")]
        if lows:
            lo, hi = min(lows), max(lows)
            if adr > hi * 1.5 or adr < lo * 0.5:
                issues.append(
                    Issue(
                        "adopted_unit_rate",
                        f"adopted rate {adr:,.0f} sits well outside the unadjusted comparable range "
                        f"{lo:,.0f}-{hi:,.0f}; that may be right, but state the reasoning",
                        Severity.WARNING,
                    )
                )

    # rate sanity: a term rate above the reversion rate is unusual but legal;
    # a term rate of zero breaks the annuity formula
    if values.get("term_rate") in (0, 0.0):
        issues.append(Issue("term_rate", "term rate cannot be zero", Severity.ERROR))
    if values.get("reversion_rate") in (0, 0.0):
        issues.append(Issue("reversion_rate", "reversion rate cannot be zero", Severity.ERROR))

    return issues


# --- helpers ----------------------------------------------------------------

def _put(ws, row, col, value, font=INPUT_FONT, fill=None, fmt=None, border=True, align=None):
    c = ws.cell(row=row, column=col, value=value)
    c.font = font
    if fill is not None:
        c.fill = fill
    if fmt:
        c.number_format = fmt
    if border:
        c.border = BOX
    if align:
        c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=(align == "left"))
    return c


def _banner(ws, text, width):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    c = ws.cell(row=1, column=1, value=text)
    c.font = TITLE_FONT
    c.fill = BANNER
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 22


def _num(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) else default


def _as_date(v):
    return v if isinstance(v, date) else (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else "")


# --- sheet builders ---------------------------------------------------------

def _build_comps(ws, v):
    """Comps table. Returns (first_row, last_row) of the data rows."""
    ncol = len(COMPS_HEADERS)
    _banner(ws, "COMPARABLE RENTALS — DIRECT COMPARISON", ncol)

    hdr_row = 3
    for i, h in enumerate(COMPS_HEADERS, start=1):
        c = _put(ws, hdr_row, i, h, font=Font(name="Calibri", size=9, bold=True), fill=PINK, align="center")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[hdr_row].height = 30

    first = hdr_row + 1
    r = first
    for idx, comp in enumerate(v["comps"], start=1):
        _put(ws, r, 1, idx, font=FORMULA_FONT, align="center")
        _put(ws, r, 2, comp.get("address", ""), align="left")
        _put(ws, r, 3, comp.get("year", ""), fmt="0")
        _put(ws, r, 4, _as_date(comp.get("transaction_date")), align="center")
        _put(ws, r, 5, _num(comp.get("rent")), fmt="#,##0")
        _put(ws, r, 6, _num(comp.get("sa")), fmt="#,##0.0")

        # effective SA first: everything downstream depends on it
        _put(ws, r, 13, f"=F{r}+K{r}*L{r}", font=FORMULA_FONT, fmt="#,##0.0")
        _put(ws, r, 7, f"=ROUND(E{r}/M{r},0)", font=FORMULA_FONT, fmt="#,##0")
        _put(ws, r, 8, f"=ROUND(G{r}/{SQM_PER_SQFT},0)", font=FORMULA_FONT, fmt="#,##0")

        _put(ws, r, 9, comp.get("floor", ""), align="center")
        _put(ws, r, 10, comp.get("aspect", ""), align="center")
        _put(ws, r, 11, _num(comp.get("ac_platform")), fmt="#,##0.0")
        _put(ws, r, 12, _num(comp.get("ac_pct")), fmt="0%")
        _put(ws, r, 14, _num(comp.get("price_index")), fmt="#,##0.0")

        # time adjustment is derived from the indices, never typed by hand
        _put(ws, r, 15, f"=Summary!$B$6/N{r}-1", font=XREF_FONT, fmt="0.0%")
        for col, key in ((16, "adj_floor"), (17, "adj_qa"), (18, "adj_sw"),
                         (19, "adj_age"), (20, "adj_location")):
            _put(ws, r, col, _num(comp.get(key)), fmt="0.0%")

        _put(ws, r, 21, f"=SUM(O{r}:T{r})", font=FORMULA_FONT, fmt="0.0%")
        _put(ws, r, 23, f"=E{r}/M{r}*(1+U{r})", font=FORMULA_FONT, fmt="#,##0.00")
        _put(ws, r, 22, f"=ROUND(W{r},0)", font=FORMULA_FONT, fmt="#,##0")
        _put(ws, r, 24, comp.get("remarks", ""), align="left")
        r += 1

    last = r - 1

    # QA cross-checks. SUMPRODUCT needs contiguous ranges -- passing separate
    # single cells would multiply them instead of summing the products.
    qa = last + 2
    _put(ws, qa, 1, "QA CROSS-CHECKS", font=HEAD_FONT, fill=PINK)
    ws.merge_cells(start_row=qa, start_column=1, end_row=qa, end_column=6)
    checks = [
        ("WAvg adjusted unit price (SA-weighted)",
         f"=SUMPRODUCT(W{first}:W{last},F{first}:F{last})/SUM(F{first}:F{last})", "#,##0"),
        ("WAvg effective SA",
         f"=SUMPRODUCT(M{first}:M{last},F{first}:F{last})/SUM(F{first}:F{last})", "#,##0.0"),
        ("Sum of effective SAs",
         f"=SUM(M{first}:M{last})", "#,##0.0"),
    ]
    for i, (label, formula, fmt) in enumerate(checks):
        rr = qa + 1 + i
        _put(ws, rr, 1, label, font=HEAD_FONT)
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=4)
        _put(ws, rr, 5, formula, font=FORMULA_FONT, fmt=fmt)

    widths = [4, 30, 7, 12, 12, 10, 12, 12, 8, 10, 11, 8, 12, 10, 10, 9, 9, 9, 11, 10, 10, 13, 11, 34]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=first, column=3)
    return first, last


def _build_summary(ws, v, first, last):
    _banner(ws, "SUBJECT & RENTAL CONCLUSION", 6)
    for i, w in enumerate([34, 16, 12, 12, 12, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    rows = [
        (3, "Subject address", v["subject_address"], INPUT_FONT, None, None),
        (4, "Valuation date", _as_date(v.get("val_date")), INPUT_FONT, None, None),
        (5, "Valuer", v.get("valuer", ""), INPUT_FONT, None, None),
        (6, "Subject price index", _num(v.get("subject_price_index")), INPUT_FONT, None, "#,##0.0"),
        (7, "Subject saleable area (sqm)", _num(v.get("subject_sa")), INPUT_FONT, None, "#,##0.0"),
        (8, "Subject ancillary platform (sqm)", _num(v.get("subject_ac_platform")), INPUT_FONT, None, "#,##0.0"),
        (9, "Subject platform weighting", _num(v.get("subject_ac_pct")), INPUT_FONT, None, "0%"),
    ]
    for r, label, val, font, fill, fmt in rows:
        _put(ws, r, 1, label, font=HEAD_FONT)
        _put(ws, r, 2, val, font=font, fill=fill, fmt=fmt, align="left" if isinstance(val, str) else None)

    _put(ws, 10, 1, "Subject effective SA (sqm)", font=HEAD_FONT)
    _put(ws, 10, 2, "=B7+B8*B9", font=FORMULA_FONT, fmt="#,##0.0")

    _put(ws, 12, 1, "ADOPTED UNIT RATE (HK$/sqm)", font=HEADLINE_FONT, fill=YELLOW)
    _put(ws, 12, 2, _num(v.get("adopted_unit_rate")), font=HEADLINE_FONT, fill=YELLOW, fmt="#,##0")

    _put(ws, 13, 1, "MR (monthly, HK$)", font=HEADLINE_FONT, fill=YELLOW)
    _put(ws, 13, 2, "=ROUND(B10*B12,0)", font=Font(name="Calibri", size=10, bold=True), fill=YELLOW, fmt="#,##0")

    _put(ws, 14, 1, "Adopted rate (HK$/sqft)", font=HEAD_FONT)
    _put(ws, 14, 2, f"=ROUND(B12/{SQM_PER_SQFT},0)", font=FORMULA_FONT, fmt="#,##0")

    _put(ws, 16, 1, "Highest adjusted comparable", font=HEAD_FONT)
    _put(ws, 16, 2, f"=MAX(Comps!V{first}:V{last})", font=XREF_FONT, fmt="#,##0")
    _put(ws, 17, 1, "Lowest adjusted comparable", font=HEAD_FONT)
    _put(ws, 17, 2, f"=MIN(Comps!V{first}:V{last})", font=XREF_FONT, fmt="#,##0")
    _put(ws, 18, 1, "Spread (unrounded basis)", font=HEAD_FONT)
    _put(ws, 18, 2,
         f"=ROUND((MAX(Comps!W{first}:W{last})-MIN(Comps!W{first}:W{last}))/MIN(Comps!W{first}:W{last}),3)",
         font=XREF_FONT, fmt="0.0%")

    _put(ws, 20, 1, "Comparables used", font=HEAD_FONT)
    _put(ws, 20, 2, f"=COUNT(Comps!A{first}:A{last})", font=XREF_FONT, fmt="0")

    note = (
        "MR is derived from the subject's effective SA so that the adopted rate, which was itself "
        "concluded on an effective-SA basis, reproduces the comparable unit prices."
    )
    ws.merge_cells(start_row=22, start_column=1, end_row=23, end_column=6)
    c = _put(ws, 22, 1, note, font=Font(name="Calibri", size=9, italic=True, color="666666"), border=False)
    c.alignment = Alignment(wrap_text=True, vertical="top")


def _build_tr(ws, v):
    _banner(ws, "TERM & REVERSION — INCOME APPROACH", 6)
    for i, w in enumerate([38, 18, 14, 14, 14, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    _put(ws, 3, 1, "INITIAL TERM", font=HEAD_FONT, fill=PINK)
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=3)
    _put(ws, 4, 1, "Initial term (years)", font=HEAD_FONT)
    _put(ws, 4, 2, int(v.get("term_years", 1)), fmt="0")
    _put(ws, 5, 1, "Term discount rate", font=HEAD_FONT)
    _put(ws, 5, 2, _num(v.get("term_rate")), fmt="0.00%")
    _put(ws, 6, 1, "Term rent (annual, HK$)", font=HEAD_FONT)
    _put(ws, 6, 2, _num(v.get("term_rent_annual", 0)), fmt="#,##0")
    _put(ws, 7, 1, "YP factor (term)", font=HEAD_FONT)
    _put(ws, 7, 2, "=ROUND((1-(1+B5)^-B4)/B5,4)", font=FORMULA_FONT, fmt="0.0000")
    _put(ws, 8, 1, "PV of term", font=HEAD_FONT)
    _put(ws, 8, 2, "=ROUND(B6*B7,0)", font=FORMULA_FONT, fmt="#,##0")

    _put(ws, 10, 1, "REVERSION", font=HEAD_FONT, fill=PINK)
    ws.merge_cells(start_row=10, start_column=1, end_row=10, end_column=3)
    _put(ws, 11, 1, "Reversion capitalisation rate", font=HEAD_FONT)
    _put(ws, 11, 2, _num(v.get("reversion_rate")), fmt="0.00%")
    _put(ws, 12, 1, "MR (monthly, from Summary)", font=HEAD_FONT)
    _put(ws, 12, 2, "=Summary!B13", font=XREF_FONT, fmt="#,##0")
    _put(ws, 13, 1, "Reversion rent (annual)", font=HEAD_FONT)
    _put(ws, 13, 2, "=ROUND(B12*12,0)", font=FORMULA_FONT, fmt="#,##0")
    _put(ws, 14, 1, "YP perpetuity", font=HEAD_FONT)
    _put(ws, 14, 2, "=ROUND(1/B11,4)", font=FORMULA_FONT, fmt="0.0000")
    _put(ws, 15, 1, "PV factor (n years)", font=HEAD_FONT)
    _put(ws, 15, 2, "=ROUND(1/(1+B11)^B4,4)", font=FORMULA_FONT, fmt="0.0000")
    _put(ws, 16, 1, "PV of reversion", font=HEAD_FONT)
    # two ROUNDs, matching the rounding behaviour of the source workback
    _put(ws, 16, 2, "=ROUND(ROUND(B13*B14,0)*B15,0)", font=FORMULA_FONT, fmt="#,##0")

    _put(ws, 18, 1, "Total capital value", font=HEADLINE_FONT, fill=YELLOW)
    _put(ws, 18, 2, "=B8+B16", font=Font(name="Calibri", size=10, bold=True), fill=YELLOW, fmt="#,##0")
    _put(ws, 19, 1, "SAY (nearest $1M)", font=HEADLINE_FONT, fill=YELLOW)
    _put(ws, 19, 2, "=ROUND(B18/1000000,0)*1000000",
         font=Font(name="Calibri", size=10, bold=True), fill=YELLOW, fmt="#,##0")
    _put(ws, 20, 1, "Capital value (HK$/sqm effective SA)", font=HEAD_FONT)
    _put(ws, 20, 2, "=ROUND(B18/Summary!B10,0)", font=XREF_FONT, fmt="#,##0")


# --- entry point ------------------------------------------------------------

def generate(values: dict[str, Any], out_dir: Path) -> list[dict[str, str]]:
    wb = Workbook()
    ws_comps = wb.active
    ws_comps.title = "Comps"
    ws_summary = wb.create_sheet("Summary")
    ws_tr = wb.create_sheet("T&R")

    first, last = _build_comps(ws_comps, values)
    _build_summary(ws_summary, values, first, last)
    _build_tr(ws_tr, values)

    # term rent defaults to the MR from the summary so the T&R block is usable
    # even when the caller does not supply an explicit term rent
    if not values.get("term_rent_annual"):
        ws_tr["B6"] = "=Summary!B13*12"
        ws_tr["B6"].font = XREF_FONT
        ws_tr["B6"].number_format = "#,##0"
        ws_tr["B6"].border = BOX

    pin_document_properties(wb.properties)

    safe = "".join(ch if ch.isalnum() or ch in " -" else "" for ch in values["subject_address"]).strip()
    path = out_dir / f"{safe} Comps and T&R.xlsx"
    wb.save(path)
    return [{"path": str(path), "type": "xlsx"}]