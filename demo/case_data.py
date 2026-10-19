"""Embedded case data for the Bel-Air M2 (ORO 120) demo.

Source: extracted 2026-10-19 from
  /workspace/attachments/dab5f97ed71fba85/CHFT Map ver 8.0 20260921.xlsm
  Sheet1 (12 selected transactions for Island Place Tower, 510 King's Road)

The Map's Sheet1 is the selected-comps set the valuer worked from.
Sheet2 (194 rows, 100% Mong Kok) was the unfiltered pool; per the QA findings
we treat the selected 11 as the working comp set, with QC flags lifted from
the source's REMARK2 column.

For the demo MVP the data ships in the repo. v0.2 wires the same struct to
real ORO-workbook parsing via `ingest.oro_ingest.parse_case`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# --- Subject ----------------------------------------------------------------

SUBJECT: dict = {
    "case_ref": "V/ALE/303927/2026",
    "case_id": "ORO 120",
    "address": "510 King's Road, Hong Kong",
    "building": "Island Place Tower",
    "district": "HNP",
    "lot": "IL 8849",
    "floor": "8/F",
    "gfa_sqft": 20_090,
    "nfa_sqft": 13_812,
    "usage": "Office",
    "grade": "A",
    "op_date": "1997",
    "valuation_date": "2026-09-15",
    "valuer": "Alex Leung, MRICS MHKIS",
    "purpose": "Official Receiver's Office (Bankruptcy)",
    "adopted_value_hkd": 76_000_000,
    "adopted_rate_psf_n": 5_502,            # 76,000,000 / 13,812 ≈ 5,502
    "previous_value_hkd": 73_500_000,       # carried from prior case review
    "previous_date": "2025-09-15",
}


# --- Comparables ------------------------------------------------------------

# Fields mirror what the CHFT Map's locations[] contract emits, plus the
# raw transaction metadata we need for ranking. Each comp dict is what
# `relevance.score_one()` expects.

@dataclass
class Comp:
    src_row: int
    name: str                       # building display name (CARNIVAL COM BLDG)
    address: str                    # "18 JAVA RD, Hong Kong"
    floor: str                      # "8" or "12-13" or "whole of 19-20"
    unit: str                       # "1" / "B1" / "1701, 1706-1710 and whole of 18/F"
    nfa_sqft: float
    gfa_sqft: float
    price_hkd: float                # consideration in dollars
    psf_gross: float                # $/psf GFA
    psf_net: float                  # $/psf NFA
    inst_date: str                  # "2025-03-25" (ISO)
    nat_inst: str                   # ASP / PRE-ASP / PRO-ASP / ASSIGN / news (unverified) / Subject
    op_date: str                    # "1994/07" — month of Occupation Permit
    usage: str                      # OFC
    grade: str                      # A / B / C
    qc_flags: list[str] = field(default_factory=list)
    is_subject: bool = False


def _q(flags: list[str]) -> list[str]:
    """Lift QC flags from the source workbook's REMARK2 + structural checks."""
    out: list[str] = []
    if any("unverified" in f or "信報" in f or "news" in f.lower() for f in flags):
        out.append("PRESS")
    if any("connected" in f.lower() for f in flags):
        out.append("CONNECTED")
    if any("appro" in f.lower() for f in flags):  # APPROX
        out.append("APPROX")
    return out


