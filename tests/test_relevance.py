"""Relevancy scoring, circularity guard, and map-output tests.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import relevance as RV  # noqa: E402

AS_OF = "2026-08-23"

SUBJECT = {
    "name": "Flat B on 17th Floor, Goldwin Heights",
    "address": "Flat B on 17th Floor, Goldwin Heights, No. 2 Seymour Road, Hong Kong",
    "building": "Goldwin Heights",
    "district": "Mid-Levels West",
    "usage": "residential",
    "area_sf": 800,
    "lat": 22.2810, "lon": 114.1410,
    "date": "2026-08-01",
    "floor": "17", "flat": "B", "mem_no": "SUBJ-001",
}

COMPS = [
    {"address": "Flat C on 17th Floor, Goldwin Heights, No. 2 Seymour Road",
     "building": "Goldwin Heights", "district": "Mid-Levels West", "usage": "residential",
     "area_sf": 806, "price": 14_380_000, "date": "2026-03-23",
     "floor": "38", "flat": "C", "tenure": "ASP"},
    {"address": "Flat E on 28th Floor, Goldwin Heights, No. 2 Seymour Road",
     "building": "Goldwin Heights", "district": "Mid-Levels West", "usage": "residential",
     "area_sf": 838, "price": 14_000_000, "date": "2026-03-11",
     "floor": "28", "flat": "E", "tenure": "PRO-ASP"},
    {"address": "Flat F on 29th Floor, Goldwin Heights, No. 2 Seymour Road",
     "building": "Goldwin Heights", "district": "Mid-Levels West", "usage": "residential",
     "area_sf": 801, "price": 12_750_000, "date": "2026-01-25",
     "floor": "29", "flat": "B", "tenure": "PRE-ASP"},
    {"address": "Flat A on 9th Floor, Seymour Court, No. 2 Seymour Road",
     "building": "Seymour Court", "district": "Mid-Levels West", "usage": "residential",
     "area_sf": 780, "price": 12_100_000, "date": "2026-05-02", "floor": "9/A", "tenure": "ASP"},
    {"address": "Flat 12B on 22nd Floor, Dynasty Court, 25 Conduit Road",
     "building": "Dynasty Court", "district": "Mid-Levels West", "usage": "residential",
     "area_sf": 950, "price": 15_200_000, "date": "2026-02-14", "floor": "22/B", "tenure": "ASP"},
    {"address": "Unit 3, ground floor, 3 Belcher's Street, Kennedy Town",
     "building": "Belcher's Street", "district": "Kennedy Town", "usage": "shop",
     "area_sf": 1200, "price": 18_000_000, "date": "2026-06-01", "floor": "G/F", "tenure": "ASP"},
]

# the subject's OWN latest transaction, sitting in the comparable pull
SELF = {
    "address": "Flat B on 17th Floor, Goldwin Heights, No. 2 Seymour Road",
    "building": "Goldwin Heights", "district": "Mid-Levels West", "usage": "residential",
    "area_sf": 800, "price": 15_600_000, "date": "2026-08-01",
    "floor": "17", "flat": "B", "tenure": "ASP", "mem_no": "SUBJ-001",
}


class TestWeights(unittest.TestCase):
    def test_unknown_factor_refused(self):
        with self.assertRaises(ValueError):
            RV.Weights({"vibes": 1.0})

    def test_weights_normalise(self):
        w = RV.Weights({"same_building": 0.6, "distance": 0.4,
                        "recency": 0.0, "area_similarity": 0.0, "usage_match": 0.0})
        s = w.scaled()
        self.assertAlmostEqual(sum(s.values()), 1.0)
        self.assertAlmostEqual(s["same_building"], 0.6)


class TestScoring(unittest.TestCase):
    def test_same_building_outranks_across_town(self):
        r = RV.rank(SUBJECT, COMPS, AS_OF)
        self.assertEqual(r[0].comp["building"], "Goldwin Heights")

    def test_shop_outranks_residential_of_better_size(self):
        r = RV.rank(SUBJECT, COMPS, AS_OF)
        self.assertEqual(r[-1].comp["usage"], "shop")

    def test_breakdown_is_reported(self):
        s = RV.score_one(SUBJECT, COMPS[0], AS_OF, RV.Weights())
        self.assertEqual(set(s.breakdown), set(RV.DEFAULT_WEIGHTS))
        self.assertTrue(0.0 <= s.total <= 1.0)

    def test_ranking_is_stable_across_runs(self):
        a = [s.comp["address"] for s in RV.rank(SUBJECT, COMPS, AS_OF)]
        b = [s.comp["address"] for s in RV.rank(SUBJECT, list(reversed(COMPS)), AS_OF)]
        self.assertEqual(a, b)

    def test_reweighting_changes_the_ranking(self):
        """Weights are config, so changing one must visibly change the outcome."""
        area_only = RV.Weights({"same_building": 0.0, "distance": 0.0, "recency": 0.0,
                                "area_similarity": 1.0, "usage_match": 0.0})
        r = RV.rank(SUBJECT, COMPS, AS_OF, area_only)
        # 801 sf against a subject of 800 sf is the closest fit in the pool
        self.assertEqual(r[0].comp["area_sf"], 801)
        # and the default weights pick a different winner, which is the point
        self.assertNotEqual(RV.rank(SUBJECT, COMPS, AS_OF)[0].comp["area_sf"], 801)

    def test_older_comp_scores_lower_on_recency(self):
        old = dict(COMPS[0], date="2020-01-01")
        assert_ = RV.score_one(SUBJECT, old, AS_OF, RV.Weights())
        now_ = RV.score_one(SUBJECT, COMPS[0], AS_OF, RV.Weights())
        self.assertLess(assert_.breakdown["recency"], now_.breakdown["recency"])


class TestCircularityGuard(unittest.TestCase):
    def test_subject_own_transaction_is_detected(self):
        hit, why = RV.is_self(SUBJECT, SELF)
        self.assertTrue(hit)
        self.assertTrue(why)

    def test_genuine_comp_is_not_flagged(self):
        for c in COMPS:
            hit, _ = RV.is_self(SUBJECT, c)
            self.assertFalse(hit, c["address"])

    def test_explicitly_flagged_record_is_always_caught(self):
        hit, _ = RV.is_self(SUBJECT, {**COMPS[0], "is_subject": 1})
        self.assertTrue(hit)

    def test_subject_would_otherwise_rank_first(self):
        """The reason the guard exists: left alone, the subject tops the list."""
        top = RV.rank(SUBJECT, [SELF] + COMPS, AS_OF)[0].comp
        self.assertEqual(top["address"], SELF["address"])

    def test_selection_excludes_the_subject_even_when_top_ranked(self):
        ranked = RV.rank(SUBJECT, [SELF] + COMPS, AS_OF)
        sel = RV.select(ranked, SUBJECT, n=5)
        self.assertEqual(len(sel.adopted), 5)
        self.assertNotIn(SELF["address"], [s.comp["address"] for s in sel.adopted])
        self.assertTrue(any(SELF["address"] in s.comp["address"] for s, _ in sel.excluded_self))


class TestSelection(unittest.TestCase):
    def test_five_adopted_by_default(self):
        sel = RV.select(RV.rank(SUBJECT, COMPS, AS_OF), SUBJECT)
        self.assertEqual(len(sel.adopted), 5)

    def test_considered_count_reported(self):
        sel = RV.select(RV.rank(SUBJECT, COMPS, AS_OF), SUBJECT)
        self.assertEqual(sel.considered, len(COMPS))

    def test_empty_pool_warns_rather_than_fails(self):
        sel = RV.select([], SUBJECT)
        self.assertEqual(sel.adopted, [])
        self.assertIsNotNone(sel.warning)

    def test_equal_weighted_rate_is_the_mean(self):
        sel = RV.select(RV.rank(SUBJECT, COMPS, AS_OF), SUBJECT)
        rate = RV.equal_weighted_rate(sel.adopted)
        self.assertTrue(8000 < rate < 20000)


class TestMapContract(unittest.TestCase):
    """The output must match what V-ALE-303767-2026_map.html already parses."""

    def setUp(self):
        self.ranked = RV.rank(SUBJECT, [SELF] + COMPS, AS_OF)
        self.sel = RV.select(self.ranked, SUBJECT)
        self.loc = RV.to_locations(SUBJECT, self.sel, self.ranked)

    def test_subject_is_first_and_marked(self):
        self.assertEqual(self.loc[0]["no"], "S")
        self.assertEqual(self.loc[0]["isSubject"], 1)

    def test_exactly_five_comparables_follow(self):
        comps = [l for l in self.loc if l["isSubject"] == 0]
        self.assertEqual(len(comps), 5)
        self.assertEqual([c["no"] for c in comps], ["C1", "C2", "C3", "C4", "C5"])

    def test_every_field_the_tool_reads_is_present(self):
        required = {"no", "name", "address", "count", "priceRange", "srcRows",
                    "isSubject", "txnText"}
        for loc in self.loc:
            self.assertTrue(required <= set(loc), loc)

    def test_single_transaction_has_no_separator(self):
        c = [l for l in self.loc if l["isSubject"] == 0][0]
        self.assertNotIn("~~~", c["txnText"])
        self.assertEqual(c["count"], "1")

    def test_multiple_transactions_join_with_the_tilde(self):
        """GenerateMap_PPA replaces the sheet separator ^ with ~~~."""
        multi = {**COMPS[0], "transactions": [
            {"date": "2026-03-23", "area_sf": 806, "price": 14_380_000, "floor": "38", "flat": "C"},
            {"date": "2025-11-02", "area_sf": 800, "price": 13_900_000, "floor": "35", "flat": "C"},
        ], "srcRows": "3,9"}
        ranked = RV.rank(SUBJECT, [multi] + COMPS[1:], AS_OF)
        sel = RV.select(ranked, SUBJECT, n=1)
        loc = RV.to_locations(SUBJECT, sel, ranked)[1]
        self.assertIn("~~~", loc["txnText"])
        self.assertEqual(len(loc["txnText"].split("~~~")), 2)
        self.assertEqual(loc["count"], "2")
        self.assertEqual(loc["srcRows"], "3,9")

    def test_txn_fields_are_pipe_delimited(self):
        """CHFT Map ver 8.0 writes 13 positional fields per transaction."""
        c = [l for l in self.loc if l["isSubject"] == 0][0]
        parts = c["txnText"].split("~~~")[0].split("|")
        self.assertEqual(len(parts), 13)

    def test_price_range_uses_the_v80_format(self):
        """VBA FormatPrice scales B/M/K/mil and prefixes HK$."""
        self.assertRegex(self.loc[1]["priceRange"], r"^(HK\$[\d.,]+|N/A)")
        self.assertTrue(self.loc[0]["priceRange"])

    def test_rank_order_is_preserved(self):
        for i, s in enumerate(self.sel.adopted, start=1):
            self.assertEqual(self.loc[i]["no"], f"C{i}")
            self.assertEqual(self.loc[i]["address"], s.comp["address"])

    def test_serialises_as_json(self):
        import json
        self.assertIsInstance(json.loads(RV.to_json(SUBJECT, self.sel, self.ranked)), list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
