"""Site Value tests — the third tab on the map.

The source is the valuer's own Google Sheet. Three things this must
never do, and most of what follows is about them:

- it must not guess which column is which (the mapping is detected,
  shown, and overridable);
- it must not turn a blank row or a total row into a pin;
- it must not place a pin it has no coordinates for, and must not
  overwrite a pin the valuer set himself.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="vf-sites-"))
os.environ["VF_DB"] = str(_TMP / "ledger.sqlite3")
os.environ["VF_OUTPUT_DIR"] = str(_TMP / "outputs")


class TestColumnDetection(unittest.TestCase):
    """Which column is which."""

    HEADERS = ["Site", "Address", "District", "Client", "Site Area (sf)",
               "Value", "Rate", "Date", "Lat", "Lon", "Remarks"]

    def test_the_common_columns_are_found(self):
        from demo import sites
        m = sites.detect_columns(self.HEADERS)
        self.assertEqual(m["name"], "Site")
        self.assertEqual(m["address"], "Address")
        self.assertEqual(m["district"], "District")
        self.assertEqual(m["site_area"], "Site Area (sf)")
        self.assertEqual(m["value"], "Value")
        self.assertEqual(m["lat"], "Lat")
        self.assertEqual(m["lon"], "Lon")

    def test_the_brochure_shapes_are_not_confused(self):
        """'Site Area' must not be claimed as the site's name, and
        'Area' alone must not steal it."""
        from demo import sites
        m = sites.detect_columns(self.HEADERS)
        self.assertEqual(m["site_area"], "Site Area (sf)")
        self.assertEqual(m["name"], "Site")

    def test_no_two_fields_claim_the_same_column(self):
        from demo import sites
        m = sites.detect_columns(self.HEADERS)
        self.assertEqual(len(set(m.values())), len(m))

    def test_an_unrecognised_column_is_reported_not_guessed(self):
        from demo import sites
        m = sites.detect_columns(self.HEADERS)
        self.assertIn("Remarks", sites.unmapped_headers(self.HEADERS, m))

    def test_the_valuers_own_mapping_wins(self):
        from demo import sites
        m = sites.detect_columns(self.HEADERS,
                                 overrides={"name": "Remarks"})
        self.assertEqual(m["name"], "Remarks")

    def test_an_override_is_not_stolen_by_another_field(self):
        from demo import sites
        m = sites.detect_columns(self.HEADERS,
                                 overrides={"address": "Remarks"})
        # 'Remarks' is now the address, so nothing else may claim it
        self.assertEqual(m["address"], "Remarks")
        self.assertNotIn("Remarks", sites.unmapped_headers(self.HEADERS, m))

    def test_a_missing_column_is_simply_absent(self):
        from demo import sites
        m = sites.detect_columns(["Address", "District"])
        self.assertEqual(m["address"], "Address")
        self.assertNotIn("value", m)
        self.assertNotIn("lat", m)

    def test_matching_is_whole_word(self):
        from demo import sites
        self.assertTrue(sites.header_matches("Site Area (sf)", "site"))
        self.assertTrue(sites.header_matches("Site", "site"))
        self.assertFalse(sites.header_matches("Website", "site"))
        self.assertFalse(sites.header_matches("Estimate", "site"))


class TestTheSheetUrl(unittest.TestCase):
    """The link Bobby pastes."""

    def test_a_full_url_with_a_tab(self):
        from demo import sites
        fid, gid = sites.parse_sheet_url(
            "https://docs.google.com/spreadsheets/d/1ABCdef/edit?gid=590379647#gid=590379647")
        self.assertEqual(fid, "1ABCdef")
        self.assertEqual(gid, "590379647")

    def test_a_url_without_a_tab_reads_the_first_one(self):
        from demo import sites
        fid, gid = sites.parse_sheet_url(
            "https://docs.google.com/spreadsheets/d/1ABCdef/edit")
        self.assertEqual(fid, "1ABCdef")
        self.assertEqual(gid, "")

    def test_something_that_is_not_a_sheet_link_is_refused(self):
        from demo import sites
        self.assertEqual(sites.parse_sheet_url("hello world"), ("", ""))
        self.assertEqual(sites.parse_sheet_url(""), ("", ""))


class TestRows(unittest.TestCase):
    """A sheet is not always tidy."""

    def test_the_first_non_blank_row_is_the_header(self):
        from demo import sites
        headers, rows = sites.rows_from_csv(
            "\n\nSite,Address\nA,1\n")
        self.assertEqual(headers, ["Site", "Address"])
        self.assertEqual(len(rows), 1)

    def test_blank_rows_are_dropped(self):
        from demo import sites
        _, rows = sites.rows_from_csv("Site,Address\nA,1\n\n,\n")
        self.assertEqual(len(rows), 1)

    def test_a_row_with_only_one_cell_filled_is_still_a_row(self):
        """An address with no name is a site; the address is the name
        of it."""
        from demo import sites
        _, rows = sites.rows_from_csv("Site,Address\nA,1\n,B\n")
        self.assertEqual(len(rows), 2)

    def test_a_row_with_nothing_in_it_is_not_a_site(self):
        from demo import sites
        headers, rows = sites.rows_from_csv(
            "Site,Address,Value\nA,1,100\n,,999\n")
        built, _ = sites.build_sites(headers, rows)
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["name"], "A")

    def test_a_total_row_is_not_a_site(self):
        from demo import sites
        headers, rows = sites.rows_from_csv(
            "Site,Address,Value\nA,1,100\nTotal,,900\n")
        built, _ = sites.build_sites(headers, rows)
        self.assertEqual(len(built), 1)

    def test_every_site_has_the_same_shape(self):
        """A row with coordinates and a row without must be usable the
        same way."""
        from demo import sites
        headers, rows = sites.rows_from_csv(
            "Site,Address,Lat,Lon\nA,1,22.4,114.0\nB,2,,\n")
        built, _ = sites.build_sites(headers, rows)
        self.assertEqual(len(built), 2)
        for s in built:
            self.assertIn("lat", s)
            self.assertIn("lon", s)
        self.assertEqual(built[0]["lat"], 22.4)
        self.assertIsNone(built[1]["lat"])

    def test_only_one_of_lat_and_lon_is_not_a_position(self):
        from demo import sites
        headers, rows = sites.rows_from_csv(
            "Site,Address,Lat,Lon\nA,1,22.4,\n")
        built, _ = sites.build_sites(headers, rows)
        self.assertIsNone(built[0]["lat"])
        self.assertIsNone(built[0]["lon"])

    def test_a_date_is_normalised_whatever_the_sheet_wrote(self):
        from demo import sites
        for written, expected in [("2026-10-09", "2026-10-09"),
                                   ("30/09/2026", "2026-09-30"),
                                   ("9 Oct 2026", "2026-10-09")]:
            headers, rows = sites.rows_from_csv(
                f"Site,Address,Date\nA,1,{written}\n")
            built, _ = sites.build_sites(headers, rows)
            self.assertEqual(built[0]["valuation_date"], expected, written)

    def test_the_row_number_points_back_at_the_sheet(self):
        """Row 2 is the first row under the header."""
        from demo import sites
        headers, rows = sites.rows_from_csv("Site,Address\nA,1\nB,2\n")
        built, _ = sites.build_sites(headers, rows)
        self.assertEqual(built[0]["row"], 2)
        self.assertEqual(built[1]["row"], 3)


class TestNothingConfiguredYet(unittest.TestCase):
    """The honest empty state."""

    def test_no_sheet_means_no_sites_and_no_error(self):
        from demo import sites
        from unittest import mock
        with mock.patch.object(sites, "load_settings",
                               return_value={"url": "", "mapping": {},
                                             "overrides": {}}):
            data = sites.read_sites()
        self.assertFalse(data["configured"])
        self.assertEqual(data["sites"], [])
        self.assertEqual(data["error"], "")

    def test_an_unreadable_sheet_is_a_state_not_a_crash(self):
        from demo import sites
        from unittest import mock
        with mock.patch.object(sites, "load_settings",
                               return_value={"url": "https://example/s/x",
                                             "mapping": {}, "overrides": {}}), \
             mock.patch.object(sites, "load_sheet",
                               side_effect=RuntimeError("boom")):
            data = sites.read_sites()
        self.assertTrue(data["configured"])
        self.assertIn("boom", data["error"])
        self.assertEqual(data["sites"], [])


class TestTheThirdTab(unittest.TestCase):
    """Site Value is a tab on the map like the other two."""

    def test_the_map_has_three_tabs(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        body = TestClient(app).get("/map").text
        for label in ("Property Valuation", "Sales Brochure Review",
                      "Site Value"):
            self.assertIn(label, body)

    def test_the_third_tab_has_its_own_id(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        body = TestClient(app).get("/map").text
        self.assertIn('data-tab="sv"', body)

    def test_the_site_value_page_exists(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).get("/map/site-value")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No sheet connected yet", r.text)

    def test_the_settings_can_be_saved(self):
        from demo.app import app
        posts = {r.path for r in app.routes
                 if hasattr(r, "methods") and "POST" in (r.methods or set())}
        self.assertIn("/map/site-value", posts)

    def test_collect_returns_three_groups(self):
        from demo import mappins
        groups = mappins.collect()
        for tab in ("pv", "sbr", "sv"):
            self.assertIn(tab, groups)
            self.assertIsInstance(groups[tab], list)


class TestAPinIsNeverInvented(unittest.TestCase):
    """The map's rules apply to sites exactly as they do to everything
    else."""

    def test_a_site_with_no_coordinates_gets_no_pin(self):
        from demo import mappins
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        site = {"row": 2, "name": "A Site", "address": "1 Nowhere Road",
                "district": "Yuen Long", "client": "", "valuation_date": "",
                "site_area": "", "floor_area": "", "value": "", "rate": "",
                "use": "", "lat": None, "lon": None}
        with mock.patch("demo.sites.read_sites",
                        return_value={"sites": [site]}):
            pins = mappins._site_value_pins()
        self.assertEqual(len(pins), 1)
        self.assertIsNone(pins[0]["lat"])
        self.assertEqual(pins[0]["how"], "")
        with geo._lock:
            geo._cache.clear()

    def test_sheet_coordinates_become_a_pin(self):
        from demo import mappins
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        site = {"row": 2, "name": "A Site", "address": "1 Somewhere Road",
                "district": "", "client": "", "valuation_date": "",
                "site_area": "12000", "floor_area": "", "value": "180m",
                "rate": "", "use": "", "lat": 22.44, "lon": 114.01}
        with mock.patch("demo.sites.read_sites",
                        return_value={"sites": [site]}):
            pins = mappins._site_value_pins()
        self.assertEqual(pins[0]["lat"], 22.44)
        self.assertEqual(pins[0]["how"], "confirmed")
        self.assertEqual(pins[0]["tab"], "sv")
        with geo._lock:
            geo._cache.clear()

    def test_sheet_coordinates_never_overwrite_a_pin_the_valuer_set(self):
        """His own placement is better information than the sheet."""
        from demo import mappins
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        key = geo._clean("1 Somewhere Road")
        geo.confirm("1 Somewhere Road", 22.50, 114.10, note="by hand")
        site = {"row": 2, "name": "A Site", "address": "1 Somewhere Road",
                "district": "", "client": "", "valuation_date": "",
                "site_area": "", "floor_area": "", "value": "", "rate": "",
                "use": "", "lat": 22.44, "lon": 114.01}
        with mock.patch("demo.sites.read_sites",
                        return_value={"sites": [site]}):
            pins = mappins._site_value_pins()
        self.assertEqual(pins[0]["lat"], 22.50)
        self.assertEqual(pins[0]["lon"], 114.10)
        with geo._lock:
            geo._cache.clear()

    def test_a_site_with_neither_name_nor_address_is_skipped(self):
        from demo import mappins
        from unittest import mock
        with mock.patch("demo.sites.read_sites", return_value={"sites": [
                {"row": 2, "name": "", "address": "", "district": "",
                 "lat": 22.4, "lon": 114.0}]}):
            pins = mappins._site_value_pins()
        self.assertEqual(pins, [])


class TestThePopupShowsSiteFields(unittest.TestCase):
    """The map has to say what the site actually is."""

    def test_the_popup_mentions_area_value_and_rate(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("pin.site_area", js)
        self.assertIn("pin.value", js)
        self.assertIn("pin.rate", js)

    def test_the_popup_says_where_a_site_position_came_from(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("Position taken from your Site", js)
        self.assertIn("sheet_row", js)

    def test_sites_get_their_own_colour(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parents[1] / "demo" / "static"
               / "map.css").read_text()
        self.assertIn(".map-tag.is-sv", css)
        self.assertIn(".map-item.is-sv", css)


if __name__ == "__main__":
    unittest.main()
