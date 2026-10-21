"""bma-factory demo — work console, per-case review, and content intake.

Routes:
  GET  /                          — work console (dashboard)
  GET  /case/<case_id>            — per-case review (ranked comps + Generate)
  GET  /intake                    — content page: workbooks in Drive
  GET  /content/<file_id>         — the parsed content of one workbook
  POST /upload                    — put a workbook into Drive /Intake/
  GET  /api/cases                 — JSON list of all cases
  GET  /api/cases/<case_id>       — JSON view of one case
  GET  /api/intake                — JSON view of the intake listing
  POST /generate/<case_id>        — run the engine for one case
  GET  /download/<filename>      — serve a generated workbook
  GET  /diag                      — smoke test (always returns JSON)
  GET  /healthz                   — liveness

Case data comes from two places. The Bel-Air M2 demo case is embedded in
`case_data.py`; anything the valuer drops into `Drive:/Intake/` is
parsed by `demo/intake.py`, and a Memfus Wong extract carries its own
subject row, so it becomes a project without being re-keyed.
"""

from __future__ import annotations

import io
import sys
import logging
import traceback
from datetime import date, datetime
from urllib.parse import quote_plus
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
    try:
        return case.data_loader()
    except ValueError as e:
        # A Drive-derived project whose source workbook no longer yields a
        # subject row. Say so plainly rather than showing an empty page.
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.warning("could not load case %s: %s", case.id, e)
        raise HTTPException(
            status_code=502,
            detail=f"could not read the source workbook for '{case.title}': {e}",
        ) from e


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
    folder_ids, whole_drive = _scan_setting(request)
    # List Drive once and reuse it. get_active_cases and get_history_cases
    # would each list Drive on their own, and listing Drive is the
    # slowest thing this app does.
    from demo.cases import get_drive_cases
    drive_cases = get_drive_cases(folder_ids, whole_drive)
    active_cases  = get_active_cases(folder_ids, whole_drive, drive_cases)
    history_cases = get_history_cases(folder_ids, whole_drive, drive_cases)
    # One implementation of "who is signed in" — the console and the
    # content page must agree, including on when the sign-in expires.
    auth = _auth_state()
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
    folder_ids, whole_drive = _scan_setting(request)
    case = get_case(case_id, folder_ids, whole_drive)
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


# ---------------------------------------------------------------------------
# Content intake — workbooks in Drive, parsed and shown
# ---------------------------------------------------------------------------

def _auth_state() -> dict[str, Any]:
    """Who is signed in, if anyone — and when that sign-in stops working."""
    state: dict[str, Any] = {
        "signed_in": False, "user": None, "configured": True,
        "expires_on": None, "expired": False,
    }
    try:
        from demo.oauth import (
            get_user_info, is_configured, sign_in_expiry, token_expired,
        )
        state["configured"] = is_configured()
        if state["configured"]:
            state["user"] = get_user_info()
            state["signed_in"] = state["user"] is not None
            state["expires_on"] = sign_in_expiry()
            state["expired"] = token_expired()
    except Exception:  # noqa: BLE001
        pass
    return state


def _scan_setting(request: Request | None) -> tuple[tuple[str, ...], bool]:
    """Which extra folders the valuer is pointing the app at."""
    from demo import intake
    if request is None:
        return (), False
    return intake.parse_scan_setting(request.cookies.get(intake.SCAN_COOKIE))


def _intake_rows(
    extra_folder_ids: tuple[str, ...] = (),
    whole_drive: bool = False,
) -> list[dict[str, Any]]:
    """The intake listing, one row per Drive workbook."""
    from demo import intake

    rows: list[dict[str, Any]] = []
    try:
        files = intake.list_drive_workbooks(
            folder_ids=extra_folder_ids, whole_drive=whole_drive
        )
    except Exception as e:  # noqa: BLE001
        log.warning("intake listing failed: %s", e)
        return rows
    for f in files:
        try:
            parsed = intake.parse_drive_file(f)
        except Exception as e:  # noqa: BLE001
            log.warning("intake parse failed for %s: %s", e)
            continue
        d = f.to_dict()
        d.update({
            "format_id": parsed.format_id,
            "format_label": parsed.format_label,
            "total_rows": parsed.total_rows,
            "qc_counts": parsed.qc_counts,
            "error": parsed.error,
            "project": intake.project_from_sections(parsed),
            "sections": [
                {"id": s.get("id"), "label": s.get("label"),
                 "kind": s.get("kind"), "sheet": s.get("sheet"),
                 "rows": len(s.get("rows") or [])}
                for s in parsed.sections
            ],
        })
        rows.append(d)
    return rows


