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
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from demo.cases import Case, CaseData, CASES, get_case, get_cases_by_category, get_active_cases, get_history_cases
from ingest.relevance import Weights, rank, Scored, is_self as is_self_txn

log = logging.getLogger("bma-factory.demo")

app = FastAPI(title="bma-factory demo", version="0.2.0")
templates = Jinja2Templates(directory=str(_REPO / "demo" / "templates"))

# Static assets. The templates reference /static/style.css, so this mount is
# required — without it every page renders unstyled and the browser logs a 404.
_STATIC_DIR = _REPO / "demo" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:  # pragma: no cover - only when the image is built without demo/static
    log.warning("static directory not found at %s — /static will 404", _STATIC_DIR)


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
    from demo.oauth import get_flow, make_auth_context
    try:
        flow = get_flow()
    except RuntimeError as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in the container env."},
            status_code=503,
        )
    auth_url, state = flow.authorization_url(
        access_type="offline",          # request a refresh token
        include_granted_scopes="true",
        prompt="consent",               # force re-consent to get a fresh refresh token
    )
    # Carry the PKCE verifier and the CSRF state to the callback in a
    # signed cookie. The cookie travels with the browser, so the callback
    # works even if Cloud Run routes it to a different instance.
    ctx = make_auth_context(flow.code_verifier, state)
    resp = RedirectResponse(auth_url)
    resp.set_cookie(
        "bma_oauth_ctx", ctx,
        max_age=600, httponly=True, samesite="lax", secure=True, path="/",
    )
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", error: str = "",
                  state: str = ""):
    """Google redirects here with ?code=... after the user signs in.

    The PKCE code_verifier and the CSRF state both travel in a signed
    cookie set at /auth/login. Without the verifier the token exchange
    fails with invalid_grant; without the state check the callback is
    open to CSRF.
    """
    from demo.oauth import get_flow, make_auth_context, read_auth_context, save_credentials
    if error:
        return RedirectResponse(url=f"/?auth_error={error}", status_code=302)
    if not code:
        return RedirectResponse(url="/?auth_error=missing_code", status_code=302)

    ctx = read_auth_context(request.cookies.get("bma_oauth_ctx", ""))
    if ctx is None:
        log.warning("OAuth callback had no valid context cookie — "
                    "the sign-in took too long, or the cookie was dropped")
        return RedirectResponse(url="/?auth_error=expired_or_missing_state",
                                status_code=302)
    if state and ctx.get("s") and state != ctx["s"]:
        log.warning("OAuth state mismatch — possible CSRF, aborting")
        return RedirectResponse(url="/?auth_error=state_mismatch", status_code=302)

    try:
        flow = get_flow(code_verifier=ctx.get("v"))
        flow.fetch_token(code=code)
        save_credentials(flow.credentials)
    except Exception as e:
        log.exception("OAuth callback failed")
        return RedirectResponse(url=f"/?auth_error={type(e).__name__}", status_code=302)

    resp = RedirectResponse(url="/?auth=ok", status_code=302)
    resp.delete_cookie("bma_oauth_ctx", path="/")
    return resp


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    from demo.oauth import get_user_info, is_configured
    info = get_user_info()
    return {
        "oauth_configured": is_configured(),
        "signed_in": info is not None,
        "user": info,
    }


@app.get("/diag/secrets", include_in_schema=False)
def diag_secrets() -> JSONResponse:
    """Report what the app can do with Secret Manager (token durability)."""
    from demo.oauth import secret_store_status
    return JSONResponse(secret_store_status())


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


# ---------------------------------------------------------------------------
# Public pages required by the Google OAuth consent screen. The Branding page
# wants a home page, a privacy policy URL and a terms of service URL. Serving
# them from the app itself keeps the whole thing on one hostname and costs
# nothing.
# ---------------------------------------------------------------------------

