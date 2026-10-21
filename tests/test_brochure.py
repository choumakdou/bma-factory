"""Sales Brochure Review — the quarterly re-examination routine."""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.brochure import (  # noqa: E402
    CHECK_ITEMS, Development, Round, STAGES,
    add_months, by_business_line, fmt_date, load_developments,
    parse_date, preceding_friday, save_developments, summarise,
)


class TestDates(unittest.TestCase):
    def test_the_deadline_is_three_calendar_months_on(self):
        self.assertEqual(add_months(date(2026, 8, 7), 3), date(2026, 11, 7))

    def test_a_short_month_clamps_to_its_last_day(self):
        """31 August plus three months is 30 November, not 3 December."""
        self.assertEqual(add_months(date(2026, 8, 31), 3), date(2026, 11, 30))

    def test_a_leap_day_clamps_to_the_28th(self):
        self.assertEqual(add_months(date(2028, 2, 29), 3), date(2028, 5, 29))

    def test_the_target_is_the_friday_before_the_deadline(self):
        # 7 November 2026 is a Saturday, so the target is the 6th.
        self.assertEqual(preceding_friday(date(2026, 11, 7)), date(2026, 11, 6))

    def test_a_friday_deadline_is_its_own_target(self):
        self.assertEqual(preceding_friday(date(2026, 10, 9)), date(2026, 10, 9))

    def test_a_monday_deadline_looks_back_to_the_friday(self):
        self.assertEqual(preceding_friday(date(2026, 11, 9)), date(2026, 11, 6))

    def test_the_whole_chain_on_bobbys_example(self):
        """7/8/2026 -> deadline 7/11/2026 -> target 6/11/2026."""
        last = parse_date("2026-08-07")
        self.assertEqual(last, date(2026, 8, 7))
        deadline = add_months(last, 3)
        self.assertEqual(fmt_date(deadline), "07/11/2026")
        self.assertEqual(fmt_date(preceding_friday(deadline)), "06/11/2026")

    def test_a_date_is_read_in_the_hong_kong_order(self):
        self.assertEqual(parse_date("07/11/2026"), date(2026, 11, 7))
        self.assertEqual(parse_date("2026-11-07"), date(2026, 11, 7))

    def test_a_bad_date_is_none_not_an_error(self):
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))

    def test_the_display_is_day_first(self):
        self.assertEqual(fmt_date(date(2026, 11, 7)), "07/11/2026")
        self.assertEqual(fmt_date(None), "")


class TestStageZero(unittest.TestCase):
    def _round(self, last="2026-08-07"):
        return Round(number=1, last_actual=last)

    def test_the_deadline_comes_from_the_last_actual(self):
        r = self._round()
        self.assertEqual(r.deadline, date(2026, 11, 7))

    def test_the_target_defaults_to_the_preceding_friday(self):
        r = self._round()
        self.assertEqual(r.default_target, date(2026, 11, 6))
        self.assertEqual(r.target_date, date(2026, 11, 6))

    def test_an_agreed_target_overrides_the_computed_one(self):
        r = self._round()
        r.target = "2026-11-04"
        self.assertEqual(r.target_date, date(2026, 11, 4))
        # the deadline is still the computed one
        self.assertEqual(r.deadline, date(2026, 11, 7))

    def test_no_last_actual_means_no_deadline(self):
        r = Round(number=1)
        self.assertIsNone(r.deadline)
        self.assertIsNone(r.target_date)

    def test_the_days_to_the_deadline_are_counted(self):
        r = self._round()
        self.assertEqual(r.days_to_deadline(date(2026, 11, 1)), 6)
        self.assertEqual(r.days_to_deadline(date(2026, 11, 10)), -3)


