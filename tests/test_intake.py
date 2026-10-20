"""Intake tests — Drive workbook -> detected format -> content -> project.

The Drive layer is mocked; everything below it (detection, parsing,
project derivation, page rendering) is exercised for real.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="vf-intake-"))
os.environ["VF_DB"] = str(_TMP / "ledger.sqlite3")
os.environ["VF_OUTPUT_DIR"] = str(_TMP / "outputs")

from demo import intake  # noqa: E402
from demo.sample_extract import build_workbook_bytes, sample_rows  # noqa: E402


class TestDetection(unittest.TestCase):
    def test_memfus_extract_is_detected(self):
        headers = list(intake.COLUMNS if hasattr(intake, "COLUMNS") else [])
        from ingest.memfus import COLUMNS
        self.assertEqual(intake.detect_format(COLUMNS).id, "memfus-mw")

    def test_generic_falls_through(self):
        spec = intake.detect_format(["Name", "Value", "Date"])
        self.assertEqual(spec.id, "generic")

    def test_oro_signature(self):
        spec = intake.detect_format(["MEM_NO", "CASE_ID", "VALUATION_DATE", "AMOUNT"])
        self.assertEqual(spec.id, "oro-case")

    def test_format_order_puts_memfus_before_generic(self):
        """Detection order matters — the specific signature must win."""
        ids = [f.id for f in intake.FORMATS]
        self.assertLess(ids.index("memfus-mw"), ids.index("generic"))
        self.assertLess(ids.index("oro-case"), ids.index("generic"))


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.data = build_workbook_bytes()
        self.parsed = intake.parse_workbook_bytes(self.data)

    def test_format_and_row_count(self):
        self.assertEqual(self.parsed.format_id, "memfus-mw")
        self.assertEqual(self.parsed.total_rows, len(sample_rows()))
        self.assertIsNone(self.parsed.error)

    def test_headers_are_the_memfus_columns(self):
        from ingest.memfus import COLUMNS
        self.assertEqual(self.parsed.headers, COLUMNS)

    def test_qc_flags_are_counted(self):
        # The sample carries one press, one connected and one approximate row.
        self.assertEqual(self.parsed.qc_counts.get("press_sourced"), 1)
        self.assertEqual(self.parsed.qc_counts.get("connected_transaction"), 1)
        self.assertEqual(self.parsed.qc_counts.get("approximate"), 1)

    def test_subject_row_is_found(self):
        self.assertTrue(self.parsed.stats["subject_found"])
        self.assertEqual(self.parsed.stats["comps"], 5)

    def test_broken_file_does_not_raise(self):
        parsed = intake.parse_workbook_bytes(b"not a spreadsheet at all")
        self.assertIsNotNone(parsed.error)
        self.assertEqual(parsed.format_id, "error")

    def test_empty_workbook_does_not_raise(self):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        buf = io.BytesIO()
        wb.save(buf)
        parsed = intake.parse_workbook_bytes(buf.getvalue())
        self.assertIsNone(parsed.error)


class TestProjectDerivation(unittest.TestCase):
    def setUp(self):
        self.parsed = intake.parse_workbook_bytes(build_workbook_bytes())

    def test_project_is_derived(self):
        p = self.parsed.project
        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "Island Place Tower")
        self.assertEqual(p["category"], "Office / Commercial")
        self.assertEqual(p["district"], "HNP")

    def test_considertn_is_read_as_millions(self):
        """CONSIDERTN is in millions. Reading it as dollars is a million-fold error."""
        p = self.parsed.project
        self.assertEqual(p["adopted_value_hkd"], 76_000_000)
        self.assertEqual(p["adopted_rate_psf_n"], 5_502)

    def test_rate_matches_the_known_case(self):
        """The sample subject is the Bel-Air M2 subject — the numbers must agree
        with the case the app already ships (HK$76,000,000 at HK$5,502/sf N)."""
        p = self.parsed.project
        self.assertEqual(p["adopted_value_hkd"], 76_000_000)
        self.assertEqual(p["nfa_sqft"], 13_812)
        self.assertEqual(p["adopted_rate_psf_n"], 5_502)

    def test_slug_is_stable_and_derived_from_the_subject(self):
        p = self.parsed.project
        self.assertEqual(p["id"], "mw-island-place-tower-hnp")
        self.assertTrue(p["id"].startswith("mw-"))

    def test_case_data_can_be_rebuilt(self):
        built = intake.build_case_data(self.parsed)
        self.assertIsNotNone(built)
        subject, comps = built
        self.assertEqual(subject["adopted_value_hkd"], 76_000_000)
        self.assertEqual(len(comps), 5)
        self.assertTrue(all(c["nfa_sqft"] > 0 for c in comps))
        self.assertTrue(all(c["psf_net"] > 0 for c in comps))

    def test_a_workbook_without_a_subject_yields_no_project(self):
        """A comps-only extract is evidence, not a project."""
        import io
        import openpyxl
        from ingest.memfus import COLUMNS
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(COLUMNS)
        for r in sample_rows():
            if r["MEM_NO"] == "Subject":
                continue
            ws.append([r.get(c) for c in COLUMNS])
        buf = io.BytesIO()
        wb.save(buf)
        parsed = intake.parse_workbook_bytes(buf.getvalue())
        self.assertIsNone(parsed.project)
        self.assertTrue(parsed.notes)
        self.assertIn("subject", parsed.notes[0].lower())


class TestPages(unittest.TestCase):
    """The content page renders. Drive is mocked so no Google account is needed."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        self.client = TestClient(app)
        self.data = build_workbook_bytes()

    def _mock_drive(self, name="MW extract 20260921.xlsx"):
        import datetime
        from unittest import mock

        f = intake.DriveFile(
            id="FILEID123",
            name=name,
            folder="Intake",
            modified_time="2026-09-21T04:00:00.000Z",
            size=len(self.data),
            web_view_link="https://drive.google.com/file/d/FILEID123/view",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return mock.patch.multiple(
            "demo.intake",
            list_drive_workbooks=mock.DEFAULT,
            find_file=mock.DEFAULT,
            download_workbook=mock.DEFAULT,
            parse_drive_file=mock.DEFAULT,
        ), f

    def test_intake_page_renders_when_signed_out(self):
        r = self.client.get("/intake")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign in to see your content", r.text)

    def test_intake_page_lists_a_workbook(self):
        from unittest import mock
        f = intake.DriveFile(
            id="FILEID123", name="MW extract.xlsx", folder="Intake",
            modified_time="2026-09-21T04:00:00.000Z", size=100,
            web_view_link="https://drive.google.com/file/d/FILEID123/view",
        )
        parsed = intake.parse_workbook_bytes(self.data)
        with mock.patch("demo.intake.list_drive_workbooks", return_value=[f]), \
             mock.patch("demo.intake.parse_drive_file", return_value=parsed), \
             mock.patch("demo.app._auth_state", return_value={
                 "signed_in": True, "configured": True,
                 "user": {"email": "bobby.hk.mak@gmail.com", "name": "Bobby"},
             }):
            r = self.client.get("/intake")
        self.assertEqual(r.status_code, 200)
        self.assertIn("MW extract.xlsx", r.text)
        self.assertIn("Memfus Wong transaction extract", r.text)
        self.assertIn("Island Place Tower", r.text)

    def test_content_page_renders_the_table(self):
        from unittest import mock
        f = intake.DriveFile(
            id="FILEID123", name="MW extract.xlsx", folder="Intake",
            modified_time="2026-09-21T04:00:00.000Z", size=100,
            web_view_link="https://drive.google.com/file/d/FILEID123/view",
        )
        parsed = intake.parse_workbook_bytes(self.data)
        with mock.patch("demo.intake.find_file", return_value=f), \
             mock.patch("demo.intake.parse_drive_file", return_value=parsed):
            r = self.client.get("/content/FILEID123")
        self.assertEqual(r.status_code, 200)
        self.assertIn("CONSIDERTN", r.text)
        self.assertIn("Island Place Tower", r.text)
        # it links to the project it creates
        self.assertIn("/case/mw-island-place-tower-hnp", r.text)

    def test_content_page_404s_for_an_unknown_file(self):
        from unittest import mock
        with mock.patch("demo.intake.find_file", return_value=None):
            r = self.client.get("/content/NOPE")
        self.assertEqual(r.status_code, 404)

    def test_console_links_to_the_content_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/intake"', r.text)

    def test_drive_extract_becomes_a_project_page(self):
        """The whole chain: workbook -> parsed -> project -> project page."""
        from unittest import mock
        f = intake.DriveFile(
            id="FILEID123", name="MW extract.xlsx", folder="Intake",
            modified_time="2026-09-21T04:00:00.000Z", size=len(self.data),
            web_view_link="https://drive.google.com/file/d/FILEID123/view",
        )
        parsed = intake.parse_workbook_bytes(self.data)
        with mock.patch("demo.intake.list_drive_workbooks", return_value=[f]), \
             mock.patch("demo.intake.parse_drive_file", return_value=parsed):
            r = self.client.get("/case/mw-island-place-tower-hnp")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Island Place Tower", r.text)
        # the source workbook is shown on the project page
        self.assertIn("Source content", r.text)
        self.assertIn("MW extract.xlsx", r.text)
        self.assertIn("/content/FILEID123", r.text)
        # the comparables came through the relevancy engine
        self.assertIn("AIA Kowloon Tower", r.text)
        self.assertIn("press_sourced", r.text)
        # and the adopted figures are the ones the extract says
        self.assertIn("76,000,000", r.text)
        self.assertIn("5,502", r.text)

    def test_console_lists_the_drive_project(self):
        from unittest import mock
        f = intake.DriveFile(
            id="FILEID123", name="MW extract.xlsx", folder="Intake",
            modified_time="2026-09-21T04:00:00.000Z", size=len(self.data),
            web_view_link="https://drive.google.com/file/d/FILEID123/view",
        )
        parsed = intake.parse_workbook_bytes(self.data)
        with mock.patch("demo.intake.list_drive_workbooks", return_value=[f]), \
             mock.patch("demo.intake.parse_drive_file", return_value=parsed):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/case/mw-island-place-tower-hnp", r.text)


if __name__ == "__main__":
    unittest.main()
