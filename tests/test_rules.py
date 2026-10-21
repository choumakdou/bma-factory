"""Section rules — the valuer's own arrangement templates."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.rules import (  # noqa: E402
    SectionRule, apply_rules, known_columns, rules_digest,
)


def _sections(headers, rows=None, sheet="S"):
    from demo import sections as S
    return [S.Section(
        id="unknown", label="", kind="table", sheet=sheet,
        header_row=1, end_row=1 + len(rows or []),
        headers=list(headers), rows=list(rows or []),
    )]


class TestMatching(unittest.TestCase):
    def test_an_exact_column_name_matches(self):
        r = SectionRule(header_contains=["MEM_NO"])
        self.assertTrue(r.matches("S", ["MEM_NO", "PRICE"], []))

    def test_a_word_inside_a_longer_column_name_matches(self):
        """A valuer writes ADOPTED; the column is ADOPTED PSF (SF)."""
        r = SectionRule(header_contains=["ADOPTED"])
        self.assertTrue(r.matches("S", ["TIME ADJ %", "ADOPTED PSF (SF)"], []))

    def test_a_word_in_the_middle_does_not_match(self):
        r = SectionRule(header_contains=["ADOPTED"])
        self.assertFalse(r.matches("S", ["NOTADOPTEDTHING"], []))

    def test_all_of_the_columns_must_be_present(self):
        r = SectionRule(header_contains=["MEM_NO", "ADOPTED"])
        self.assertFalse(r.matches("S", ["MEM_NO", "PRICE"], []))
        self.assertTrue(r.matches("S", ["MEM_NO", "ADOPTED PSF"], []))

    def test_one_of_the_columns_is_enough(self):
        r = SectionRule(header_contains_any=["ADOPTED", "WEIGHTED"])
        self.assertTrue(r.matches("S", ["MEM_NO", "WEIGHTED PSF"], []))
        self.assertFalse(r.matches("S", ["MEM_NO", "PRICE"], []))

    def test_the_sheet_can_be_narrowed(self):
        r = SectionRule(sheet="Comps", header_contains=["MEM_NO"])
        self.assertTrue(r.matches("Comps", ["MEM_NO"], []))
        self.assertFalse(r.matches("MW Extract", ["MEM_NO"], []))

    def test_a_row_can_be_the_test(self):
        r = SectionRule(row_column="MEM_NO", row_value="Subject")
        self.assertTrue(r.matches("S", ["MEM_NO", "X"], [["Subject", "1"]]))
        self.assertFalse(r.matches("S", ["MEM_NO", "X"], [["Other", "1"]]))

    def test_a_row_test_needs_both_the_column_and_the_value(self):
        r = SectionRule(row_column="MEM_NO", row_value="")
        self.assertFalse(r.matches("S", ["MEM_NO"], [["Subject"]]))

    def test_a_disabled_rule_never_matches(self):
        r = SectionRule(header_contains=["MEM_NO"], enabled=False)
        self.assertFalse(r.matches("S", ["MEM_NO"], []))


class TestApply(unittest.TestCase):
    def test_a_user_rule_wins_over_the_built_in(self):
        secs = _sections(["MEM_NO", "ADOPTED PSF (SF)"], [["1", "2"]])
        rule = SectionRule(label="My comps", kind="comps",
                           header_contains=["MEM_NO", "ADOPTED"])
        apply_rules(secs, [rule])
        self.assertEqual(secs[0].kind, "comps")
        self.assertEqual(secs[0].label, "My comps")
        self.assertEqual(secs[0].rule_origin, "your rules")

    def test_an_unclaimed_block_falls_back_to_the_built_in(self):
        secs = _sections(["MEM_NO", "ADOPTED PSF (SF)"], [["1", "2"]])
        rule = SectionRule(label="Never", kind="subject",
                           header_contains=["NO_SUCH_COLUMN"])
        apply_rules(secs, [rule])
        self.assertEqual(secs[0].rule_origin, "built-in")
        self.assertNotEqual(secs[0].kind, "subject")

    def test_the_first_rule_to_claim_a_block_wins(self):
        secs = _sections(["MEM_NO"], [["1"]])
        first = SectionRule(label="First", kind="comps", header_contains=["MEM_NO"])
        second = SectionRule(label="Second", kind="subject", header_contains=["MEM_NO"])
        apply_rules(secs, [first, second])
        self.assertEqual(secs[0].label, "First")

    def test_a_rule_claims_only_the_blocks_it_matches(self):
        secs = _sections(["MEM_NO"], [["1"]]) + _sections(["PRICE"], [["2"]], sheet="T")
        rule = SectionRule(label="Only the first", kind="comps",
                           header_contains=["MEM_NO"])
        apply_rules(secs, [rule])
        self.assertEqual(secs[0].rule_origin, "your rules")
        self.assertEqual(secs[1].rule_origin, "built-in")


class TestRoundTrip(unittest.TestCase):
    def test_a_rule_survives_a_save_and_load(self):
        r = SectionRule(
            label="Adopted comps", kind="comps", sheet="Comps",
            header_contains=["MEM_NO", "ADOPTED"],
            header_contains_any=["WEIGHTED"],
            row_column="MEM_NO", row_value="Subject", enabled=False,
        )
        back = SectionRule.from_dict(json.loads(json.dumps(r.to_dict())))
        self.assertEqual(back.to_dict(), r.to_dict())

    def test_a_comma_separated_list_becomes_a_list(self):
        r = SectionRule.from_dict({"header_contains": "MEM_NO, ADOPTED"})
        self.assertEqual(r.header_contains, ["MEM_NO", "ADOPTED"])

    def test_an_unknown_kind_falls_back_to_table(self):
        r = SectionRule.from_dict({"kind": "nonsense"})
        self.assertEqual(r.kind, "table")

    def test_the_digest_changes_when_a_rule_changes(self):
        a = [SectionRule(label="One", kind="comps", header_contains=["A"])]
        b = [SectionRule(label="One", kind="comps", header_contains=["B"])]
        self.assertNotEqual(rules_digest(a), rules_digest(b))

    def test_the_digest_is_stable_for_the_same_rules(self):
        a = [SectionRule(label="One", kind="comps", header_contains=["A"])]
        b = [SectionRule(label="One", kind="comps", header_contains=["A"])]
        self.assertEqual(rules_digest(a), rules_digest(b))


class TestKnownColumns(unittest.TestCase):
    def test_the_columns_come_back_once_each(self):
        class P:
            sections = [
                {"headers": ["MEM_NO", "PRICE"]},
                {"headers": ["PRICE", "DATE"]},
            ]
        self.assertEqual(known_columns([P()]), ["PRICE", "DATE", "MEM_NO"])


class TestTheSheetMap(unittest.TestCase):
    def test_the_map_shows_the_blocks_and_the_gaps(self):
        from demo import sheetmap as SM
        from demo.sample_extract import build_working_file_bytes
        from demo.intake import parse_workbook_bytes

        parsed = parse_workbook_bytes(build_working_file_bytes())
        smap = SM.build_map(parsed)
        self.assertEqual([s["sheet"] for s in smap], ["Subject", "Comps", "MW Extract"])
        comps = [s for s in smap if s["sheet"] == "Comps"][0]
        self.assertEqual(len(comps["blocks"]), 1)
        self.assertEqual(comps["blocks"][0]["kind"], "comps")
        self.assertEqual(comps["blocks"][0]["header_row"], 1)

    def test_a_gap_is_shown_when_a_block_does_not_start_at_row_one(self):
        from demo import sheetmap as SM
        from demo import sections as S
        secs = [S.Section(id="u", label="", kind="table", sheet="S",
                          header_row=3, end_row=5, headers=["A", "B"],
                          rows=[[1, 2], [3, 4]])]

        class Fake:
            sheets = ["S"]
            sections = secs
            sheet_grid = {}
            sheet_sizes = {"S": 6}

        smap = SM.build_map(Fake())
        kinds = [(s["kind"], s["from"], s["to"]) for s in smap[0]["band"]]
        self.assertIn(("gap", 1, 2), kinds)
        self.assertIn(("table", 3, 5), kinds)

    def test_the_map_takes_a_dict_as_well_as_an_object(self):
        from demo import sheetmap as SM
        as_dict = {
            "sheets": ["S"],
            "sections": [{"sheet": "S", "kind": "comps", "label": "C",
                          "header_row": 1, "end_row": 3,
                          "headers": ["A"], "rows": [[1], [2]]}],
            "sheet_grid": {},
            "sheet_sizes": {"S": 3},
        }
        smap = SM.build_map(as_dict)
        self.assertEqual(smap[0]["blocks"][0]["kind"], "comps")

    def test_the_preview_tints_each_row_by_its_section(self):
        from demo import sheetmap as SM
        from demo.sample_extract import build_working_file_bytes
        from demo.intake import parse_workbook_bytes

        parsed = parse_workbook_bytes(build_working_file_bytes())
        smap = SM.build_map(parsed)
        rows = SM.preview_rows(smap, "Comps")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["row"], 1)
        self.assertEqual(rows[0]["kind"], "comps")

    def test_the_column_letters_run_a_to_z_then_aa(self):
        from demo.sheetmap import column_letters
        self.assertEqual(column_letters(2), ["A", "B"])
        self.assertEqual(column_letters(26)[-1], "Z")
        self.assertEqual(column_letters(27)[-1], "AA")


if __name__ == "__main__":
    unittest.main()


class TestStorage(unittest.TestCase):
    """The rules live in the valuer's Drive."""

    def _fake_drive(self, existing_id=None):
        calls = {"list": 0, "create": 0, "update": 0}

        class Files:
            def list(self, **kw):
                calls["list"] += 1
                files = [{"id": existing_id, "name": "section-rules.json"}] if existing_id else []
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

    def test_a_new_rules_file_is_created(self):
        from demo import rules as R
        drive, calls = self._fake_drive()
        with mock.patch("demo.drive.app_folder", return_value="FOLDER"), \
             mock.patch("demo.oauth.get_drive", return_value=drive):
            ok, msg = R.save_rules([SectionRule(label="One", kind="comps")])
        self.assertTrue(ok, msg)
        self.assertEqual(calls["create"], 1)
        self.assertEqual(calls["update"], 0)

    def test_an_existing_rules_file_is_replaced_not_duplicated(self):
        from demo import rules as R
        drive, calls = self._fake_drive(existing_id="OLD")
        with mock.patch("demo.drive.app_folder", return_value="FOLDER"), \
             mock.patch("demo.oauth.get_drive", return_value=drive):
            ok, msg = R.save_rules([SectionRule(label="One", kind="comps")])
        self.assertTrue(ok, msg)
        self.assertEqual(calls["update"], 1)
        self.assertEqual(calls["create"], 0)

    def test_a_failure_is_reported_not_raised(self):
        from demo import rules as R
        with mock.patch("demo.drive.app_folder", side_effect=RuntimeError("no drive")):
            ok, msg = R.save_rules([SectionRule(label="One", kind="comps")])
        self.assertFalse(ok)
        self.assertIn("no drive", msg)
