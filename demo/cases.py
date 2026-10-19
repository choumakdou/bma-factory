"""Case registry for the bma-factory demo.

Each case is a unit of valuation work with:
  - id (URL slug)
  - title, category, case_ref, address, district
  - valuation_date, adopted_value_hkd, nfa_sqft, usage
  - one-line summary
  - data_loader — returns (subject_dict, comps_list) on demand

For the demo MVP, the Bel-Air M2 case data is embedded in
``demo.case_data``. Adding a new case is one entry here + a loader
function (or a Drive round-trip in v0.2).

Categories are derived from the data, not declared, so adding a case
in a brand-new category is automatic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


# --- case categories in display order ---------------------------------------

# Display order for the category grouping on the index page. Cases whose
# category is not in this list are appended at the end in alphabetical
# order. New categories automatically appear when the first case lands.
CATEGORY_ORDER: list[str] = [
    "ORO Bankruptcy",
    "Mortgage Valuations",
    "Shop / Retail",
    "Office / Commercial",
    "Industrial",
    "Residential",
    "Land",
]


@dataclass
class CaseData:
    """Subject + comparables for a single case."""
    subject: dict[str, Any]
    comps: list[dict[str, Any]]


@dataclass
class Case:
    id: str
    title: str
    category: str
    case_ref: str
    address: str
    district: str
    valuation_date: str
    adopted_value_hkd: int
    nfa_sqft: int
    usage: str
    summary: str
    source: str
    client: str = ""
    case_id_short: str = ""            # e.g. "ORO 120" — for the header pill
    adopted_rate_psf_n: int = 0         # HK$/sf N — for the adopted-rate pill
    previous_value_hkd: Optional[int] = None
    previous_date: Optional[str] = None
    status: str = "active"             # "active" | "completed" | "archived"
    last_action: str = ""              # free-form, e.g. "Generated workbook 2026-10-13"
    data_loader: Optional[Callable[[], CaseData]] = None


# --- loaders -----------------------------------------------------------------

def _load_bel_air_m2() -> CaseData:
    from demo.case_data import SUBJECT, comp_pool
    return CaseData(subject=SUBJECT, comps=comp_pool())


# --- registry ----------------------------------------------------------------

CASES: list[Case] = [
    Case(
        id="bel-air-m2",
        title="Bel-Air M2 (Island Place Tower)",
        category="ORO Bankruptcy",
        case_ref="V/ALE/303927/2026",
        case_id_short="ORO 120",
        client="Official Receiver's Office",
        address="510 King's Road, Hong Kong",
        district="HNP",
        valuation_date="2026-09-15",
        adopted_value_hkd=76_000_000,
        adopted_rate_psf_n=5_502,
        nfa_sqft=13_812,
        usage="Office",
        summary=(
            "Grade A office in North Point. 11 comparables ranked by "
            "engine. 4 QC flags surfaced (2 PRESS, 2 CONNECTED, 1 "
            "APPROX, 1 NO_GFA). Adopted value HK$76,000,000."
        ),
        source="Embedded — extracted from CHFT Map ver 8.0 Sheet1",
        previous_value_hkd=73_500_000,
        previous_date="2025-09-15",
        status="active",
        last_action="Engine smoke-test passed · 4 QC flags raised",
        data_loader=_load_bel_air_m2,
    ),
]


def get_case(case_id: str) -> Optional[Case]:
    """Look up a case by its URL slug."""
    for c in CASES:
        if c.id == case_id:
            return c
    return None


def get_active_cases() -> list[Case]:
    """Cases that are in progress or recently valued — the things on your desk."""
    return [c for c in CASES if c.status in ("active", "in_progress")]


def get_history_cases() -> list[Case]:
    """Cases that are completed and kept for reference."""
    return [c for c in CASES if c.status == "completed"]


def get_cases_by_category() -> list[tuple[str, list[Case]]]:
    """Return categories with their cases, in display order.

    Categories with no cases are still returned (so the index page can
    show empty-state placeholders), but only if they're in CATEGORY_ORDER.
    """
    by_cat: dict[str, list[Case]] = {}
    for c in CASES:
        by_cat.setdefault(c.category, []).append(c)

    out: list[tuple[str, list[Case]]] = []
    seen: set[str] = set()
    for cat in CATEGORY_ORDER:
        out.append((cat, by_cat.get(cat, [])))
        seen.add(cat)
    # any new categories not in the order list — appended alphabetically
    for cat in sorted(by_cat):
        if cat not in seen:
            out.append((cat, by_cat[cat]))
    return out
