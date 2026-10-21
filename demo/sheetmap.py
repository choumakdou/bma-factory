"""The sheet map — how a workbook is put together, shown as it stands.

When the app carves a working file into sections, the valuer needs to
see that carving, not just the results. Two things go wrong if they
cannot:

  - a section silently swallows the wrong rows, and
  - rows that belong to no section simply vanish from the page.

So the content page carries a map of every sheet: the blocks, the
gaps, and the original cells with the detected section tinted on them.

The map is built from the parsed workbook, so it is a view of the file,
not a re-parse of it.
"""

from __future__ import annotations

from typing import Any, Optional

#: The colour each kind is drawn in. Kept here so the template and the
#: map agree, and so a new kind has one place to be added.
KIND_COLOURS = {
    "subject": "blue",
    "comps": "green",
    "extract": "amber",
    "table": "slate",
}

#: What a row is when it is not part of any section.
GAP = "gap"


def _colour(kind: str) -> str:
    return KIND_COLOURS.get(kind, "slate")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict or an object, whichever this is."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_map(parsed: Any) -> list[dict[str, Any]]:
    """A map of every sheet in a parsed workbook.

    Takes a ParsedWorkbook or a plain dict of one, and a section may be
    a dict or an object — the map is a view, and it should not care.
    """
    sections: list[Any] = list(_get(parsed, "sections", []) or [])
    grid: dict[str, list[list[Any]]] = dict(_get(parsed, "sheet_grid", {}) or {})
    sizes: dict[str, int] = dict(_get(parsed, "sheet_sizes", {}) or {})

    sheet_names = list(_get(parsed, "sheets", []) or [])
    seen = {_get(s, "sheet") for s in sections}
    for name in sheet_names:
        if name not in seen:
            sizes.setdefault(name, 0)

    out: list[dict[str, Any]] = []
    for name in sheet_names:
        blocks = [s for s in sections if _get(s, "sheet") == name]
        blocks.sort(key=lambda s: _get(s, "header_row") or 0)

        total = sizes.get(name) or 0
        for b in blocks:
            total = max(total, _get(b, "end_row") or 0, _get(b, "header_row") or 0)

        # The band — one entry per row, so the arrangement reads as a
        # picture. Rows nobody claimed are the gaps.
        band: list[dict[str, Any]] = []
        cursor = 1
        for b in blocks:
            start = _get(b, "header_row") or 0
            end = _get(b, "end_row") or start
            if start > cursor:
                band.append({
                    "kind": GAP, "from": cursor, "to": start - 1,
                    "label": "", "rows": start - cursor,
                })
            band.append({
                "kind": _get(b, "kind") or "table",
                "from": start, "to": end,
                "label": _get(b, "label") or "",
                "rows": max(1, end - start + 1),
                "header_row": start,
                "is_header": True,
            })
            cursor = end + 1
        if total and cursor <= total:
            band.append({
                "kind": GAP, "from": cursor, "to": total,
                "label": "", "rows": total - cursor + 1,
            })

        out.append({
            "sheet": name,
            "rows": total,
            "blocks": [_block(b) for b in blocks],
            "band": band,
            "grid": grid.get(name, []),
            "has_content": bool(blocks),
        })
    return out


def _block(b: Any) -> dict[str, Any]:
    headers = _get(b, "headers") or []
    rows = _get(b, "rows") or []
    kind = _get(b, "kind") or "table"
    return {
        "kind": kind,
        "colour": _colour(kind),
        "label": _get(b, "label") or "",
        "header_row": _get(b, "header_row") or 0,
        "end_row": _get(b, "end_row") or 0,
        "data_rows": len(rows),
        "columns": len(headers),
        "origin": _get(b, "rule_origin") or "built-in",
        "rule_id": _get(b, "rule_id") or "",
        "notes": _get(b, "notes") or [],
        "adjustment_columns": _get(b, "adjustment_columns") or [],
    }


def kind_for_row(sheet_map: list[dict[str, Any]], sheet: str, row: int) -> str:
    """Which kind a given spreadsheet row belongs to."""
    for sm in sheet_map:
        if sm.get("sheet") != sheet:
            continue
        for seg in sm.get("band", []):
            if seg["from"] <= row <= seg["to"]:
                return seg["kind"]
    return GAP


def summary(sheet_map: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A per-kind tally for the page header."""
    tally: dict[str, int] = {}
    for sm in sheet_map:
        for b in sm.get("blocks", []):
            tally[b["kind"]] = tally.get(b["kind"], 0) + 1
    return [
        {"kind": k, "colour": _colour(k), "count": v}
        for k, v in sorted(tally.items(), key=lambda kv: -kv[1])
    ]


def column_letters(n: int) -> list[str]:
    """Excel column letters — A, B, ... Z, AA, AB and so on.

    The original sheet's own header row is not always the first row, and
    it is not always the same for every block, so the map is headed with
    column letters rather than a header row that may not apply.
    """
    out: list[str] = []
    for i in range(n):
        letters = ""
        v = i
        while True:
            letters = chr(ord("A") + v % 26) + letters
            v = v // 26 - 1
            if v < 0:
                break
        out.append(letters)
    return out


def preview_rows(
    sheet_map: list[dict[str, Any]],
    sheet: str,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """The original cells of a sheet, with the section tint attached.

    This is the "highlight the cells in different colours" view — the
    file as it stands, with each row's section shown behind it.
    """
    for sm in sheet_map:
        if sm.get("sheet") != sheet:
            continue
        grid = sm.get("grid") or []
        out: list[dict[str, Any]] = []
        for idx, row in enumerate(grid[:limit], start=1):
            kind = kind_for_row(sheet_map, sheet, idx)
            out.append({
                "row": idx,
                "kind": kind,
                "colour": _colour(kind) if kind != GAP else "gap",
                "cells": [_cell(v) for v in row],
            })
        return out
    return []


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float, bool)):
        return str(v)
    return str(v).strip()
