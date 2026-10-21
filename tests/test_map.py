"""Map view tests.

The map is the one page that could quietly lie. A pin in the wrong
place looks exactly as authoritative as a pin in the right one, and a
valuer who trusts it puts a comparable on the wrong side of the
harbour. So most of what follows is about what the map must *not* do:

- it must never geocode while rendering a page;
- it must never place a project it has no coordinates for;
- it must never show a suggestion as though it were confirmed.

The geocoder's own behaviour gets checked too, because two of its
properties are the opposite of what its documentation implies.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="vf-map-"))
os.environ["VF_DB"] = str(_TMP / "ledger.sqlite3")
os.environ["VF_OUTPUT_DIR"] = str(_TMP / "outputs")


class _NoNetwork(unittest.TestCase):
    """Base: any outbound HTTP during a test fails loudly.

    ``urllib.request.urlopen`` is the only way geo.py reaches the
    geocoder, so blocking that is enough — and unlike replacing
    ``socket.socket`` it leaves the test client's own event loop
    working.
    """

    def setUp(self):
        import urllib.request
        self._real_urlopen = urllib.request.urlopen

        def _blocked(*a, **kw):
            raise AssertionError(
                "the map page must not touch the network while rendering")

        urllib.request.urlopen = _blocked
        self.addCleanup(setattr, urllib.request, "urlopen",
                        self._real_urlopen)


class TestNoGeocodingOnRender(_NoNetwork):
    """Rendering the map must never call the public geocoder.

    This is a regression guard. An earlier version of `_pin` called
    geo.resolve(), which geocodes — so one page view became a series of
    one-per-second network calls and the request timed out.
    """

    def test_the_page_renders_with_no_network(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).get("/map")
        self.assertEqual(r.status_code, 200)

    def test_the_api_renders_with_no_network(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).get("/api/map")
        self.assertEqual(r.status_code, 200)

    def test_building_a_pin_does_not_reach_the_geocoder(self):
        import demo.geo as geo
        import demo.mappins as mp

        def _boom(*a, **kw):
            raise AssertionError("_pin must not geocode")

        for name in ("geocode", "resolve"):
            setattr(geo, name, _boom)
        try:
            pin = mp._pin("some address", pid="p", name="n", address="a",
                          district="d", tab="pv", href="/x")
        finally:
            import importlib
            importlib.reload(geo)
            importlib.reload(mp)
        self.assertIsNone(pin["lat"])

    def test_mappins_does_not_import_resolve_into_its_own_namespace(self):
        """A future edit must not re-bind the network path into this
        module, where _pin would pick it up without anyone noticing."""
        src = (Path(__file__).resolve().parents[1] / "demo" / "mappins.py")
        text = src.read_text()
        self.assertNotIn("from demo.geo import resolve", text)
        self.assertNotIn("geo.resolve(", text)


class TestAPinIsNeverInvented(unittest.TestCase):
    """A project with no coordinates is unplaced, not approximately placed."""

    def test_no_address_no_pin(self):
        from demo.mappins import _address_key
        self.assertEqual(_address_key("", "", ""), "")

    def test_an_empty_key_produces_no_location(self):
        import demo.geo as geo
        self.assertEqual(geo.coords(""), (None, None, ""))

    def test_an_unknown_address_has_no_coordinates(self):
        import demo.geo as geo
        geo.forget("a street nobody has heard of")
        self.assertEqual(geo.coords("a street nobody has heard of"),
                         (None, None, ""))

    def test_a_project_with_no_address_is_still_listed(self):
        """It must appear in the side panel with 'Find on map', not
        silently disappear from the module's list."""
        import demo.geo as geo
        from demo.mappins import _address_key
        # A development with no address at all is skipped by collect(),
        # but a development WITH a name is keyed and therefore listed.
        key = _address_key("", "Ping Shan", "Some Estate")
        self.assertEqual(key, "Some Estate, Ping Shan")
        self.assertIsNone(geo.coords(key)[0])


