"""Valuation Forge — HTTP API.

One generation core, several front doors. The browser UI and any AI assistant
both come through here; neither gets its own implementation.

Authentication is deliberately closed by default. If VF_API_KEYS is unset, only
the Apps Script identity token is accepted -- a fresh install has exactly one
caller and no way in from the internet.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from ..core import auth, generate, ledger, registry
from ..core.auth import AuthError, Principal, ScopeError
from ..core.generate import GenerationRefused

app = FastAPI(
    title="Valuation Forge",
    version="1.0.0",
    description="Deterministic Word / Excel / PowerPoint generation for valuation reporting.",
)

OUTPUT_ROOT = Path(os.environ.get("VF_OUTPUT_DIR", "data/outputs"))


def current_principal(authorization: str | None = Header(default=None)) -> Principal:
    try:
        return auth.verify_bearer(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


Operator = Depends(current_principal)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "modules": registry.module_ids()}


@app.get("/v1/auth/whoami")
def whoami(p: Principal = Operator) -> dict[str, Any]:
    return {"name": p.name, "kind": p.kind, "scopes": sorted(p.scopes)}


@app.get("/v1/modules")
def list_modules(p: Principal = Operator) -> dict[str, Any]:
    p.require(auth.SCOPE_CATALOG)
    return {
        "modules": [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "version": m.version,
                "outputs": m.outputs,
                "field_count": len(m.fields),
                "sections": m.schema().sections(),
            }
            for m in registry.list_modules()
        ]
    }


@app.get("/v1/modules/{module_id}/schema")
def module_schema(module_id: str, p: Principal = Operator) -> dict[str, Any]:
    p.require(auth.SCOPE_CATALOG)
    try:
        m = registry.get_module(module_id).manifest
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown module '{module_id}'") from None
    return {
        "id": m.id,
        "version": m.version,
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "type": f.type,
                "required": f.required,
                "section": f.section,
                "unit": f.unit,
                "choices": list(f.choices),
                "minimum": f.minimum,
                "maximum": f.maximum,
                "help": f.help,
            }
            for f in m.fields
        ],
    }


@app.post("/v1/generate/{module_id}")
def run_generate(
    module_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    p: Principal = Operator,
) -> JSONResponse:
    p.require(auth.SCOPE_GENERATE)

    payload = request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    # Retry safety. An agent that times out must not create a second case.
    if idempotency_key:
        prior = ledger.idempotent_lookup(p.name, idempotency_key)
        if prior:
            entry = ledger.find_run(prior)
            if entry:
                response.headers["Idempotent-Replay"] = "true"
                return JSONResponse(
                    {
                        "run_id": entry.run_id,
                        "module": entry.module_id,
                        "template_version": entry.template_version,
                        "input_hash": entry.input_hash,
                        "replayed": True,
                        "files": [
                            {"filename": f, "download": f"/v1/runs/{entry.run_id}/files/{f}"}
                            for f in entry.filename.split(";")
                        ],
                    }
                )

    try:
        result = generate.generate(module_id, payload, caller=p.name)
    except GenerationRefused as exc:
        raise HTTPException(status_code=422, detail={"refused": exc.issues}) from exc

    if idempotency_key:
        ledger.idempotent_store(p.name, idempotency_key, result.run_id)

    return JSONResponse(
        {
            **result.to_dict(),
            "files": [
                {**f, "download": f"/v1/runs/{result.run_id}/files/{f['filename']}"}
                for f in result.to_dict()["files"]
            ],
        }
    )


@app.get("/v1/runs")
def list_runs(
    module: str | None = None,
    limit: int = 50,
    p: Principal = Operator,
) -> dict[str, Any]:
    p.require(auth.SCOPE_READ_OWN)
    entries = ledger.history(module, limit)
    # an agent may only see its own runs; the operator sees everything
    visible = entries if p.kind == "operator" else [e for e in entries if e.caller == p.name]
    return {
        "runs": [
            {
                "run_id": e.run_id,
                "module": e.module_id,
                "template_version": e.template_version,
                "input_hash": e.input_hash,
                "output_hash": e.output_hash,
                "files": e.filename.split(";"),
                "caller": e.caller,
                "created_at": e.created_at,
            }
            for e in visible
        ]
    }


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, p: Principal = Operator) -> dict[str, Any]:
    p.require(auth.SCOPE_READ_OWN)
    entry = ledger.find_run(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such run")
    if p.kind != "operator" and entry.caller != p.name:
        raise HTTPException(status_code=403, detail="not your run")
    return {
        "run_id": entry.run_id,
        "module": entry.module_id,
        "template_version": entry.template_version,
        "input_hash": entry.input_hash,
        "output_hash": entry.output_hash,
        "files": entry.filename.split(";"),
        "caller": entry.caller,
        "created_at": entry.created_at,
    }


@app.get("/v1/runs/{run_id}/files/{filename}")
def download(run_id: str, filename: str, p: Principal = Operator) -> FileResponse:
    p.require(auth.SCOPE_READ_OWN)
    entry = ledger.find_run(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such run")
    if p.kind != "operator" and entry.caller != p.name:
        raise HTTPException(status_code=403, detail="not your run")
    if filename not in entry.filename.split(";"):
        raise HTTPException(status_code=404, detail="no such file in this run")

    path = OUTPUT_ROOT / run_id / filename
    if not path.exists():
        raise HTTPException(status_code=410, detail="file no longer on disk; regenerate")
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@app.get("/v1/runs/{run_id}/verify")
def verify_run(run_id: str, p: Principal = Operator) -> dict[str, Any]:
    """Re-hash what is on disk against what the ledger recorded."""
    p.require(auth.SCOPE_READ_OWN)
    entry = ledger.find_run(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such run")
    if p.kind != "operator" and entry.caller != p.name:
        raise HTTPException(status_code=403, detail="not your run")
    results = []
    for name in entry.filename.split(";"):
        path = OUTPUT_ROOT / run_id / name
        ok, msg = ledger.verify(run_id, path) if path.exists() else (False, "file missing")
        results.append({"filename": name, "verified": ok, "detail": msg})
    return {"run_id": run_id, "files": results, "all_verified": all(r["verified"] for r in results)}


@app.exception_handler(ScopeError)
def _scope_handler(request: Request, exc: ScopeError) -> JSONResponse:  # pragma: no cover
    return JSONResponse(status_code=403, content={"detail": str(exc)})