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
        self.assertEqual(key, "some estate ping shan")
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
        # the bare name leads, because adding the district narrows the
        # search past the point the geocoder can see the estate
        self.assertTrue(qs[0].startswith("La Aldea"))
        self.assertIn("La Aldea, Ping Shan, Hong Kong", qs)
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

    def test_every_query_is_held_to_the_gate(self):
        """A result that does not name the estate is never accepted,
        whichever query produced it."""
        src = (Path(__file__).resolve().parents[1] / "demo" / "geo.py").read_text()
        self.assertIn("_geocode_one(query, must_contain=name)", src)


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



class TestOneBuildingIsOnePin(unittest.TestCase):
    """Bel-Air M2 is 510 King's Road in the case list and 510 KING'S RD
    in the workbook. One building, two spellings, two pins two metres
    apart — and two coordinate entries free to drift apart."""

    def test_the_two_spellings_collapse_to_one_key(self):
        from demo.mappins import _address_key
        self.assertEqual(
            _address_key("510 King's Road, Hong Kong", "HNP", "Bel-Air M2"),
            _address_key("510 KING'S RD, Hong Kong", "HNP", "Island Place Tower"))

    def test_the_key_is_lowercased_and_unpunctuated(self):
        from demo.mappins import _address_key
        self.assertEqual(
            _address_key("510 King's Road, Hong Kong", "HNP", "x"),
            "510 king s road")

    def test_the_boilerplate_tail_is_dropped(self):
        from demo.mappins import _norm_place
        for tail in (", Hong Kong", ", Hongkong", ", HONG KONG"):
            self.assertEqual(_norm_place(f"8 Ping Pak Lane{tail}"),
                             "8 ping pak lane")

    def test_a_longer_boilerplate_tail_is_dropped_too(self):
        from demo.mappins import _norm_place
        self.assertEqual(
            _norm_place("Ping Pak Lane, Yuen Long, New Territories, Hong Kong"),
            "ping pak lane yuen long new territories")

    def test_a_distinct_building_keeps_a_distinct_key(self):
        from demo.mappins import _address_key
        self.assertNotEqual(
            _address_key("8 Ping Pak Lane, Ping Shan, Yuen Long", "Ping Shan", "La Aldea"),
            _address_key("9 Ping Pak Lane, Ping Shan, Yuen Long", "Ping Shan", "Other"))

    def test_the_display_address_is_left_as_written(self):
        """Normalising the key must not rewrite what the valuer reads."""
        from demo.mappins import _pin
        import demo.geo as geo
        pin = _pin("some key", pid="p", name="Bel-Air M2",
                   address="510 KING'S RD, Hong Kong", district="HNP",
                   tab="pv", href="/case/x")
        self.assertEqual(pin["address"], "510 KING'S RD, Hong Kong")