@app.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request) -> HTMLResponse:
    """The content page — Excel files from Drive, parsed and listed."""
    from concurrent.futures import ThreadPoolExecutor

    from demo import intake
    from demo.oauth import can_read_all_drive, granted_scopes

    auth = _auth_state()
    folder_ids, whole_drive = _scan_setting(request)
    signed_in = auth["signed_in"]

    # Three independent things: the file listing, the folder picker and
    # the granted scopes. They each cost a round trip, so do them at the
    # same time rather than in series.
    rows: list[dict[str, Any]] = []
    folders: list[dict[str, str]] = []
    if signed_in:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_rows = pool.submit(_intake_rows, folder_ids, whole_drive)
            f_folders = pool.submit(intake.list_drive_folders)
            f_scopes = pool.submit(granted_scopes)
            rows = f_rows.result()
            folders = f_folders.result()
            scopes = f_scopes.result()
    else:
        scopes = []

    # A sign-in that only holds `drive.file` cannot see files the valuer
    # put in Drive himself. Say so, rather than showing an empty list that
    # looks like the app is broken.
    limited = signed_in and not can_read_all_drive()
    return templates.TemplateResponse(
        request,
        "intake.html",
        {
            "auth": auth,
            "files": rows,
            "upload_error": None,
            "limited_scope": limited,
            "granted_scopes": scopes,
            "folders": folders,
            "scan_folders": folder_ids,
            "scan_whole_drive": whole_drive,
        },
    )


@app.post("/intake/scan")
async def set_scan(request: Request):
    """Choose which Drive folders the app reads.

    Keeps the console and the content page looking at the same place, so
    a project that appears in one appears in the other.
    """
    from demo import intake

    form = await request.form()
    choice = (form.get("folders") or "")
    if isinstance(choice, list):
        choice = choice[0] if choice else ""
    choice = str(choice).strip()

    if choice == "all":
        value = "all"
    elif choice == "default":
        value = ""
    else:
        value = ",".join(
            x for x in (part.strip() for part in choice.split(",")) if x
        )

    response = RedirectResponse("/intake", status_code=303)
    if value:
        response.set_cookie(
            intake.SCAN_COOKIE, value, max_age=60 * 60 * 24 * 90,
            httponly=True, samesite="lax",
        )
    else:
        response.delete_cookie(intake.SCAN_COOKIE)
    return response


@app.get("/drive/layout", include_in_schema=False)
def drive_layout() -> JSONResponse:
    """Where the app's folders are in Drive, and where they should be."""
    from demo.drive import describe_layout
    return JSONResponse(describe_layout())


@app.post("/drive/organise", include_in_schema=False)
def drive_organise() -> JSONResponse:
    """Move the app's folders under the configured root path.

    Idempotent and scoped: it touches only the four folders the app
    owns, and only moves ones that are direct children of the Drive
    root. Anything it cannot do, it reports rather than guessing.
    """
    from demo.drive import organise_drive
    report = organise_drive()
    return JSONResponse({"ok": not report.get("errors"), "report": report})


# ---------------------------------------------------------------------------
# Section rules — the valuer's own arrangement templates
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sales Brochure Review — the quarterly re-examination routine
# ---------------------------------------------------------------------------

@app.get("/brochure", response_class=HTMLResponse)
def brochure_index(request: Request) -> HTMLResponse:
    """The developments under review, grouped by business line."""
    from demo import brochure as B
    from demo.sources import check_all

    auth = _auth_state()
    devs = B.load_developments()
    today = date.today()
    groups = []
    for line, members in B.by_business_line(devs):
        groups.append({
            "line": line,
            "items": [B.summarise(d, today) for d in members],
        })
    return templates.TemplateResponse(
        request,
        "brochure.html",
        {
            "auth": auth,
            "groups": groups,
            "total": len(devs),
            "sources": check_all(devs[0]) if devs else {},
        },
    )


