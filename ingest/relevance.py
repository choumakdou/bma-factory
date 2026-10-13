"""Comparable relevancy scoring and adoption.

Input: a Memfus Wong extract carrying the subject's own latest transaction plus
a pool of comparable transactions. Output: comparables ranked by relevancy, the
top N adopted, and a `locations` array shaped for the existing HTML map tool.

The one design decision everything else follows from: **relevancy is a function,
not an opinion.** Every factor is declared, weighted in config, and reported per
comparable, so the ranking is reproducible and arguable. A ranking a model
produced from intuition is not a ranking — it is a vibe with a number on it.

The second decision: **the subject is never its own comparable.** The extract
carries the subject's latest transaction, and the subject scores highest on
every factor by construction — it is the same building, the same area, the most
recent. Left unguarded, "adopt the top five" adopts the subject's own last price
and reports it as independent market evidence. That is circular, and it looks
exactly like a supported valuation.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

# --- weights ----------------------------------------------------------------
# Declared, not baked in. Changing a weight changes the ranking, and that change
# is visible in the config rather than buried in a model.

DEFAULT_WEIGHTS: dict[str, float] = {
    "same_building": 0.30,   # same building beats same street beats same district
    "distance":      0.25,   # physical proximity
    "recency":       0.20,   # how recent the transaction
    "area_similarity": 0.15,  # size comparability
    "usage_match":   0.10,   # shop vs shop, residential vs residential
}

MONTHS_HALF_LIFE = 18.0    # recency decay constant
DISTANCE_FULL_M = 400.0    # beyond this, proximity contributes nothing


@dataclass
class Weights:
    data: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def total(self) -> float:
        return sum(self.data.values()) or 1.0

    def __post_init__(self) -> None:
        unknown = set(self.data) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown relevance factor(s): {sorted(unknown)}")

    def scaled(self) -> dict[str, float]:
        """Normalise so weights always sum to 1 — the score stays comparable
        across subjects even if a factor set is later re-weighted."""
        t = self.total()
        return {k: v / t for k, v in self.data.items()}


# --- helpers ----------------------------------------------------------------

def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").replace("HK$", "").strip()
    m = re.match(r"^-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%b %Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --- factors ----------------------------------------------------------------

def f_same_building(subject: dict, comp: dict) -> float:
    sb = _norm(subject.get("building"))
    cb = _norm(comp.get("building"))
    if sb and cb and sb == cb:
        return 1.0
    sd, cd = _norm(subject.get("district")), _norm(comp.get("district"))
    if sd and cd and sd == cd:
        return 0.45
    if sd and cd and {sd[:2], cd[:2]} <= {"sha", "tin", "kow", "new"}:
        return 0.25
    return 0.0


def f_distance(subject: dict, comp: dict) -> float:
    slat, slon = _num(subject.get("lat")), _num(subject.get("lon"))
    clat, clon = _num(comp.get("lat")), _num(comp.get("lon"))
    if None in (slat, slon, clat, clon):
        return 0.5           # unknown → neutral, not zero
    d = haversine_m(slat, slon, clat, clon)
    if d <= 50:
        return 1.0
    if d >= DISTANCE_FULL_M:
        return 0.0
    return round(1.0 - (d - 50) / (DISTANCE_FULL_M - 50), 4)


def f_recency(subject: dict, comp: dict, as_of: str) -> float:
    cd = _date(comp.get("date"))
    if cd is None:
        return 0.3
    a = _date(as_of) or date.today()
    months = max(0.0, (a - cd).days / 30.44)
    return round(0.5 ** (months / MONTHS_HALF_LIFE), 4)


def f_area_similarity(subject: dict, comp: dict) -> float:
    sa, ca = _num(subject.get("area_sf")), _num(comp.get("area_sf"))
    if not sa or not ca:
        return 0.3
    ratio = abs(ca - sa) / sa
    return round(max(0.0, 1.0 - ratio), 4)      # ±0% → 1.0, ±100% → 0.0


def f_usage_match(subject: dict, comp: dict) -> float:
    su, cu = _norm(subject.get("usage")), _norm(comp.get("usage"))
    if not su or not cu:
        return 0.4
    if su == cu:
        return 1.0
    a, b = set(su.replace("/", " ").split()), set(cu.replace("/", " ").split())
    if a & b:
        return 0.6
    if ("shop" in su) == ("shop" in cu):
        return 0.4
    return 0.0


FACTORS = {
    "same_building": f_same_building,
    "distance": f_distance,
    "recency": f_recency,
    "area_similarity": f_area_similarity,
    "usage_match": f_usage_match,
}


# --- scoring ----------------------------------------------------------------

@dataclass
class Scored:
    comp: dict
    total: float
    breakdown: dict[str, float]

    @property
    def address(self) -> str:
        return str(self.comp.get("address", ""))


def score_one(subject: dict, comp: dict, as_of: str, w: Weights) -> Scored:
    weights = w.scaled()
    parts: dict[str, float] = {}
    for name, weight in weights.items():
        if name == "recency":
            parts[name] = f_recency(subject, comp, as_of)
        else:
            parts[name] = FACTORS[name](subject, comp)
    total = sum(parts[k] * weights[k] for k in weights)
    return Scored(comp, round(total, 4), {k: round(v, 4) for k, v in parts.items()})


def rank(subject: dict, comps: Sequence[dict], as_of: str, w: Weights | None = None) -> list[Scored]:
    w = w or Weights()
    out = [score_one(subject, c, as_of, w) for c in comps]
    # deterministic ordering: score desc, then recency, then address, so a tie
    # never reorders between runs
    out.sort(key=lambda s: (-s.total, _date(s.comp.get("date")) or date.min, s.address))
    return out


# --- circularity guard ------------------------------------------------------

def is_self(subject: dict, comp: dict) -> tuple[bool, str]:
    """Is this 'comparable' actually the subject transacting again?

    Every flat in a building shares the street number, so a street-number
    overlap proves nothing — it flags the whole building. The subject is one
    specific flat, so the evidence has to identify a specific flat: the same
    transaction reference, the same floor and flat number, or the same floor
    with a matching area.
    """
    if comp.get("is_subject") or _norm(comp.get("role")) == "subject":
        return True, "flagged as the subject record in the extract"

    sref = str(subject.get("mem_no") or subject.get("txn_ref") or "").strip()
    cref = str(comp.get("mem_no") or comp.get("txn_ref") or "").strip()
    if sref and cref and sref == cref:
        return True, f"same transaction reference ({sref}) as the subject"

    sb, cb = _norm(subject.get("building")), _norm(comp.get("building"))
    same_building = bool(sb) and sb == cb

    if same_building:
        sf, cf = _norm(subject.get("flat")), _norm(comp.get("flat"))
        of, nf = _norm(subject.get("floor")), _norm(comp.get("floor"))
        if sf and cf and sf == cf and of and nf and of == nf:
            return True, f"same building and the same unit ({of}/{sf}) as the subject"
        sarea, carea = _num(subject.get("area_sf")), _num(comp.get("area_sf"))
        if of and nf and of == nf and sarea and carea and abs(sarea - carea) / sarea < 0.05:
            return True, (f"same building and floor {of}, area within 5% "
                          f"({carea:,.0f} vs {sarea:,.0f} sf) — reads as the same unit")

    saddr, caddr = _norm(subject.get("address")), _norm(comp.get("address"))
    if saddr and caddr and saddr == caddr:
        return True, "identical address string to the subject"
    return False, ""


# --- selection --------------------------------------------------------------

@dataclass
class Selection:
    adopted: list[Scored]
    excluded_self: list[tuple[Scored, str]]
    considered: int
    weighting: str = "equal"
    below_floor: list[Scored] = field(default_factory=list)
    floor: float = 0.0

    @property
    def warning(self) -> str | None:
        if not self.adopted:
            return ("no comparable reached the relevancy floor — the evidence is not "
                    "good enough to adopt a rate; widen the search or gather more "
                    "transactions rather than adopting the least-bad option")
        if len(self.adopted) < 3:
            return (f"only {len(self.adopted)} comparable(s) cleared the floor — "
                    "a rate from fewer than three is not supportable")
        if self.below_floor:
            return f"{len(self.below_floor)} ranked below the floor of {self.floor:.2f}"
        return None


def select(ranked: list[Scored], subject: dict, n: int = 5, weighting: str = "equal",
           min_score: float = 0.0) -> Selection:
    """Top n by relevancy, equal weighting, with the subject removed from the pool.

    `min_score` exists because "the best of a bad set" is not evidence. Without
    a floor, a 194-row market-wide extract will always return five comps and an
    equal-weighted mean — and that number looks exactly as authoritative as one
    built from good evidence. The floor makes "there is nothing here" a possible
    answer, which is the honest one.
    """
    kept: list[Scored] = []
    dropped: list[tuple[Scored, str]] = []
    below: list[Scored] = []
    for s in ranked:
        hit, why = is_self(subject, s.comp)
        if hit:
            dropped.append((s, why))
        elif s.total < min_score:
            below.append(s)
        else:
            kept.append(s)
    return Selection(kept[:n], dropped, len(ranked), weighting, below, min_score)


def equal_weighted_rate(adopted: list[Scored]) -> float | None:
    rates = []
    for s in adopted:
        c = s.comp
        area, price = _num(c.get("area_sf")), _num(c.get("price"))
        if area and price:
            rates.append(price / area)
    return sum(rates) / len(rates) if rates else None


# --- output shape for the existing HTML map tool ----------------------------

def _fmt_price(v: Any) -> str:
    n = _num(v)
    if n is None:
        return ""
    return f"${n / 1_000_000:.2f}M" if n >= 1_000_000 else f"${n:,.0f}"


def _fmt_rate(v: Any) -> str:
    n = _num(v)
    return f"${n:,.0f}/sf" if n else ""


def txn_line(rec: dict) -> str:
    """The pipe-delimited form the existing tool already parses."""
    d = _date(rec.get("date"))
    area, price = _num(rec.get("area_sf")), _num(rec.get("price"))
    fl, fa = str(rec.get("floor", "")), str(rec.get("flat", ""))
    floor = f"{fl}/{fa}" if fl and fa and "/" not in fl else fl
    return "|".join([
        f"#{rec.get('no', '')}".strip("#"),
        floor,
        str(rec.get("tenure", "")),
        d.strftime("%d %b %Y") if d else "",
        f"{area:,.0f}sf" if area else "",
        _fmt_price(price),
        _fmt_rate(price / area) if price and area else "",
    ])


def txn_text(rec: dict) -> str:
    """The tilde-joined transaction block the existing tool already parses.

    One record when the comp is a single transaction; several joined with `~~~`
    when a location carries more than one, which is the shape
    V-ALE-303767-2026_map.html was built around (`srcRows: "2,3,4,5,6"`).
    """
    txns = rec.get("transactions")
    if isinstance(txns, list) and txns:
        return "~~~".join(txn_line({**rec, **t}) for t in txns)
    return txn_line(rec)


def to_locations(subject: dict, sel: Selection, ranked: list[Scored],
                 adopted_ids: set[str] | None = None) -> list[dict]:
    """Emit the exact `locations` array the existing HTML tool consumes.

    Fields, delimiter conventions and ordering match V-ALE-303767-2026_map.html
    so the tool needs no change beyond being pointed at new data.
    """
    adopted_ids = adopted_ids or {id(s.comp) for s in sel.adopted}
    out: list[dict] = []

    out.append({
        "no": "S",
        "name": subject.get("name", "Subject"),
        "address": subject.get("address", ""),
        "count": str(len(sel.adopted)),
        "priceRange": (
            f"{_fmt_price(min((_num(s.comp.get('price')) for s in sel.adopted), default=0))}-"
            f"{_fmt_price(max((_num(s.comp.get('price')) for s in sel.adopted), default=0))}"
            if sel.adopted else "—"
        ),
        "srcRows": subject.get("srcRows", ""),
        "isSubject": 1,
        "txnText": "",
    })

    for i, s in enumerate(sel.adopted, start=1):
        c = s.comp
        out.append({
            "no": f"C{i}",
            "name": c.get("building") or c.get("address", ""),
            "address": c.get("address", ""),
            "count": str(len(c.get("transactions") or [c])),
            "priceRange": _fmt_price(c.get("price")),
            "srcRows": c.get("srcRows", ""),
            "isSubject": 0,
            "txnText": txn_text({**c, "no": f"C{i}"}),
            "_score": s.total,
            "_breakdown": s.breakdown,
        })
    return out


def to_json(subject: dict, sel: Selection, ranked: list[Scored]) -> str:
    return json.dumps(to_locations(subject, sel, ranked), ensure_ascii=False, indent=2)