class TestNumberedTags(unittest.TestCase):
    """The number on the pin is the number in the list."""

    def test_the_marker_carries_the_number(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("map-tag", js)
        self.assertIn("iconFor(pin, pin._n)", js)

    def test_numbering_is_by_list_position_not_by_placement_order(self):
        """The number must be stable between visits, so it is assigned
        in list order before anything is drawn."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("pins.forEach(function (pin, i)", js)
        self.assertIn("pin._n = i + 1", js)

    def test_the_list_is_numbered_the_same_way(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("pins.map(function (p, i)", js)
        self.assertIn("map-item-no", js)

    def test_a_suggestion_still_offers_confirmation_from_the_list(self):
        """Accepting a pin must not require opening the popup."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn('action="/map/confirm"', js)
        self.assertIn("if (p.how === 'geocoded')", js)

    def test_clicking_show_pin_moves_the_list_too(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("is-flash", js)
        # the row it highlights is the one carrying the same number
        self.assertIn("getElementById('item-' + (i + 1))", js)


class TestFittingTheView(unittest.TestCase):
    """All the projects visible without clicking anything."""

    def test_the_view_bar_offers_both_framings(self):
        from pathlib import Path
        tpl = (Path(__file__).resolve().parents[1] / "demo" / "templates"
               / "map.html").read_text()
        self.assertIn('data-view="fit"', tpl)
        self.assertIn('data-view="hk"', tpl)

    def test_the_default_is_whole_hong_kong(self):
        from pathlib import Path
        tpl = (Path(__file__).resolve().parents[1] / "demo" / "templates"
               / "map.html").read_text()
        # with nothing placed, the whole territory is the only honest view
        self.assertIn('data-view="hk">', tpl)
        self.assertIn("Whole of Hong Kong", tpl)

    def test_fit_uses_the_hk_bounds_as_a_fallback(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("HK_BOUNDS", js)
        self.assertIn("[22.15, 113.83]", js)
        self.assertIn("[22.60, 114.45]", js)

    def test_fit_bounds_is_capped_so_two_neighbours_do_not_zoom_in(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("maxZoom: 17", js)

    def test_one_or_no_pins_shows_the_territory_not_a_street(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("placed.length >= 2 ? 'fit' : 'hk'", js)

    def test_the_map_lib_actually_fits_rather_than_defaulting(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("fitBounds", js)


class TestTheFillHappensInTheRequest(unittest.TestCase):
    """Locations appear without Bobby clicking — but only if the work
    happens while a request is still open.

    Cloud Run suspends a container's CPU once the response has been
    sent. The first version of this used a background thread and placed
    absolutely nothing, silently: the thread was frozen a fraction of
    a second in. These tests pin the work inside the request.
    """

    def test_the_map_page_fills_one_location_itself(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from demo import app as appmod
        called = []
        with mock.patch.object(appmod, "_fill_one_missing_location",
                               side_effect=lambda g: called.append(g)):
            TestClient(appmod.app).get("/map")
        self.assertTrue(called, "the map page should place one itself")

    def test_one_page_view_only_asks_for_one(self):
        from demo import app as appmod
        self.assertEqual(appmod._WARM_BUDGET, 1)

    def test_a_deliberate_pass_has_a_much_larger_budget(self):
        from demo import app as appmod
        self.assertGreaterEqual(appmod._WARM_BUDGET_BULK, 20)

    def test_there_is_no_background_thread_any_more(self):
        """A thread started from a page view is frozen by Cloud Run.
        Its presence would be a silent no-op that looks like it works."""
        from demo import app as appmod
        self.assertFalse(hasattr(appmod, "_warm_locations_in_background"))
        src = (Path(__file__).resolve().parents[1] / "demo" / "app.py").read_text()
        self.assertNotIn("_warm_locations_in_background", src)

    def test_a_refusal_is_remembered_so_it_is_not_re_asked(self):
        """Otherwise every page view costs a second re-asking about a
        building the public map has never heard of."""
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        with mock.patch.object(geo, "geocode", return_value=None):
            self.assertIsNone(geo.suggest("refused key", "a", "d", "n"))
        self.assertTrue(geo.lookup("refused key").get("tried"))
        with geo._lock:
            geo._cache.clear()

    def test_an_already_refused_project_is_skipped_by_the_fill(self):
        """The skip is the caller's job: `warm` is told what to do, the
        page decides what is worth asking about."""
        import demo.geo as geo
        from demo import app as appmod
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        geo.remember("refused again", {"tried": "2026-01-01T00:00:00Z"})
        asked = []
        with mock.patch.object(geo, "geocode",
                               side_effect=lambda *a, **k: asked.append(1)):
            appmod._fill_one_missing_location(
                {"pv": [{"key": "refused again", "address": "a",
                         "district": "d", "name": "n", "lat": None}]})
        self.assertEqual(asked, [], "must not re-ask a refused address")
        with geo._lock:
            geo._cache.clear()

    def test_a_missing_project_is_asked_about_once(self):
        import demo.geo as geo
        from demo import app as appmod
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        asked = []
        with mock.patch.object(geo, "geocode",
                               side_effect=lambda *a, **k: asked.append(1) or None):
            appmod._fill_one_missing_location(
                {"pv": [{"key": "fresh key", "address": "a",
                         "district": "d", "name": "n", "lat": None}]})
        self.assertTrue(asked)
        with geo._lock:
            geo._cache.clear()

    def test_a_confirmed_location_is_never_re_looked_up(self):
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        geo.confirm("warm test key", 22.4, 114.0)
        called = []
        with mock.patch.object(geo, "suggest",
                               side_effect=lambda *a, **k: called.append(1)):
            out = geo.warm([("warm test key", "a", "d", "n")])
        self.assertEqual(called, [])
        self.assertEqual(out["already_had_one"], 1)
        with geo._lock:
            geo._cache.clear()

    def test_warm_respects_its_budget(self):
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        with mock.patch.object(geo, "suggest", return_value=(22.4, 114.0, "x")):
            out = geo.warm([(f"budgetk{i}", "a", "d", "n") for i in range(10)],
                           budget=3)
        self.assertEqual(out["placed"], 3)
        with geo._lock:
            geo._cache.clear()

    def test_warm_survives_a_geocoder_that_raises(self):
        import demo.geo as geo
        from unittest import mock
        with mock.patch.object(geo, "suggest", side_effect=RuntimeError("no")):
            out = geo.warm([("k", "a", "d", "n")])
        self.assertEqual(out["no_match"], 1)

    def test_the_manual_warm_route_exists_and_reports(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        from demo import geo
        from unittest import mock
        # Mocked: this route really does call the geocoder, one request
        # a second, and the suite must not spend a minute on it.
        with mock.patch.object(geo, "warm", return_value={
                "placed": 2, "no_match": 1, "already_had_one": 0}) as w:
            r = TestClient(app).post("/map/warm", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/map?saved=", r.headers["location"])
        self.assertIn("placed+2", r.headers["location"])



class TestEveryQueryIsGated(unittest.TestCase):
    """The gate must not be routeable around.

    Held only against the first query, a bad match was simply accepted
    on the second: "La Aldea, Ping Shan, Hong Kong" was correctly
    rejected for naming a house on Ping Kwai Road, and then
    "8 Ping Pak Lane, Ping Shan, Yuen Long, Hong Kong" returned that
    same wrong house and was taken. The map showed a confident pin on
    the wrong building.
    """

    def test_every_query_carries_the_name(self):
        src = (Path(__file__).resolve().parents[1] / "demo" / "geo.py").read_text()
        self.assertIn("hit = _geocode_one(query, must_contain=name)", src)
        self.assertNotIn('i == 0 and name', src)

    def test_the_bare_name_is_tried_before_the_district(self):
        """"La Aldea, Ping Shan, Hong Kong" finds a house on Ping Kwai
        Road; "La Aldea, Hong Kong" finds La Aldea. Adding the district
        narrows the search past the estate."""
        from demo.geo import _queries
        qs = _queries("8 Ping Pak Lane, Ping Shan, Yuen Long", "Ping Shan",
                      "La Aldea")
        self.assertEqual(qs[0], "La Aldea, Hong Kong")
        self.assertIn("La Aldea, Ping Shan, Hong Kong", qs)

    def test_the_real_la_aldea_match_passes_the_gate(self):
        from demo.geo import _result_is_the_right_place
        self.assertTrue(_result_is_the_right_place(
            "La Aldea, 8, Hung Uk Tsuen, Yuen Long District, "
            "New Territories, Hong Kong, China", "La Aldea"))

    def test_the_wrong_ping_kwai_road_match_fails_every_time(self):
        from demo.geo import _result_is_the_right_place
        for n in (1, 2, 3):
            self.assertFalse(_result_is_the_right_place(
                f"House {n}, Ping Kwai Road, Hung Uk Tsuen, Yuen Long "
                "District, New Territories, Hong Kong, China", "La Aldea"))


class TestRuleChangesInvalidateOldAnswers(unittest.TestCase):
    """A stored suggestion made under older rules must not survive them.

    Same principle as the parsed-workbook cache: any cache over derived
    output is keyed on a version derived from the producing code.
    """

    def test_a_record_with_no_gate_reads_as_nothing(self):
        from demo.geo import _fresh
        self.assertFalse(_fresh({"lat": 22.4, "lon": 114.0, "how": "geocoded"}))

    def test_a_record_from_an_older_gate_reads_as_nothing(self):
        import demo.geo as geo
        self.assertFalse(geo._fresh({
            "lat": 22.4, "lon": 114.0, "how": "geocoded", "gate": 0}))
        self.assertFalse(geo._fresh({
            "lat": 22.4, "lon": 114.0, "how": "geocoded",
            "gate": geo._GATE_VERSION - 1}))

    def test_a_record_from_the_current_gate_is_fresh(self):
        import demo.geo as geo
        self.assertTrue(geo._fresh({
            "lat": 22.4, "lon": 114.0, "how": "geocoded",
            "gate": geo._GATE_VERSION}))

    def test_a_confirmed_location_survives_any_rule_change(self):
        """The valuer put that pin there. No amount of rule-tuning puts
        it somewhere else."""
        import demo.geo as geo
        self.assertTrue(geo._fresh({"lat": 22.4, "lon": 114.0,
                                    "how": "confirmed"}))

    def test_a_stale_suggestion_is_treated_as_absent(self):
        """Written straight into the cache, the way a record written
        by the PREVIOUS version of the code would be — remember() now
        stamps the current gate on everything it writes."""
        import demo.geo as geo
        with geo._lock:
            geo._cache.clear()
            geo._cache["stale key"] = {"lat": 22.4, "lon": 114.0,
                                       "how": "geocoded", "gate": 0}
        self.assertEqual(geo.coords("stale key"), (None, None, ""))
        with geo._lock:
            geo._cache.clear()

    def test_a_confirmed_pin_written_the_old_way_survives(self):
        import demo.geo as geo
        with geo._lock:
            geo._cache.clear()
            geo._cache["old confirmed"] = {"lat": 22.4, "lon": 114.0,
                                           "how": "confirmed"}
        self.assertEqual(geo.coords("old confirmed")[2], "confirmed")
        with geo._lock:
            geo._cache.clear()

    def test_every_stored_thing_carries_the_gate(self):
        import demo.geo as geo
        with geo._lock:
            geo._cache.clear()
        geo.remember("gated key", {"lat": 22.4, "lon": 114.0,
                                   "how": "geocoded"})
        self.assertEqual(geo.lookup("gated key")["gate"], geo._GATE_VERSION)
        with geo._lock:
            geo._cache.clear()



class TestPlacingAPinByHand(unittest.TestCase):
    """The guarantee: any project can be located, in two clicks.

    The geocoder only knows estates that are in OpenStreetMap. Most of
    Hong Kong's smaller developments are not — Bel-Air M2 is not, and
    with the relevance gate on, it gets no pin at all. That is the safe
    answer and a useless one, so the valuer drops the pin themselves.
    """

    def test_every_project_offers_placing(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("data-place=", js)

    def test_placing_is_offered_even_for_a_confirmed_pin(self):
        """Moving a pin that is in the wrong place is as necessary as
        making one that does not exist."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("'Move pin'", js)

    def test_a_click_while_placing_confirms_that_spot(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("map.on('click'", js)
        self.assertIn("action = '/map/confirm'", js)

    def test_placing_can_be_cancelled(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        self.assertIn("data-place-cancel", js)

    def test_the_clicking_cursor_and_banner_exist(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parents[1] / "demo" / "static"
               / "map.css").read_text()
        self.assertIn("cursor: crosshair", css)
        tpl = (Path(__file__).resolve().parents[1] / "demo" / "templates"
               / "map.html").read_text()
        self.assertIn("map-placing", tpl)

    def test_a_hand_placed_pin_is_confirmed_not_suggested(self):
        """Clicking the map is the valuer's judgement, so it is recorded
        as confirmed and never re-looked-up."""
        import demo.geo as geo
        from unittest import mock
        with geo._lock:
            geo._cache.clear()
        called = []
        with mock.patch.object(geo, "geocode",
                               side_effect=lambda *a, **k: called.append(1)):
            geo.confirm("hand placed", 22.44, 114.01, note="placed by hand")
        self.assertEqual(geo.coords("hand placed")[2], "confirmed")
        geo.geocode("hand placed", "", "x")
        self.assertEqual(called, [], "a hand-placed pin is never re-looked-up")
        with geo._lock:
            geo._cache.clear()

    def test_a_hand_placed_pin_says_so(self):
        from fastapi.testclient import TestClient
        from demo.app import app
        r = TestClient(app).post(
            "/map/confirm",
            data={"key": "k2", "lat": "22.4", "lon": "114.0",
                  "note": "placed by hand on the map"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("by+hand", r.headers["location"])


class TestBelAirIsNotGuessed(unittest.TestCase):
    """The false negative we accepted, pinned down so it stays deliberate.

    Bel-Air M2 is "Bel-Air M2 (Island Place Tower)". OSM does not name
    Island Place Tower, so no result can be corroborated by name and the
    app places nothing. That is the right trade — a missing pin is
    recoverable, a wrong one is not — but it must not look like a
    bug, and "Place on map" must be there.
    """

    def test_the_name_yields_a_real_token_to_check(self):
        from demo.geo import _name_tokens
        toks = _name_tokens("Bel-Air M2 (Island Place Tower)")
        # "Island" and "Tower" are area/generic words, not identity
        self.assertNotIn("island", toks)
        self.assertNotIn("tower", toks)

    def test_a_street_level_answer_is_not_taken_as_the_building(self):
        """The answer for '510 King's Road' names King's Road and Hong
        Kong Island — neither corroborates the estate."""
        from demo.geo import _result_is_the_right_place
        answer = ("King's Road, Tin Hau, Wan Chai District, "
                  "Hong Kong Island, Hong Kong, China")
        self.assertFalse(_result_is_the_right_place(answer,
                                                    "Bel-Air M2 (Island Place Tower)"))

    def test_the_place_button_is_there_for_it(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "demo" / "static"
              / "map.js").read_text()
        # the hand-place action is unconditional
        self.assertIn("Place on map", js)
        self.assertIn("Move pin", js)


if __name__ == "__main__":
    unittest.main()