@app.get("/brochure/new", response_class=HTMLResponse)
def brochure_new(request: Request) -> HTMLResponse:
    """The form for adding a development."""
    from demo import brochure as B
    return templates.TemplateResponse(
        request,
        "brochure_new.html",
        {"auth": _auth_state(), "today": date.today()},
    )


@app.post("/brochure/new")
async def brochure_create(request: Request):
    """Add a development and open its first round."""
    from demo import brochure as B

    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/brochure/new?error=Give the development a name.",
                                status_code=303)

    devs = B.load_developments()
    dev = B.Development(
        id=B.slugify(name),
        name=name,
        dev_id=str(form.get("dev_id") or "").strip(),
        address=str(form.get("address") or "").strip(),
        district=str(form.get("district") or "").strip(),
        client=str(form.get("client") or "").strip(),
        business_line=str(form.get("business_line") or "").strip(),
    )
    # A development id is the only way into the platform, and without
    # it the whole deadline chain is unreachable.
    if not dev.dev_id:
        return RedirectResponse(
            "/brochure/new?error=The+SRPE+development+id+is+needed+—+it+is+the+"
            "devId+in+the+platform's+link.",
            status_code=303)
    dev.open_round(date.today())
    devs = [d for d in devs if d.id != dev.id] + [dev]
    ok, detail = B.save_developments(devs)
    if not ok:
        return RedirectResponse(f"/brochure/new?error=Could+not+save%3A+{detail}",
                                status_code=303)
    return RedirectResponse(f"/brochure/{dev.id}", status_code=303)


@app.get("/brochure/{dev_id}/edit", response_class=HTMLResponse)
def brochure_edit(request: Request, dev_id: str) -> HTMLResponse:
    """Correct a development's details.

    Details go wrong — a district typed from memory, a developer
    mis-remembered — and there has to be a way to fix one without
    editing the data file by hand.
    """
    from demo import brochure as B
    devs = B.load_developments()
    dev = next((d for d in devs if d.id == dev_id), None)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such development")
    return templates.TemplateResponse(
        request,
        "brochure_new.html",
        {
            "auth": _auth_state(),
            "today": date.today(),
            "dev": dev.to_dict(),
            "editing": True,
        },
    )


@app.post("/brochure/{dev_id}/edit")
async def brochure_update(request: Request, dev_id: str):
    """Save the corrected details. The rounds are left alone."""
    from demo import brochure as B

    form = await request.form()
    devs = B.load_developments()
    dev = next((d for d in devs if d.id == dev_id), None)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such development")

    new_name = str(form.get("name") or "").strip()
    if not new_name:
        return RedirectResponse(
            f"/brochure/{dev_id}/edit?error=Give the development a name.",
            status_code=303)
    dev.name = new_name
    dev.dev_id = str(form.get("dev_id") or "").strip()
    dev.address = str(form.get("address") or "").strip()
    dev.district = str(form.get("district") or "").strip()
    dev.client = str(form.get("client") or "").strip()
    dev.business_line = str(form.get("business_line") or "").strip()

    ok, detail = B.save_developments(devs)
    if not ok:
        return RedirectResponse(
            f"/brochure/{dev_id}/edit?error=Could+not+save%3A+{detail}",
            status_code=303)
    return RedirectResponse(f"/brochure/{dev_id}?saved=Details+updated", status_code=303)