COMPS: list[Comp] = [
    Comp(
        src_row=2,
        name="CARNIVAL COM BLDG",
        address="18 JAVA RD, Hong Kong",
        floor="8", unit="1",
        nfa_sqft=371, gfa_sqft=525,
        price_hkd=3_580_000,
        psf_gross=6_819, psf_net=9_650,
        inst_date="2025-03-25", nat_inst="ASP",
        op_date="1994/07", usage="OFC", grade="C",
    ),
    Comp(
        src_row=3,
        name="JAVA COM CTR",
        address="124-132 JAVA RD, Hong Kong",
        floor="21", unit="1",
        nfa_sqft=620, gfa_sqft=952,
        price_hkd=6_800_000,
        psf_gross=7_143, psf_net=10_968,
        inst_date="2024-10-30", nat_inst="PRE-ASP",
        op_date="1983/10", usage="OFC", grade="C",
    ),
    Comp(
        src_row=4,
        name="WELLBORNE COM CTR",
        address="8 JAVA RD, Hong Kong",
        floor="8", unit="6",
        nfa_sqft=209, gfa_sqft=300,
        price_hkd=2_380_000,
        psf_gross=7_933, psf_net=11_388,
        inst_date="2025-03-20", nat_inst="ASP",
        op_date="1993/06", usage="OFC", grade="C",
    ),
    Comp(
        src_row=5,
        name="CAUSEWAY BAY COM BLDG",
        address="1-5 SUGAR ST, Hong Kong",
        floor="16", unit="B1",
        nfa_sqft=413, gfa_sqft=0,            # GFA=0 → basement / unusual
        price_hkd=5_700_000,
        psf_gross=0, psf_net=13_801,
        inst_date="2025-01-23", nat_inst="ASP",
        op_date="1975/12", usage="OFC", grade="B",
    ),
    Comp(
        src_row=6,
        name="WORKINGBERG COM BLDG",
        address="41-47 MARBLE RD, Hong Kong",
        floor="13", unit="8",
        nfa_sqft=180, gfa_sqft=282,
        price_hkd=1_880_000,
        psf_gross=6_667, psf_net=10_444,
        inst_date="2025-01-04", nat_inst="PRO-ASP",
        op_date="1992/01", usage="OFC", grade="C",
    ),
    Comp(
        src_row=7,
        name="ISLAND PLACE TOWER",
        address="510 KING'S RD, Hong Kong",
        floor="1701, 1706-1710 and whole of 18/F", unit="",
        nfa_sqft=24_271, gfa_sqft=35_284,
        price_hkd=271_765_000,
        psf_gross=7_702, psf_net=11_197,
        inst_date="2025-06-13", nat_inst="ASP",
        op_date="1997/05", usage="OFC", grade="A",
        qc_flags=["Connected Transaction"],
    ),
    Comp(
        src_row=8,
        name="ISLAND PLACE TOWER",
        address="510 KING'S RD, Hong Kong",
        floor="whole of 19-20", unit="",
        nfa_sqft=28_852, gfa_sqft=41_944,
        price_hkd=292_170_000,
        psf_gross=6_966, psf_net=10_127,
        inst_date="2025-06-13", nat_inst="ASP",
        op_date="1997/05", usage="OFC", grade="A",
        qc_flags=["Connected Transaction"],
    ),
    Comp(
        src_row=9,
        name="ISLAND PLACE TOWER",
        address="510 KING'S RD, Hong Kong",
        floor="12-13", unit="",
        nfa_sqft=27_762, gfa_sqft=40_380,
        price_hkd=440_000_000,
        psf_gross=10_896, psf_net=15_849,
        inst_date="2025-01-15", nat_inst="news (unverified)",
        op_date="1997/05", usage="OFC", grade="A",
        qc_flags=["transacted price not sure"],
    ),
    Comp(
        src_row=10,
        name="ISLAND PLACE TOWER",
        address="510 KING'S RD, Hong Kong",
        floor="12-13", unit="",
        nfa_sqft=27_762, gfa_sqft=40_380,
        price_hkd=323_000_000,
        psf_gross=7_999, psf_net=11_635,
        inst_date="2025-01-15", nat_inst="news (unverified)",
        op_date="1997/05", usage="OFC", grade="A",
        qc_flags=["信報"],
    ),
    Comp(
        src_row=11,
        name="WORKINGBERG COM BLDG",
        address="41-47 MARBLE RD, Hong Kong",
        floor="13", unit="8",
        nfa_sqft=180, gfa_sqft=282,
        price_hkd=1_880_000,
        psf_gross=6_667, psf_net=10_444,
        inst_date="2025-02-04", nat_inst="ASP",
        op_date="1992/01", usage="OFC", grade="B",
    ),
    Comp(
        src_row=12,
        name="(no building name — see address)",
        address="9 KESWICK ST, Hong Kong",
        floor="19", unit="",
        nfa_sqft=820, gfa_sqft=1_310,
        price_hkd=16_500_000,
        psf_gross=12_595, psf_net=20_122,
        inst_date="2025-03-31", nat_inst="ASSIGN",
        op_date="2022/07", usage="OFC", grade="",
        qc_flags=["AREA APPROX ONLY"],
    ),
]


# --- Post-process: lift QC flags and drop the subject row --------------------

# (The subject doesn't appear in Sheet1; row 13 IS the subject marker but
# it is metadata-only, no consideration. We expose it separately as SUBJECT.)

for _c in COMPS:
    _c.qc_flags = _q(_c.qc_flags)
    if _c.gfa_sqft == 0:
        _c.qc_flags = sorted(set(_c.qc_flags + ["NO_GFA"]))
    if _c.nat_inst.lower().startswith("news") or "unverified" in _c.nat_inst.lower():
        _c.qc_flags = sorted(set(_c.qc_flags + ["PRESS"]))


def to_relevance_input(c: Comp) -> dict:
    """Shape the comp as the relevancy scorer expects.

    Mirrors the keys the scorer reads:
      building, district, area_sf, usage, date  — used by the factors
      everything else is forwarded for the UI / QC display
    """
    return {
        "src_row": c.src_row,
        "name": c.name,
        "address": c.address,
        "building": c.name,                    # f_same_building reads .building
        "district": "HNP" if "JAVA" in c.address or "KING" in c.address or "MARBLE" in c.address
                    else "HCB",                # Causeway Bay vs North Point
        "floor": c.floor,
        "unit": c.unit,
        "nfa_sqft": c.nfa_sqft,
        "gfa_sqft": c.gfa_sqft,
        "area_sf": c.nfa_sqft,                 # f_area_similarity reads .area_sf
        "price_hkd": c.price_hkd,
        "psf_gross": c.psf_gross,
        "psf_net": c.psf_net,
        "inst_date": c.inst_date,
        "date": c.inst_date,                   # f_recency reads .date
        "nat_inst": c.nat_inst,
        "op_date": c.op_date,
        "usage": c.usage,
        "grade": c.grade,
        "qc_flags": c.qc_flags,
    }


def comp_pool() -> list[dict]:
    """The 11 comparables shaped for the relevancy engine."""
    return [to_relevance_input(c) for c in COMPS]
