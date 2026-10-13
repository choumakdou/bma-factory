"""Property rules — the institutional memory layer.

Some properties carry a standing treatment: a narrowed set of acceptable
buildings to draw comparables from, a location adjustment that reflects a
building characteristic rather than a market movement, a mandatory disclosure,
a method the client requires. These are not recalculated each year — they are
carried forward and reviewed deliberately.

Without them stored, they live in the valuer's memory and in last year's file.
Miss one and the value is wrong with nothing to catch it. That is the failure
this module exists to remove.

Two kinds of adjustment must not be confused:

    time adjustment       moves with the RVD index   -> recompute every year
    property adjustment   fixed by the asset         -> carry forward, review

The schema keeps them apart on purpose. Recomputing a property adjustment from
scratch each year is how it drifts.

Governance, not storage, is the point: a carried-forward rule that nobody has
looked at in three years is a liability, so `last_reviewed` and
`carried_count` are first-class, and staleness is a query rather than a
surprise.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

# --- rule types -------------------------------------------------------------

RULE_COMP_ALLOWLIST = "comp_allowlist"
RULE_COMP_DENYLIST = "comp_denylist"
RULE_ADJUSTMENT_OVERRIDE = "adjustment_override"
RULE_MANDATORY_NOTE = "mandatory_note"
RULE_METHOD_OVERRIDE = "method_override"

RULE_TYPES = {
    RULE_COMP_ALLOWLIST: "Only draw comparables from these buildings / estates",
    RULE_COMP_DENYLIST: "Never draw comparables from these buildings / estates",
    RULE_ADJUSTMENT_OVERRIDE: "Force an adjustment factor, overriding the derived one",
    RULE_MANDATORY_NOTE: "A disclosure that must appear in the report",
    RULE_METHOD_OVERRIDE: "A valuation method the client or asset requires",
}

# adjustments that move with the market and must be recomputed, never carried
TIME_VARYING = {"time", "date", "market"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS property_rules (
    id            INTEGER PRIMARY KEY,
    property_id   INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    rule_type     TEXT NOT NULL,
    rule_key      TEXT,
    value_json    TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    last_reviewed  TEXT,
    set_by         TEXT,
    carried_count  INTEGER DEFAULT 0,
    superseded_by  INTEGER REFERENCES property_rules(id),
    UNIQUE (property_id, rule_type, rule_key, effective_from)
);

CREATE INDEX IF NOT EXISTS ix_rules_property ON property_rules(property_id, effective_from);
"""


@dataclass
class Rule:
    id: int
    property_id: int
    rule_type: str
    rule_key: str
    value: dict[str, Any]
    rationale: str
    effective_from: str
    effective_to: str | None
    last_reviewed: str | None
    set_by: str
    carried_count: int

    @property
    def label(self) -> str:
        return f"{self.rule_type}:{self.rule_key}" if self.rule_key else self.rule_type


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# --- CRUD -------------------------------------------------------------------