class TestRounds(unittest.TestCase):
    def _dev(self):
        return Development(id="la-aldea", name="La Aldea", dev_id="7825",
                           business_line="Mass housing")

    def test_the_first_round_starts_with_the_last_actual_empty(self):
        d = self._dev()
        r = d.open_round(date(2026, 8, 1))
        self.assertEqual(r.number, 1)
        self.assertEqual(r.last_actual, "")

    def test_the_second_round_carries_the_actual_upload_date(self):
        d = self._dev()
        first = d.open_round(date(2026, 8, 1))
        first.last_actual = "2026-08-07"
        first.actual = "2026-11-06"
        second = d.open_round(date(2026, 11, 1))
        # the first round's actual upload date is the second round's
        # last round's actual upload date
        self.assertEqual(second.last_actual, "2026-11-06")
        self.assertEqual(second.actual, "")

    def test_the_deadline_rolls_forward_each_round(self):
        d = self._dev()
        first = d.open_round(date(2026, 8, 1))
        first.actual = "2026-11-06"
        second = d.open_round(date(2026, 11, 1))
        self.assertEqual(second.deadline, date(2027, 2, 6))

    def test_the_item_versions_carry_forward(self):
        d = self._dev()
        first = d.open_round(date(2026, 8, 1))
        first.items["ozp"] = {
            "brochure_version": "2025-01-10", "source_version": "2026-02-01",
            "note": "S-2 sheet",
        }
        second = d.open_round(date(2026, 11, 1))
        self.assertEqual(second.items["ozp"]["brochure_version"], "2025-01-10")
        # the source version is not carried — it has to be looked at again
        self.assertEqual(second.items["ozp"].get("source_version", ""), "")

    def test_the_current_round_is_the_open_one(self):
        d = self._dev()
        first = d.open_round(date(2026, 8, 1))
        first.closed = True
        second = d.open_round(date(2026, 11, 1))
        self.assertIs(d.current_round, second)


class TestVerdicts(unittest.TestCase):
    def _round(self):
        return Round(number=1)

    def test_a_newer_source_version_means_an_update(self):
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2025-01-10", "source_version": "2026-02-01"}
        state = r.item_state(CHECK_ITEMS[2])
        self.assertEqual(state["verdict"], "update")
        self.assertEqual(state["verdict_label"], "Update needed")

    def test_the_same_version_needs_no_update(self):
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2026-02-01", "source_version": "2026-02-01"}
        self.assertEqual(r.item_state(CHECK_ITEMS[2])["verdict"], "ok")

    def test_an_older_source_version_needs_no_update(self):
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2026-03-01", "source_version": "2026-02-01"}
        self.assertEqual(r.item_state(CHECK_ITEMS[2])["verdict"], "ok")

    def test_an_unchecked_item_says_so(self):
        r = self._round()
        self.assertEqual(r.item_state(CHECK_ITEMS[0])["verdict"], "unchecked")

    def test_a_source_with_no_brochure_version_is_unknown_not_ok(self):
        """Silently saying 'no update needed' when nothing was compared
        is the one answer that must never come out of this."""
        r = self._round()
        r.items["ozp"] = {"source_version": "2026-02-01"}
        self.assertEqual(r.item_state(CHECK_ITEMS[2])["verdict"], "unknown")

    def test_every_item_carries_its_source(self):
        r = self._round()
        for item in CHECK_ITEMS:
            state = r.item_state(item)
            self.assertTrue(state["source"].startswith("https://"))
            self.assertTrue(state["source_label"])


class TestTheRoutine(unittest.TestCase):
    def test_there_are_two_stages_and_three_items(self):
        self.assertEqual(len(STAGES), 2)
        self.assertEqual(STAGES[0].number, 0)
        self.assertEqual(STAGES[1].number, 1)
        self.assertEqual(len(CHECK_ITEMS), 3)
        self.assertEqual([i.id for i in CHECK_ITEMS],
                         ["location_plan", "aerial_photo", "ozp"])

    def test_the_three_items_are_the_ones_that_change(self):
        ids = {i.id for i in CHECK_ITEMS}
        self.assertEqual(ids, {"location_plan", "aerial_photo", "ozp"})


