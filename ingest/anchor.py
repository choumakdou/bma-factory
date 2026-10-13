"""Label-anchored extraction.

The problem this solves: a workbook follows a stable *backbone* — subject
section, data pull, comps, adjustments, adopted conclusion — but individual
files drift. Someone inserts a column for a remark, adds a note, reorders a
block. Anything that matches by cell position (column C row 24) breaks
silently, which is the worst possible failure: no error, plausible numbers.

So: **match by label, resolve relative to the match.**

    find "Adopted Unit Rate"  →  whatever row carries that text
    value                     →  the cell to its right

Insert a column anywhere and the anchor does not move, because text does not
move the way an index does. This is the difference between a schema and a
fingerprint, and it is what makes a family of drifting workbooks parseable.

Cell comments are captured too, not discarded. In a valuation workbook the
comments are where the judgement lives — why a comparable was excluded, when a
rent was verified, what a landlord confirmed. They exist nowhere else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

# Tokens that carry no meaning for matching, so "the" and "of" do not have to
# appear on both sides for two labels to be considered the same thing.
STOPWORDS = {"the", "of", "a", "an", "and", "for", "in", "on", "at", "to", "per"}

_PUNCT = re.compile(r"[^\w\s%$/]+", re.UNICODE)
_WS = re.compile(r"\s+")


def norm(value: Any) -> str:
    """Normalise a label for comparison: casefold, strip punctuation, collapse
    whitespace, drop stopwords. 'Adopted Unit Rate (HK$/sqm)' and
    'ADOPTED RATE PER SQM' both collapse toward the same token set."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = s.replace("／", "/").replace("＄", "$").replace("　", " ")
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip().lower()
    return s


def tokens(value: Any) -> set[str]:
    raw = norm(value).split()
    return {t for t in raw if t not in STOPWORDS and len(t) > 1}


