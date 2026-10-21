"""The pin list — every project under Property Valuation and Sales
Brochure Review, in one shape.

One function, ``collect()``, returns both lists. The map page, the
JSON API and the tests all read this, so a project can never appear on
the map but not in the API, or the other way round.

Nothing here contacts the network. Coordinates come from
``demo.geo``'s stored values only. Geocoding happens when Bobby asks
for one project, on demand — fifty projects at one request a second is
a fifty-second page load, and the public geocoder will start refusing
before it finishes.
"""

from __future__ import annotations

import logging
from typing import Any

from demo import geo

log = logging.getLogger("bma-factory.map")


def _pin(
    key: str, *, pid: str, name: str, address: str, district: str,
    tab: str, href: str, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One project, with whatever location we already hold for it.

    Reads stored coordinates and nothing else. It must never call the
    geocoder: the public instance allows one request a second, so
    geocoding here would turn a two-second page into a minute-long one
    and get throttled before it finished. Locating a project is a
    deliberate action through ``POST /map/locate``.

    ``key`` is the shared lookup — the address, or the name plus
    district when there is no address — so a valuation case and a
    brochure review for the same estate resolve to one pin.
    """
    rec = geo.lookup(key)
    lat = rec.get("lat")
    lon = rec.get("lon")
    return {
        "key": key,
        "id": pid,
        "tab": tab,
        "name": name,
        "address": address,
        "district": district,
        "href": href,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "how": str(rec.get("how") or "") if lat is not None else "",
        "label": str(rec.get("label") or ""),
        **(extra or {}),
    }


def _address_key(address: str, district: str, name: str) -> str:
    if address:
        return address
    if name and district:
        return f"{name}, {district}"
    return name or ""


def collect() -> dict[str, list[dict[str, Any]]]:
    """Every project, grouped by tab."""
    return {"pv": _pv_pins(), "sbr": _sbr_pins()}


def _pv_pins() -> list[dict[str, Any]]:
    """Property Valuation: the embedded cases plus anything parsed out
    of a workbook in Drive."""
    pins: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        from demo.cases import CASES
        for c in CASES:
            key = _address_key(c.address, c.district, c.title)
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            pins.append(_pin(
                key, pid=c.id, name=c.title,
                address=c.address or "", district=c.district or "",
                tab="pv", href=f"/case/{c.id}",
                extra={
                    "source": "case",
                    "category": getattr(c, "category", ""),
                    "valuation_date": getattr(c, "valuation_date", ""),
                    "adopted_value_hkd": getattr(c, "adopted_value_hkd", 0) or 0,
                },
            ))
    except Exception as e:  # noqa: BLE001
        log.warning("could not list case pins: %s", e)

    # Drive-derived projects, via the one canonical walk in intake.
    # A parse failure on one workbook must not cost us the rest of the
    # list, which is why list_projects isolates each file.
    try:
        from demo import intake
        for entry in intake.list_projects():
            proj = (entry or {}).get("project") or {}
            name = str(proj.get("title") or "").strip()
            address = str(proj.get("address") or "").strip()
            district = str(proj.get("district") or "").strip()
            key = _address_key(address, district, name)
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            pins.append(_pin(
                key, pid=str(proj.get("id") or entry.get("file_id") or name),
                name=name or address or entry.get("name") or "Unnamed project",
                address=address, district=district,
                tab="pv", href=f"/content/{entry.get('file_id', '')}",
                extra={
                    "source": "drive",
                    "file_id": entry.get("file_id", ""),
                    "workbook": entry.get("name", ""),
                    "category": proj.get("category", ""),
                    "valuation_date": proj.get("valuation_date", ""),
                    "adopted_value_hkd": proj.get("adopted_value_hkd", 0) or 0,
                },
            ))
    except Exception as e:  # noqa: BLE001
        log.info("no Drive-derived pins: %s", e)

    return pins


def _sbr_pins() -> list[dict[str, Any]]:
    """Sales Brochure Review: one pin per development under review."""
    pins: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        from demo import brochure as B
        from datetime import date as _d
        devs = B.load_developments()
        today = _d.today()
    except Exception as e:  # noqa: BLE001
        log.warning("could not list brochure pins: %s", e)
        return pins

    for dev in devs:
        key = _address_key(dev.address, dev.district, dev.name)
        if not key:
            # No address and no name — nothing to put on a map, and
            # nothing to invent either.
            continue
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        summary = B.summarise(dev, today)
        rnd = dev.current_round
        pins.append(_pin(
            key, pid=dev.id, name=dev.name or dev.id,
            address=dev.address or "", district=dev.district or "",
            tab="sbr", href=f"/brochure/{dev.id}",
            extra={
                "client": dev.client or "",
                "round": getattr(rnd, "number", ""),
                "deadline": summary.get("deadline", ""),
                "target": summary.get("target", ""),
                "days_to_deadline": summary.get("days_to_deadline"),
                "items_needing_update": summary.get("items_needing_update", 0),
            },
        ))
    return pins


def summary(groups: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Counts for the page header and the tests."""
    out: dict[str, int] = {}
    for tab, pins in groups.items():
        out[f"{tab}_total"] = len(pins)
        out[f"{tab}_confirmed"] = sum(1 for p in pins if p["how"] == "confirmed")
        out[f"{tab}_geocoded"] = sum(1 for p in pins if p["how"] == "geocoded")
        out[f"{tab}_unlocated"] = sum(
            1 for p in pins if not p["lat"])
    return out