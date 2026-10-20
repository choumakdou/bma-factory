"""Build a synthetic Memfus Wong transaction extract.

The real extract comes from the agency. We need one that follows the
same shape to exercise the intake path without a live agency file, and
to give Bobby something to drop into Drive to try the flow.

The columns are the ones ``ingest.memfus`` declares. The subject row is
marked the way the real workbook marks it: MEM_NO = "Subject".
"""

from __future__ import annotations

import io
from datetime import date
from typing import Any

from ingest.memfus import COLUMNS


def _row(
    mem_no: str,
    inst_date: str,
    usage: str,
    unit: str,
    floor: str,
    block: str,
    bld: str,
    st: str,
    st_no: str,
    lot: str,
    d_code: str,
    consid: str,
    totgfa: str,
    totnfa: str,
    remark: str = "",
) -> dict[str, Any]:
    return {
        "MEM_NO": mem_no,
        "NAT_INST": "",
        "INST_DATE": inst_date,
        "PUSAGE": usage,
        "CP_PT": "",
        "UNIT": unit,
        "FLOOR": floor,
        "BLOCK": block,
        "PHASE": "",
        "BLD_E_NAME": bld,
        "BLD_C_NAME": "",
        "EST_E_NAME": "",
        "EST_C_NAME": "",
        "ST_E_NAME": st,
        "ST_C_NAME": "",
        "ST_NO_FRM": st_no,
        "ST_NO_TO": st_no,
        "LOT_NO1": lot,
        "D_CODE": d_code,
        "OP_DATE": "",
        "CONSIDERTN": consid,
        "TOTGFA": totgfa,
        "TOTNFA": totnfa,
        "GROSS_PSF": "",
        "NET_PSF": "",
        "REMARK": remark,
        "REMARK2": "",
    }


def sample_rows() -> list[dict[str, Any]]:
    """A small, realistic extract: one subject plus five comparables.

    CONSIDERTN is in MILLIONS — that is the single most dangerous field in
    the Memfus Wong extract, and ``ingest.memfus.consideration_hkd``
    multiplies it by a million. Get it wrong and every price in the app
    is out by a factor of a million.
    """
    return [
        _row("Subject", "2026-08-20", "Office", "12", "12/F", "Tower A",
             "Island Place Tower", "KING'S RD", "510", "1001", "HNP",
             "76", "16,000", "13,812", ""),
        _row("T1", "2026-07-18", "Office", "11", "11/F", "Tower A",
             "Island Place Tower", "KING'S RD", "510", "1001", "HNP",
             "72.5", "15,200", "13,100", ""),
        _row("T2", "2026-05-06", "Office", "8", "8/F", "Tower A",
             "Island Place Tower", "KING'S RD", "510", "1001", "HNP",
             "68", "14,600", "12,600", ""),
        _row("T3", "2026-02-27", "Office", "9", "9/F", "Tower B",
             "Island Place Tower", "KING'S RD", "510", "1001", "HNP",
             "55", "12,800", "11,050", "Press"),
        _row("T4", "2025-11-14", "Office", "25", "25/F", "Block 1",
             "Kwong Waik Commercial Centre", "HANG LOK ST", "31", "2830", "HNP",
             "41.2", "9,400", "8,100", "Connected transaction"),
        _row("T5", "2025-08-29", "Office", "3", "3/F", "Tower",
             "AIA Kowloon Tower", "AMOY RD", "183", "2529", "KCD",
             "29.8", "7,900", "6,850", "Approx"),
    ]


def build_workbook_bytes() -> bytes:
    """An .xlsx of the sample extract, as bytes."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(COLUMNS)
    for r in sample_rows():
        ws.append([r.get(c) for c in COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_workbook_path(path) -> Any:
    """Write the sample extract to ``path`` and return it."""
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(build_workbook_bytes())
    return p
