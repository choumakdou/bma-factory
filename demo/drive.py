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
import os
import threading
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

log = logging.getLogger("bma-factory.drive")

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"

#: Where the app's folders live, as a path from the Drive root.
#:
#: This is a *path*, not a flat name, because the valuer's Drive is full of
#: folders called "ORO", "Output", "Outputs" and "Intake" already. A flat
#: name search picks whichever one the API returns first, which is not a
#: thing to leave to chance. Resolving a path means the app always means
#: the same folder, and moving the whole tree is a one-line change here.
DRIVE_ROOT_PATH = os.environ.get("VF_DRIVE_ROOT_PATH", "bma-factory/Valuation")

#: The folders the app owns, relative to ``DRIVE_ROOT_PATH``.
APP_FOLDERS = ("ORO", "Outputs", "Intake", "Templates")

# Folder cache — first lookup hits the API, subsequent ones don't.
_lock = threading.Lock()
_folder_ids: dict[str, str] = {}
_drive = None

#: Persistent cache of path -> folder id, in Cloud Storage.
#:
#: A cold start otherwise walks the whole path against the Drive API —
#: roughly twenty sequential round trips, which is several seconds on
#: every request after the container sleeps.
#:
#: The cache is safe because **moving a folder does not change its id**.
#: Only deleting and recreating it would, and that shows up as a miss on
#: the next lookup, which falls back to the API.
_CACHE_OBJECT = "bma-factory/drive/folder-cache.json"
_cache_loaded = False
_cache_dirty = False


def _load_cache() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        from demo.oauth import _gcs_read_from
        blob = _gcs_read_from(_CACHE_OBJECT)
        if not blob:
            return
        import json as _json
        data = _json.loads(blob)
        if isinstance(data, dict):
            with _lock:
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        _folder_ids.setdefault(k, v)
            log.info("loaded %d cached Drive folder ids", len(_folder_ids))
    except Exception as e:  # noqa: BLE001
        log.warning("could not load the Drive folder cache: %s", e)


def _save_cache() -> None:
    """Persist the cache — but only when it actually changed.

    Called after every lookup, so an unconditional write would be one
    Cloud Storage write per folder segment per request.
    """
    global _cache_dirty
    if not _cache_dirty:
        return
    _cache_dirty = False
    try:
        from demo.oauth import _gcs_write_to
        import json as _json
        with _lock:
            payload = _json.dumps(_folder_ids, ensure_ascii=False)
        _gcs_write_to(payload, _CACHE_OBJECT)
    except Exception as e:  # noqa: BLE001
        log.warning("could not save the Drive folder cache: %s", e)


def _cache_set(key: str, value: str) -> None:
    global _cache_dirty
    with _lock:
        if _folder_ids.get(key) != value:
            _folder_ids[key] = value
            _cache_dirty = True


