"""Resilience tests for label-anchored extraction.

The point of these is the drift scenarios that actually happen in a folder of
hand-maintained workbooks: a column inserted for a remark, a label reworded, a
row moved, a comment added. Each of these breaks index-based parsing silently,
which is the failure mode that matters.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.comments import Comment  # noqa: E402

from ingest.anchor import (  # noqa: E402
    BlockSpec,
    column_map,
    extract_annotations,
    extract_block,
    find_header_row,
    norm,
    pick_column,
    similarity,
)


def build(seed: dict[str, object], n_rows: int = 30) -> Workbook:
    """A two-column block: label in B, value in C — the common shape."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Subject"
    for i, (label, value) in enumerate(seed.items(), start=3):
        ws.cell(row=i, column=2, value=label)
        ws.cell(row=i, column=3, value=value)
    return wb


SEED = {
    "Property address": "Shop 12, Queen's Road Central",
    "Valuation date": "2026-10-13",
    "Gross floor area (GFA), sf": 850,
    "Net saleable area (SA), sf": 720,
    "Existing passing rent, HK$/month": 45000,
    "Rent-free period, months": 3,
    "Occupancy / vacancy": 1.0,
    "Capitalisation rate — spread": 0.007,
}

SPEC = [
    BlockSpec("address", ["property address"]),
    BlockSpec("valuation_date", ["valuation date"], value_type="date", required=True),
    BlockSpec("gfa", ["gross floor area gfa sf"], value_type="number", required=True),
    BlockSpec("sa", ["net saleable area sa sf"], value_type="number", required=True),
    BlockSpec("passing_rent", ["existing passing rent"], value_type="number"),
    BlockSpec("rent_free", ["rent free period months"], value_type="int"),
    BlockSpec("occupancy", ["occupancy vacancy"], value_type="pct"),
    BlockSpec("cap_spread", ["capitalisation rate spread"], value_type="pct"),
]


def as_dict(wb) -> dict:
    return extract_block(wb["Subject"], SPEC).values


class TestMatching(unittest.TestCase):
    def test_normalisation_ignores_case_and_punctuation(self):
        self.assertEqual(norm("Capitalisation rate — spread"), norm("Capitalisation rate spread"))

    def test_similarity_survives_reordering(self):
        self.assertGreater(similarity("Net saleable area SA sf", "SA net saleable area"), 0.6)

    def test_similarity_rejects_unrelated(self):
        self.assertLess(similarity("Property address", "Encumbrance memorial"), 0.3)


class TestDriftResilience(unittest.TestCase):
    def test_column_inserted_above_the_block(self):
        """The scenario from the field: someone adds a remark column."""
        a = as_dict(build(SEED))
        wb = build(SEED)
        wb["Subject"].insert_cols(2)
        b = as_dict(wb)
        self.assertEqual(a, b)

    def test_empty_column_inserted_between_label_and_value(self):
        """The insert lands *inside* the block. Value is still found, and the
        shift is reported rather than silently absorbed."""
        a = as_dict(build(SEED))
        wb = build(SEED)
        wb["Subject"].insert_cols(3)
        wb["Subject"].cell(row=1, column=3, value="reviewer note")
        ex = extract_block(wb["Subject"], SPEC)
        self.assertEqual(a, ex.values)
        self.assertTrue(ex.shifted, "the shift should be recorded, not hidden")

    def test_rows_reordered(self):
        a = as_dict(build(SEED))
        wb = build(SEED)
        ws = wb["Subject"]
        rows = [(ws.cell(row=r, column=2).value, ws.cell(row=r, column=3).value)
                for r in range(3, 3 + len(SEED))]
        for r, (lab, val) in zip(range(3, 3 + len(SEED)), reversed(rows)):
            ws.cell(row=r, column=2, value=lab)
            ws.cell(row=r, column=3, value=val)
        self.assertEqual(a, as_dict(wb))

    def test_label_reworded(self):
        """'Rent-free period, months' becomes 'Rent Free Months'."""
        a = as_dict(build(SEED))
        seed2 = dict(SEED)
        seed2["Rent Free Months"] = seed2.pop("Rent-free period, months")
        self.assertEqual(a["rent_free"], as_dict(build(seed2))["rent_free"])

    def test_extra_column_between_fields(self):
        a = as_dict(build(SEED))
        seed2: dict = {}
        for k, v in SEED.items():
            seed2[k] = v
            seed2[f"remark — {k}"] = "checked 2026-10-13"
        self.assertEqual(a, {k: v for k, v in as_dict(build(seed2)).items() if k in a})

    def test_missing_optional_field_is_not_fatal(self):
        seed2 = {k: v for k, v in SEED.items() if k != "Rent-free period, months"}
        out = as_dict(build(seed2))
        self.assertNotIn("rent_free", out)
        self.assertEqual(out["gfa"], 850)

    def test_missing_required_field_is_reported(self):
        seed2 = {k: v for k, v in SEED.items() if k != "Gross floor area (GFA), sf"}
        ex = extract_block(build(seed2)["Subject"], SPEC)
        self.assertIn("gfa", ex.missing)

    def test_value_actually_changed_is_detected(self):
        seed2 = dict(SEED)
        seed2["Gross floor area (GFA), sf"] = 900
        self.assertNotEqual(as_dict(build(SEED))["gfa"], as_dict(build(seed2))["gfa"])


class TestAnnotations(unittest.TestCase):
    def test_comments_are_captured_not_discarded(self):
        wb = build(SEED)
        wb["Subject"]["C5"].comment = Comment("Verified with Centaline 2026-08-03.", "BMA")
        ann = extract_annotations(wb)
        self.assertEqual(len(ann), 1)
        self.assertIn("Centaline", ann[0].comment)
        self.assertEqual(ann[0].author, "BMA")

    def test_annotations_survive_a_column_insert(self):
        wb = build(SEED)
        wb["Subject"]["C5"].comment = Comment("Verified.", "BMA")
        wb["Subject"].insert_cols(2)
        ann = extract_annotations(wb)
        self.assertEqual(len(ann), 1)
        self.assertEqual(ann[0].cell, "D5")


class TestCompsHeader(unittest.TestCase):
    def test_header_found_and_columns_mapped(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Comps"
        ws["A1"] = "preamble"
        headers = ["Address", "Transaction Date", "Price", "Area", "Unit Px $/sqm"]
        for i, h in enumerate(headers, start=1):
            ws.cell(row=4, column=i, value=h)
        r = find_header_row(ws, ["address", "price", "area"])
        self.assertEqual(r, 4)
        cols = column_map(ws, r)
        name, idx = pick_column(cols, ["address"])
        self.assertEqual(idx, 1)

    def test_header_found_after_a_row_is_inserted_above(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Comps"
        headers = ["Address", "Transaction Date", "Price", "Area", "Unit Px $/sqm"]
        for i, h in enumerate(headers, start=1):
            ws.cell(row=4, column=i, value=h)
        r1 = find_header_row(ws, ["address", "price", "area"])
        ws.insert_rows(1, 3)
        r2 = find_header_row(ws, ["address", "price", "area"])
        self.assertEqual(r2, r1 + 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