@app.get("/brochure/{dev_id}", response_class=HTMLResponse)
def brochure_development(request: Request, dev_id: str) -> HTMLResponse:
    """One development: its rounds, and the round's stages as sub-tabs."""
    from demo import brochure as B
    from demo.sources import check_all

    auth = _auth_state()
    devs = B.load_developments()
    dev = next((d for d in devs if d.id == dev_id), None)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such development")

    today = date.today()
    rnd = dev.current_round
    if rnd is None:
        rnd = dev.open_round(today)

    items = [rnd.item_state(i) for i in B.CHECK_ITEMS]
    stages = []
    for stage in B.STAGES:
        if stage.kind == "items":
            stages.append({
                "stage": stage,
                "items": items,
                "done": sum(1 for i in items if i["verdict"] != "unchecked"),
                "total": len(items),
                "needing": sum(1 for i in items if i["verdict"] == "update"),
            })
        else:
            stages.append({
                "stage": stage,
                "deadline": B.fmt_date(rnd.deadline),
                "target": B.fmt_date(rnd.target_date),
                "last_actual": B.fmt_date(rnd.last_actual_date),
                "actual": B.fmt_date(rnd.actual_date),
                "days_to_deadline": rnd.days_to_deadline(today),
                "note": rnd.stage0_note,
            })

    return templates.TemplateResponse(
        request,
        "brochure_development.html",
        {
            "auth": auth,
            "dev": dev.to_dict(),
            "summary": B.summarise(dev, today),
            "rounds": [
                {
                    "number": r.number,
                    "opened": r.opened,
                    "closed": r.closed,
                    "last_actual": B.fmt_date(B.parse_date(r.last_actual)),
                    "actual": B.fmt_date(B.parse_date(r.actual)),
                    "deadline": B.fmt_date(r.deadline),
                    "target": B.fmt_date(r.target_date),
                }
                for r in sorted(dev.rounds, key=lambda x: -x.number)
            ],
            "round": rnd,
            "stages": stages,
            "sources": check_all(dev),
            "today": date.today(),
            "srpe_link": _srpe_link(dev.dev_id),
        },
    )


def _srpe_link(dev_id: str) -> str:
    from demo.sources import srpe_development_link
    return srpe_development_link(dev_id)


@app.post("/brochure/{dev_id}/round")
async def brochure_open_round(request: Request, dev_id: str):
    """Open the next round, carrying the dates and the versions forward."""
    from demo import brochure as B

    devs = B.load_developments()
    dev = next((d for d in devs if d.id == dev_id), None)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such development")
    dev.open_round(date.today())
    ok, detail = B.save_developments(devs)
    if not ok:
        return RedirectResponse(f"/brochure/{dev_id}?error=Could+not+save%3A+{detail}",
                                status_code=303)
    return RedirectResponse(f"/brochure/{dev_id}", status_code=303)


@app.post("/brochure/{dev_id}/round/{number}")
async def brochure_save_round(request: Request, dev_id: str, number: int):
    """Save a round — the deadline fields, an item, a note, or a close."""
    from demo import brochure as B

    form = await request.form()
    action = str(form.get("action") or "stage0")

    devs = B.load_developments()
    dev = next((d for d in devs if d.id == dev_id), None)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such development")
    rnd = dev.round(number)
    if rnd is None:
        raise HTTPException(status_code=404, detail="no such round")

    message = "Saved."

    if action == "stage0":
        rnd.last_actual = str(form.get("last_actual") or "").strip()
        rnd.target = str(form.get("target") or "").strip()
        rnd.actual = str(form.get("actual") or "").strip()
        rnd.stage0_note = str(form.get("stage0_note") or "").strip()
        if rnd.actual and not rnd.closed:
            rnd.closed = True
            message = "Saved, and the round is closed — the actual upload date "\
                      "becomes the next round's starting point."

    elif action == "item":
        item_id = str(form.get("item_id") or "")
        item = next((i for i in B.CHECK_ITEMS if i.id == item_id), None)
        if item is None:
            raise HTTPException(status_code=400, detail="unknown item")
        state = dict(rnd.items.get(item_id) or {})
        state["brochure_version"] = str(form.get("brochure_version") or "").strip()
        state["note"] = str(form.get("note") or "").strip()
        state["checked_at"] = datetime.now().isoformat(timespec="seconds")
        state["checked_by"] = str(form.get("checked_by") or "").strip()

        evidence = await _read_evidence(form, item, dev, state.get("evidence") or [])
        state["evidence"] = evidence
        # The source version is the latest piece of evidence. Kept as a
        # field as well, so an older record still reads sensibly.
        dated = [e for e in evidence if e.get("date")]
        if dated:
            state["source_version"] = max(
                dated, key=lambda e: e["date"]
            )["date"]
        else:
            state["source_version"] = ""

        if str(form.get("action_flag") or "") == "brochure_updated":
            # The brochure has been re-issued with the new version, so
            # that is what it now carries.
            if state["source_version"]:
                state["brochure_version"] = state["source_version"]
                message = "Recorded — the brochure now carries the new version."
        rnd.items[item_id] = state

    elif action == "note":
        rnd.notes[str(form.get("stage") or "")] = str(form.get("note") or "").strip()

    elif action == "close":
        rnd.closed = not rnd.closed
        message = "Round closed." if rnd.closed else "Round reopened."

    ok, detail = B.save_developments(devs)
    if not ok:
        return RedirectResponse(
            f"/brochure/{dev_id}?error=Could+not+save%3A+{detail}", status_code=303)
    return RedirectResponse(
        f"/brochure/{dev_id}?saved={quote_plus(message)}#stage-{action}", status_code=303)


