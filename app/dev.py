"""Sample generation — run with:  python3 -m app.dev

Uses the Grandtech Centre figures as worked example data so the output can be
checked against a real workback rather than invented numbers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import generate  # noqa: E402

SAMPLE = {
    "subject_address": "Units 6-11 and 13, 18/F, Grandtech Centre, No. 8 On Ping Street, Shatin",
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
        {"address": "Unit 7, 18/F, Grandtech Centre", "rent": "HK$220,000", "sa": 900,
         "price_index": 366.2, "year": 2026, "transaction_date": "2026-05-01",
         "floor": "18/F", "aspect": "E", "adj_floor": "-5%", "adj_qa": "0%",
         "adj_sw": "-3%", "adj_age": "0%", "adj_location": "0%",
         "remarks": "Same building, direct evidence"},
        {"address": "Unit 20, 22/F, Grandtech Centre", "rent": "HK$205,000", "sa": 850,
         "price_index": 360.1, "year": 2026, "transaction_date": "2026-06-10",
         "floor": "22/F", "aspect": "W", "adj_floor": "-8%", "adj_qa": "-2%",
         "adj_sw": "-5%", "adj_age": "0%", "adj_location": "0%",
         "remarks": "Higher floor, aspect differs"},
        {"address": "Unit 3, 12/F, Millennium City, On Ping Street", "rent": "HK$195,000",
         "sa": 820, "price_index": 355.4, "year": 2026, "transaction_date": "2026-03-20",
         "floor": "12/F", "aspect": "S", "adj_floor": "5%", "adj_qa": "-3%",
         "adj_sw": "0%", "adj_age": "-2%", "adj_location": "-4%",
         "remarks": "Sibling building, lower specification"},
    ],
}


def main() -> int:
    try:
        result = generate.generate("hk-rental-comps", SAMPLE, caller="dev")
    except generate.GenerationRefused as exc:
        print("REFUSED:")
        for issue in exc.issues:
            print("  ", issue)
        return 1

    print(f"run_id      {result.run_id}")
    print(f"module      {result.module_id} v{result.template_version}")
    print(f"input_hash  {result.input_hash}")
    for a in result.artifacts:
        print(f"file        {a.path}")
        print(f"            {a.size:,} bytes   sha256={a.sha256}")
    if result.warnings:
        print("warnings:")
        for w in result.warnings:
            print("  ", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())