def _cache_drop(key: str) -> None:
    global _cache_dirty
    with _lock:
        if key in _folder_ids:
            del _folder_ids[key]
            _cache_dirty = True


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
    """Find a folder by name (under parent, if given), create if missing.

    When ``parent_id`` is given the search is confined to that parent, so
    the same folder name in two places never collides. This is what makes
    the app's folders addressable by path.
    """
    drive_id = _shared_drive_id()
    cache_key = f"{parent_id or 'root'}/{name}"
    _load_cache()
    with _lock:
        if cache_key in _folder_ids:
            return _folder_ids[cache_key]
    drive = _get_drive()
    kwargs: dict[str, Any] = {
        "q": f"name='{name}' and mimeType='{FOLDER_MIME}' and trashed=false",
        "fields": "files(id,name,parents)",
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
        with _lock:
            _folder_ids[cache_key] = files[0]["id"]
        _save_cache()
        return files[0]["id"]
    # Create
    body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if drive_id:
        body["parents"] = [drive_id]
    elif parent_id:
        body["parents"] = [parent_id]
    create_kwargs: dict[str, Any] = {"body": body, "fields": "id,name", "supportsAllDrives": True}
    f = drive.files().create(**create_kwargs).execute()
    _cache_set(cache_key, f["id"])
    _save_cache()
    log.info("created Drive folder %r → %s (under %s)", name, f["id"], parent_id or "root")
    return f["id"]


def resolve_path(path: str, create: bool = True) -> str:
    """``'bma-factory/Valuation/Intake'`` -> that folder's id.

    Walks the path one segment at a time, creating what is missing unless
    ``create`` is False. A trailing slash is ignored.
    """
    segments = [s.strip() for s in path.strip("/").split("/") if s.strip()]
    if not segments:
        raise ValueError("empty path")
    parent_id: str | None = None
    for seg in segments:
        if not create:
            parent_id = _find_child(seg, parent_id)
            if parent_id is None:
                raise KeyError(path)
            continue
        parent_id = _find_or_create_folder(seg, parent_id)
    return parent_id


def _find_child(name: str, parent_id: str | None) -> str | None:
    """Find a child folder by name. None if it does not exist."""
    drive_id = _shared_drive_id()
    _load_cache()
    cache_key = f"{parent_id or 'root'}/{name}"
    with _lock:
        if cache_key in _folder_ids:
            return _folder_ids[cache_key]
    drive = _get_drive()
    kwargs: dict[str, Any] = {
        "q": f"name='{name}' and mimeType='{FOLDER_MIME}' and trashed=false",
        "fields": "files(id,name,parents)",
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
    files = drive.files().list(**kwargs).execute().get("files", [])
    if not files:
        # A miss may mean the folder was deleted and recreated. Drop the
        # stale entry so the next lookup goes back to the API.
        _cache_drop(cache_key)
        _save_cache()
        return None
    _cache_set(cache_key, files[0]["id"])
    _save_cache()
    return files[0]["id"]


def app_folder(name: str) -> str:
    """The id of one of the app's own folders, by its name under the root path."""
    return resolve_path(f"{DRIVE_ROOT_PATH}/{name}")


def app_folder_if_exists(name: str) -> str | None:
    """The id of one of the app's folders, or None if it is not there yet.

    Read paths use this. Creating a folder as a side effect of *looking*
    at it is how you end up with two empty ``/Outputs/`` folders — one at
    the old path and one at the new — and then a tidy-up that moves the
    wrong one.
    """
    try:
        return resolve_path(f"{DRIVE_ROOT_PATH}/{name}", create=False)
    except KeyError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("cannot resolve %s/%s: %s", DRIVE_ROOT_PATH, name, e)
        return None


def folder_parent_id(folder_id: str) -> str:
    """The id of the folder that currently holds ``folder_id``."""
    drive = _get_drive()
    f = drive.files().get(
        fileId=folder_id, fields="id,name,parents", supportsAllDrives=True
    ).execute()
    parents = f.get("parents") or []
    return parents[0] if parents else ""


_root_id_cache: str | None = None


def root_folder_id() -> str:
    """The id of "My Drive" root.

    Do not assume a file at the root has an empty ``parents`` list. On
    some accounts Drive reports the root's own numeric id
    (``0ANLh9MR99SLaUk9PVA``) instead, so "is this folder at the root?"
    has to compare against the real root id rather than test for
    emptiness.
    """
    global _root_id_cache
    if _root_id_cache is not None:
        return _root_id_cache
    drive = _get_drive()
    try:
        f = drive.files().get(fileId="root", fields="id", supportsAllDrives=True).execute()
        _root_id_cache = f.get("id") or "root"
    except Exception as e:  # noqa: BLE001
        log.warning("could not resolve the Drive root id: %s", e)
        _root_id_cache = "root"
    return _root_id_cache


def is_at_drive_root(parents: list[str] | None) -> bool:
    """True when a file sits directly in "My Drive"."""
    if not parents:
        return True
    return root_folder_id() in parents


def move_folder(folder_id: str, new_parent_id: str) -> dict[str, Any]:
    """Move a folder under a new parent.

    Google Drive takes this as a single ``files.update`` with both the
    old and the new parent; a folder can have more than one parent, so
    the old one has to be removed explicitly or the folder stays put and
    appears in both places.
    """
    drive = _get_drive()
    old_parent = folder_parent_id(folder_id)
    if old_parent == new_parent_id:
        return {"moved": False, "reason": "already there", "parent": new_parent_id}
    f = drive.files().update(
        fileId=folder_id,
        addParents=new_parent_id,
        removeParents=old_parent,
        fields="id,name,parents",
        supportsAllDrives=True,
    ).execute()
    with _lock:
        stale = [k for k in _folder_ids if k.endswith("/" + f.get("name", ""))]
    for k in stale:
        _cache_drop(k)
    _save_cache()
    log.info("moved Drive folder %s → parent %s", f.get("name"), new_parent_id)
    return {"moved": True, "folder": f.get("name"), "id": f.get("id"),
            "parent": new_parent_id, "previous_parent": old_parent}


def describe_layout() -> dict[str, Any]:
    """What the app's folders look like in Drive right now, and where they
    are meant to be. Read-only — safe to call."""
    out: dict[str, Any] = {
        "root_path": DRIVE_ROOT_PATH,
        "app_folders": list(APP_FOLDERS),
        "root_exists": False,
        "root_id": None,
        "folders": [],
        "notes": [],
    }
    try:
        drive = _get_drive()
    except Exception as e:  # noqa: BLE001
        out["notes"].append(f"not signed in: {e}")
        return out

    try:
        root_id = resolve_path(DRIVE_ROOT_PATH, create=False)
    except KeyError:
        root_id = None
    except Exception as e:  # noqa: BLE001
        out["notes"].append(f"could not resolve {DRIVE_ROOT_PATH!r}: {e}")
        root_id = None
    out["root_exists"] = root_id is not None
    out["root_id"] = root_id

    for name in APP_FOLDERS:
        entry: dict[str, Any] = {"name": name}
        target_path = f"{DRIVE_ROOT_PATH}/{name}"
        # Where it should be.
        try:
            entry["target_id"] = resolve_path(target_path, create=False)
        except KeyError:
            entry["target_id"] = None
        except Exception as e:  # noqa: BLE001
            entry["target_id"] = None
            entry["error"] = str(e)[:200]
        # Where it actually is — every folder with this name, anywhere.
        try:
            found = _candidates_named(name)
        except Exception as e:  # noqa: BLE001
            found = []
            entry["error"] = str(e)[:200]
        entry["candidates"] = [
            {"id": f["id"], "parents": f.get("parents", [])} for f in found
        ]
        in_place = bool(root_id) and any(
            root_id in (f.get("parents") or []) for f in found
        )
        at_root = any(is_at_drive_root(f.get("parents")) for f in found)
        entry["in_place"] = in_place
        entry["at_drive_root"] = at_root
        # Name the parent so the report reads like a person wrote it.
        for f in found:
            pid = (f.get("parents") or [None])[0]
            if pid and pid != root_id and not is_at_drive_root([pid]):
                try:
                    pf = drive.files().get(
                        fileId=pid, fields="id,name", supportsAllDrives=True
                    ).execute()
                    entry.setdefault("parent_names", []).append(pf.get("name") or pid)
                except Exception:  # noqa: BLE001
                    entry.setdefault("parent_names", []).append(pid)
            else:
                entry.setdefault("parent_names", []).append("My Drive")
        entry["status"] = (
            "in place" if in_place
            else "at Drive root" if at_root
            else "elsewhere" if found
            else "missing"
        )
        out["folders"].append(entry)
    return out


def _candidates_named(name: str) -> list[dict[str, Any]]:
    """Every folder with this name, wherever it is in Drive."""
    drive = _get_drive()
    return drive.files().list(
        q=f"name='{name}' and mimeType='{FOLDER_MIME}' and trashed=false",
        fields="files(id,name,parents)",
        pageSize=50,
        supportsAllDrives=True,
    ).execute().get("files", [])


def merge_folder(source_id: str, target_id: str) -> dict[str, Any]:
    """Move everything in ``source`` into ``target``, then drop the empty source.

    Used when a same-named folder already exists at the destination and
    another one is still sitting at the Drive root. Two folders with the
    same name in the same place is not a thing Drive allows, so the
    contents are merged and the emptied source is trashed.
    """
    drive = _get_drive()
    children = drive.files().list(
        q=f"'{source_id}' in parents and trashed=false",
        fields="files(id,name)",
        pageSize=500,
        supportsAllDrives=True,
    ).execute().get("files", [])
    moved = 0
    for child in children:
        drive.files().update(
            fileId=child["id"],
            addParents=target_id,
            removeParents=source_id,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        moved += 1
    drive.files().update(
        fileId=source_id,
        addParents=None,
        removeParents=None,
        body={"trashed": True},
        fields="id,name",
        supportsAllDrives=True,
    ).execute()
    return {"merged": True, "children_moved": moved, "source": source_id, "target": target_id}


def organise_drive(create_root: bool = True) -> dict[str, Any]:
    """Move the app's four folders under ``DRIVE_ROOT_PATH``.

    Idempotent: a folder already in place is left alone. Only the four
    folders the app owns are touched, and only ones sitting at the Drive
    root are moved. Returns a report of what changed.
    """
    report: dict[str, Any] = {
        "root_path": DRIVE_ROOT_PATH,
        "moved": [],
        "merged": [],
        "already_in_place": [],
        "missing": [],
        "elsewhere": [],
        "errors": [],
    }
    try:
        root_id = resolve_path(DRIVE_ROOT_PATH, create=create_root)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"could not resolve {DRIVE_ROOT_PATH!r}: {e}")
        return report
    report["root_id"] = root_id

    for name in APP_FOLDERS:
        try:
            found = _candidates_named(name)
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{name}: {e}")
            continue
        if not found:
            report["missing"].append(name)
            continue

        in_place = [f for f in found if root_id in (f.get("parents") or [])]
        at_root = [f for f in found
                   if f not in in_place and is_at_drive_root(f.get("parents"))]
        other = [f for f in found
                 if f not in in_place and f not in at_root]

        if other:
            report["elsewhere"].append({
                "name": name,
                "folders": [{"id": f["id"], "parents": f.get("parents", [])} for f in other],
            })

        if in_place:
            report["already_in_place"].append(name)
            # A same-named folder still sitting at the Drive root would
            # be the one the valuer actually uses. Merge it in.
            for src in at_root:
                if src["id"] == root_id:
                    report["errors"].append(
                        f"{name}: refusing to move a folder into itself")
                    continue
                try:
                    report["merged"].append(
                        merge_folder(src["id"], in_place[0]["id"]))
                except Exception as e:  # noqa: BLE001
                    report["errors"].append(f"{name}: merge failed: {e}")
            continue

        if not at_root:
            continue

        src = at_root[0]
        if src["id"] == root_id:
            report["errors"].append(f"{name}: refusing to move a folder into itself")
            continue
        try:
            report["moved"].append(move_folder(src["id"], root_id))
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{name}: {e}")
    return report


def ensure_drive_layout() -> dict[str, str]:
    """Create the app's folders under ``DRIVE_ROOT_PATH``, idempotently.

    Returns ``{name: id}``. The path is created segment by segment, so
    the app can live at ``bma-factory/Valuation`` and nothing else in
    the code needs to know.
    """
    return {name: app_folder(name) for name in APP_FOLDERS}


def upload_to_folder(name: str, data: bytes, folder: str = "Intake") -> dict[str, Any]:
    """Upload bytes into one of the app's folders.

    Used by the browser upload route, so a file the valuer picks on their
    phone lands in exactly the same place as a file they drag into Drive
    by hand.
    """
    import io as _io

    drive = _get_drive()
    folder_id = app_folder(folder)
    media = MediaIoBaseUpload(
        _io.BytesIO(data), mimetype=MIME_XLSX, resumable=False
    )
    body = {"name": name, "parents": [folder_id]}
    f = drive.files().create(
        body=body, media_body=media,
        fields="id,name,webViewLink,parents,size,modifiedTime",
        supportsAllDrives=True,
    ).execute()
    log.info("uploaded %s → Drive /%s/ (%s)", name, folder, f.get("webViewLink", f.get("id")))
    return f


def upload_to_outputs(local_path: Path, mime: str = MIME_XLSX) -> dict[str, Any]:
    """Upload a local file to the user's ``/Outputs/`` folder.

    Uses the OAuth credentials (the valuer's own Drive), so the upload
    counts against the valuer's personal quota.
    """
    drive = _get_drive()
    folder_id = app_folder("Outputs")
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    body = {"name": local_path.name, "parents": [folder_id]}
    f = drive.files().create(
        body=body, media_body=media,
        fields="id,name,webViewLink,parents",
        supportsAllDrives=True,
    ).execute()
    log.info("uploaded %s → Drive %s", local_path.name, f.get("webViewLink", f.get("id")))
    return f
