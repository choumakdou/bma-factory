"""Property-rules tests.

The behaviours that matter are the refusals and the governance signals, not the
filtering arithmetic. A rules store that silently accepts a pinned time
adjustment, or that makes a carried-forward rule look freshly reviewed, is worse
than no rules store at all — it launders a stale judgement into a clean-looking
number.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import rules as R  # noqa: E402


def fresh() -> tuple[sqlite3.Connection, int]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE properties (id INTEGER PRIMARY KEY);
        INSERT INTO properties (id) VALUES (1);
        """
    )
    conn.executescript(R.SCHEMA_SQL)
    return conn, 1


def seed(conn, pid, **over):
    kw = dict(
        rule_type=R.RULE_ADJUSTMENT_OVERRIDE,
        value={"adj": "location", "pct": -0.04},
        rationale="backs onto the refuse transfer station",
        effective_from="2023-01-01",
        set_by="BMA",
        last_reviewed="2024-01-01",
    )
    kw.update(over)
    return R.add_rule(conn, pid, **kw)


class TestGuardrails(unittest.TestCase):
    def setUp(self):
        self.conn, self.pid = fresh()

    def test_time_adjustment_cannot_be_pinned(self):
        with self.assertRaises(ValueError) as c:
            seed(self.conn, self.pid, value={"adj": "time", "pct": -0.05})
        self.assertIn("time-varying", str(c.exception))

    def test_date_adjustment_cannot_be_pinned(self):
        with self.assertRaises(ValueError):
            seed(self.conn, self.pid, value={"adj": "date", "pct": 0.02})

    def test_rationale_is_mandatory(self):
        with self.assertRaises(ValueError) as c:
            seed(self.conn, self.pid, rationale="  ")
        self.assertIn("rationale", str(c.exception))

    def test_unknown_rule_type_refused(self):
        with self.assertRaises(ValueError):
            seed(self.conn, self.pid, rule_type="vibes")

    def test_allowlist_needs_buildings(self):
        with self.assertRaises(ValueError):
            R.add_rule(self.conn, self.pid, R.RULE_COMP_ALLOWLIST, {},
                       rationale="narrow it", effective_from="2023-01-01")

    def test_override_needs_a_percentage(self):
        with self.assertRaises(ValueError):
            seed(self.conn, self.pid, value={"adj": "location"})


class TestEffectiveDating(unittest.TestCase):
    def setUp(self):
        self.conn, self.pid = fresh()

    def test_rule_not_in_force_before_its_start(self):
        seed(self.conn, self.pid, effective_from="2025-01-01")
        self.assertEqual(R.effective_rules(self.conn, self.pid, "2024-06-01"), [])
        self.assertEqual(len(R.effective_rules(self.conn, self.pid, "2025-06-01")), 1)

    def test_ended_rule_drops_out(self):
        rid = seed(self.conn, self.pid, effective_from="2023-01-01")
        R.end_rule(self.conn, rid, "2025-01-01")
        self.assertEqual(len(R.effective_rules(self.conn, self.pid, "2024-06-01")), 1)
        self.assertEqual(R.effective_rules(self.conn, self.pid, "2025-06-01"), [])

    def test_prior_year_returns_that_years_rules(self):
        seed(self.conn, self.pid, effective_from="2023-01-01")
        rid = R.add_rule(self.conn, self.pid, R.RULE_COMP_ALLOWLIST,
                         {"buildings": ["New One"]}, rationale="widened",
                         effective_from="2025-01-01", set_by="BMA")
        prior = R.effective_rules(self.conn, self.pid, "2024-10-01")
        now = R.effective_rules(self.conn, self.pid, "2026-10-01")
        # label is "type" when rule_key is empty, "type:key" when it is set
        self.assertNotIn(R.RULE_COMP_ALLOWLIST, [r.label for r in prior])
        self.assertIn(R.RULE_COMP_ALLOWLIST, [r.label for r in now])


class TestGovernance(unittest.TestCase):
    def setUp(self):
        self.conn, self.pid = fresh()

    def test_carrying_forward_is_not_reviewing(self):
        """The distinction the whole staleness signal depends on."""
        rid = seed(self.conn, self.pid, last_reviewed="2023-01-01")
        for _ in range(3):
            R.mark_carried(self.conn, [rid])
        rule = R.effective_rules(self.conn, self.pid, "2026-01-01")[0]
        self.assertEqual(rule.carried_count, 3)
        self.assertEqual(rule.last_reviewed, "2023-01-01",
                         "carrying a rule forward must not reset its review date")

    def test_review_resets_the_clock(self):
        rid = seed(self.conn, self.pid, last_reviewed="2023-01-01")
        R.mark_carried(self.conn, [rid])
        R.mark_reviewed(self.conn, [rid], "2026-01-01")
        rule = R.effective_rules(self.conn, self.pid, "2026-01-01")[0]
        self.assertEqual(rule.last_reviewed, "2026-01-01")

    def test_long_unreviewed_rule_is_flagged(self):
        seed(self.conn, self.pid, last_reviewed="2022-06-01")
        st = R.staleness(R.effective_rules(self.conn, self.pid, "2026-01-01"), "2026-01-01")
        self.assertEqual(len(st), 1)
        self.assertIn("not reviewed", st[0].reason)

    def test_recently_reviewed_rule_is_not_flagged(self):
        seed(self.conn, self.pid, last_reviewed="2025-11-01")
        st = R.staleness(R.effective_rules(self.conn, self.pid, "2026-01-01"), "2026-01-01")
        self.assertEqual(st, [])


