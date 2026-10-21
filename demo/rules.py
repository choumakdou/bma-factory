"""Section rules — the valuer's own arrangement templates.

The built-in section detection in ``demo.sections`` is a guess. It knows
what a Memfus Wong extract looks like and what an adjustments table
looks like, but every valuer's working file has its own arrangement, and
the point of a section map is to show how *this* file is put together.

So the rules are editable, and they live with the valuer rather than in
the code:

  - A rule says "a block with these column names, in this sheet, is
    this kind of section".
  - Rules are stored in ``Drive:/bma-factory/Valuation/Templates/section-rules.json``
    so they survive a deploy, travel with the Drive, and can be edited
    from the app's own UI or by hand in Drive.
  - User rules are tried first, in order. The built-ins are the
    fallback, not the authority.

The four kinds are the ones the content page knows how to render:

  subject   the property being valued
  comps     comparables, with the adjustment ladder beside them
  extract   the raw Memfus Wong transaction extract
  table     anything else
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("bma-factory.rules")

#: Where the rules live, relative to the app's root path.
RULES_FILE = "section-rules.json"

#: The kinds a section can be. The content page renders each its own way.
KINDS = ("subject", "comps", "extract", "table")

KIND_LABELS = {
    "subject": "Subject",
    "comps": "Comparables, with adjustments",
    "extract": "Memfus Wong data extraction",
    "table": "Table",
}


def _norm(v: Any) -> str:
    """Header comparison form — same one the section detection uses."""
    if v is None:
        return ""
    return re.sub(r"[\s_\-]+", "", str(v)).upper()


def _words(v: Any) -> str:
    """The header with its word boundaries kept: "ADOPTED PSF (SF)"."""
    if v is None:
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", str(v).upper()).strip()


def _column_matches(header: Any, token: str) -> bool:
    """Does a column answer to this name?

    Exact match first, then a whole-word match. The whole-word part
    matters: a valuer writes "ADOPTED" and the column is called
    "ADOPTED PSF (SF)" — an exact test would find nothing, and a
    valuer who cannot make a rule work stops making rules.
    """
    t = _norm(token)
    if not t:
        return False
    h = _norm(header)
    if h == t:
        return True
    if len(t) < 3:
        return False
    return bool(re.search(r"(?<![A-Z0-9])" + re.escape(t) + r"(?![A-Z0-9])", _words(header)))


@dataclass
class SectionRule:
    """One user-defined rule."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    label: str = ""
    kind: str = "table"
    #: Only apply to this sheet. Empty means any sheet.
    sheet: str = ""
    #: All of these must appear in the block's header.
    header_contains: list[str] = field(default_factory=list)
    #: At least one of these must appear.
    header_contains_any: list[str] = field(default_factory=list)
    #: A data row must have this column with this value (e.g. MEM_NO =
    #: "Subject"). Empty means don't care.
    row_column: str = ""
    row_value: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "sheet": self.sheet,
            "header_contains": list(self.header_contains),
            "header_contains_any": list(self.header_contains_any),
            "row_column": self.row_column,
            "row_value": self.row_value,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SectionRule":
        def _list(key: str) -> list[str]:
            v = d.get(key)
            if isinstance(v, str):
                return [x.strip() for x in v.split(",") if x.strip()]
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []
        kind = str(d.get("kind") or "table")
        if kind not in KINDS:
            kind = "table"
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:10]),
            label=str(d.get("label") or ""),
            kind=kind,
            sheet=str(d.get("sheet") or ""),
            header_contains=_list("header_contains"),
            header_contains_any=_list("header_contains_any"),
            row_column=str(d.get("row_column") or ""),
            row_value=str(d.get("row_value") or ""),
            enabled=bool(d.get("enabled", True)),
        )

    # --- matching ----------------------------------------------------

    def matches(self, sheet_name: str, headers: list[str], rows: list[list[Any]]) -> bool:
        """Does this block match the rule?"""
        if not self.enabled:
            return False
        if self.sheet and _norm(self.sheet) != _norm(sheet_name):
            return False

        for want in self.header_contains:
            if not any(_column_matches(h, want) for h in headers):
                return False
        if self.header_contains_any:
            if not any(
                any(_column_matches(h, want) for h in headers)
                for want in self.header_contains_any
            ):
                return False

        if self.row_column:
            want_col = _norm(self.row_column)
            want_val = _norm(self.row_value)
            if not want_val:
                return False
            for r in rows:
                for i, h in enumerate(headers):
                    if _norm(h) != want_col:
                        continue
                    if i < len(r) and _norm(r[i]) == want_val:
                        return True
            return False
        return True

    def describe(self) -> str:
        """A one-line plain-English summary, for the rules list."""
        bits: list[str] = []
        if self.sheet:
            bits.append(f"in {self.sheet}")
        if self.header_contains:
            bits.append("has " + ", ".join(self.header_contains))
        if self.header_contains_any:
            bits.append("has one of " + ", ".join(self.header_contains_any))
        if self.row_column and self.row_value:
            bits.append(f"a row with {self.row_column} = {self.row_value}")
        return "; ".join(bits) if bits else "matches everything"


# ---------------------------------------------------------------------------
# Storage — the rules live in the valuer's Drive
# ---------------------------------------------------------------------------

def _rules_path() -> str:
    from demo.drive import DRIVE_ROOT_PATH
    return f"{DRIVE_ROOT_PATH}/Templates/{RULES_FILE}"


def _rules_object_name() -> str:
    """The name of the rules file inside the Templates folder."""
    return f"bma-factory/drive/{RULES_FILE}"


