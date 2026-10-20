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

    def test_address_is_put_into_house_style(self):
        """The extract gives "510 KING'S RD, Hong Kong" — the house style is
        "510 King's Road, Hong Kong"."""
        p = self.parsed.project
        self.assertEqual(p["address"], "510 King's Road, Hong Kong")
        # the raw value is kept for anything that needs to geocode it
        self.assertEqual(p["address_raw"], "510 KING'S RD, Hong Kong")

    def test_address_formatting_does_not_mangle_possessives_or_acronyms(self):
        from demo.intake import _pretty_address
        self.assertEqual(
            _pretty_address("AIA Kowloon Tower, 183 AMOY RD, Hong Kong"),
            "AIA Kowloon Tower, 183 Amoy Road, Hong Kong",
        )
        self.assertEqual(
            _pretty_address("31 HANG LOK ST, Hong Kong"),
            "31 Hang Lok Street, Hong Kong",
        )
        # "HANG LOK" is two short romanised words, not an acronym
        self.assertNotIn("LOK", _pretty_address("31 HANG LOK ST, Hong Kong"))

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


class TestScopeGuard(unittest.TestCase):
    """The sign-in must be able to see files the valuer put in Drive himself."""

    def test_requested_scopes_include_readonly(self):
        from demo.oauth import DRIVE_READ_SCOPE, SCOPES
        self.assertIn(DRIVE_READ_SCOPE, SCOPES)
        # drive.file stays, so the app can still write what it owns
        self.assertIn("https://www.googleapis.com/auth/drive.file", SCOPES)

    def test_a_limited_sign_in_is_reported(self):
        """A token without drive.readonly must be detectable, not silent."""
        from unittest import mock
        from demo import oauth
        with mock.patch.object(oauth, "granted_scopes", return_value=[
            "https://www.googleapis.com/auth/drive.file",
            "openid",
        ]):
            self.assertFalse(oauth.can_read_all_drive())
        with mock.patch.object(oauth, "granted_scopes", return_value=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]):
            self.assertTrue(oauth.can_read_all_drive())

    def test_content_page_explains_a_limited_sign_in(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from demo.app import app
        client = TestClient(app)
        with mock.patch("demo.app._auth_state", return_value={
                "signed_in": True, "configured": True,
                "user": {"email": "bobby.hk.mak@gmail.com"},
            }), \
             mock.patch("demo.oauth.granted_scopes", return_value=[
                 "https://www.googleapis.com/auth/drive.file",
             ]), \
             mock.patch("demo.oauth.can_read_all_drive", return_value=False):
            r = client.get("/intake")
        self.assertEqual(r.status_code, 200)
        self.assertIn("cannot see files you put in Drive yourself", r.text)
        self.assertIn("drive.readonly", r.text)

    def test_api_intake_reports_the_scope(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from demo.app import app
        client = TestClient(app)
        with mock.patch("demo.app._auth_state", return_value={
                "signed_in": True, "configured": True,
                "user": {"email": "bobby.hk.mak@gmail.com"},
            }), \
             mock.patch("demo.oauth.granted_scopes", return_value=[
                 "https://www.googleapis.com/auth/drive.readonly",
             ]), \
             mock.patch("demo.oauth.can_read_all_drive", return_value=True):
            r = client.get("/api/intake")
        d = r.json()
        self.assertTrue(d["can_read_all_drive"])
        self.assertIn(
            "https://www.googleapis.com/auth/drive.readonly", d["granted_scopes"]
        )


class TestScanSetting(unittest.TestCase):
    """Which folders the valuer is pointing the app at."""

    def test_default_is_no_extra_folders(self):
        from demo.intake import parse_scan_setting
        self.assertEqual(parse_scan_setting(None), ((), False))
        self.assertEqual(parse_scan_setting(""), ((), False))

    def test_all_means_whole_drive(self):
        from demo.intake import parse_scan_setting
        self.assertEqual(parse_scan_setting("all"), ((), True))

    def test_folder_ids_are_parsed(self):
        from demo.intake import parse_scan_setting
        self.assertEqual(parse_scan_setting("abc, def ,"), (("abc", "def"), False))

    def test_cookie_round_trip(self):
        from demo.intake import scan_setting_cookie
        self.assertEqual(scan_setting_cookie(("a", "b"), False), "a,b")
        self.assertEqual(scan_setting_cookie((), True), "all")
        self.assertEqual(scan_setting_cookie((), False), "")

    def test_scan_route_sets_the_cookie(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from demo.app import app
        client = TestClient(app, follow_redirects=False)
        with mock.patch("demo.app._auth_state", return_value={
                "signed_in": True, "configured": True, "user": {"email": "b@x.com"},
            }), \
             mock.patch("demo.intake.list_drive_folders", return_value=[]), \
             mock.patch("demo.intake.list_drive_workbooks", return_value=[]):
            r = client.post("/intake/scan", data={"folders": "all"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("bma_intake_scan=all", r.headers.get("set-cookie", ""))

    def test_scan_route_clears_the_cookie_for_default(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from demo.app import app
        client = TestClient(app, follow_redirects=False)
        with mock.patch("demo.app._auth_state", return_value={
                "signed_in": True, "configured": True, "user": {"email": "b@x.com"},
            }), \
             mock.patch("demo.intake.list_drive_folders", return_value=[]), \
             mock.patch("demo.intake.list_drive_workbooks", return_value=[]):
            r = client.post("/intake/scan", data={"folders": "default"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("bma_intake_scan=", r.headers.get("set-cookie", ""))


class TestCacheVersioning(unittest.TestCase):
    """A cache keyed on the file alone cannot tell that the code changed."""

    def test_the_parse_version_is_derived_from_the_code(self):
        from demo import intake
        v1 = intake._parse_version()
        self.assertTrue(v1)
        self.assertEqual(v1, intake._parse_version())

    def test_the_cache_key_includes_the_version(self):
        from demo import intake
        a = intake._parse_key("F1", "2026-09-21T04:00:00Z")
        b = intake._parse_key("F1", "2026-09-21T04:00:00Z")
        self.assertEqual(a, b)
        self.assertIn(intake._parse_version(), a)
        # a different file, or a different mtime, is a different key
        self.assertNotEqual(a, intake._parse_key("F2", "2026-09-21T04:00:00Z"))
        self.assertNotEqual(a, intake._parse_key("F1", "2026-09-22T04:00:00Z"))
