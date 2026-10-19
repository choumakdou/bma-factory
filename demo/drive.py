"""Google Drive client for the bma-factory demo.

Uses the valuer's own Google credentials (OAuth2) — generated workbooks
land in their personal Google Drive, not a service account. This
matches how every real valuer tool works (DocuSign, HelloSign, Dropbox).

Single-user for the demo (Bobby). Token lives at
``/tmp/bma-factory-demo/tokens/user.json`` and is rotated on every
successful sign-in.

The service account (``service-ac1@bma-factory.iam...``) is still
mounted as ``GOOGLE_APPLICATION_CREDENTIALS`` because the container
needs *some* identity to boot — but it's only used for the runtime, not
for Drive.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("bma-factory.drive")

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Folder cache — first lookup hits the API, subsequent ones don't.
_lock = threading.Lock()
_folder_ids: dict[str, str] = {}
_drive = None


def _get_drive():
    """Lazy auth — uses Bobby's OAuth credentials, not a service account.

    The service-account path was removed because personal Google
    accounts don't allow SA writes (quota error). The OAuth path uses
    the valuer's own 15 GB quota via the ``drive.file`` scope.
    """
    global _drive
    if _drive is not None:
        return _drive
    from demo.oauth import get_drive as _oauth_drive
    _drive = _oauth_drive()
    return _drive


def _shared_drive_id() -> str | None:
    """The Shared Drive to use as the root, from the env. None → use the
    user's own Drive (the OAuth default for personal Google accounts).
    """
    import os
    return os.environ.get("VF_DRIVE_ID") or None


def _find_or_create_folder(name: str, parent_id: str | None = None) -> str:
    """Find a folder by name (under parent, if given), create if missing."""
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
    """Create the three demo folders, idempotently. Returns ``{name: id}``."""
    return {
        "ORO":       _find_or_create_folder("ORO"),
        "Outputs":   _find_or_create_folder("Outputs"),
        "Templates": _find_or_create_folder("Templates"),
    }


def upload_to_outputs(local_path: Path, mime: str = MIME_XLSX) -> dict[str, Any]:
    """Upload a local file to the user's ``/Outputs/`` folder.

    Uses the OAuth credentials (the valuer's own Drive), so the upload
    counts against the valuer's personal quota.
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