class TestStorage(unittest.TestCase):
    def _fake_drive(self, existing_id=None):
        calls = {"create": 0, "update": 0}

        class Files:
            def list(self, **kw):
                files = [{"id": existing_id}] if existing_id else []
                return type("R", (), {"execute": lambda self, *a, **k: {"files": files}})()

            def create(self, **kw):
                calls["create"] += 1
                return type("R", (), {"execute": lambda self, *a, **k: {"id": "NEW"}})()

            def update(self, **kw):
                calls["update"] += 1
                return type("R", (), {"execute": lambda self, *a, **k: {"id": "NEW"}})()

        class Drive:
            def files(self):
                return Files()

        return Drive(), calls

    def test_a_development_survives_a_save_and_load(self):
        d = Development(id="la-aldea", name="La Aldea", dev_id="7825",
                        address="1 La Aldea", business_line="Mass housing")
        d.open_round(date(2026, 8, 1)).last_actual = "2026-08-07"
        back = Development.from_dict(d.to_dict())
        self.assertEqual(back.to_dict(), d.to_dict())

    def test_the_rules_file_is_created_when_there_is_none(self):
        from demo import brochure as B
        drive, calls = self._fake_drive()
        with mock.patch("demo.drive.app_folder", return_value="FOLDER"), \
             mock.patch("demo.oauth.get_drive", return_value=drive):
            ok, msg = B.save_developments([Development(id="a", name="A")])
        self.assertTrue(ok, msg)
        self.assertEqual(calls["create"], 1)

    def test_an_existing_file_is_replaced_not_duplicated(self):
        from demo import brochure as B
        drive, calls = self._fake_drive(existing_id="OLD")
        with mock.patch("demo.drive.app_folder", return_value="FOLDER"), \
             mock.patch("demo.oauth.get_drive", return_value=drive):
            ok, msg = B.save_developments([Development(id="a", name="A")])
        self.assertTrue(ok, msg)
        self.assertEqual(calls["update"], 1)
        self.assertEqual(calls["create"], 0)

    def test_a_failure_is_reported_not_raised(self):
        from demo import brochure as B
        with mock.patch("demo.drive.app_folder", side_effect=RuntimeError("no drive")):
            ok, msg = B.save_developments([])
        self.assertFalse(ok)
        self.assertIn("no drive", msg)

    def test_a_missing_file_reads_as_no_developments(self):
        with mock.patch("demo.drive.app_folder_if_exists", return_value=None):
            self.assertEqual(load_developments(), [])


class TestSummary(unittest.TestCase):
    def test_a_development_with_no_rounds_summarises(self):
        d = Development(id="x", name="X")
        s = summarise(d, date(2026, 8, 1))
        self.assertEqual(s["round_number"], None)
        self.assertEqual(s["deadline"], "")

    def test_the_summary_carries_the_dates(self):
        d = Development(id="x", name="X")
        d.open_round(date(2026, 8, 1)).last_actual = "2026-08-07"
        s = summarise(d, date(2026, 11, 1))
        self.assertEqual(s["last_actual"], "07/08/2026")
        self.assertEqual(s["deadline"], "07/11/2026")
        self.assertEqual(s["target"], "06/11/2026")
        self.assertEqual(s["days_to_deadline"], 6)

    def test_the_developments_group_by_business_line(self):
        devs = [
            Development(id="a", name="A", business_line="Mass housing"),
            Development(id="b", name="B", business_line="Mass housing"),
            Development(id="c", name="C", business_line="Land"),
        ]
        groups = by_business_line(devs)
        self.assertEqual([g[0] for g in groups], ["Land", "Mass housing"])
        self.assertEqual(len(groups[1][1]), 2)


class TestSources(unittest.TestCase):
    def test_the_srpe_link_carries_the_development_id(self):
        from demo.sources import srpe_development_link
        self.assertEqual(
            srpe_development_link("7825"),
            "https://www.srpe.gov.hk/opip/selected_dev?devId=7825")

    def test_the_map_service_link_carries_the_address(self):
        from demo.sources import hkms_link
        self.assertIn("address=", hkms_link("1 La Aldea"))
        self.assertIn("map-search", hkms_link(""))

    def test_the_ozp_link_is_the_viewer(self):
        from demo.sources import ozp_link
        self.assertTrue(ozp_link().startswith("https://www.ozp.tpb.gov.hk"))

    def test_the_item_sources_are_the_sites_bobby_named(self):
        self.assertTrue(CHECK_ITEMS[0].source.startswith("https://www.hkmapservice.gov.hk"))
        self.assertTrue(CHECK_ITEMS[1].source.startswith("https://www.hkmapservice.gov.hk"))
        self.assertTrue(CHECK_ITEMS[2].source.startswith("https://www.ozp.tpb.gov.hk"))


if __name__ == "__main__":
    unittest.main()