def similarity(a: Any, b: Any) -> float:
    """Token overlap, 0..1. Handles reordering and extra words, which is what
    a remark column or a reworded header actually looks like."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def label_matches(cell: Any, patterns: Sequence[str], threshold: float = 0.6) -> bool:
    for p in patterns:
        if similarity(cell, p) >= threshold:
            return True
    return False


# --- row / block resolution -------------------------------------------------


@dataclass
class BlockSpec:
    """One backbone field: how to recognise its label, and how to find its value.

    `label_cols` is a *search window*, not a fixed column. A fixed label column
    looks robust and is not: insert one column above the block and the label
    moves out of the window while the cell still looks plausible. Whichever
    column actually carries the label becomes the anchor origin, and the value
    is resolved relative to *that*.
    """

    key: str
    patterns: list[str]
    label_cols: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8)
    value_offset: int = 1              # value sits N columns right of the label
    value_span: int = 3                # how far right to look if that cell is empty
    note_col: int | None = None        # optional guidance column, captured not parsed
    value_type: str = "raw"            # raw | text | number | int | date | pct
    required: bool = False


@dataclass
class Extraction:
    values: dict[str, Any] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    ambiguous: dict[str, float] = field(default_factory=dict)
    anchors: dict[str, str] = field(default_factory=dict)       # key -> value cell
    label_anchor: dict[str, str] = field(default_factory=dict)  # key -> label cell
    shifted: dict[str, str] = field(default_factory=dict)       # key -> "C7→D7"

    @property
    def complete(self) -> bool:
        return not self.missing


def _coerce(raw: Any, kind: str) -> Any:
    # handle real date/datetime objects before stringifying, otherwise
    # "2026-10-13 00:00:00" fails every strptime format and silently becomes None
    if isinstance(raw, datetime):
        raw = raw.date()
    if isinstance(raw, date):
        return raw.isoformat()
    if kind in ("raw", "text"):
        return "" if raw is None else str(raw).strip()
    s = str(raw or "").strip().replace(",", "").replace("$", "").replace("HK$", "").replace("%", "")
    if s in ("", "-", "—", "n/a", "N/A", "TBC"):
        return None
    if kind in ("number", "int", "pct"):
        try:
            n = float(s)
        except ValueError:
            return None
        return int(n) if kind == "int" else (n / 100.0 if kind == "pct" and abs(n) > 1 else n)
    if kind == "date":
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    return s


# Strings that are template prompts, not data. A blank template legitimately
# contains these; treating them as values would poison the database.
PLACEHOLDERS = re.compile(
    r"^(e\.g\.|eg\.|example|enter|input|placeholder|tbc|tbd|n/?a|xxx|\.\.\.|—|-)$|"
    r"^\S+ \/ \S+ .*\.\.\.$",
    re.IGNORECASE,
)


def looks_like_placeholder(v: Any) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    if not s:
        return True
    if PLACEHOLDERS.match(s):
        return True
    # "Shop / unit, building, street, district" — a hint listing what to type,
    # not an address. Commas, a slash, no digits.
    if "," in s and "/" in s and not re.search(r"\d", s):
        return True
    return False


def extract_block(ws, spec: list[BlockSpec], row_window: int = 80) -> Extraction:
    """Resolve a label-anchored block: one row per backbone field.

    Each label is searched for across a window of columns. Whichever cell
    matches becomes the anchor origin, and the value is read relative to it —
    so a column inserted anywhere in or above the block shifts nothing.
    """
    out = Extraction()
    used_rows: set[int] = set()

    for f in spec:
        best: tuple[float, int, int] | None = None   # (score, row, label_col)
        for r in range(1, min(ws.max_row, row_window) + 1):
            if r in used_rows:
                continue
            for lc in f.label_cols:
                if lc > ws.max_column:
                    continue
                cell_text = ws.cell(row=r, column=lc).value
                if cell_text is None or not str(cell_text).strip():
                    continue
                score = similarity(cell_text, " ".join(f.patterns))
                if score < 0.6:
                    continue
                if f.value_offset:
                    vcell, shifted = _resolve_value(ws, r, lc, f)
                    if vcell is None:
                        continue          # a label with nothing beside it is not the field
                if best is None or score > best[0]:
                    best = (score, r, lc)

        if best is None:
            if f.required:
                out.missing.append(f.key)
            continue

        score, row, lc = best
        used_rows.add(row)
        if score < 0.999:
            out.ambiguous[f.key] = round(score, 3)

        vcell, shifted = _resolve_value(ws, row, lc, f)
        cell = ws.cell(row=row, column=lc + f.value_offset) if vcell is None else vcell
        val = _coerce(cell.value, f.value_type)
        if f.value_type in ("raw", "text") and looks_like_placeholder(val):
            val = ""
        out.values[f.key] = val
        out.anchors[f.key] = cell.coordinate
        out.label_anchor[f.key] = ws.cell(row=row, column=lc).coordinate
        if shifted:
            # the value was not where the spec said it would be. Record it —
            # a silent wrong read is the one outcome worth engineering against.
            out.shifted[f.key] = f"{ws.cell(row=row, column=lc + f.value_offset).coordinate}→{cell.coordinate}"
        if f.note_col is not None:
            n = ws.cell(row=row, column=f.note_col).value
            if n:
                out.notes[f.key] = str(n).strip()

    return out


def _resolve_value(ws, row: int, label_col: int, f: "BlockSpec"):
    """Find the value cell to the right of a matched label.

    Normally the cell immediately right. If that is empty and a later cell in
    the same row is not — which is what inserting a column between the label
    and the value looks like — take the later one and report the shift, so the
    drift is visible rather than silently absorbed.
    """
    first = ws.cell(row=row, column=label_col + f.value_offset)
    if f.value_offset == 0:
        return first, False
    if first.value is not None and str(first.value).strip() != "":
        return first, False
    for c in range(label_col + f.value_offset + 1, label_col + f.value_offset + 1 + f.value_span):
        if c > ws.max_column:
            break
        cell = ws.cell(row=row, column=c)
        if cell.value is not None and str(cell.value).strip() != "":
            return cell, True
    return first, False


# --- comparables table ------------------------------------------------------


def find_header_row(ws, keywords: Iterable[str], scan_rows: int = 15) -> int | None:
    """Locate a comps header by looking for the row carrying most of the
    expected column names. Survives a column being inserted above it."""
    kws = [norm(k) for k in keywords]
    best, best_hits = None, 0
    for r in range(1, min(ws.max_row, scan_rows) + 1):
        cells = [norm(ws.cell(row=r, column=c).value) for c in range(1, min(ws.max_column, 60) + 1)]
        joined = " ".join(c for c in cells if c)
        hits = sum(1 for k in kws if k and k in joined)
        if hits > best_hits:
            best, best_hits = r, hits
    return best if best_hits >= max(2, len(kws) // 3) else None


def column_map(ws, header_row: int) -> dict[str, int]:
    """Map normalised header text → column index, for the given header row."""
    out: dict[str, int] = {}
    for c in range(1, min(ws.max_column, 80) + 1):
        h = norm(ws.cell(row=header_row, column=c).value)
        if h and h not in out:
            out[h] = c
    return out


def pick_column(cols: dict[str, int], patterns: Sequence[str], threshold: float = 0.6) -> tuple[str | None, int | None]:
    best, best_score = None, 0.0
    for header, idx in cols.items():
        for p in patterns:
            s = similarity(header, p)
            if s > best_score:
                best, best_score = header, s
    if best_score >= threshold:
        return best, cols[best]
    return None, None


# --- annotations ------------------------------------------------------------


@dataclass
class Annotation:
    sheet: str
    cell: str
    value: str
    comment: str
    author: str = ""


def extract_annotations(wb) -> list[Annotation]:
    """Every cell comment in the workbook, with the value it annotates.

    Comments are the judgement trail. A parser that reads numbers and throws
    comments away has captured the arithmetic and lost the reasoning.
    """
    out: list[Annotation] = []
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                if c.comment is not None:
                    out.append(
                        Annotation(
                            sheet=ws.title,
                            cell=c.coordinate,
                            value="" if c.value is None else str(c.value)[:200],
                            comment=(c.comment.text or "").strip(),
                            author=c.comment.author or "",
                        )
                    )
    return out


def near(ws, row: int, col: int, span: int = 3) -> str:
    """Nearest non-empty text to the left of a cell — usually the row label a
    comment is attached to, which is what makes an annotation interpretable."""
    for c in range(col - 1, max(0, col - span - 1), -1):
        v = ws.cell(row=row, column=c).value
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""
