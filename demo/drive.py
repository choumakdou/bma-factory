"""Google Drive client for the bma-factory demo.

Authenticates as the runtime service account (the one whose JSON is
mounted as GOOGLE_APPLICATION_CREDENTIALS by Cloud Run). Creates the
``/ORO/``, ``/Outputs/``, ``/Templates/`` folder layout on first call,
and pushes generated workbooks to ``/Outputs/``.

For the demo MVP, only the upload-to-Outputs path is used. Pulling from
``/ORO/`` (for real ORO-workbook parsing) is v0.2.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build as gbuild
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("bma-factory.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Folder cache — first lookup hits the API, subsequent ones don't.
_lock = threading.Lock()
_folder_ids: dict[str, str] = {}
_drive = None


def _get_drive():
    """Lazy auth — only when the first Drive call is made.

    Accepts GOOGLE_APPLICATION_CREDENTIALS in two forms:
      1. A file path (the Google-recommended pattern)
      2. The raw service-account JSON content (what Cloud Run's
         --set-secrets flag actually gives you)

    The two are disambiguated by checking whether the value starts
    with ``{`` — JSON always does, a file path doesn't.
    """
    global _drive
    if _drive is not None:
        return _drive
    creds_data = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_data:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS not set in the container env")
    if creds_data.lstrip().startswith("{"):
        # Raw JSON — Cloud Run's --set-secrets puts the value here
        import json
        info = json.loads(creds_data)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # File path — the local-dev pattern
        if not Path(creds_data).exists():
            raise RuntimeError(f"GOOGLE_APPLICATION_CREDENTIALS points to {creds_data} but the file is missing")
        creds = service_account.Credentials.from_service_account_file(creds_data, scopes=SCOPES)
    _drive = gbuild("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive


def _find_or_create_folder(name: str, parent_id: str | None = None) -> str:
    """Find a folder by name (under parent, if given), create if missing."""
    with _lock:
        if name in _folder_ids:
            return _folder_ids[name]
        drive = _get_drive()
        q = f"name='{name}' and mimeType='{FOLDER_MIME}' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        r = drive.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
        files = r.get("files", [])
        if files:
            _folder_ids[name] = files[0]["id"]
            return files[0]["id"]
        # Create
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        f = drive.files().create(body=body, fields="id,name").execute()
        _folder_ids[name] = f["id"]
        log.info("created Drive folder %r → %s", name, f["id"])
        return f["id"]


def ensure_drive_layout() -> dict[str, str]:
    """Create the three demo folders at the Drive root, idempotently.

    Returns a dict of ``{name: folder_id}`` for ``ORO``, ``Outputs``,
    ``Templates``. Safe to call on every container boot.
    """
    return {
        "ORO":       _find_or_create_folder("ORO"),
        "Outputs":   _find_or_create_folder("Outputs"),
        "Templates": _find_or_create_folder("Templates"),
    }


def upload_to_outputs(local_path: Path, mime: str = MIME_XLSX) -> dict[str, Any]:
    """Upload a local file to the ``/Outputs/`` folder.

    Returns the Drive file metadata (``id``, ``name``, ``webViewLink``).
    Each call creates a NEW file (no overwrite) — the file name is the
    workbook's own filename which already includes case + date.
    """
    drive = _get_drive()
    folder_id = _find_or_create_folder("Outputs")
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    body = {"name": local_path.name, "parents": [folder_id]}
    f = drive.files().create(body=body, media_body=media, fields="id,name,webViewLink,parents").execute()
    log.info("uploaded %s → Drive %s", local_path.name, f.get("webViewLink", f.get("id")))
    return f