def add_rule(
    conn: sqlite3.Connection,
    property_id: int,
    rule_type: str,
    value: dict[str, Any],
    rationale: str,
    effective_from: str,
    rule_key: str = "",
    set_by: str = "",
    last_reviewed: str | None = None,
) -> int:
    if rule_type not in RULE_TYPES:
        raise ValueError(f"unknown rule type {rule_type!r}; expected one of {sorted(RULE_TYPES)}")
    if not rationale or not rationale.strip():
        # a bare number with no reason cannot be reviewed by anyone, including
        # the person who set it, two years later
        raise ValueError("a rule must carry a rationale — it is what makes it reviewable")
    if rule_type == RULE_ADJUSTMENT_OVERRIDE:
        adj = (value.get("adj") or "").lower()
        if adj in TIME_VARYING:
            raise ValueError(
                f"'{adj}' is time-varying and must be recomputed, not carried forward"
            )
        if "pct" not in value:
            raise ValueError("adjustment_override needs a 'pct'")
    if rule_type in (RULE_COMP_ALLOWLIST, RULE_COMP_DENYLIST) and not value.get("buildings"):
        raise ValueError(f"{rule_type} needs a 'buildings' list")

    cur = conn.execute(
        """INSERT INTO property_rules
           (property_id, rule_type, rule_key, value_json, rationale,
            effective_from, last_reviewed, set_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (property_id, rule_type, rule_key, json.dumps(value, sort_keys=True), rationale.strip(),
         effective_from, last_reviewed or effective_from, set_by or ""),
    )
    conn.commit()
    return cur.lastrowid


def _row_to_rule(row) -> Rule:
    return Rule(
        id=row[0], property_id=row[1], rule_type=row[2], rule_key=row[3] or "",
        value=json.loads(row[4]), rationale=row[5], effective_from=row[6],
        effective_to=row[7], last_reviewed=row[8], set_by=row[9] or "",
        carried_count=row[10] or 0,
    )


def effective_rules(conn: sqlite3.Connection, property_id: int, as_of: str) -> list[Rule]:
    """Rules in force for this property on this date.

    Effective dating means you can answer "which treatments applied to last
    year's valuation" as well as "which apply now" — without which a change
    report cannot explain a movement.
    """
    rows = conn.execute(
        """SELECT id, property_id, rule_type, rule_key, value_json, rationale,
                  effective_from, effective_to, last_reviewed, set_by, carried_count
             FROM property_rules
            WHERE property_id = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
              AND superseded_by IS NULL
            ORDER BY rule_type, rule_key""",
        (property_id, as_of, as_of),
    ).fetchall()
    return [_row_to_rule(r) for r in rows]


def end_rule(conn: sqlite3.Connection, rule_id: int, as_of: str) -> None:
    """Retire a rule without deleting it. History is the point."""
    conn.execute("UPDATE property_rules SET effective_to=? WHERE id=?", (as_of, rule_id))
    conn.commit()


def mark_carried(conn: sqlite3.Connection, rule_ids: Iterable[int]) -> None:
    """Record that these rules were applied again.

    Deliberately does NOT touch `last_reviewed`. Reusing a rule is not the same
    as looking at it — conflating the two means a treatment can be carried
    forward every year for a decade and always look freshly reviewed. Reviewing
    is a human action and goes through `mark_reviewed`.
    """
    conn.executemany(
        "UPDATE property_rules SET carried_count = carried_count + 1 WHERE id = ?",
        [(rid,) for rid in rule_ids],
    )
    conn.commit()


def mark_reviewed(conn: sqlite3.Connection, rule_ids: Iterable[int], as_of: str) -> None:
    """A person looked at this rule and confirmed it still holds."""
    conn.executemany(
        "UPDATE property_rules SET last_reviewed = ? WHERE id = ?",
        [(as_of, rid) for rid in rule_ids],
    )
    conn.commit()


# --- staleness --------------------------------------------------------------

@dataclass
class Stale:
    rule: Rule
    days_since_review: int | None
    carried: int
    reason: str


def staleness(rules: list[Rule], as_of: str, review_days: int = 730) -> list[Stale]:
    """Rules that should be looked at again before they are reused.

    Not an error — a carried-forward treatment can be perfectly right for a
    decade. But it should be a decision, not an oversight, and the only way to
    keep that distinction visible is to surface it.
    """
    out: list[Stale] = []
    asof = date.fromisoformat(as_of)
    for r in rules:
        days = None
        if r.last_reviewed:
            try:
                days = (asof - date.fromisoformat(r.last_reviewed)).days
            except ValueError:
                pass
        if days is not None and days > review_days:
            out.append(Stale(r, days, r.carried_count,
                             f"not reviewed for {days // 365}y {days % 365}d"))
        elif r.carried_count >= 4 and days is None:
            out.append(Stale(r, None, r.carried_count,
                             f"carried forward {r.carried_count}x, never formally reviewed"))
    return out


# --- application ------------------------------------------------------------

@dataclass
class Application:
    """What the rules actually did, so the report can say so."""
    adjustments: dict[str, Any]
    comps: list[dict[str, Any]]
    notes: list[str]
    applied: list[str]          # rule labels applied
    excluded: list[str]         # comp addresses dropped, and why


def apply_adjustments(rules: list[Rule], derived: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Overlay property-specific adjustments on the derived set.

    Time adjustments are never touched here — that is enforced at write time,
    so a time adjustment cannot be quietly pinned.
    """
    out = dict(derived)
    applied: list[str] = []
    for r in rules:
        if r.rule_type != RULE_ADJUSTMENT_OVERRIDE:
            continue
        adj = (r.value.get("adj") or "").lower()
        if adj in TIME_VARYING:
            continue
        out[adj] = r.value["pct"]
        applied.append(f"{adj} pinned at {r.value['pct']:+.1%} — {r.rationale}")
    return out, applied


def filter_comps(rules: list[Rule], comps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(kept, applied_notes, dropped)

    Kept separate because a rule's *effect* and a comp it *removed* are
    different facts and belong in different places in the report.
    """
    allow: set[str] = set()
    deny: set[str] = set()
    applied: list[str] = []
    for r in rules:
        if r.rule_type == RULE_COMP_ALLOWLIST:
            allow |= {b.lower() for b in r.value["buildings"]}
            applied.append(f"comparables narrowed to {len(allow)} building(s) — {r.rationale}")
        elif r.rule_type == RULE_COMP_DENYLIST:
            deny |= {b.lower() for b in r.value["buildings"]}
            applied.append(f"{len(deny)} building(s) excluded — {r.rationale}")

    def building_of(c: dict[str, Any]) -> str:
        return str(c.get("building") or c.get("estate") or "").lower()

    kept, dropped = [], []
    for c in comps:
        b = building_of(c)
        if deny and b in deny:
            dropped.append(f"{c.get('address', '?')} — {b} is on the denylist")
        elif allow and b not in allow:
            dropped.append(f"{c.get('address', '?')} — {b} is not on the allowlist")
        else:
            kept.append(c)
    return kept, applied, dropped


def collect_notes(rules: list[Rule]) -> list[str]:
    """Mandatory notes, carrying where they belong.

    The insertion point is part of the requirement — a disclosure that has to
    appear in §5.2 is not satisfied by appearing somewhere, and a report
    generator cannot place it without being told.
    """
    out: list[str] = []
    for r in rules:
        if r.rule_type != RULE_MANDATORY_NOTE:
            continue
        text = (r.value.get("text") or "").strip()
        if not text:
            continue
        where = r.value.get("insert_in") or r.rule_key or "body"
        out.append(f"[{where}] {text}")
    return out


def apply_all(rules: list[Rule], derived_adjustments: dict[str, Any],
              comps: list[dict[str, Any]]) -> Application:
    adj, adj_notes = apply_adjustments(rules, derived_adjustments)
    kept, comp_notes, dropped = filter_comps(rules, comps)
    return Application(
        adjustments=adj,
        comps=kept,
        notes=collect_notes(rules) + adj_notes + comp_notes,
        applied=[r.label for r in rules],
        excluded=dropped,
    )


# --- change report ----------------------------------------------------------

def method_drift(prior: list[Rule], current: list[Rule]) -> dict[str, list[str]]:
    """What changed in the *method*, as distinct from what changed in the data.

    A value can move because the market moved or because the treatment moved.
    Those are different events and only one of them is the market.
    """
    pk = {r.label for r in prior}
    ck = {r.label for r in current}
    return {
        "rules_added": sorted(ck - pk),
        "rules_removed": sorted(pk - ck),
        "rules_carried": sorted(pk & ck),
    }
