"""bma-factory demo — case library + per-case review.

Routes:
  GET  /                          — case library (categories + cards)
  GET  /case/<case_id>            — per-case review (ranked comps + Generate)
  GET  /api/cases                 — JSON list of all cases
  GET  /api/cases/<case_id>       — JSON view of one case
  POST /generate/<case_id>        — run the engine for one case
  GET  /download/<filename>      — serve a generated workbook
  GET  /diag                      — smoke test (always returns JSON)
  GET  /healthz                   — liveness

For the demo MVP the case data is embedded in `case_data.py` (Bel-Air M2
only). v0.2 swaps the loader to read ORO workbooks from the user's
Drive `/ORO/` folder.
"""

from __future__ import annotations

import io
import sys
import logging
import traceback
from pathlib import Path
from typing import Any

# Make the repo importable as a package root when running under uvicorn
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from demo.cases import Case, CaseData, CASES, get_case, get_cases_by_category, get_active_cases, get_history_cases
from ingest.relevance import Weights, rank, Scored, is_self as is_self_txn

log = logging.getLogger("bma-factory.demo")

app = FastAPI(title="bma-factory demo", version="0.2.0")
templates = Jinja2Templates(directory=str(_REPO / "demo" / "templates"))


# ---------------------------------------------------------------------------
# Per-case load + rank
# ---------------------------------------------------------------------------

def _load_case(case: Case) -> CaseData:
    if case.data_loader is None:
        raise HTTPException(status_code=501, detail=f"case '{case.id}' has no data loader yet")
    return case.data_loader()


def _score_breakdown(s: Scored) -> dict[str, float]:
    bd = s.breakdown
    return {
        "same_building":   round(bd.get("same_building", 0.0), 3),
        "distance":        round(bd.get("distance", 0.0), 3),
        "recency":         round(bd.get("recency", 0.0), 3),
        "area_similarity": round(bd.get("area_similarity", 0.0), 3),
        "usage_match":     round(bd.get("usage_match", 0.0), 3),
    }


def _ranked_for(case_data: CaseData, as_of: str) -> list[dict[str, Any]]:
    subject = {
        "name":     case_data.subject.get("building", ""),
        "building": case_data.subject.get("building", ""),
        "address":  case_data.subject.get("address", ""),
        "nfa_sqft": case_data.subject.get("nfa_sqft", 0),
        "area_sf":  case_data.subject.get("nfa_sqft", 0),
        "usage":    case_data.subject.get("usage", ""),
        "case_ref": case_data.subject.get("case_ref", ""),
    }
    pool = case_data.comps
    w = Weights()
    ranked = rank(subject, pool, as_of=as_of, w=w)
    out: list[dict[str, Any]] = []
    for s in ranked:
        c = s.comp
        hit, why = is_self_txn(subject, c)
        out.append({
            "rank":         len(out) + 1,
            "src_row":      c.get("src_row"),
            "name":         c.get("name"),
            "address":      c.get("address"),
            "floor":        c.get("floor"),
            "unit":         c.get("unit"),
            "nfa_sqft":     c.get("nfa_sqft"),
            "psf_net":      c.get("psf_net"),
            "inst_date":    str(c.get("inst_date") or c.get("date") or ""),
            "nat_inst":     c.get("nat_inst"),
            "grade":        c.get("grade"),
            "qc_flags":     c.get("qc_flags") or [],
            "score":        round(s.total, 3),
            "breakdown":    _score_breakdown(s),
            "is_self":      hit,
            "self_reason":  why,
        })
    return out


