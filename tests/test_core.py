"""Core tests. Standard library only, so they run anywhere the app runs.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="vf-test-"))
os.environ["VF_DB"] = str(_TMP / "ledger.sqlite3")
os.environ["VF_OUTPUT_DIR"] = str(_TMP / "outputs")

from app.core import auth, generate, ledger, registry  # noqa: E402
from app.core.auth import AuthError, Principal, ScopeError  # noqa: E402
from app.core.schemas import Field, Schema, validate  # noqa: E402

PAYLOAD = {
    "subject_address": "Units 6-11 and 13, 18/F, Grandtech Centre, Shatin",
    "subject_sa": 935.5,
    "subject_ac_platform": 120.0,
    "subject_ac_pct": "50%",
    "subject_price_index": 366.2,
    "val_date": "2026-08-01",
    "valuer": "BMA",
    "adopted_unit_rate": 225,
    "term_years": 3,
    "term_rate": "4.4%",
    "reversion_rate": "4.9%",
    "comps": [
        {"address": "Unit 7, 18/F", "rent": "HK$220,000", "sa": 900, "price_index": 366.2,
         "adj_floor": "-5%", "adj_sw": "-3%"},
        {"address": "Unit 20, 22/F", "rent": "HK$205,000", "sa": 850, "price_index": 360.1,
         "adj_floor": "-8%", "adj_qa": "-2%", "adj_sw": "-5%"},
        {"address": "Unit 3, Millennium City", "rent": "HK$195,000", "sa": 820, "price_index": 355.4,
         "adj_floor": "5%", "adj_qa": "-3%", "adj_age": "-2%", "adj_location": "-4%"},
    ],
}


class TestCoercion(unittest.TestCase):
    """The whole point of the typed layer: same number, different spelling,
    same result."""

    SCHEMA = Schema(fields=[
        Field("rent", "Rent", "currency"),
        Field("adj", "Adjustment", "pct", required=False),
        Field("when", "Date", "date", required=False),
        Field("lot", "Lot", "lot", required=False),
    ])

    def test_currency_spellings_agree(self):
        for raw in ["1250", "1,250", "HK$1,250.00", " 1250 "]:
            r = validate(self.SCHEMA, {"rent": raw})
            self.assertTrue(r.ok, f"{raw!r} rejected")
            self.assertEqual(r.values["rent"], 1250, raw)

    def test_percent_spellings_agree(self):
        for raw in ["5%", "5", "0.05"]:
            r = validate(self.SCHEMA, {"rent": "1250", "adj": raw})
            self.assertTrue(r.ok, raw)
            self.assertAlmostEqual(r.values["adj"], 0.05, msg=raw)

    def test_negative_percent_keeps_sign(self):
        for raw in ["-5%", "-5", "-0.05"]:
            r = validate(self.SCHEMA, {"rent": "1250", "adj": raw})
            self.assertTrue(r.ok, raw)
            self.assertAlmostEqual(r.values["adj"], -0.05, msg=raw)

    def test_percent_point_typo_is_caught(self):
        """'5' and '5%' mean the same. '50' typed by mistake means 50%."""
        r = validate(self.SCHEMA, {"rent": "1250", "adj": "50"})
        self.assertAlmostEqual(r.values["adj"], 0.50)

    def test_date_formats_agree(self):
        for raw in ["2026-08-01", "01/08/2026", "1 August 2026"]:
            r = validate(self.SCHEMA, {"rent": "1250", "when": raw})
            self.assertTrue(r.ok, raw)
            self.assertEqual(r.values["when"].isoformat(), "2026-08-01", raw)

    def test_bad_values_refuse_rather_than_default_to_zero(self):
        r = validate(self.SCHEMA, {"rent": "about twelve hundred"})
        self.assertFalse(r.ok)
        self.assertTrue(any("rent" in i.field for i in r.errors))

    def test_lot_format(self):
        self.assertTrue(validate(self.SCHEMA, {"rent": "1250", "lot": "IL 588 S.A R.P."}).ok)
        self.assertFalse(validate(self.SCHEMA, {"rent": "1250", "lot": "somewhere in shatin"}).ok)

    def test_missing_required_is_an_error(self):
        r = validate(self.SCHEMA, {})
        self.assertFalse(r.ok)


class TestRegistry(unittest.TestCase):
    def test_module_discovered(self):
        self.assertIn("hk-rental-comps", registry.module_ids())

    def test_schema_shape(self):
        m = registry.get_module("hk-rental-comps").manifest
        self.assertEqual(m.outputs[0]["type"], "xlsx")
        self.assertIn("Comparables", m.schema().sections())


class TestGeneration(unittest.TestCase):
    def test_produces_xlsx(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        r = generate.generate("hk-rental-comps", PAYLOAD, caller="test", out_dir=d)
        self.assertEqual(len(r.artifacts), 1)
        self.assertEqual(r.artifacts[0].type, "xlsx")
        self.assertTrue(r.artifacts[0].path.exists())
        self.assertGreater(r.artifacts[0].size, 4000)

    def test_determinism_same_input_same_bytes(self):
        a = generate.generate("hk-rental-comps", PAYLOAD, caller="t1", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        b = generate.generate("hk-rental-comps", PAYLOAD, caller="t2", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        self.assertEqual(a.input_hash, b.input_hash)
        self.assertEqual(a.artifacts[0].sha256, b.artifacts[0].sha256)

    def test_determinism_formatting_does_not_change_output(self):
        """'HK$220,000' and '220000' must produce the identical file."""
        alt = dict(PAYLOAD)
        alt["comps"] = [dict(c) for c in PAYLOAD["comps"]]
        alt["comps"][0] = {**alt["comps"][0], "rent": "220000"}
        a = generate.generate("hk-rental-comps", PAYLOAD, caller="t3", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        b = generate.generate("hk-rental-comps", alt, caller="t4", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        self.assertEqual(a.artifacts[0].sha256, b.artifacts[0].sha256)

    def test_different_input_changes_output(self):
        alt = {**PAYLOAD, "adopted_unit_rate": 250}
        a = generate.generate("hk-rental-comps", PAYLOAD, caller="t5", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        b = generate.generate("hk-rental-comps", alt, caller="t6", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        self.assertNotEqual(a.input_hash, b.input_hash)
        self.assertNotEqual(a.artifacts[0].sha256, b.artifacts[0].sha256)

    def test_missing_comparables_refused(self):
        with self.assertRaises(generate.GenerationRefused):
            generate.generate("hk-rental-comps", {**PAYLOAD, "comps": []},
                              caller="t7", out_dir=Path(tempfile.mkdtemp(dir=_TMP)))

    def test_unknown_module_refused(self):
        with self.assertRaises(generate.GenerationRefused):
            generate.generate("no-such-module", PAYLOAD, caller="t8",
                              out_dir=Path(tempfile.mkdtemp(dir=_TMP)))

    def test_bad_field_refused_with_field_name(self):
        bad = {**PAYLOAD, "adopted_unit_rate": "not a number"}
        with self.assertRaises(generate.GenerationRefused) as ctx:
            generate.generate("hk-rental-comps", bad, caller="t9",
                              out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        self.assertIn("adopted_unit_rate", str(ctx.exception))

    def test_single_comparable_warns(self):
        one = {**PAYLOAD, "comps": [PAYLOAD["comps"][0]]}
        r = generate.generate("hk-rental-comps", one, caller="t10",
                              out_dir=Path(tempfile.mkdtemp(dir=_TMP)))
        self.assertTrue(any("three" in w for w in r.warnings))

    def test_workbook_contains_live_formulas(self):
        from openpyxl import load_workbook
        d = Path(tempfile.mkdtemp(dir=_TMP))
        r = generate.generate("hk-rental-comps", PAYLOAD, caller="t11", out_dir=d)
        wb = load_workbook(r.artifacts[0].path)
        self.assertEqual(wb.sheetnames, ["Comps", "Summary", "T&R"])
        c = wb["Comps"]
        self.assertTrue(str(c["M4"].value).startswith("="))
        self.assertTrue(str(c["V4"].value).startswith("="))
        self.assertIn("Summary!", str(c["O4"].value))
        # the generator's name must not leak into the file's metadata
        self.assertNotEqual(wb.properties.creator, "openpyxl")
        self.assertEqual(wb.properties.creator in (None, ""), True)


class TestLedger(unittest.TestCase):
    def test_run_recorded_and_reported(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        r = generate.generate("hk-rental-comps", PAYLOAD, caller="ledger-test", out_dir=d)
        entry = ledger.find_run(r.run_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.caller, "ledger-test")

    def test_idempotency(self):
        key = "test-key-abc"
        self.assertIsNone(ledger.idempotent_lookup("caller", key))
        ledger.idempotent_store("caller", key, "run-123")
        self.assertEqual(ledger.idempotent_lookup("caller", key), "run-123")
        # scoped per caller: another agent does not inherit the mapping
        self.assertIsNone(ledger.idempotent_lookup("other-caller", key))


class TestAuth(unittest.TestCase):
    def test_no_header_is_unauthorised(self):
        with self.assertRaises(AuthError):
            auth.verify_bearer(None)

    def test_garbage_token_is_unauthorised(self):
        with self.assertRaises(AuthError):
            auth.verify_bearer("Bearer nonsense-token")

    def test_unknown_key_rejected(self):
        os.environ["VF_API_KEYS"] = "tester:catalog:read|case:write"
        auth.reset_keys()
        try:
            p = auth.verify_bearer("Bearer tester")
            self.assertIn(auth.SCOPE_GENERATE, p.scopes)
            with self.assertRaises(AuthError):
                auth.verify_bearer("Bearer someone-else")
        finally:
            os.environ.pop("VF_API_KEYS", None)
            auth.reset_keys()

    def test_scope_enforcement(self):
        read_only = Principal(name="api:reader", scopes={auth.SCOPE_CATALOG}, kind="agent")
        read_only.require(auth.SCOPE_CATALOG)
        with self.assertRaises(ScopeError):
            read_only.require(auth.SCOPE_GENERATE)

    def test_agent_inherits_nothing_by_default(self):
        """With no keys configured, there is no agent caller at all."""
        os.environ.pop("VF_API_KEYS", None)
        auth.reset_keys()
        self.assertEqual(auth._keys(), {})

    def test_unknown_scope_in_config_fails_loudly(self):
        os.environ["VF_API_KEYS"] = "bad:catalog:read|case:delete"
        auth.reset_keys()
        try:
            with self.assertRaises(RuntimeError):
                auth._load_keys()
        finally:
            os.environ.pop("VF_API_KEYS", None)
            auth.reset_keys()


def tearDownModule():  # noqa: N802
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)