"""Diagnostic boot wrapper.

Runs BEFORE uvicorn. Captures the real startup failure (import errors,
missing shared libraries, wrong CWD) and reports it to a GitHub gist so
it can be read without Cloud Logging access.

Then execs uvicorn exactly as the container CMD would.

The gist token is passed in as ``VF_DIAG_TOKEN`` (a plain env var, not a
Secret Manager reference — a secret mount is itself suspect while we are
debugging this).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request
from datetime import datetime, timezone


def _report(text: str) -> None:
    token = os.environ.get("VF_DIAG_TOKEN", "")
    if not token:
        return
    payload = {
        "description": f"bma-factory boot diag {datetime.now(timezone.utc).isoformat()}",
        "public": False,
        "files": {"boot-diag.txt": {"content": text}},
    }
    req = urllib.request.Request(
        "https://api.github.com/gists",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "bma-factory-diag",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[boot-diag] gist created: {r.status}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[boot-diag] could not create gist: {e}", flush=True)


def _diagnose() -> str:
    lines = [
        f"time: {datetime.now(timezone.utc).isoformat()}",
        f"python: {sys.version}",
        f"executable: {sys.executable}",
        f"cwd: {os.getcwd()}",
        f"PYTHONPATH: {os.environ.get('PYTHONPATH')}",
        f"PORT: {os.environ.get('PORT')}",
        "",
        "--- import demo.app ---",
    ]
    try:
        import demo.app  # noqa: F401
        lines.append("IMPORT OK")
    except Exception:
        lines.append("IMPORT FAILED")
        lines.append(traceback.format_exc())
    lines.append("")
    lines.append("--- import fastapi / uvicorn ---")
    for mod in ("fastapi", "uvicorn", "jinja2", "openpyxl", "docx", "pptx",
                "googleapiclient", "google.auth", "google_auth_oauthlib"):
        try:
            __import__(mod)
            lines.append(f"  {mod}: OK")
        except Exception as e:
            lines.append(f"  {mod}: FAILED — {type(e).__name__}: {e}")
    lines.append("")
    lines.append("--- site-packages ---")
    for p in sys.path:
        lines.append(f"  {p}")
    return "\n".join(lines)


def main() -> None:
    diag = _diagnose()
    print("[boot-diag] ===== DIAG START =====", flush=True)
    print(diag, flush=True)
    print("[boot-diag] ===== DIAG END =====", flush=True)
    _report(diag)

    # Now hand over to uvicorn exactly as the container CMD does.
    os.execvp(
        "uvicorn",
        ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port",
         os.environ.get("PORT", "8080"), "--workers", "1", "--log-level", "info"],
    )


if __name__ == "__main__":
    main()