def _comps_for_module(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the ranked comps into the shape the hk-rental-comps module wants."""
    out: list[dict[str, Any]] = []
    for r in ranked:
        if r["is_self"]:
            continue
        monthly_rent = r["psf_net"] * r["nfa_sqft"] / 12.0
        rent_str = f"HK${monthly_rent:,.0f}"
        out.append({
            "address":          r["address"],
            "year":             int(r["inst_date"][:4]) if r["inst_date"] else 2025,
            "transaction_date": r["inst_date"],
            "rent":             rent_str,
            "sa":               round(r["nfa_sqft"] / 10.76391, 1),
            "floor":            r["floor"],
            "aspect":           "—",
            "price_index":      325.1,
            "adj_floor":        "0%",
            "adj_qa":           "0%",
            "adj_sw":           "0%",
            "adj_age":          "0%",
            "adj_location":     "0%",
            "remarks":          f"Rank #{r['rank']} by relevancy; score {r['score']:.3f}. "
                               + (f"QC: {', '.join(r['qc_flags'])}" if r['qc_flags'] else "No QC flags."),
        })
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """BMA Factory work console — dashboard for daily valuation work."""
    active_cases  = get_active_cases()
    history_cases = get_history_cases()
    auth = {"signed_in": False, "user": None, "configured": True}
    try:
        from demo.oauth import get_user_info, is_configured
        auth["configured"] = is_configured()
        if auth["configured"]:
            auth["user"] = get_user_info()
            auth["signed_in"] = auth["user"] is not None
    except Exception:
        pass
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active_cases":  active_cases,
            "history_cases": history_cases,
            "auth":          auth,
        },
    )


@app.get("/case/{case_id}", response_class=HTMLResponse)
def case_review(request: Request, case_id: str) -> HTMLResponse:
    """Per-case review — ranked comps, QC flags, Generate button."""
    case = get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"case '{case_id}' not found")
    case_data = _load_case(case)
    ranked = _ranked_for(case_data, as_of=case.valuation_date)
    return templates.TemplateResponse(
        request,
        "case_review.html",
        {
            "case":        case,
            "subject":     case_data.subject,
            "ranked":      ranked,
            "comps_total": len(ranked),
            "qc_counts": {
                "PRESS":     sum(1 for r in ranked if "PRESS"     in r["qc_flags"]),
                "CONNECTED": sum(1 for r in ranked if "CONNECTED" in r["qc_flags"]),
                "APPROX":    sum(1 for r in ranked if "APPROX"    in r["qc_flags"]),
                "NO_GFA":    sum(1 for r in ranked if "NO_GFA"    in r["qc_flags"]),
            },
        },
    )


@app.get("/api/cases", include_in_schema=False)
def api_cases() -> dict[str, Any]:
    """JSON list of all cases (for the map / MCP / future UI bits)."""
    return {
        "total":  len(CASES),
        "cases":  [
            {
                "id":               c.id,
                "title":            c.title,
                "category":         c.category,
                "case_ref":         c.case_ref,
                "client":           c.client,
                "address":          c.address,
                "district":         c.district,
                "valuation_date":   c.valuation_date,
                "adopted_value_hkd": c.adopted_value_hkd,
                "nfa_sqft":         c.nfa_sqft,
                "usage":            c.usage,
                "summary":          c.summary,
                "url":              f"/case/{c.id}",
            }
            for c in CASES
        ],
    }


@app.get("/api/cases/{case_id}", include_in_schema=False)
def api_case(case_id: str) -> dict[str, Any]:
    case = get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"case '{case_id}' not found")
    case_data = _load_case(case)
    ranked = _ranked_for(case_data, as_of=case.valuation_date)
    return {
        "case":     case,
        "subject":  case_data.subject,
        "ranked":   ranked,
        "qc_counts": {
            "PRESS":     sum(1 for r in ranked if "PRESS"     in r["qc_flags"]),
            "CONNECTED": sum(1 for r in ranked if "CONNECTED" in r["qc_flags"]),
            "APPROX":    sum(1 for r in ranked if "APPROX"    in r["qc_flags"]),
            "NO_GFA":    sum(1 for r in ranked if "NO_GFA"    in r["qc_flags"]),
        },
    }


@app.post("/generate/{case_id}")
def generate(case_id: str) -> JSONResponse:
    """Run the engine for a given case. Returns the artifact + download link.

    On any failure the full traceback is returned as JSON so the UI can
    show it (this is a demo — production would log + return 500 only).
    """
    from app.core.generate import generate as engine_generate
    from app.core.schemas import validate as schema_validate
    from app.core.registry import get_module

    case = get_case(case_id)
    if case is None:
        return JSONResponse({"ok": False, "stage": "lookup", "error": f"case '{case_id}' not found"}, status_code=200)
    case_data = _load_case(case)
    ranked = _ranked_for(case_data, as_of=case.valuation_date)

    subject = case_data.subject
    values: dict[str, Any] = {
        "subject_address":    f"{subject.get('building', '')}, {subject.get('address', '')}",
        "subject_sa":         round(subject.get("nfa_sqft", 0) / 10.76391, 1),
        "subject_ac_platform": 0.0,
        "subject_ac_pct":     "0%",
        "subject_price_index": 325.1,
        "val_date":           case.valuation_date,
        "valuer":             subject.get("valuer", "BMA"),
        "comps":              _comps_for_module(ranked),
        "adopted_unit_rate":  case.adopted_rate_psf_n * 10.76391,
        "term_years":         3,
        "term_rate":          "4.4%",
        "reversion_rate":     "4.9%",
    }

    # Pre-flight: surface validation errors as JSON before the engine
    try:
        loaded = get_module("hk-rental-comps")
        vr = schema_validate(loaded.manifest.schema(), values)
        if not vr.ok:
            return JSONResponse({
                "ok": False,
                "stage": "validate",
                "errors": [
                    {"field": i.field, "message": i.message, "severity": str(i.severity)}
                    for i in vr.errors
                ],
                "warnings": [
                    {"field": i.field, "message": i.message}
                    for i in vr.warnings
                ],
            }, status_code=200)
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "stage": "preflight",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc().splitlines()[-15:],
        }, status_code=200)

    out_dir = Path("/tmp/bma-factory-demo/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = engine_generate(
            module_id="hk-rental-comps",
            payload=values,
            caller="demo-ui",
            out_dir=out_dir,
        )
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "stage": "engine",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc().splitlines()[-25:],
        }, status_code=200)

    artifacts = [
        {"filename": a.filename, "size": a.size, "sha256": a.sha256, "type": a.type}
        for a in result.artifacts
    ]

    # Drive push — best effort, the local download link is the fallback
    drive_meta: dict | None = None
    drive_error: str | None = None
    if artifacts:
        try:
            from demo.drive import ensure_drive_layout, upload_to_outputs
            ensure_drive_layout()
            local_path = Path(result.artifacts[0].path)
            drive_meta = upload_to_outputs(local_path)
        except Exception as e:
            drive_error = f"{type(e).__name__}: {e}"
            log.warning("Drive upload failed: %s", drive_error)

    return JSONResponse({
        "ok":         True,
        "case_id":    case_id,
        "run_id":     result.run_id,
        "input_hash": result.input_hash,
        "warnings":   result.warnings,
        "artifacts":  artifacts,
        "download":   f"/download/{artifacts[0]['filename']}" if artifacts else None,
        "drive":      drive_meta,
        "drive_error": drive_error,
    })


@app.on_event("startup")
async def _on_startup() -> None:
    """Create the Drive folder layout on first boot (idempotent, best effort)."""
    try:
        from demo.drive import ensure_drive_layout
        layout = ensure_drive_layout()
        log.info("Drive layout ready: %s", layout)
    except Exception as e:
        # Don't crash the container if Drive is unavailable; the /generate
        # endpoint will surface the error.
        log.warning("Drive layout not created on startup: %s", e)


# ---------------------------------------------------------------------------
# OAuth2 routes — the valuer signs in with their own Google account so the
# engine can write to their personal Drive (uses their 15 GB quota, not a
# service account's zero quota).
# ---------------------------------------------------------------------------

@app.get("/auth/login")
def auth_login():
    """Redirect to Google's consent screen."""
    from demo.oauth import get_flow
    try:
        flow = get_flow()
    except RuntimeError as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in the container env."},
            status_code=503,
        )
    auth_url, _state = flow.authorization_url(
        access_type="offline",          # request a refresh token
        include_granted_scopes="true",
        prompt="consent",               # force re-consent to get a fresh refresh token
    )
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
def auth_callback(code: str = "", error: str = ""):
    """Google redirects here with ?code=... after the user signs in."""
    from demo.oauth import get_flow, save_credentials
    if error:
        return RedirectResponse(url=f"/?auth_error={error}", status_code=302)
    if not code:
        return RedirectResponse(url="/?auth_error=missing_code", status_code=302)
    try:
        flow = get_flow()
        flow.fetch_token(code=code)
        save_credentials(flow.credentials)
    except Exception as e:
        log.exception("OAuth callback failed")
        return RedirectResponse(url=f"/?auth_error={type(e).__name__}", status_code=302)
    return RedirectResponse(url="/?auth=ok", status_code=302)


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    from demo.oauth import get_user_info, is_configured
    info = get_user_info()
    return {
        "oauth_configured": is_configured(),
        "signed_in": info is not None,
        "user": info,
    }