#: The rules are re-read from Drive at most this often. A rule change
#: made in the app takes effect immediately (the cache is invalidated);
#: this only stops every page view from making a Drive call.
_RULES_TTL = 60.0
_rules_cache: Optional[list["SectionRule"]] = None
_rules_cache_at: float = 0.0


def current_rules() -> list["SectionRule"]:
    """The rules, cached briefly so page views do not hit Drive."""
    global _rules_cache, _rules_cache_at
    now = time.time()
    if _rules_cache is not None and (now - _rules_cache_at) < _RULES_TTL:
        return _rules_cache
    rules = load_rules()
    _rules_cache = rules
    _rules_cache_at = now
    return rules


def invalidate_cache() -> None:
    """Forget the cached rules — call after a change."""
    global _rules_cache, _rules_cache_at
    _rules_cache = None
    _rules_cache_at = 0.0


def load_rules() -> list[SectionRule]:
    """The valuer's rules. Empty list when there are none or nobody is signed in."""
    try:
        from demo.drive import app_folder_if_exists
        from demo.oauth import get_drive
        folder_id = app_folder_if_exists("Templates")
        if not folder_id:
            return []
        drive = get_drive()
        r = drive.files().list(
            q=f"'{folder_id}' in parents and name='{RULES_FILE}' and trashed=false",
            fields="files(id,name,modifiedTime)",
            pageSize=5,
            supportsAllDrives=True,
        ).execute()
        files = r.get("files", [])
        if not files:
            return []
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, drive.files().get_media(fileId=files[0]["id"]))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = json.loads(buf.getvalue().decode("utf-8"))
        raw = data.get("rules") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        return [SectionRule.from_dict(d) for d in raw if isinstance(d, dict)]
    except Exception as e:  # noqa: BLE001
        log.info("could not load the section rules: %s", e)
        return []


def save_rules(rules: list[SectionRule]) -> tuple[bool, str]:
    """Write the rules back to Drive. Returns (ok, message)."""
    try:
        from demo.drive import app_folder
        from demo.oauth import get_drive
        from googleapiclient.http import MediaIoBaseUpload
        folder_id = app_folder("Templates")
        drive = get_drive()
        payload = json.dumps(
            {"version": 1, "rules": [r.to_dict() for r in rules]},
            indent=2, ensure_ascii=False,
        ).encode("utf-8")
        # Replace an existing rules file rather than piling up copies.
        existing = drive.files().list(
            q=f"'{folder_id}' in parents and name='{RULES_FILE}' and trashed=false",
            fields="files(id)",
            pageSize=5,
            supportsAllDrives=True,
        ).execute().get("files", [])
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )
        if existing:
            drive.files().update(
                fileId=existing[0]["id"], media_body=media,
                fields="id,name", supportsAllDrives=True,
            ).execute()
        else:
            drive.files().create(
                body={"name": RULES_FILE, "parents": [folder_id]},
                media_body=media, fields="id,name", supportsAllDrives=True,
            ).execute()
        return True, "saved"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def rules_digest(rules: list[SectionRule]) -> str:
    """A short digest of the rule set, for the parse cache key.

    The random id is left out and the position is kept, so:
      - renaming a rule changes the digest, because the label is on
        the page and the file has to be read again;
      - reordering changes the digest, because order decides which
        rule claims a block;
      - rebuilding the same rule set gives the same digest, so the
        cache is not thrown away for nothing.
    """
    import hashlib
    h = hashlib.sha256()
    for i, r in enumerate(rules):
        d = r.to_dict()
        d.pop("id", None)
        h.update(f"{i}:".encode("utf-8"))
        h.update(json.dumps(d, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:12]


def known_columns(parsed_workbooks: list[Any]) -> list[str]:
    """Every distinct column name across a set of parsed workbooks.

    Shown on the rules page so a rule can be written against the exact
    spelling in the file, instead of a guess. A rule that names a
    column wrongly matches nothing and is very hard to see why.
    """
    seen: dict[str, int] = {}
    for parsed in parsed_workbooks:
        for section in getattr(parsed, "sections", []) or []:
            for h in section.get("headers", []) or []:
                if not h:
                    continue
                seen[h] = seen.get(h, 0) + 1
    return sorted(seen, key=lambda h: (-seen[h], h))


def apply_rules(
    sections: list[Any],
    rules: list[SectionRule],
) -> list[Any]:
    """Label and classify the blocks, user rules first.

    A user rule that matches wins outright — the built-in detection is
    only consulted for blocks no rule claimed.
    """
    from demo import sections as S

    for section in sections:
        section.id = ""
        section.label = ""
        section.kind = ""
        section.rule_id = ""
        section.rule_origin = ""

    for rule in rules:
        if not rule.enabled:
            continue
        for section in sections:
            if section.kind:
                continue        # already claimed by an earlier rule
            if not rule.matches(section.sheet, section.headers, section.rows):
                continue
            section.id = f"user-{rule.id}"
            section.label = rule.label or KIND_LABELS.get(rule.kind, "Section")
            section.kind = rule.kind
            section.rule_id = rule.id
            section.rule_origin = "your rules"

    # Everything the rules did not claim falls back to the built-ins.
    for section in sections:
        if section.kind:
            continue
        spec = S.detect_section(section.headers)
        section.id = spec.id
        section.label = spec.label
        section.kind = spec.kind
        section.rule_id = ""
        section.rule_origin = "built-in"

    # Enrichment is per kind, so it runs after the kind is settled.
    for section in sections:
        spec = next((s for s in S.SECTION_SPECS if s.kind == section.kind), None)
        if spec is not None and spec.enrich is not None:
            try:
                spec.enrich(section)
            except Exception as e:  # noqa: BLE001
                section.notes.append(f"could not read this section fully: {e}")
    return sections