class TestApplication(unittest.TestCase):
    def setUp(self):
        self.conn, self.pid = fresh()
        seed(self.conn, self.pid)
        self.rules = R.effective_rules(self.conn, self.pid, "2026-01-01")

    def test_time_adjustment_is_untouched(self):
        derived = {"time": 0.08, "location": 0.0}
        out, _ = R.apply_adjustments(self.rules, derived)
        self.assertEqual(out["time"], 0.08)

    def test_property_adjustment_is_pinned(self):
        out, notes = R.apply_adjustments(self.rules, {"time": 0.08, "location": 0.0})
        self.assertEqual(out["location"], -0.04)
        self.assertTrue(notes)

    def test_allowlist_keeps_and_drops(self):
        R.add_rule(self.conn, self.pid, R.RULE_COMP_ALLOWLIST,
                   {"buildings": ["Tai Wo Estate", "Tai Hong Estate"]},
                   rationale="old village stock only", effective_from="2023-01-01")
        rules = R.effective_rules(self.conn, self.pid, "2026-01-01")
        comps = [{"address": "A", "building": "Tai Wo Estate"},
                 {"address": "B", "building": "Wa Mei Tuk"}]
        kept, applied, dropped = R.filter_comps(rules, comps)
        self.assertEqual([c["address"] for c in kept], ["A"])
        self.assertEqual(len(dropped), 1)
        self.assertIn("wa mei tuk", dropped[0])

    def test_denylist_wins_and_both_are_reported_separately(self):
        R.add_rule(self.conn, self.pid, R.RULE_COMP_ALLOWLIST,
                   {"buildings": ["A Bldg", "B Bldg"]}, rationale="allow two",
                   effective_from="2023-01-01")
        R.add_rule(self.conn, self.pid, R.RULE_COMP_DENYLIST,
                   {"buildings": ["B Bldg"]}, rationale="but not B",
                   effective_from="2023-01-01")
        rules = R.effective_rules(self.conn, self.pid, "2026-01-01")
        kept, applied, dropped = R.filter_comps(
            rules, [{"address": "A", "building": "A Bldg"},
                    {"address": "B", "building": "B Bldg"},
                    {"address": "C", "building": "C Bldg"}])
        self.assertEqual([c["address"] for c in kept], ["A"])
        self.assertEqual(len(dropped), 2)
        # the *effect* and the *removals* are different facts
        self.assertTrue(any("narrowed" in a for a in applied))
        self.assertTrue(all("allowlist" in d or "denylist" in d for d in dropped))

    def test_mandatory_note_is_surfaced(self):
        R.add_rule(self.conn, self.pid, R.RULE_MANDATORY_NOTE,
                   {"text": "Restricted comparable range", "insert_in": "§5.2"},
                   rationale="client asked", effective_from="2023-01-01")
        rules = R.effective_rules(self.conn, self.pid, "2026-01-01")
        self.assertIn("§5.2", R.collect_notes(rules)[0])

    def test_no_rules_means_no_change(self):
        derived = {"time": 0.08, "location": 0.0}
        out, _ = R.apply_adjustments([], derived)
        self.assertEqual(out, derived)


class TestMethodDrift(unittest.TestCase):
    def setUp(self):
        self.conn, self.pid = fresh()

    def test_added_and_removed_are_distinguished_from_carried(self):
        a = seed(self.conn, self.pid, effective_from="2023-01-01")
        R.add_rule(self.conn, self.pid, R.RULE_MANDATORY_NOTE,
                   {"text": "n", "insert_in": "§5.2"}, rationale="added later",
                   effective_from="2025-01-01")
        prior = R.effective_rules(self.conn, self.pid, "2024-10-01")
        now = R.effective_rules(self.conn, self.pid, "2026-10-01")
        drift = R.method_drift(prior, now)
        self.assertIn(R.RULE_ADJUSTMENT_OVERRIDE, drift["rules_carried"])
        self.assertIn(R.RULE_MANDATORY_NOTE, drift["rules_added"])
        R.end_rule(self.conn, a, "2025-06-01")
        drift2 = R.method_drift(prior, R.effective_rules(self.conn, self.pid, "2026-10-01"))
        self.assertIn(R.RULE_ADJUSTMENT_OVERRIDE, drift2["rules_removed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