_LEGAL_CSS = """
body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     color:#1f2328;background:#f6f7f9;margin:0;padding:40px 20px}
.wrap{max-width:720px;margin:0 auto;background:#fff;border:1px solid #e3e5e8;
      border-radius:6px;padding:32px 36px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.meta{color:#656d76;font-size:13px;margin:0 0 24px}
li{margin:6px 0}code{background:#f0f1f3;padding:1px 5px;border-radius:3px;font-size:13px}
"""


def _legal_page(title: str, updated: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · BMA Factory</title><style>{_LEGAL_CSS}</style></head>
<body><div class="wrap"><h1>{title}</h1>
<p class="meta">BMA Factory · last updated {updated}</p>
{body_html}
</div></body></html>"""
    )


@app.get("/privacy", include_in_schema=False)
def privacy() -> HTMLResponse:
    return _legal_page(
        "Privacy Policy",
        "20 October 2026",
        """
<p>BMA Factory is an internal tool operated by CHFT Valuation Advisory. It is
used by a single authorised valuer to prepare property valuation workpapers.</p>

<h2>What the app stores</h2>
<ul>
<li><b>Google authentication tokens</b> — an access token and a refresh token,
    written to the container's local filesystem so the app can write generated
    workbooks to Google Drive on the valuer's behalf. These are discarded when
    the container is redeployed or when you click <b>Sign out</b>.</li>
<li><b>Case data you upload</b> — property comparables, valuation parameters
    and generated workbooks, which are written to your own Google Drive.</li>
<li><b>An idempotency ledger</b> — a local record of generation runs, used to
    guarantee the same inputs always produce the same output file.</li>
</ul>

<h2>What the app does NOT do</h2>
<ul>
<li>It does not read files in your Drive that it did not create. The
    <code>drive.file</code> permission grants access only to files this app
    creates or that you explicitly open with it.</li>
<li>It does not sell, share or transmit your data to any third party.</li>
<li>It does not send your data to any AI or analytics service.</li>
</ul>

<h2>Third-party services</h2>
<p>The app connects to Google APIs (Drive, OAuth) and to public data sources
such as the Rating and Valuation Department and Centaline, to retrieve market
statistics and floor plan imagery for valuation reference.</p>

<h2>Data retention</h2>
<p>You can revoke the app's access at any time from your
<a href="https://myaccount.google.com/permissions">Google Account permissions</a>
page. Revoking access stops the app writing to Drive and deletes the stored
tokens.</p>

<h2>Contact</h2>
<p>CHFT Valuation Advisory · <code>bobby.hk.mak@gmail.com</code></p>
""",
    )


@app.get("/terms", include_in_schema=False)
def terms() -> HTMLResponse:
    return _legal_page(
        "Terms of Service",
        "20 October 2026",
        """
<h2>1. Use of the service</h2>
<p>BMA Factory is provided to authorised CHFT staff for the preparation of
property valuation workpapers. Access is personal to the valuer and may not be
shared. Generated outputs are drafts for professional review; they are not
final valuations and must not be issued or relied upon without the valuer's
review and signature.</p>

<h2>2. Professional responsibility</h2>
<p>The app assembles figures from data you supply and from public sources. It
does not exercise professional judgement. Every comparable selection, every
adopted value and every assumption recorded in the output remains the
valuer's responsibility, in accordance with the applicable valuation
standards.</p>

<h2>3. Data accuracy</h2>
<p>Market statistics and imagery are retrieved from third-party sources
including the Rating and Valuation Department and Centaline. The app does not
independently verify that third-party data is accurate or current. Check any
figure you rely upon against the source before use.</p>

<h2>4. Availability</h2>
<p>The service is provided on an as-is basis for internal use. It may be
interrupted for maintenance or changes in third-party services without notice.</p>

<h2>5. Liability</h2>
<p>To the fullest extent permitted by law, CHFT Valuation Advisory accepts no
liability for loss arising from reliance on the output of this tool prior to
professional review.</p>

<h2>6. Governing terms</h2>
<p>Use of this service constitutes acceptance of these terms. These terms may
be updated from time to time; the date above indicates the current version.</p>
""",
    )


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