async def _read_evidence(
    form: Any,
    item: Any,
    dev: Any,
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read the evidence rows off the form, and attach any screenshots.

    The rows are parallel lists — ``ev_ref``, ``ev_title``, ``ev_date``
    and ``ev_file`` — so row *i* of each belongs together. A row with
    nothing in it is dropped; a row with a file but no text keeps the
    file, because the picture is the evidence.
    """
    def _list(name: str) -> list[Any]:
        v = form.getlist(name)
        return list(v) if isinstance(v, list) else ([v] if v else [])

    refs = _list("ev_ref")
    titles = _list("ev_title")
    dates = _list("ev_date")
    files = _list("ev_file")

    # The evidence already on file keeps its screenshot, matched by row.
    prior = list(existing or [])
    out: list[dict[str, Any]] = []
    for i in range(max(len(refs), len(titles), len(dates), len(files))):
        ref = str(refs[i] or "").strip() if i < len(refs) else ""
        title = str(titles[i] or "").strip() if i < len(titles) else ""
        date = str(dates[i] or "").strip() if i < len(dates) else ""
        upload = files[i] if i < len(files) else None
        has_file = hasattr(upload, "filename") and bool(getattr(upload, "filename", ""))

        if not (ref or title or date or has_file):
            continue

        row = {
            "ref": ref,
            "title": title,
            "date": date,
            "kind": item.evidence_kind,
            "file_id": "",
            "file_name": "",
        }
        # Keep the screenshot already attached to this row.
        if i < len(prior) and prior[i].get("file_id"):
            row["file_id"] = prior[i]["file_id"]
            row["file_name"] = prior[i].get("file_name", "")

        if has_file:
            name = getattr(upload, "filename", "") or "evidence"
            data = await _read_upload(upload)
            if data:
                saved = _store_evidence_file(dev, item, name, data)
                if saved:
                    row["file_id"] = saved.get("id", "")
                    row["file_name"] = saved.get("name", name)
        out.append(row)
    return out


async def _read_upload(upload: Any) -> bytes:
    """The bytes of an uploaded file, or nothing."""
    try:
        reader = getattr(upload, "read", None)
        if reader is None:
            return b""
        data = reader()
        if hasattr(data, "__await__"):
            data = await data
        return data or b""
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the uploaded evidence: %s", e)
        return b""


def _store_evidence_file(
    dev: Any, item: Any, name: str, data: bytes,
) -> dict[str, Any] | None:
    """Put a screenshot in the development's own folder in Drive."""
    try:
        from demo.drive import DRIVE_ROOT_PATH, resolve_path
        from demo.oauth import get_drive
        from googleapiclient.http import MediaIoBaseUpload
        import io as _io

        folder = resolve_path(
            f"{DRIVE_ROOT_PATH}/SalesBrochure/{dev.id}/Evidence")
        drive = get_drive()
        media = MediaIoBaseUpload(
            _io.BytesIO(data), mimetype="application/octet-stream", resumable=False)
        f = drive.files().create(
            body={"name": name, "parents": [folder]},
            media_body=media,
            fields="id,name,webViewLink", supportsAllDrives=True,
        ).execute()
        return f
    except Exception as e:  # noqa: BLE001
        log.warning("could not store the evidence file: %s", e)
        return None


@app.get("/api/brochure", include_in_schema=False)
def brochure_api() -> JSONResponse:
    """The reviews as JSON."""
    from demo import brochure as B
    today = date.today()
    devs = B.load_developments()
    return JSONResponse({
        "developments": [B.summarise(d, today) for d in devs],
        "stages": [
            {"number": s.number, "name": s.name, "kind": s.kind}
            for s in B.STAGES
        ],
        "items": [
            {"id": i.id, "name": i.name, "source": i.source}
            for i in B.CHECK_ITEMS
        ],
    })


@app.get("/intake/rules", response_class=HTMLResponse)
def rules_page(request: Request) -> HTMLResponse:
    """Build and edit the rules that decide what a section is."""
    from demo import rules as R

    auth = _auth_state()
    rules = R.current_rules()
    # The files the rules would be tested against.
    candidates: list[dict[str, Any]] = []
    columns: list[str] = []
    if auth["signed_in"]:
        try:
            from demo import intake
            parsed_files = []
            for f in intake.list_drive_workbooks()[:20]:
                candidates.append({"id": f.id, "name": f.name})
                try:
                    parsed_files.append(intake.parse_drive_file(f))
                except Exception:  # noqa: BLE001
                    continue
            columns = R.known_columns(parsed_files)
        except Exception as e:  # noqa: BLE001
            log.warning("could not list candidates for the rules page: %s", e)
    return templates.TemplateResponse(
        request,
        "rules.html",
        {
            "auth": auth,
            "rules": [r.to_dict() for r in rules],
            "kinds": [{"id": k, "label": R.KIND_LABELS[k]} for k in R.KINDS],
            "candidates": candidates,
            "columns": columns,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/intake/rules", include_in_schema=False)
async def rules_save(request: Request):
    """Add, change, reorder or delete a rule."""
    from demo import rules as R

    form = await request.form()
    action = str(form.get("action") or "add")
    # Read fresh, not the cached copy — an edit must not be made against
    # a list that is up to a minute out of date.
    current = R.load_rules()

    def _by_id(rid: str) -> R.SectionRule | None:
        return next((r for r in current if r.id == rid), None)

    def _from_form(d: dict[str, Any]) -> R.SectionRule:
        return R.SectionRule.from_dict(d)

    message = ""
    ok = True

    if action == "delete":
        rid = str(form.get("rule_id") or "")
        current = [r for r in current if r.id != rid]
        message = "Rule deleted." if _by_id(rid) else "That rule was already gone."

    elif action == "toggle":
        rid = str(form.get("rule_id") or "")
        target = _by_id(rid)
        if target is not None:
            target.enabled = not target.enabled
            message = "Rule turned on." if target.enabled else "Rule turned off."

    elif action == "move":
        rid = str(form.get("rule_id") or "")
        direction = str(form.get("direction") or "up")
        idx = next((i for i, r in enumerate(current) if r.id == rid), -1)
        if idx >= 0:
            j = idx - 1 if direction == "up" else idx + 1
            if 0 <= j < len(current):
                current[idx], current[j] = current[j], current[idx]
                message = "Order changed."

    elif action == "add":
        rule = _from_form({
            "label": form.get("label"),
            "kind": form.get("kind"),
            "sheet": form.get("sheet"),
            "header_contains": form.get("header_contains"),
            "header_contains_any": form.get("header_contains_any"),
            "row_column": form.get("row_column"),
            "row_value": form.get("row_value"),
        })
        if not rule.label.strip():
            ok, message = False, "Give the rule a name so you can recognise it."
        elif not (rule.header_contains or rule.header_contains_any
                  or (rule.row_column and rule.row_value)):
            ok, message = False, "A rule needs something to match on."
        else:
            current.append(rule)
            message = "Rule added."

    elif action == "update":
        rid = str(form.get("rule_id") or "")
        target = _by_id(rid)
        if target is None:
            ok, message = False, "That rule no longer exists."
        else:
            updated = _from_form({
                "id": rid,
                "label": form.get("label"),
                "kind": form.get("kind"),
                "sheet": form.get("sheet"),
                "header_contains": form.get("header_contains"),
                "header_contains_any": form.get("header_contains_any"),
                "row_column": form.get("row_column"),
                "row_value": form.get("row_value"),
                "enabled": target.enabled,
            })
            current = [updated if r.id == rid else r for r in current]
            message = "Rule changed."

    if ok:
        written, detail = R.save_rules(current)
        if not written:
            ok, message = False, f"Could not save: {detail}"
        else:
            R.invalidate_cache()
            # A rule change changes what a file means, so the parsed
            # workbooks have to be read again.
            from demo import intake
            intake.invalidate_parse_cache()

    url = "/intake/rules"
    if message:
        url += f"?{('error' if not ok else 'saved')}={quote_plus(message)}"
    return RedirectResponse(url, status_code=303)


@app.get("/api/rules", include_in_schema=False)
def rules_api() -> JSONResponse:
    """The rules as JSON, and what they would match."""
    from demo import rules as R

    rules = R.current_rules()
    out: list[dict[str, Any]] = []
    for r in rules:
        d = r.to_dict()
        d["describe"] = r.describe()
        out.append(d)
    return JSONResponse({"rules": out, "kinds": list(R.KINDS)})


@app.get("/api/rules/test", include_in_schema=False)
def rules_test(file_id: str = "") -> JSONResponse:
    """Show which rule claims which block of a file."""
    from demo import intake
    from demo import rules as R
    from demo import sheetmap as SM

    f = intake.find_file(file_id)
    if f is None:
        return JSONResponse({"error": "file not found"}, status_code=404)
    parsed = intake.parse_drive_file(f)
    sheet_map = SM.build_map(parsed)
    return JSONResponse({
        "file": f.to_dict(),
        "map": sheet_map,
        "summary": SM.summary(sheet_map),
    })


@app.get("/content/{file_id}", response_class=HTMLResponse)
def content_page(request: Request, file_id: str) -> HTMLResponse:
    """The content of one workbook, section by section."""
    from demo import intake

    auth = _auth_state()
    f = intake.find_file(file_id)
    if f is None:
        raise HTTPException(status_code=404, detail="file not found in the intake folders")
    parsed = intake.parse_drive_file(f)
    # Prefer the section that carries the adopted comparables over the
    # raw extract — the adjustments beside them are the better evidence.
    project = intake.project_from_sections(parsed)

    # The map — how the file is arranged, and which rows each section
    # claimed. The preview rows are attached here rather than in the
    # template so the template stays about layout.
    from demo import sheetmap as SM
    sheet_map = SM.build_map(parsed)
    for sm in sheet_map:
        sm["preview"] = SM.preview_rows(sheet_map, sm["sheet"])
        width = max((len(r.get("cells") or []) for r in sm["preview"]), default=0)
        sm["columns"] = SM.column_letters(width)
    map_summary = SM.summary(sheet_map)

    return templates.TemplateResponse(
        request,
        "content.html",
        {
            "auth": auth,
            "file": f.to_dict(),
            "parsed": parsed.to_dict(),
            "project": project,
            "shown_rows": 200,
            "sheet_map": sheet_map,
            "map_summary": map_summary,
        },
    )


@app.post("/upload")
async def upload(request: Request):
    """Accept a workbook from the browser and put it in Drive /Intake/.

    Everything enters through Drive on purpose: a file uploaded here and
    a file dropped into the folder by hand are the same thing to the app,
    and there is one code path afterwards.
    """
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename") or not upload.filename:
        return RedirectResponse("/intake", status_code=303)
    name = Path(upload.filename).name
    if not name.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return templates.TemplateResponse(
            request,
            "intake.html",
            {
                "auth": _auth_state(),
                "files": _intake_rows(),
                "upload_error": f"'{name}' is not an Excel workbook.",
            },
            status_code=400,
        )
    data = await upload.read()
    try:
        from demo.drive import upload_to_folder
        info = upload_to_folder(name, data, "Intake")
    except Exception as e:  # noqa: BLE001
        log.warning("upload failed: %s", e)
        return templates.TemplateResponse(
            request,
            "intake.html",
            {
                "auth": _auth_state(),
                "files": _intake_rows(),
                "upload_error": (
                    f"Could not save to Drive: {e}. "
                    "Sign in with Google and try again."
                ),
            },
            status_code=400,
        )
    return RedirectResponse(f"/content/{info['id']}", status_code=303)


@app.get("/api/intake", include_in_schema=False)
def api_intake(request: Request) -> dict[str, Any]:
    """JSON view of the intake listing (for agents / MCP)."""
    from demo.oauth import can_read_all_drive, granted_scopes

    from demo import intake

    auth = _auth_state()
    folder_ids, whole_drive = _scan_setting(request)
    return {
        "signed_in": auth["signed_in"],
        "user": auth["user"],
        "can_read_all_drive": can_read_all_drive(),
        "granted_scopes": granted_scopes(),
        "scan_folders": list(folder_ids),
        "scan_whole_drive": whole_drive,
        "files": _intake_rows(folder_ids, whole_drive) if auth["signed_in"] else [],
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
    """Warm the Drive folder layout on first boot (idempotent, best effort).

    This must never block the container from listening on PORT 8080. A
    google-auth refresh retries for up to two minutes on a transient
    network error, and the startup probe gives up after four — so a slow
    identity call used to push cold starts past the probe window. The
    layout is also created on demand by the /generate path, so skipping
    it here costs nothing.
    """
    try:
        from demo.oauth import load_credentials
        if load_credentials() is None:
            log.info("not signed in yet — skipping Drive layout warm-up")
            return
        from demo.drive import APP_FOLDERS, app_folder_if_exists
        # Find-only. Creating the folders here would put empty copies at
        # the new path before the tidy-up has moved the real ones, and
        # then the tidy-up has two candidates to choose between.
        present = {
            name: app_folder_if_exists(name) for name in APP_FOLDERS
        }
        log.info(
            "Drive layout: %s",
            {k: ("present" if v else "missing") for k, v in present.items()},
        )
    except Exception as e:
        # Don't crash the container if Drive is unavailable; the /generate
        # endpoint will surface the error.
        log.warning("Drive layout check failed on startup: %s", e)


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
        # A fresh consent restarts the seven-day Testing-mode clock.
        from demo.oauth import start_new_sign_in
        start_new_sign_in()
        save_credentials(flow.credentials)
    except Exception as e:
        log.exception("OAuth callback failed")
        return RedirectResponse(url=f"/?auth_error={type(e).__name__}", status_code=302)

    resp = RedirectResponse(url="/?auth=ok", status_code=302)
    resp.delete_cookie("bma_oauth_ctx", path="/")
    return resp


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    from demo.oauth import (
        get_user_info, is_configured, sign_in_expiry, token_expired,
    )
    info = get_user_info()
    return {
        "oauth_configured": is_configured(),
        "signed_in": info is not None,
        "user": info,
        "expires_on": sign_in_expiry(),
        "expired": token_expired(),
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


@app.get("/api/health", include_in_schema=False)
def api_health() -> dict[str, str]:
    """Liveness, at a path the Google Front End does not intercept.

    ``/healthz`` is reserved at the GFE, so a request to it never
    reaches the app — it comes back as Google''s own HTML 404 while
    every other unknown path comes back as FastAPI''s JSON 404. This
    route is the one to actually use.
    """
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
    import time
    diag: dict[str, Any] = {
        "ok": True,
        "python": sys.version.split()[0],
        "cases_total": len(CASES),
        "case_ids": [c.id for c in CASES],
        "timings_ms": {},
    }

    def _t(label: str, fn):
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            diag["timings_ms"][label] = f"error: {type(e).__name__}"
            return
        diag["timings_ms"][label] = round((time.time() - t0) * 1000)

    try:
        from demo.oauth import get_user_info
        _t("auth_state", lambda: get_user_info())
    except Exception:  # noqa: BLE001
        pass
    try:
        from demo import intake
        _t("drive_listing", lambda: intake.list_drive_workbooks())
    except Exception:  # noqa: BLE001
        pass
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
