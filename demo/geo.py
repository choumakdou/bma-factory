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
import re
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

#: Bumped when the rules for accepting a result change. A stored
#: suggestion made under older rules is treated as absent, so a rule
#: change cannot leave yesterday's wrong answer sitting on the map.
#: CONFIRMED locations are never invalidated — the valuer's judgement
#: outranks this version number.
_GATE_VERSION = 2


def _fresh(rec: dict[str, Any]) -> bool:
    """Is this record still valid under the current rules?

    A CONFIRMED location always is: the valuer put that pin there, and
    no amount of rule-tuning puts it somewhere else.
    """
    if not rec:
        return False
    if rec.get("how") == "confirmed":
        return True
    return rec.get("gate", 0) >= _GATE_VERSION


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
    """What we know about a location: confirmed, geocoded, or nothing.

    A record stored under older acceptance rules reads as nothing, so a
    change to those rules cannot leave a stale suggestion on the map.
    """
    if not key:
        return {}
    _load()
    with _lock:
        rec = dict(_cache.get(_clean(key), {}))
    return rec if _fresh(rec) else {}


def remember(key: str, record: dict[str, Any]) -> None:
    _load()
    record = dict(record)
    record.setdefault("gate", _GATE_VERSION)
    if record.get("how") != "confirmed":
        record["gate"] = _GATE_VERSION
    with _lock:
        _cache[_clean(key)] = record
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


#: Words that carry no identifying weight when checking whether a
#: geocoder result is actually the thing we asked for.
_NOISE = {
    "hong", "kong", "china", "district", "territories", "new",
    "the", "and", "for", "road", "street", "lane", "avenue",
    "building", "estate", "tower", "house", "court", "block",
    "phase", "site", "area", "northern", "kowloon", "island",
}