@app.post("/auth/signout")
def auth_signout() -> dict[str, str]:
    from demo.oauth import clear_credentials
    clear_credentials()
    return {"ok": "true"}


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


# Backwards compat: POST /generate with no case_id uses bel-air-m2
@app.post("/generate", include_in_schema=False)
def generate_legacy() -> RedirectResponse:
    return RedirectResponse(url="/generate/bel-air-m2", status_code=307)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/diag", include_in_schema=False)
def diag() -> JSONResponse:
    """Smoke-test everything and return every diagnostic as JSON."""
    import sys
    diag: dict[str, Any] = {
        "ok": True,
        "python": sys.version.split()[0],
        "cases_total": len(CASES),
        "case_ids": [c.id for c in CASES],
    }
    try:
        from app.core.registry import get_module
        loaded = get_module("hk-rental-comps")
        diag["module_id"] = loaded.manifest.id
        diag["module_version"] = loaded.manifest.version
    except Exception as e:
        diag["ok"] = False
        diag["module_load_error"] = f"{type(e).__name__}: {e}"
        diag["traceback"] = traceback.format_exc().splitlines()[-10:]
        return JSONResponse(diag, status_code=200)

    try:
        case = get_case("bel-air-m2")
        if case is None:
            diag["ok"] = False
            diag["case_lookup"] = "missing"
            return JSONResponse(diag, status_code=200)
        case_data = _load_case(case)
        ranked = _ranked_for(case_data, as_of=case.valuation_date)
        diag["ranked_count"] = len(ranked)
        diag["top_score"] = ranked[0]["score"] if ranked else None
    except Exception as e:
        diag["ok"] = False
        diag["rank_error"] = f"{type(e).__name__}: {e}"
        diag["traceback"] = traceback.format_exc().splitlines()[-10:]
        return JSONResponse(diag, status_code=200)

    try:
        out_dir = Path("/tmp/bma-factory-demo/output")
        out_dir.mkdir(parents=True, exist_ok=True)
        test_file = out_dir / ".diag-test"
        test_file.write_text("ok")
        test_file.unlink()
        diag["fs_writable"] = True
    except Exception as e:
        diag["ok"] = False
        diag["fs_error"] = f"{type(e).__name__}: {e}"
        return JSONResponse(diag, status_code=200)

    return JSONResponse(diag, status_code=200)
