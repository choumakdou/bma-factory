"""The sources a sales brochure review is checked against.

Each adapter does two things:

  - builds the **link** the valuer follows, deep-linked to the
    development where the site allows it, so a check is one click
    rather than a hunt; and
  - tries to **read** the source itself, and says plainly when it
    cannot.

The reading is deliberately kept behind a small interface. Where a site
publishes a machine-readable version, the adapter reads it. Where it
does not — a session-gated application, or a portal that needs a key —
the adapter returns a reason and the valuer confirms the value in the
app. The review still works; it just carries the human step.

The probe is cached per container, so a page view costs one request,
not one request per source.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Optional

log = logging.getLogger("bma-factory.sources")

#: How long a source read may take. Long enough for a government
#: website, short enough not to hold a page.
TIMEOUT = 12

#: A context that will not refuse a government site's certificate
#: chain in a sandbox that intercepts TLS.
_SSL = ssl.create_default_context()


class UnverifiedContextFactory:
    """A fallback for sandboxes that intercept TLS."""

    @staticmethod
    def create() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def _fetch(url: str, data: Optional[bytes] = None,
           headers: Optional[dict[str, str]] = None) -> tuple[int, str]:
    """A GET or POST that returns (status, text). Never raises."""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "*/*",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as r:
            raw = r.read()
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as e:  # noqa: BLE001
        log.info("could not read %s: %s", url, e)
        return 0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# SRPE — the platform the sales brochures are filed on
# ---------------------------------------------------------------------------

SRPE_BASE = "https://www.srpe.gov.hk"
SRPE_API = f"{SRPE_BASE}/api/SrpeWebService"

#: The development's own page on the platform. This is the link the
#: valuer follows, and it needs no searching.
def srpe_development_link(dev_id: str) -> str:
    if not dev_id:
        return f"{SRPE_BASE}/opip/"
    return f"{SRPE_BASE}/opip/selected_dev?devId={dev_id}"


def srpe_newly_uploaded_link() -> str:
    return f"{SRPE_BASE}/opip/selected_dev_newly_upload"


#: The API answers on this family — verified against
#: ``/Common/checkPermission``, which returns ``{"code":0}`` — so the
#: path shape is right. The method that carries the development's
#: upload history has not been found; the platform is a single-page
#: application behind a disclaimer, and the method names are elided in
#: its published bundle.
_SRPE_METHOD_CANDIDATES = [
    "/Common/getSelectedDev",
    "/Common/getSelectedDevelopment",
    "/Common/getDevUploadDate",
    "/SelectedDev/getSelectedDev",
    "/Dev/getSelectedDev",
]

#: The result of the last probe, so a page view does not re-probe.
_srpe_probe: Optional[tuple[bool, str]] = None


def srpe_probe() -> tuple[bool, str]:
    """Try to read the platform. Returns (worked, message).

    Cached for the life of the container. When it works, the app can
    fill the upload date in; when it does not, the app says so and
    points at the development's page instead of failing silently.
    """
    global _srpe_probe
    if _srpe_probe is not None:
        return _srpe_probe
    for method in _SRPE_METHOD_CANDIDATES:
        body = json.dumps({"language": "en", "devId": 0}).encode()
        status, text = _fetch(
            f"{SRPE_API}{method}", data=body,
            headers={"Content-Type": "application/json"},
        )
        if status == 200 and '"code"' in text:
            _srpe_probe = (True, method)
            return _srpe_probe
    _srpe_probe = (False, "The platform's data service could not be read.")
    return _srpe_probe


def srpe_upload_dates(dev_id: str) -> dict[str, Any]:
    """The upload dates the platform holds for a development.

    Returns ``{"ok": True, "dates": [...]}`` when the platform could be
    read, or ``{"ok": False, "reason": ..., "link": ...}`` when it
    could not — which is the normal case at the moment, so the reason
    is written for a human rather than a stack trace.
    """
    worked, message = srpe_probe()
    if not worked:
        return {
            "ok": False,
            "reason": (
                "The SRPE platform is a single-page application behind a "
                "disclaimer, and its data service cannot be read without "
                "a browser session. Open the development below and confirm "
                "the date — everything else on this page is then worked "
                "out from it."
            ),
            "link": srpe_development_link(dev_id),
            "detail": message,
        }
    body = json.dumps({"language": "en", "devId": dev_id}).encode()
    status, text = _fetch(
        f"{SRPE_API}/Common/getSelectedDev", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "The platform's reply could not be read.",
                "link": srpe_development_link(dev_id)}
    return {"ok": True, "raw": payload, "link": srpe_development_link(dev_id)}


# ---------------------------------------------------------------------------
# TPB — the Outlying Zoning Plan
# ---------------------------------------------------------------------------

OZP_BASE = "https://www.ozp.tpb.gov.hk/"


def ozp_link(sheet: str = "") -> str:
    if sheet:
        return f"{OZP_BASE}#/{sheet}"
    return OZP_BASE


def ozp_probe() -> tuple[bool, str]:
    """Can the OZP's list of sheets be read?

    The viewer is a single-page application with a JSON service under
    ``/api/{locale}/``. The service was not reachable from outside, so
    the check falls back to the valuer's eye — and the app says that
    rather than pretending.
    """
    for path in ("/api/en/list.json", "/api/en/index.json", "/api/list.json"):
        status, text = _fetch(f"{OZP_BASE.rstrip('/')}{path}",
                               headers={"Accept": "application/json",
                                        "Referer": OZP_BASE})
        if status == 200 and text.strip().startswith(("{", "[")):
            return True, path
    return False, "The OZP sheet list could not be read."


# ---------------------------------------------------------------------------
# HK Map Service — the Location Plan and the Aerial Photo
# ---------------------------------------------------------------------------

HKMS_BASE = "https://www.hkmapservice.gov.hk"
HKMS_SEARCH = f"{HKMS_BASE}/OneStopSystem/map-search"


def hkms_link(address: str = "") -> str:
    """The map search, deep-linked to the address where it can be."""
    if not address:
        return HKMS_SEARCH
    from urllib.parse import quote
    return f"{HKMS_SEARCH}?address={quote(address)}"


def hkms_probe() -> tuple[bool, str]:
    """Can the Map Service be read without a key?

    Its map services sit behind an API key, and the search screen is
    driven from the browser, so the read is not available to the app.
    The link is still worth building — the valuer is going there
    anyway.
    """
    status, text = _fetch(HKMS_SEARCH, headers={"Accept": "text/html"})
    if status == 200 and "HKMS" in text:
        return True, "the search screen is reachable"
    return False, "The Map Service could not be read."


# ---------------------------------------------------------------------------
# One place to ask about all three
# ---------------------------------------------------------------------------

def check_all(dev: Any) -> dict[str, Any]:
    """What each source can and cannot do right now."""
    dev_id = getattr(dev, "dev_id", "") or ""
    address = getattr(dev, "address", "") or ""
    ozp_ok, ozp_detail = ozp_probe()
    hkms_ok, hkms_detail = hkms_probe()
    return {
        "srpe": {
            "ok": False,
            "label": "SRPE — Sales of First-hand Residential Properties Electronic Platform",
            "link": srpe_development_link(dev_id),
            "note": "The platform holds the actual upload date for the "
                    "development's sales brochure.",
        },
        "hkms": {
            "ok": hkms_ok,
            "label": "HK Map Service — Map Searching",
            "link": hkms_link(address),
            "note": "Checked for the Location Plan and the Aerial Photo.",
            "detail": hkms_detail,
        },
        "ozp": {
            "ok": ozp_ok,
            "label": "TPB — Outlying Zoning Plan",
            "link": ozp_link(),
            "note": "Checked for the OZP sheet and its revision date.",
            "detail": ozp_detail,
        },
    }
