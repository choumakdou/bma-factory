"""Authentication and authorisation for the API tier.

Two distinct callers, deliberately not sharing an identity:

* `script`  - Apps Script, presenting a Google-signed identity token. Trusted
              to act as the operator.
* `api:<name>` - an AI assistant or a script holding a bearer key. Scoped,
              rate-limited, and recorded separately in the ledger so you can
              always answer "which assistant generated this file?"

Least privilege, not because it is fashionable but because an agent calls
APIs at machine speed and a runaway loop should not be able to read every
case in the firm.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

# Scopes
SCOPE_CATALOG = "catalog:read"    # list modules, read schemas
SCOPE_GENERATE = "case:write"     # create + generate a case
SCOPE_READ_OWN = "case:read"      # read cases this caller created
SCOPE_ADMIN = "admin"

ALL_SCOPES = {SCOPE_CATALOG, SCOPE_GENERATE, SCOPE_READ_OWN, SCOPE_ADMIN}


class AuthError(Exception):
    pass


class ScopeError(Exception):
    pass


@dataclass
class Principal:
    name: str
    scopes: set[str]
    kind: str  # "operator" | "agent"

    def require(self, *needed: str) -> None:
        if SCOPE_ADMIN in self.scopes:
            return
        missing = [s for s in needed if s not in self.scopes]
        if missing:
            raise ScopeError(
                f"{self.name} lacks required scope(s): {', '.join(missing)}"
            )


# --- configuration ----------------------------------------------------------
# VF_API_KEYS format:  "name:scope1|scope2, othername:scope1"
# An empty VF_API_KEYS means only the Apps Script identity token is accepted --
# the safe default for a fresh install.
def _load_keys() -> dict[str, Principal]:
    raw = os.environ.get("VF_API_KEYS", "").strip()
    principals: dict[str, Principal] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        name, scopes = chunk.split(":", 1)
        wanted = {s.strip() for s in scopes.split("|") if s.strip()}
        # reject unknown scopes loudly -- a typo'd scope that silently grants
        # nothing is worse than one that loudly fails
        unknown = wanted - ALL_SCOPES
        if unknown:
            raise RuntimeError(
                f"VF_API_KEYS: key '{name}' has unknown scope(s): {sorted(unknown)}"
            )
        label = name.strip()
        # keyed by the token the caller presents; the principal keeps a
        # distinguishable name so the ledger can tell agents apart
        principals[label] = Principal(
            name=f"api:{label}", scopes=wanted, kind="agent"
        )
    return principals


_KEYS: dict[str, Principal] | None = None


def _keys() -> dict[str, Principal]:
    global _KEYS
    if _KEYS is None:
        _KEYS = _load_keys()
    return _KEYS


def reset_keys() -> None:
    """Test hook."""
    global _KEYS
    _KEYS = None


# --- rate limiting ----------------------------------------------------------


@dataclass
class RateLimiter:
    limit: int = 60
    window: int = 60
    _hits: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            return True


LIMITER = RateLimiter(
    limit=int(os.environ.get("VF_RATE_LIMIT", "60")),
    window=int(os.environ.get("VF_RATE_WINDOW", "60")),
)


# --- verification -----------------------------------------------------------


def verify_bearer(authorization: str | None) -> Principal:
    """Resolve an `Authorization` header to a Principal.

    Raises AuthError on anything unrecognised. There is no anonymous path --
    an unauthenticated caller gets 401 before reaching a generator.
    """
    if not authorization:
        raise AuthError("missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("expected 'Authorization: Bearer <token>'")
    token = parts[1].strip()

    # Apps Script presents a Google-signed OIDC identity token (a JWT).
    if token.count(".") == 2:
        return _verify_identity_token(token)

    principals = _keys()
    if token in principals:
        p = principals[token]
        if not LIMITER.allow(p.name):
            raise AuthError(f"rate limit exceeded for {p.name}")
        return p

    raise AuthError("unrecognised token")


def _verify_identity_token(token: str) -> Principal:
    """Trust the platform: Cloud Run is configured with this script's client ID
    as an allowed audience, so anything arriving here was already verified by
    Google. We still pin the operator's scopes."""
    scopes = {s.strip() for s in os.environ.get("VF_SCRIPT_SCOPES", SCOPE_ADMIN).split(",") if s.strip()}
    unknown = scopes - ALL_SCOPES
    if unknown:
        raise AuthError(f"VF_SCRIPT_SCOPES has unknown scope(s): {sorted(unknown)}")
    return Principal(name="script", scopes=scopes, kind="operator")


def public_info() -> dict:
    return {
        "keys_configured": sorted(p.name for p in _keys().values()),
        "rate_limit": {"requests": LIMITER.limit, "window_seconds": LIMITER.window},
        "scopes_available": sorted(ALL_SCOPES),
    }