def _norm(s: str) -> str:
    """Lowercase, unaccented, letters and digits only."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s.lower() if c.isalnum())


def _name_tokens(name: str) -> list[str]:
    """The identifying words in an estate name."""
    return [t for t in re.findall(r"[A-Za-z][A-Za-z0-9']{3,}", str(name or ""))
            if t.lower() not in _NOISE]


def _result_is_the_right_place(display_name: str, name: str) -> bool:
    """Does the result actually look like the estate we asked for?

    This gate exists because the public geocoder will happily invent
    an answer. Asking for ``La Aldea, Ping Shan, Hong Kong`` returns
    ``House 2, Ping Kwai Road, Hung Uk Tsuen`` — a different house on a
    different road, and a *different* one for every spelling of the
    query. It is inside Hong Kong, so the bounding box does not catch
    it, and it is type ``house``, so it looks like a plausible result.

    The one reliable signal is that a correct answer names the place we
    asked about. If OSM knows the estate, its name is in the display
    name. If it does not, we get a street address that has nothing to
    do with it — and no pin.
    """
    if not name:
        return True
    tokens = _name_tokens(name)
    if not tokens:
        return True
    dn = _norm(display_name)
    return any(_norm(t) in dn for t in tokens)


def geocode(address: str, district: str = "", name: str = "") -> Optional[tuple[float, float, str]]:
    """Look a place up. ``(lat, lon, label)`` or None.

    Two hard-won facts about the public geocoder, both of which cost
    a wrong-looking empty result before they were found:

    * ``countrycodes=hk`` returns **nothing** for Hong Kong. It is
      accepted by the API and quietly filters every HK result away.
      The bounding-box check below is what actually keeps us in HK.
    * Coverage of individual buildings is patchy, and where a name is
      unknown the geocoder still returns *something* — a house on a
      nearby road. So every result is checked against the name we
      asked for, and a result that does not mention it is discarded.

    Queries are tried in descending confidence: the estate name first,
    then the address with its district, then the address alone.
    """
    queries = _queries(address, district, name)
    for query in queries:
        # EVERY query is held to the name check, not just the first.
        # Holding only the first let the second query route around the
        # gate: "La Aldea, Ping Shan, Hong Kong" was correctly rejected
        # for naming a house on Ping Kwai Road, and then
        # "8 Ping Pak Lane, Ping Shan, Yuen Long, Hong Kong" returned
        # that same wrong house and was accepted unchecked.
        hit = _geocode_one(query, must_contain=name)
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
        # The bare name first. Adding the district narrows the search
        # past the point where the geocoder can see the estate: "La
        # Aldea, Ping Shan, Hong Kong" matches a house on Ping Kwai
        # Road, while "La Aldea, Hong Kong" finds La Aldea itself.
        out.append(f"{nm}, Hong Kong")
        if dist:
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


def _geocode_one(query: str, must_contain: str = "") -> Optional[tuple[float, float, str]]:
    query = str(query or "").strip()
    if not query:
        return None

    _load()
    with _lock:
        cached = _cache.get(_clean(query))
    if cached and cached.get("lat") is not None and _fresh(cached):
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
            import urllib.error as _ue
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
            # Only a throttled or briefly-down instance is worth
            # waiting for. Retrying every error with a backoff turned a
            # rejected request into a 7-second stall, and the test
            # suite into 78 seconds.
            retryable = (
                "429" in str(e) or "503" in str(e)
                or isinstance(e, _ue.URLError)
            )
            if attempt == 2 or not retryable:
                log.info("geocode failed for %r: %s", query, e)
                return None
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
    if not _result_is_the_right_place(label, must_contain):
        # Right city, wrong building. Discarded rather than shown: a
        # pin on the wrong side of Yuen Long is worse than no pin.
        log.info("discarded geocode for %r — result does not name it: %s",
                 must_contain or query, label[:80])
        return None

    with _lock:
        _cache[_clean(query)] = {
            "lat": round(lat, 6), "lon": round(lon, 6),
            "how": "geocoded", "label": label,
            "query": query, "gate": _GATE_VERSION,
            "source": "openstreetmap/nominatim",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    _save()
    return round(lat, 6), round(lon, 6), label


def suggest(key: str, address: str = "", district: str = "", name: str = "") -> Optional[tuple[float, float, str]]:
    """Geocode for a project and file the answer under the PROJECT's key.

    The geocoder caches under the query it ran ("La Aldea, Ping Shan,
    Hong Kong") while the map looks the project up by its address
    ("8 Ping Pak Lane, Ping Shan, Yuen Long"). Storing only under the
    query meant a successful lookup was never found again — the pin
    silently refused to appear. This writes both keys.

    Returns the location, or None when the geocoder has nothing usable.
    """
    got = geocode(address, district, name)
    if not got:
        # Remember the refusal. Otherwise every page view re-asks about
        # a building the public map has never heard of, costing a
        # second each time for an answer that will not change.
        if key:
            remember(key, {"tried": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime()),
                           "source": "openstreetmap/nominatim"})
        return None
    lat, lon, label = got
    if key:
        remember(key, {
            "lat": lat, "lon": lon, "how": "geocoded",
            "label": label, "source": "openstreetmap/nominatim",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    return got


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


def warm(keys: list[tuple[str, str, str, str]], budget: int = 60) -> dict[str, Any]:
    """Fill in locations for a batch of projects, rate-limited.

    Nominatim allows one request a second, so this takes about as many
    seconds as there are projects. It therefore runs inside a request —
    Cloud Run freezes the container once a response has been sent, so
    a thread started from a page view would be cut off part-way and
    place nothing at all.

    ``keys`` is ``[(project_key, address, district, name)]``. Anything
    already on file is skipped, and anything already *confirmed* is
    never touched.
    """
    done, skipped, failed = 0, 0, 0
    for n, (key, address, district, name) in enumerate(keys):
        if n >= budget:
            break
        if coords(key)[0] is not None:
            skipped += 1
            continue
        try:
            got = suggest(key, address, district, name)
        except Exception as e:  # noqa: BLE001
            log.info("warm: %r failed: %s", key, e)
            got = None
        if got:
            done += 1
        else:
            failed += 1
    out = {"placed": done, "already_had_one": skipped, "no_match": failed}
    log.info("warm finished: %s", out)
    return out


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