class TestConfirmedVersusSuggested(unittest.TestCase):
    """The two states must stay distinguishable all the way to the page."""

    def setUp(self):
        import demo.geo as geo
        geo.forget("test key")

    def test_a_confirmed_pin_reports_itself_as_confirmed(self):
        import demo.geo as geo
        geo.confirm("test key", 22.44, 114.01, note="stood at the gate")
        lat, lon, how = geo.coords("test key")
        self.assertEqual((lat, lon), (22.44, 114.01))
        self.assertEqual(how, "confirmed")

    def test_a_confirmed_location_is_never_re_geocoded(self):
        """Bobby's pin outranks the public database, permanently."""
        import demo.geo as geo
        geo.confirm("test key", 22.5, 114.2)
        # geocode() returns None for a confirmed key rather than
        # overwriting it with the geocoder's answer.
        self.assertIsNone(geo.geocode("test key", "", ""))

    def test_a_confirmed_pin_survives_a_re_geocode_attempt(self):
        import demo.geo as geo
        geo.confirm("test key", 22.5, 114.2)
        geo.geocode("test key")
        self.assertEqual(geo.coords("test key")[2], "confirmed")
        self.assertEqual(geo.coords("test key")[0], 22.5)

    def test_the_pin_markup_distinguishes_the_two(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("is-confirmed", js)
        self.assertIn("is-geocoded", js)
        # a suggestion must offer confirmation, and a confirmed pin
        # must not
        self.assertIn("data-confirm", js)

    def test_a_geocoded_pin_says_where_it_came_from(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("Suggested from OpenStreetMap", js)

    def test_status_counts_the_two_separately(self):
        import demo.geo as geo
        geo.remember("k1", {"lat": 22.4, "lon": 114.0, "how": "confirmed"})
        geo.remember("k2", {"lat": 22.5, "lon": 114.1, "how": "geocoded"})
        s = geo.status()
        self.assertGreaterEqual(s["confirmed"], 1)
        self.assertGreaterEqual(s["geocoded"], 1)


class TestGeocoderBehaviour(unittest.TestCase):
    """Two properties of the public geocoder that contradict the docs."""

    def test_countrycodes_is_not_used(self):
        """countrycodes=hk returns nothing at all for Hong Kong. It is
        accepted, and it silently filters every real result away.

        Checked in the url-building call, not the whole file — the
        docstring deliberately names the trap so nobody re-adds it.
        """
        src = (Path(__file__).resolve().parents[1] / "demo" / "geo.py").read_text()
        # the urlencode dict is the only place a query parameter is set
        start = src.find("urlencode({")
        self.assertGreater(start, 0)
        chunk = src[start:src.find("})", start)]
        self.assertNotIn("countrycodes", chunk)

    def test_results_outside_hong_kong_are_rejected(self):
        import demo.geo as geo
        self.assertTrue(geo._in_hk(22.32, 114.17))
        self.assertTrue(geo._in_hk(22.15, 113.83))
        # Singapore, Alaska and the middle of the Atlantic are not
        self.assertFalse(geo._in_hk(1.35, 103.82))
        self.assertFalse(geo._in_hk(61.2, -149.9))
        self.assertFalse(geo._in_hk(0.0, 0.0))

    def test_the_hk_box_is_generous_enough_for_the_territory(self):
        import demo.geo as geo
        # Sha Tau Kok in the far north-east, and the western edge
        # near Deep Bay, must both be inside the box.
        self.assertTrue(geo._in_hk(22.55, 114.43))
        self.assertTrue(geo._in_hk(22.20, 113.85))

    def test_queries_try_the_estate_name_first(self):
        from demo.geo import _queries
        qs = _queries("8 Ping Pak Lane", "Ping Shan", "La Aldea")
        self.assertEqual(qs[0], "La Aldea, Ping Shan, Hong Kong")
        self.assertIn("8 Ping Pak Lane, Ping Shan, Hong Kong", qs)

    def test_an_address_already_saying_hong_kong_is_not_doubled(self):
        from demo.geo import _queries
        qs = _queries("510 King's Road, Hong Kong", "HNP", "Bel-Air")
        for q in qs:
            self.assertNotIn("Hong Kong, Hong Kong", q)

    def test_queries_are_deduplicated(self):
        from demo.geo import _queries
        qs = _queries("Ping Shan, Hong Kong", "Ping Shan", "Ping Shan")
        self.assertEqual(len(qs), len(set(q.lower() for q in qs)))

    def test_a_project_with_only_a_name_still_gets_a_query(self):
        from demo.geo import _queries
        self.assertTrue(_queries("", "", "La Aldea"))

    def test_the_user_agent_is_identifiable(self):
        """Nominatim's fair-use policy requires one. Without it the
        public instance will refuse us."""
        import demo.geo as geo
        self.assertIn("bobby", geo._USER_AGENT.lower())
        self.assertGreater(geo._MIN_INTERVAL, 1.0)

    def test_a_retry_gives_up_rather_than_hammering(self):
        import demo.geo as geo
        self.assertIsNone(geo._geocode_one(""))


class TestConfirmValidation(unittest.TestCase):
    """A confirmed pin outranks everything, so a typo in it is serious."""

    def test_coordinates_off_the_globe_are_refused(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        c = TestClient(app)
        for lat, lon in [("91", "0"), ("0", "181"), ("-91", "0")]:
            r = c.post("/map/confirm", data={"key": "k", "lat": lat, "lon": lon},
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("not+on+Earth", r.headers["location"])

    def test_non_numeric_coordinates_are_refused(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).post("/map/confirm",
                                 data={"key": "k", "lat": "abc", "lon": "1"},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("No+coordinates", r.headers["location"])

    def test_a_confirm_with_no_project_is_refused(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).post("/map/confirm",
                                 data={"key": "", "lat": "22.4", "lon": "114"},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("No+project", r.headers["location"])


class TestLocateIsDeliberate(unittest.TestCase):
    """Locating is an action, not something a page view triggers."""

    def test_locate_with_nothing_to_look_up_is_refused(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).post("/map/locate", data={}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Nothing+to+look+up", r.headers["location"])

    def test_the_locate_route_is_a_post(self):
        from demo.app import app
        gets = {r.path for r in app.routes
                if hasattr(r, "methods") and "GET" in (r.methods or set())}
        self.assertNotIn("/map/locate", gets)

    def test_locate_says_so_when_the_geocoder_has_nothing(self):
        """A miss is reported, never silently rendered as a pin."""
        from fastapi.testclient import TestClient
        from unittest import mock
        from demo.app import app
        import demo.geo as geo
        with mock.patch.object(geo, "suggest", return_value=None):
            r = TestClient(app).post(
                "/map/locate",
                data={"key": "k", "address": "nowhere at all",
                      "district": "", "name": ""},
                follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("No+reliable+match", r.headers["location"])


class TestARelevanceGate(unittest.TestCase):
    """The geocoder invents answers, and they are inside Hong Kong.

    Asking for ``La Aldea, Ping Shan, Hong Kong`` returns ``House 2,
    Ping Kwai Road, Hung Uk Tsuen`` — and a *different* house for every
    spelling of the query. The bounding box does not catch it, because
    Hung Uk Tsuen is in Hong Kong. These tests pin that shut.
    """

    # The actual response the public geocoder gave for La Aldea.
    WRONG = ("House 2, Ping Kwai Road, Hung Uk Tsuen, Yuen Long District, "
             "New Territories, Hong Kong, China")

    def test_the_wrong_match_for_la_aldea_is_rejected(self):
        from demo.geo import _result_is_the_right_place
        self.assertFalse(_result_is_the_right_place(self.WRONG, "La Aldea"))

    def test_a_genuine_match_is_accepted(self):
        from demo.geo import _result_is_the_right_place
        self.assertTrue(_result_is_the_right_place(
            "La Aldea, 8 Ping Pak Lane, Ping Shan, Yuen Long, Hong Kong",
            "La Aldea"))

    def test_a_query_with_no_name_is_not_name_checked(self):
        """An address-only query has nothing distinctive to check."""
        from demo.geo import _result_is_the_right_place
        self.assertTrue(_result_is_the_right_place("anything at all", ""))

    def test_a_name_that_is_all_noise_is_not_checked(self):
        """'The Estate' tells us nothing, so it must not reject
        everything — that would make every development unlocatable."""
        from demo.geo import _result_is_the_right_place
        for noise in ("The Estate", "Tower", "Phase 1", ""):
            self.assertTrue(
                _result_is_the_right_place(self.WRONG, noise), noise)

    def test_matching_ignores_case_and_accents(self):
        from demo.geo import _result_is_the_right_place
        self.assertTrue(_result_is_the_right_place(
            "la aldea, ping shan, hong kong", "La Aldea"))
        self.assertTrue(_result_is_the_right_place(
            "La Aldea 南屏匯, Ping Shan", "La Aldea"))

    def test_only_the_name_query_is_held_to_the_gate(self):
        """Address queries legitimately return street addresses."""
        src = (Path(__file__).resolve().parents[1] / "demo" / "geo.py").read_text()
        self.assertIn("must_contain = name if (i == 0 and name) else \"\"",
                      src)


class TestASuggestionIsFoundAgain(unittest.TestCase):
    """A successful lookup must be visible to the map.

    The geocoder caches under the query it ran; the map looks the
    project up by its address. Storing only under the query meant the
    pin never appeared — the lookup succeeded and the suggestion was
    invisible.
    """

    def test_suggest_files_under_the_projects_own_key(self):
        import demo.geo as geo
        from unittest import mock
        project_key = "8 Ping Pak Lane, Ping Shan, Yuen Long"
        with mock.patch.object(geo, "geocode",
                               return_value=(22.44, 114.01, "La Aldea")):
            got = geo.suggest(project_key,
                              "8 Ping Pak Lane, Ping Shan, Yuen Long",
                              "Ping Shan", "La Aldea")
        self.assertIsNotNone(got)
        lat, lon, how = geo.coords(project_key)
        self.assertEqual((lat, lon), (22.44, 114.01))
        self.assertEqual(how, "geocoded")
        geo.forget(project_key)

    def test_suggest_returns_nothing_when_the_geocoder_has_nothing(self):
        import demo.geo as geo
        from unittest import mock
        with mock.patch.object(geo, "geocode", return_value=None):
            self.assertIsNone(geo.suggest("k", "nowhere", "", "Nothing"))
        self.assertEqual(geo.coords("k")[0], None)


class TestPageShape(_NoNetwork):
    """The page itself."""

    def test_the_map_page_carries_both_tabs(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        body = TestClient(app).get("/map").text
        self.assertIn("Property Valuation", body)
        self.assertIn("Sales Brochure Review", body)

    def test_it_uses_openstreetmap_not_a_google_tile_url(self):
        """No API key, no billing, nothing that expires."""
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("tile.openstreetmap.org", js)
        self.assertNotIn("maps.googleapis.com", js)
        self.assertNotIn("google.com/maps", js)

    def test_the_nav_offers_the_map(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        for path in ["/", "/brochure", "/intake"]:
            body = TestClient(app).get(path).text
            self.assertIn('href="/map"', body, path)

    def test_the_payload_is_one_json_encode_not_two(self):
        """Double-encoding produced a string where the JS wanted an
        object, and the map silently rendered nothing."""
        from pathlib import Path
        tpl = (Path(__file__).resolve().parents[1] / "demo" / "templates"
               / "map.html").read_text()
        self.assertIn("map_json", tpl)
        self.assertNotIn("tojson|tojson", tpl)

    def test_the_payload_parses_as_an_object(self):
        import json as _json
        from fastapi.testclient import TestClient
        from demo.app import app
        body = TestClient(app).get("/map").text
        start = body.find('<script id="map-pins"')
        chunk = body[body.find(">", start) + 1: body.find("</script>", start)]
        data = _json.loads(chunk)
        self.assertIsInstance(data, dict)
        self.assertIn("groups", data)

    def test_every_pin_has_a_key_and_a_href(self):
        import json as _json
        from fastapi.testclient import TestClient
        from demo.app import app
        data = _json.loads(TestClient(app).get("/api/map").text)
        for pins in data["groups"].values():
            for p in pins:
                self.assertTrue(p.get("key"), p)
                self.assertTrue(p.get("href"), p)
                # a located pin has both numbers or neither
                if p.get("lat") is None:
                    self.assertIsNone(p.get("lon"), p)
                else:
                    self.assertIsNotNone(p.get("lon"), p)


if __name__ == "__main__":
    unittest.main()