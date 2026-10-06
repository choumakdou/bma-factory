"""Typed field definitions and validation.

The whole determinism argument rests on this file. A generator is only
reproducible if its inputs are *typed* -- otherwise `1250`, `1,250` and `1.25k`
all mean the same thing to a human and three different things to the code.

Nothing here knows about Word, Excel or PowerPoint. It only knows how to take a
dict and prove it is well-formed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    ERROR = "error"   # generation is refused
    WARNING = "warning"  # generation proceeds, QC flag recorded


@dataclass
class Issue:
    field: str
    message: str
    severity: Severity = Severity.ERROR

    def __str__(self) -> str:  # pragma: no cover - display only
        icon = {"error": "🔴", "warning": "🟡"}[self.severity.value]
        return f"{icon} {self.field}: {self.message}"


# --- coercion helpers -------------------------------------------------------

_NUM_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
_CURRENCY_RE = re.compile(r"^HK\$\s*(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?$")
_PCT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%?$")

# HK land lot forms we accept, e.g. "IL 588 S.A R.P.", "KCTL 123", "TWIL 45 S.A."
_LOT_RE = re.compile(
    r"^(IL|KCTL|KCIL|TWIL|ILR|TL|ML|HL|NIL|NKIL|SL|RP|KTN|TKOTL)\s*\d+"
    r"(?:\s*S\.?\s*A\.?(?:\s*R\.?\s*P\.?)?)?$",
    re.IGNORECASE,
)

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y")


def _to_float(raw: str) -> float | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace(" ", "")
    if not s:
        return None
    for pattern in (_CURRENCY_RE, _NUM_RE):
        m = pattern.match(s)
        if m:
            digits = re.sub(r"[^\d.\-]", "", s)
            try:
                return float(digits)
            except ValueError:
                return None
    return None


def _to_date(raw: str) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_pct(raw: Any) -> float | None:
    """Percent field. Returns a *fraction* (5.2 -> 0.052) so it can be used
    directly inside `(1 + x)` style adjustment formulas."""
    if isinstance(raw, (int, float)):
        v = float(raw)
    elif isinstance(raw, str):
        m = _PCT_RE.match(raw.strip())
        if not m:
            return None
        v = float(m.group(1))
        if not raw.strip().endswith("%") and abs(v) > 1:
            v = v / 100.0
    else:
        return None
    return v / 100.0 if abs(v) > 1 else v


# --- field definition -------------------------------------------------------

@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str = "str"          # str | int | float | currency | pct | date | lot | email
    required: bool = True
    section: str = "General"
    unit: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""


@dataclass
class Schema:
    fields: list[Field]
    name: str = ""

    def by_key(self) -> dict[str, Field]:
        return {f.key: f for f in self.fields}

    def sections(self) -> list[str]:
        seen: list[str] = []
        for f in self.fields:
            if f.section not in seen:
                seen.append(f.section)
        return seen


@dataclass
class ValidationResult:
    values: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]


def validate(schema: Schema, payload: dict[str, Any]) -> ValidationResult:
    """Coerce and check `payload` against `schema`.

    Coercion is deliberate: `HK$ 1,250.00` and `1250` both become 1250.0. That
    is what stops three spellings of the same number producing three different
    workbooks. Anything that *cannot* be coerced becomes an ERROR rather than a
    silent zero.
    """
    result = ValidationResult()
    known = schema.by_key()

    for key, value in payload.items():
        if key not in known and not any(k.startswith(f"{key}.") or f"{key}." in k for k in known):
            # unknown keys are allowed (modules carry extra provenance) but
            # recorded, because a typo'd field name is a silent data-loss bug.
            if key not in known:
                result.issues.append(
                    Issue(key, "field is not in the module schema - ignored", Severity.WARNING)
                )

    for f in schema.fields:
        raw = payload.get(f.key)

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if f.required:
                result.issues.append(Issue(f.key, f"{f.label} is required", Severity.ERROR))
            continue

        if f.type == "list":
            # A list field (e.g. a comparable table). The framework checks only
            # that it *is* a list; the module validates the contents, because
            # the shape of a comparable is the module's business, not ours.
            if not isinstance(raw, list):
                result.issues.append(
                    Issue(f.key, f"{f.type} field must be an array", Severity.ERROR)
                )
                continue
            result.values[f.key] = raw
            continue

        if f.type in ("str",) or f.type not in {
            "int", "float", "currency", "pct", "date", "lot", "email"
        }:
            sval = str(raw).strip()
            if f.choices and sval not in f.choices:
                result.issues.append(
                    Issue(
                        f.key,
                        f"{sval!r} is not one of: {', '.join(f.choices)}",
                        Severity.ERROR,
                    )
                )
            else:
                result.values[f.key] = sval
            continue

        if f.type == "date":
            d = _to_date(raw)
            if d is None:
                result.issues.append(
                    Issue(
                        f.key,
                        f"{raw!r} is not a date. Use YYYY-MM-DD or '21 March 2011'",
                        Severity.ERROR,
                    )
                )
            else:
                result.values[f.key] = d
            continue

        if f.type == "lot":
            s = str(raw).strip()
            if not _LOT_RE.match(s):
                result.issues.append(
                    Issue(f.key, f"{s!r} is not a recognised HK lot form (e.g. 'IL 588 S.A R.P.')", Severity.ERROR)
                )
            else:
                result.values[f.key] = s
            continue

        if f.type == "email":
            s = str(raw).strip()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s):
                result.issues.append(Issue(f.key, f"{s!r} is not a valid email", Severity.ERROR))
            else:
                result.values[f.key] = s
            continue

        # numeric family
        if f.type == "pct":
            v = _to_pct(raw)
        else:
            v = _to_float(raw if isinstance(raw, str) else str(raw))
            if v is None:
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    v = None

        if v is None:
            result.issues.append(
                Issue(f.key, f"{raw!r} is not a valid {f.type}", Severity.ERROR)
            )
            continue

        if f.type == "int" and abs(v - round(v)) > 1e-9:
            result.issues.append(Issue(f.key, f"{raw!r} must be a whole number", Severity.ERROR))
            continue

        if f.minimum is not None and v < f.minimum:
            result.issues.append(Issue(f.key, f"must be >= {f.minimum:g}", Severity.ERROR))
            continue
        if f.maximum is not None and v > f.maximum:
            result.issues.append(Issue(f.key, f"must be <= {f.maximum:g}", Severity.ERROR))
            continue

        result.values[f.key] = int(round(v)) if f.type in ("int", "currency") else v

    return result