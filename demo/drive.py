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


def _shared_drive_id() -> str | None:
    """The Shared Drive to use as the root, from the env. None → use the
    SA's own Drive (which doesn't have storage quota on personal Google
    accounts, so this only works in Workspace or with Shared Drives).
    """
    return os.environ.get("VF_DRIVE_ID") or None


def _find_or_create_folder(name: str, parent_id: str | None = None) -> str:
    """Find a folder by name (under parent, if given), create if missing.

    When a Shared Drive ID is set (``VF_DRIVE_ID``), the folder is found
    or created inside that drive; ``parent_id`` is ignored.
    """
    drive_id = _shared_drive_id()
    with _lock:
        if name in _folder_ids:
            return _folder_ids[name]
        drive = _get_drive()
        kwargs: dict[str, Any] = {
            "q": f"name='{name}' and mimeType='{FOLDER_MIME}' and trashed=false",
            "fields": "files(id,name)",
            "pageSize": 5,
        }
        if drive_id:
            kwargs["corpora"] = "drive"
            kwargs["driveId"] = drive_id
            kwargs["includeItemsFromAllDrives"] = True
            kwargs["supportsAllDrives"] = True
            kwargs["q"] += f" and '{drive_id}' in parents"
        elif parent_id:
            kwargs["q"] += f" and '{parent_id}' in parents"
        r = drive.files().list(**kwargs).execute()
        files = r.get("files", [])
        if files:
            _folder_ids[name] = files[0]["id"]
            return files[0]["id"]
        # Create
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if drive_id:
            body["parents"] = [drive_id]
        elif parent_id:
            body["parents"] = [parent_id]
        create_kwargs: dict[str, Any] = {"body": body, "fields": "id,name", "supportsAllDrives": True}
        f = drive.files().create(**create_kwargs).execute()
        _folder_ids[name] = f["id"]
        log.info("created Drive folder %r → %s (drive %s)", name, f["id"], drive_id or "root")
        return f["id"]


def ensure_drive_layout() -> dict[str, str]:
    """Create the three demo folders, idempotently.

    Returns a dict of ``{name: folder_id}`` for ``ORO``, ``Outputs``,
    ``Templates``. If ``VF_DRIVE_ID`` is set, the folders go inside
    that Shared Drive; otherwise at the SA's Drive root (which only
    works in Workspace).

    Safe to call on every container boot.
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

    When ``VF_DRIVE_ID`` is set, the upload goes to the Shared Drive's
    ``/Outputs/`` folder. Otherwise it uses the SA's Drive root, which
    fails with a quota error on personal Google accounts.
    """
    drive = _get_drive()
    folder_id = _find_or_create_folder("Outputs")
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    body = {"name": local_path.name, "parents": [folder_id]}
    f = drive.files().create(
        body=body, media_body=media,
        fields="id,name,webViewLink,parents",
        supportsAllDrives=True,
    ).execute()
    log.info("uploaded %s → Drive %s", local_path.name, f.get("webViewLink", f.get("id")))
    return f
