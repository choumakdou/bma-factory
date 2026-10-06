"""Determinism ledger.

Records, for every generation:
    input_hash  - canonical hash of the validated payload + template version
    output_hash - SHA-256 of the generated file bytes

Two facts fall out of this that matter:

1. "Same input -> same output" stops being a claim and becomes something you
   can check. Regenerate a case from last year and compare hashes.
2. When a file changes unexpectedly, you can tell whether the *inputs* moved
   or the *template* moved, because the template version is in the input hash.

Documents embed created/modified timestamps. Those are pinned here so byte
equality is achievable rather than merely content equality.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Pinned so two runs of the same input produce byte-identical files.
FIXED_TIMESTAMP = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

DB_PATH = Path(__import__("os").environ.get("VF_DB", "data/ledger.sqlite3"))
_LOCK = threading.Lock()


def canonical(payload: Any) -> str:
    """Stable JSON: sorted keys, ISO dates, no insignificant whitespace."""
    return json.dumps(payload, sort_keys=True, default=_default, separators=(",", ":"))


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"not canonicalisable: {type(o).__name__}")


def input_hash(module_id: str, template_version: str, values: dict[str, Any]) -> str:
    payload = {"module": module_id, "template_version": template_version, "values": values}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Entry:
    id: int
    run_id: str
    module_id: str
    template_version: str
    input_hash: str
    output_hash: str
    filename: str
    caller: str
    created_at: str


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            module_id TEXT NOT NULL,
            template_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            output_hash TEXT NOT NULL,
            filename TEXT NOT NULL,
            caller TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def record(
    run_id: str,
    module_id: str,
    template_version: str,
    ihash: str,
    ohash: str,
    filename: str,
    caller: str,
) -> Entry:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO runs"
            " (run_id, module_id, template_version, input_hash, output_hash, filename, caller, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, module_id, template_version, ihash, ohash, filename, caller, created),
        )
        conn.commit()
        conn.close()
    return Entry(0, run_id, module_id, template_version, ihash, ohash, filename, caller, created)


def history(module_id: str | None = None, limit: int = 50) -> list[Entry]:
    conn = _connect()
    if module_id:
        rows = conn.execute(
            "SELECT id, run_id, module_id, template_version, input_hash, output_hash,"
            " filename, caller, created_at FROM runs WHERE module_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (module_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, run_id, module_id, template_version, input_hash, output_hash,"
            " filename, caller, created_at FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [Entry(*r) for r in rows]


def verify(run_id: str, path: Path) -> tuple[bool, str]:
    """Re-hash a file on disk and compare it with what the ledger recorded."""
    conn = _connect()
    row = conn.execute(
        "SELECT output_hash FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return False, "no such run in ledger"
    actual = file_hash(path)
    if actual != row[0]:
        return False, f"hash mismatch: ledger {row[0][:12]}… vs file {actual[:12]}…"
    return True, f"hash matches ledger ({actual[:12]}…)"


def pin_document_properties(props) -> None:
    """Pin DOCX/XLSX/PPTX core properties so output is byte-stable.

    Note `creator` (openpyxl / python-pptx) and `author` (python-docx) are
    different attribute names for the same field. Missing one of them leaves
    the generator's name embedded in the file and breaks byte-equality.
    """
    props.created = FIXED_TIMESTAMP.replace(tzinfo=None)
    props.modified = FIXED_TIMESTAMP.replace(tzinfo=None)
    props.lastModifiedBy = "valuation-forge"
    props.revision = 1
    props.title = ""
    props.subject = ""
    props.creator = ""
    props.author = ""
    props.keywords = ""
    props.category = ""
    props.comments = ""


# --- idempotency ------------------------------------------------------------
# An agent that times out will retry, and a retried generation must not create
# a second case. Same key + same caller => same run_id, returned again.


def _ensure_idem(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idem (
            caller TEXT NOT NULL,
            key TEXT NOT NULL,
            run_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (caller, key)
        )
        """
    )
    conn.commit()


def idempotent_lookup(caller: str, key: str) -> str | None:
    conn = _connect()
    _ensure_idem(conn)
    row = conn.execute(
        "SELECT run_id FROM idem WHERE caller = ? AND key = ?", (caller, key)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def idempotent_store(caller: str, key: str, run_id: str) -> None:
    conn = _connect()
    _ensure_idem(conn)
    conn.execute(
        "INSERT OR REPLACE INTO idem (caller, key, run_id, created_at) VALUES (?,?,?,?)",
        (caller, key, run_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def find_run(run_id: str) -> Entry | None:
    conn = _connect()
    row = conn.execute(
        "SELECT id, run_id, module_id, template_version, input_hash, output_hash,"
        " filename, caller, created_at FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.close()
    return Entry(*row) if row else None