class TestEvidence(unittest.TestCase):
    """The evidence is what was found, not just a date typed in."""

    def _round(self):
        return Round(number=1)

    def test_the_latest_of_several_sheets_governs(self):
        """A development on three OZP sheets is only up to date if the
        brochure reflects the most recently gazetted of the three."""
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2021-08-20", "evidence": [
            {"ref": "S/YL-TYST/14", "date": "2021-08-20"},
            {"ref": "S/HSK/4", "date": "2026-10-09"},
            {"ref": "S/YL-PS/22", "date": "2026-05-22"},
        ]}
        st = r.item_state(CHECK_ITEMS[2])
        self.assertEqual(st["evidence_count"], 3)
        self.assertEqual(st["latest_date"], "2026-10-09")
        self.assertEqual(st["latest_ref"], "S/HSK/4")
        self.assertEqual(st["verdict"], "update")

    def test_a_brochure_ahead_of_the_source_needs_no_update(self):
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2026-11-01", "evidence": [
            {"ref": "S/HSK/4", "date": "2026-10-09"},
        ]}
        self.assertEqual(r.item_state(CHECK_ITEMS[2])["verdict"], "ok")

    def test_an_older_record_still_reads(self):
        """A check made before the evidence list existed keeps its date."""
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2025-01-01", "source_version": "2026-02-01"}
        st = r.item_state(CHECK_ITEMS[2])
        self.assertEqual(st["evidence_count"], 1)
        self.assertEqual(st["latest_date"], "2026-02-01")
        self.assertEqual(st["verdict"], "update")

    def test_evidence_with_no_date_is_still_evidence(self):
        r = self._round()
        r.items["ozp"] = {"brochure_version": "2025-01-01", "evidence": [
            {"ref": "S/HSK/4", "title": "Hung Shui Kiu and Ha Tsuen"},
        ]}
        st = r.item_state(CHECK_ITEMS[2])
        self.assertEqual(st["evidence_count"], 1)
        self.assertEqual(st["verdict"], "unchecked")

    def test_the_screenshot_travels_with_the_evidence(self):
        r = self._round()
        r.items["ozp"] = {"evidence": [
            {"ref": "S/HSK/4", "date": "2026-10-09",
             "file_id": "FILE123", "file_name": "ozp.png"},
        ]}
        st = r.item_state(CHECK_ITEMS[2])
        self.assertEqual(st["evidence"][0]["file_id"], "FILE123")
        self.assertEqual(st["evidence"][0]["file_name"], "ozp.png")

    def test_the_evidence_carries_forward_but_the_verdict_does_not(self):
        d = Development(id="x", name="X")
        first = d.open_round(date(2026, 8, 1))
        first.items["ozp"] = {
            "brochure_version": "2021-08-20",
            "source_version": "2026-10-09",
            "evidence": [{"ref": "S/HSK/4", "date": "2026-10-09"}],
        }
        second = d.open_round(date(2026, 11, 1))
        st = second.item_state(CHECK_ITEMS[2])
        # the evidence is a record of what was found, and stays
        self.assertEqual(st["evidence_count"], 1)
        # but the new round has to look again
        self.assertEqual(st["checked_at"], "")
        self.assertEqual(st["source_version"], "")

    def test_every_item_says_where_to_look(self):
        for item in CHECK_ITEMS:
            self.assertTrue(item.how_to_check, item.id)
            self.assertTrue(item.evidence_label, item.id)
            self.assertTrue(item.evidence_kind, item.id)

    def test_the_ozp_instruction_mentions_the_gazettal_date(self):
        ozp = next(i for i in CHECK_ITEMS if i.id == "ozp")
        text = " ".join(ozp.how_to_check)
        self.assertIn("Gazetted", text)
        self.assertIn("every sheet", text.lower())

    def test_the_aerial_photo_instruction_mentions_the_flight_date(self):
        ap = next(i for i in CHECK_ITEMS if i.id == "aerial_photo")
        self.assertIn("Flight Date", " ".join(ap.how_to_check))

    def test_the_location_plan_instruction_mentions_sorting_by_date(self):
        lp = next(i for i in CHECK_ITEMS if i.id == "location_plan")
        self.assertIn("By Date", " ".join(lp.how_to_check))
