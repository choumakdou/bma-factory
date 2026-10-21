"""Where the projects are — coordinates for the map view.

Two rules govern this module, and both come from the same place:

1. **A pin is never invented.** A location is either confirmed by the
   valuer, or geocoded from a written address and marked as
   unconfirmed, or absent. There is no third state where the app
   decides where a building is. A wrong pin on a map is worse than no
   pin, because it looks authoritative.

2. **The address is the key, not the project id.** The same building
   can appear as a valuation case, a Drive-derived project and a
   brochure development. They share one location, so they share one
   lookup — and confirming the pin once settles all three.

The map itself is Leaflet on OpenStreetMap tiles: no API key, no
billing, nothing that can expire on a Sunday night.

Storage is the same Cloud Storage bucket as everything else, under
``bma-factory/map/coordinates.json``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

log = logging.getLogger("bma-factory.geo")

_COORDS_OBJECT = "bma-factory/map/coordinates.json"

#: Nominatim's fair-use policy is one request a second and a real
#: identifying User-Agent. We hold to both rather than being the
#: client that gets the public instance throttled for everyone.
_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "bma-factory/1.0 (CHFT valuation console; contact: bobby.hk.mak@gmail.com)"
_MIN_INTERVAL = 1.05  # seconds between geocode calls
_TIMEOUT = 12

#: Hong Kong, used to sanity-check whatever the geocoder returns.
#: A geocoder that puts Yuen Long in Alaska has not geocoded Yuen Long.
_HK_LAT = (22.15, 22.60)
_HK_LON = (113.83, 114.45)

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
_loaded = False
_last_call = 0.0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from demo.oauth import _gcs_read_from
        blob = _gcs_read_from(_COORDS_OBJECT)
        if not blob:
            return
        data = json.loads(blob)
        if isinstance(data, dict):
            with _lock:
                _cache.update(data)
            log.info("loaded %d map coordinates", len(data))
    except Exception as e:  # noqa: BLE001
        log.warning("could not load map coordinates: %s", e)


def _save() -> None:
    try:
        from demo.oauth import _gcs_write_to
        with _lock:
            payload = json.dumps(_cache, ensure_ascii=False, indent=1)
        _gcs_write_to(payload, _COORDS_OBJECT)
    except Exception as e:  # noqa: BLE001
        log.warning("could not save map coordinates: %s", e)


def lookup(key: str) -> dict[str, Any]:
    """What we know about a location: confirmed, geocoded, or nothing."""
    if not key:
        return {}
    _load()
    with _lock:
        return dict(_cache.get(_clean(key), {}))


def remember(key: str, record: dict[str, Any]) -> None:
    _load()
    with _lock:
        _cache[_clean(key)] = dict(record)
    _save()


def forget(key: str) -> None:
    _load()
    with _lock:
        _cache.pop(_clean(key), None)
    _save()


def _clean(key: str) -> str:
    return " ".join(str(key or "").strip().split()).lower()


def coords(key: str) -> tuple[Optional[float], Optional[float], str]:
    """(lat, lon, how) for a location key.

    ``how`` is ``"confirmed"``, ``"geocoded"`` or ``""`` — and the
    caller is expected to show the difference. A geocoded pin is a
    suggestion from a public database, not a surveyed location.
    """
    rec = lookup(key)
    lat, lon = rec.get("lat"), rec.get("lon")
    if lat is None or lon is None:
        return None, None, ""
    return float(lat), float(lon), str(rec.get("how") or "geocoded")


def is_confirmed(key: str) -> bool:
    return lookup(key).get("how") == "confirmed"


def confirm(key: str, lat: float, lon: float, note: str = "") -> None:
    """The valuer has put the pin where it belongs. That outranks all."""
    remember(key, {
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "how": "confirmed",
        "note": str(note or ""),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "valuer",
    })


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def _in_hk(lat: float, lon: float) -> bool:
    return (_HK_LAT[0] <= lat <= _HK_LAT[1]
            and _HK_LON[0] <= lon <= _HK_LON[1])


def geocode(address: str, district: str = "", name: str = "") -> Optional[tuple[float, float, str]]:
    """Look a place up. ``(lat, lon, label)`` or None.

    Two hard-won facts about the public geocoder, both of which cost
    a wrong-looking empty result before they were found:

    * ``countrycodes=hk`` returns **nothing** for Hong Kong. It is
      accepted by the API and quietly filters every HK result away.
      The bounding-box check below is what actually keeps us in HK.
    * Coverage of individual buildings is patchy. A residential
      estate is usually in OSM under its *name*; the street address
      alone often finds nothing.

    So the queries are tried in descending confidence: the estate name
    first, then the address with its district, then the address alone.
    """
    for query in _queries(address, district, name):
        hit = _geocode_one(query)
        if hit:
            return hit
    return None


def _queries(address: str, district: str, name: str) -> list[str]:
    addr = str(address or "").strip()
    dist = str(district or "").strip()
    nm = str(name or "").strip()
    # Bel-Air's stored address already ends ", Hong Kong" — repeating it
    # makes the query noisier for no gain.
    for suffix in (", Hong Kong", ", Hongkong", ", HK"):
        if addr.lower().endswith(suffix.lower()):
            addr = addr[: -len(suffix)].strip()
    out: list[str] = []
    if nm:
        out.append(", ".join(x for x in [nm, dist, "Hong Kong"] if x))
    if addr and dist:
        out.append(", ".join(x for x in [addr, dist, "Hong Kong"] if x))
    if addr:
        out.append(f"{addr}, Hong Kong")
    if dist:
        out.append(", ".join(x for x in [dist, "Hong Kong"] if x))
    # de-duplicate, keep order
    seen: set[str] = set()
    return [q for q in out if not (q.lower() in seen or seen.add(q.lower()))]


def _geocode_one(query: str) -> Optional[tuple[float, float, str]]:
    query = str(query or "").strip()
    if not query:
        return None

    _load()
    with _lock:
        cached = _cache.get(_clean(query))
    if cached and cached.get("lat") is not None:
        return (float(cached["lat"]), float(cached["lon"]),
                str(cached.get("label") or query))

    # Nominatim asks for at most one request a second, and throttles
    # hard when that is ignored.
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)

    _last_call = time.monotonic()
    for attempt in range(3):
        try:
            import json as _json
            import urllib.parse as _up
            import urllib.request as _ur

            url = (f"{_NOMINATIM}?" + _up.urlencode({
                "q": query, "format": "jsonv2", "limit": "1",
            }))
            req = _ur.Request(url, headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "en,zh-HK",
            })
            with _ur.urlopen(req, timeout=_TIMEOUT) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                log.info("geocode failed for %r: %s", query, e)
                return None
            # A throttled public instance wants a longer pause, not
            # an immediate retry.
            time.sleep(2.5 * (attempt + 1))
            _last_call = time.monotonic()

    if not isinstance(data, list) or not data:
        log.info("no geocode result for %r", query)
        return None
    hit = data[0]
    try:
        lat, lon = float(hit["lat"]), float(hit["lon"])
    except Exception:  # noqa: BLE001
        return None
    if not _in_hk(lat, lon):
        # A street name that exists in three countries. Rejected, not
        # shown — a pin in the wrong hemisphere is worse than no pin.
        log.info("geocode for %r landed outside HK (%s, %s)", query, lat, lon)
        return None

    label = str(hit.get("display_name") or query)
    with _lock:
        _cache[_clean(query)] = {
            "lat": round(lat, 6), "lon": round(lon, 6),
            "how": "geocoded", "label": label,
            "query": query,
            "source": "openstreetmap/nominatim",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    _save()
    return round(lat, 6), round(lon, 6), label


def resolve(
    address: str, district: str = "", name: str = "",
) -> tuple[Optional[float], Optional[float], str, str]:
    """(lat, lon, how, label) for a project.

    ``how`` is ``"confirmed"``, ``"geocoded"`` or ``""``. The caller is
    expected to show the difference: a geocoded point is a suggestion
    from a public database, not a surveyed location.
    """
    for key in (address, name and f"{name}, {district}" if district else name):
        if key:
            lat, lon, how = coords(key)
            if lat is not None:
                return lat, lon, how, lookup(key).get("label", "")
    got = geocode(address, district, name)
    if got:
        lat, lon, label = got
        return lat, lon, "geocoded", label
    return None, None, "", ""


def status() -> dict[str, Any]:
    """Counts for the diagnostics page."""
    _load()
    with _lock:
        recs = list(_cache.values())
    return {
        "object": _COORDS_OBJECT,
        "known": len(recs),
        "confirmed": sum(1 for r in recs if r.get("how") == "confirmed"),
        "geocoded": sum(1 for r in recs if r.get("how") == "geocoded"),
    }