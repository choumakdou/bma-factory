"""Section detection tests.

A working file is a stack of sections, not one flat table. These tests
cover recognising them, splitting a sheet into blocks, and rendering
each kind its own way.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="vf-sections-"))
os.environ["VF_DB"] = str(_TMP / "ledger.sqlite3")
os.environ["VF_OUTPUT_DIR"] = str(_TMP / "outputs")

from demo import sections as S  # noqa: E402
from demo.intake import build_case_data, parse_workbook_bytes  # noqa: E402
from demo.sample_extract import (  # noqa: E402
    ADJUSTMENT_COLUMNS,
    build_working_file_bytes,
)


class TestSplitting(unittest.TestCase):
    def test_a_sheet_with_two_tables_splits_in_two(self):
        rows = [
            ["Name", "Value", "Note"],
            ["a", 1, "x"],
            ["b", 2, "y"],
            [],
            ["Thing", "Size", "Colour"],
            ["c", 3, "red"],
        ]
        parts = S.split_sheet("S", rows)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].headers, ["Name", "Value", "Note"])
        self.assertEqual(len(parts[0].rows), 2)
        self.assertEqual(parts[1].headers, ["Thing", "Size", "Colour"])
        self.assertEqual(len(parts[1].rows), 1)

    def test_a_data_row_is_not_mistaken_for_a_header(self):
        """A repeated value means data, not a header."""
        rows = [
            ["Tower A", "Tower A", "5"],
            ["x", "y", "z"],
        ]
        self.assertEqual(S.split_sheet("S", rows), [])

    def test_a_numeric_row_is_not_a_header(self):
        rows = [[1, 2, 3], [4, 5, 6]]
        self.assertEqual(S.split_sheet("S", rows), [])

    def test_a_blank_row_ends_nothing(self):
        rows = [
            ["A", "B", "C"],
            [1, 2, 3],
            [],
            [4, 5, 6],
        ]
        parts = S.split_sheet("S", rows)
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0].rows), 2)


class TestDetection(unittest.TestCase):
    def test_mw_extract_is_detected(self):
        from ingest.memfus import COLUMNS
        self.assertEqual(S.detect_section(COLUMNS).id, "mw-extract")

    def test_mw_with_an_adjustment_ladder_is_comps(self):
        from ingest.memfus import COLUMNS
        spec = S.detect_section(list(COLUMNS) + list(ADJUSTMENT_COLUMNS))
        self.assertEqual(spec.id, "adopted-comps")

    def test_a_property_record_is_a_subject(self):
        spec = S.detect_section([
            "SUBJECT", "ADDRESS", "DISTRICT", "LOT", "USAGE",
            "NFA (SF)", "ADOPTED VALUE (HK$)", "VALUATION DATE",
        ])
        self.assertEqual(spec.id, "subject")

    def test_adjustment_columns_are_marked(self):
        self.assertTrue(S.is_adjustment_column("TIME ADJ %"))
        self.assertTrue(S.is_adjustment_column("ADJ_FLOOR"))
        self.assertFalse(S.is_adjustment_column("CONSIDERTN"))
        self.assertFalse(S.is_adjustment_column("NET_PSF"))

    def test_the_mw_column_set_is_normalised_the_same_way(self):
        """'MEMNO' must match 'MEM_NO'. Comparing across normalisations
        makes every column look like an extra column."""
        from ingest.memfus import COLUMNS
        norm = {S._norm(c) for c in COLUMNS}
        self.assertIn("MEMNO", norm)
        self.assertIn("INSTDATE", norm)


class TestWorkingFile(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_workbook_bytes(build_working_file_bytes())

    def test_three_sections_are_found(self):
        kinds = [s["kind"] for s in self.parsed.sections]
        self.assertEqual(kinds, ["subject", "comps", "extract"])

    def test_the_sheets_are_labelled(self):
        names = [s["sheet"] for s in self.parsed.sections]
        self.assertEqual(names, ["Subject", "Comps", "MW Extract"])

    def test_the_comps_section_knows_its_adjustment_columns(self):
        comps = next(s for s in self.parsed.sections if s["kind"] == "comps")
        self.assertEqual(len(comps["adjustment_columns"]), len(ADJUSTMENT_COLUMNS))
        self.assertIn("TIME ADJ %", comps["adjustment_columns"])

    def test_the_subject_section_reads_the_property(self):
        subj = next(s for s in self.parsed.sections if s["kind"] == "subject")
        self.assertEqual(subj["project"]["title"], "Island Place Tower")
        self.assertEqual(subj["project"]["adopted_value_hkd"], 76_000_000)
        self.assertEqual(subj["project"]["nfa_sqft"], 13_812)

    def test_the_extract_section_is_not_mistaken_for_comps(self):
        extract = next(s for s in self.parsed.sections if s["kind"] == "extract")
        self.assertEqual(extract["adjustment_columns"], [])
        self.assertEqual(extract["stats"]["comps"], 5)

    def test_qc_flags_are_counted_on_the_comps(self):
        comps = next(s for s in self.parsed.sections if s["kind"] == "comps")
        self.assertEqual(comps["qc_counts"].get("press_sourced"), 1)
        self.assertEqual(comps["qc_counts"].get("connected_transaction"), 1)

    def test_case_data_prefers_the_comps_section(self):
        built = build_case_data(self.parsed)
        self.assertIsNotNone(built)
        subject, comps = built
        self.assertEqual(subject["adopted_value_hkd"], 76_000_000)
        self.assertEqual(len(comps), 5)

    def test_both_paths_give_the_same_project(self):
        """The same subject must be one project, whichever way the file
        was read. Two derivations, two ids, means two projects."""
        from demo.intake import project_from_sections
        from demo.sample_extract import build_workbook_bytes
        flat = parse_workbook_bytes(build_workbook_bytes())
        work = project_from_sections(self.parsed)
        self.assertEqual(flat.project["id"], work["id"])
        self.assertEqual(flat.project["adopted_value_hkd"],
                         work["adopted_value_hkd"])

    def test_a_flat_extract_still_works(self):
        """The old single-sheet extract must not regress."""
        from demo.sample_extract import build_workbook_bytes
        parsed = parse_workbook_bytes(build_workbook_bytes())
        built = build_case_data(parsed)
        self.assertIsNotNone(built)
        subject, comps = built
        self.assertEqual(subject["adopted_value_hkd"], 76_000_000)
        self.assertEqual(len(comps), 5)


class TestRendering(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        from demo import intake

        self.parsed = parse_workbook_bytes(build_working_file_bytes())
        self.f = intake.DriveFile(
            id="SEC123", name="working file.xlsx", folder="Intake",
            modified_time="2026-09-21T04:00:00.000Z", size=9000,
            web_view_link="https://drive.google.com/file/d/SEC123/view",
        )
        self.client = TestClient(app)
        self._p1 = mock.patch("demo.intake.find_file", return_value=self.f)
        self._p2 = mock.patch("demo.intake.parse_drive_file", return_value=self.parsed)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_the_page_renders_each_section(self):
        r = self.client.get("/content/SEC123")
        self.assertEqual(r.status_code, 200)
        for label in ("Subject", "Adopted comparables, with adjustments",
                      "Memfus Wong data extraction"):
            self.assertIn(label, r.text)

    def test_the_subject_reads_as_key_value_pairs(self):
        r = self.client.get("/content/SEC123")
        self.assertIn("Island Place Tower", r.text)
        self.assertIn("VALUATION DATE", r.text)

    def test_adjustment_columns_are_marked_in_the_table(self):
        r = self.client.get("/content/SEC123")
        self.assertIn("col-adj", r.text)
        self.assertIn("TIME ADJ %", r.text)

    def test_the_project_link_is_present(self):
        r = self.client.get("/content/SEC123")
        self.assertIn("/case/", r.text)


if __name__ == "__main__":
    unittest.main()
