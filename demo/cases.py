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
    #: Set when the case came from a workbook in Drive. Carries the file id
    #: so the project page can link back to the content it was built from.
    source_drive_file: Optional[dict[str, Any]] = None


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


def get_case(
    case_id: str,
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> Optional[Case]:
    """Look up a case by its URL slug.

    Static registry first, then projects derived from workbooks in Drive.
    """
    for c in CASES:
        if c.id == case_id:
            return c
    for c in get_drive_cases(extra_folder_ids, whole_drive):
        if c.id == case_id:
            return c
    return None


def get_drive_cases(
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[Case]:
    """Projects derived from Excel files sitting in the Drive intake folders.

    A Memfus Wong extract carries its own subject row, so dropping one in
    Drive is enough to create a project — the valuer does not have to
    re-key anything.

    Returns an empty list when nobody is signed in or Drive is empty.
    """
    try:
        from demo import intake
    except Exception:  # noqa: BLE001
        return []
    try:
        files = intake.list_drive_workbooks(
            folder_ids=extra_folder_ids, whole_drive=whole_drive
        )
    except Exception:  # noqa: BLE001
        return []

    out: list[Case] = []
    seen: set[str] = set()
    # `files` is newest-first, so the first file that yields a given
    # subject wins. A re-uploaded extract therefore supersedes the older
    # one for the same subject instead of being silently dropped.
    for f in files:
        try:
            parsed = intake.parse_drive_file(f)
        except Exception:  # noqa: BLE001
            continue
        if not parsed.project:
            continue
        p = parsed.project
        case_id = p["id"]
        if case_id in seen:
            continue
        seen.add(case_id)
        out.append(Case(
            id=case_id,
            title=p["title"],
            category=p["category"],
            case_ref=p.get("case_ref", ""),
            case_id_short="MW extract",
            client="",
            address=p.get("address", ""),
            district=p.get("district", ""),
            valuation_date=p.get("valuation_date", ""),
            adopted_value_hkd=int(p.get("adopted_value_hkd") or 0),
            adopted_rate_psf_n=int(p.get("adopted_rate_psf_n") or 0),
            nfa_sqft=int(p.get("nfa_sqft") or 0),
            usage=p.get("usage", ""),
            summary=p.get("summary", ""),
            source=f"Drive · {f.folder}/{f.name}",
            status="active",
            last_action=f"Imported from {f.name} ({f.modified_time[:10]})",
            data_loader=_make_drive_loader(f, parsed),
            source_drive_file=f.to_dict(),
        ))
    return out


def _make_drive_loader(f: Any, parsed: Any) -> Callable[[], CaseData]:
    """A loader that re-derives the subject and comps from the Drive file."""
    def loader() -> CaseData:
        from demo import intake
        current = intake.parse_drive_file(f)
        built = intake.build_case_data(current)
        if built is None:
            raise ValueError(
                f"'{f.name}' no longer yields a subject row, so the project "
                f"cannot be built. Re-upload a Memfus Wong extract with a "
                f"row marked MEM_NO = 'Subject'."
            )
        subject, comps = built
        return CaseData(subject=subject, comps=comps)
    return loader


def get_all_cases(
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[Case]:
    """Every case — static registry plus Drive-derived projects."""
    static = list(CASES)
    drive_ids = {c.id for c in static}
    for c in get_drive_cases(extra_folder_ids, whole_drive):
        if c.id not in drive_ids:
            static.append(c)
    return static


def get_active_cases(
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[Case]:
    """Cases that are in progress or recently valued — the things on your desk."""
    return [c for c in get_all_cases(extra_folder_ids, whole_drive)
            if c.status in ("active", "in_progress")]


def get_history_cases(
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[Case]:
    """Cases that are completed and kept for reference."""
    return [c for c in get_all_cases(extra_folder_ids, whole_drive)
            if c.status == "completed"]


def get_cases_by_category(
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[tuple[str, list[Case]]]:
    """Return categories with their cases, in display order.

    Categories with no cases are still returned (so the index page can
    show empty-state placeholders), but only if they're in CATEGORY_ORDER.
    """
    by_cat: dict[str, list[Case]] = {}
    for c in get_all_cases(extra_folder_ids, whole_drive):
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
