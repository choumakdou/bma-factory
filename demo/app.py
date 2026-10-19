"""bma-factory demo — Bel-Air M2 (ORO 120) case review.

Single-page demo that proves the architecture end-to-end:
  - Reads the (embedded) case data
  - Runs the existing relevancy ranking from `ingest.relevance`
  - Renders a case review with case header, ranked comps, QC flags, and a
    Generate button
  - On Generate, calls `app.core.generate.generate()` to produce the
    HK Rental Comps + T&R workbook, then offers it as a download

For the demo MVP the case data is embedded in `case_data.py` — extracted
from the CHFT Map ver 8.0 Sheet1. v0.2 swaps this for real ORO-workbook
parsing via `ingest.oro_ingest.parse_case` reading from the user's Drive.

No auth. The URL is shared by link. The service account that runs the
container has access only to Drive folders the user explicitly shares.
"""

from __future__ import annotations

import io
import sys
import logging
import zipfile
from pathlib import Path
from typing import Any

# Make the repo importable as a package root when running under uvicorn
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from demo.case_data import SUBJECT, COMPS, comp_pool
from ingest.relevance import Weights, rank, Scored, is_self as is_self_txn

log = logging.getLogger("bma-factory.demo")

app = FastAPI(title="bma-factory demo", version="0.1.0")
templates = Jinja2Templates(directory=str(_REPO / "demo" / "templates"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_breakdown(s: Scored) -> dict[str, float]:
    """Lift the per-axis score from a Scored so the template can show it."""
    bd = s.breakdown
    return {
        "same_building":   round(bd.get("same_building", 0.0), 3),
        "distance":        round(bd.get("distance", 0.0), 3),
        "recency":         round(bd.get("recency", 0.0), 3),
        "area_similarity": round(bd.get("area_similarity", 0.0), 3),
        "usage_match":     round(bd.get("usage_match", 0.0), 3),
    }


def _ranked() -> list[dict[str, Any]]:
    """Run the existing relevancy ranker on the embedded comp pool.

    Subject is the Bel-Air M2 case. We use the valuer's building name
    (ISLAND PLACE TOWER / Island Place Tower) as same-building match.
    """
    subject = {
        "name": SUBJECT["building"],
        "building": SUBJECT["building"],
        "address": SUBJECT["address"],
        "nfa_sqft": SUBJECT["nfa_sqft"],
        "area_sf": SUBJECT["nfa_sqft"],
        "usage": SUBJECT["usage"],
        "case_ref": SUBJECT["case_ref"],
    }
    pool = comp_pool()
    w = Weights()  # defaults: same_building 0.30, distance 0.25, recency 0.20, area 0.15, usage 0.10
    ranked = rank(subject, pool, as_of=SUBJECT["valuation_date"], w=w)
    out: list[dict[str, Any]] = []
    for s in ranked:
        c = s.comp
        hit, why = is_self_txn(subject, c)
        out.append({
            "rank":          len(out) + 1,
            "src_row":       c.get("src_row"),
            "name":          c.get("name"),
            "address":       c.get("address"),
            "floor":         c.get("floor"),
            "unit":          c.get("unit"),
            "nfa_sqft":      c.get("nfa_sqft"),
            "psf_net":       c.get("psf_net"),
            "inst_date":     str(c.get("inst_date") or ""),
            "nat_inst":      c.get("nat_inst"),
            "grade":         c.get("grade"),
            "qc_flags":      c.get("qc_flags") or [],
            "score":         round(s.total, 3),
            "breakdown":     _score_breakdown(s),
            "is_self":       hit,
            "self_reason":   why,
        })
    return out


def _comps_for_module(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the ranked comps into the shape the hk-rental-comps module wants."""
    out: list[dict[str, Any]] = []
    for r in ranked:
        if r["is_self"]:
            continue
        # The module's "comps" entries use simple dict fields. We feed it
        # the 11 ranked comps, with the adjustments left as 0% — the valuer
        # adjusts in the workbook, not at the ranking layer.
        out.append({
            "address":       r["address"],
            "year":          int(r["inst_date"][:4]) if r["inst_date"] else 2025,
            "date":          r["inst_date"],
            "rent":          r["psf_net"] * r["nfa_sqft"] / 12.0,  # impute monthly rent = $/sf × NFA
            "sa":            round(r["nfa_sqft"] / 10.76391, 1),  # NFA in sqm
            "floor":         r["floor"],
            "usage":         "OFC",
            "price_index":   320.1,            # RVD private-domestic Apr 2026 (placeholder)
        })
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/case/bel-air-m2", status_code=302)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/case/bel-air-m2", response_class=HTMLResponse)
def case_review(request: Request) -> HTMLResponse:
    ranked = _ranked()
    return templates.TemplateResponse(
        request,
        "case_review.html",
        {
            "subject": SUBJECT,
            "ranked": ranked,
            "comps_total": len(ranked),
            "qc_counts": {
                "PRESS":     sum(1 for r in ranked if "PRESS"     in r["qc_flags"]),
                "CONNECTED": sum(1 for r in ranked if "CONNECTED" in r["qc_flags"]),
                "APPROX":    sum(1 for r in ranked if "APPROX"    in r["qc_flags"]),
                "NO_GFA":    sum(1 for r in ranked if "NO_GFA"    in r["qc_flags"]),
            },
        },
    )


@app.post("/generate")
def generate() -> JSONResponse:
    """Run the engine to produce the HK Rental Comps + T&R workbook.

    Returns the path to the generated file (relative) so the UI can show
    a download link. For the demo MVP the file is created on the local
    container filesystem at /tmp/bma-factory-demo/output/. v0.2 writes
    the file to the user's Drive `/Outputs/` folder.
    """
    from app.core.generate import generate as engine_generate
    from datetime import date as _date

    ranked = _ranked()
    values: dict[str, Any] = {
        "subject_address":    f"{SUBJECT['building']}, {SUBJECT['address']}",
        "subject_sa":         round(SUBJECT["nfa_sqft"] / 10.76391, 1),
        "subject_ac_platform": 0.0,
        "subject_ac_pct":     0.0,
        "subject_price_index": 325.1,                # RVD Aug 2026 (placeholder)
        "val_date":           SUBJECT["valuation_date"],
        "valuer":             SUBJECT["valuer"],
        "comps":              _comps_for_module(ranked),
        "adopted_unit_rate":  SUBJECT["adopted_rate_psf_n"] * 10.76391,  # $/sqm
        "term_years":         3,
        "term_yield_pct":     4.4,
        "reversion_yield_pct": 4.9,
    }
    out_dir = Path("/tmp/bma-factory-demo/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    result = engine_generate(
        module_id="hk-rental-comps",
        payload=values,
        caller="demo-ui",
        out_dir=out_dir,
    )
    artifacts = [
        {"filename": a.filename, "size": a.size, "sha256": a.sha256, "type": a.type}
        for a in result.artifacts
    ]
    return JSONResponse({
        "run_id":     result.run_id,
        "input_hash": result.input_hash,
        "warnings":   result.warnings,
        "artifacts":  artifacts,
        "download":   f"/download/{artifacts[0]['filename']}" if artifacts else None,
    })


@app.get("/download/{filename}")
def download(filename: str):
    p = Path("/tmp/bma-factory-demo/output") / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return FileResponse(
        path=str(p),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/case/bel-air-m2", include_in_schema=False)
def api_case() -> dict[str, Any]:
    """JSON view of the case (for the map view, MCP, etc.)."""
    ranked = _ranked()
    return {
        "subject": SUBJECT,
        "ranked":  ranked,
        "qc_counts": {
            "PRESS":     sum(1 for r in ranked if "PRESS"     in r["qc_flags"]),
            "CONNECTED": sum(1 for r in ranked if "CONNECTED" in r["qc_flags"]),
            "APPROX":    sum(1 for r in ranked if "APPROX"    in r["qc_flags"]),
            "NO_GFA":    sum(1 for r in ranked if "NO_GFA"    in r["qc_flags"]),
        },
    }
