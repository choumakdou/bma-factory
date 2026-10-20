"""OAuth2 flow for the bma-factory demo.

The engine uses the valuer's own Google credentials (not a service
account) so generated workbooks can land in the valuer's personal
Google Drive. This is the same pattern as DocuSign, HelloSign, Dropbox,
etc. — server acts on behalf of the user.

Flow:
  1. User visits /auth/login
  2. We redirect to Google's consent screen
  3. User grants Drive permission
  4. Google redirects to /auth/callback?code=...
  5. We exchange the code for access+refresh tokens
  6. We store the refresh token on disk (one file per user)
  7. Every Drive call uses these credentials

For the demo, single-user (Bobby), single token file. v0.2 swaps to
Secret Manager for the token and adds a users table.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as gbuild

log = logging.getLogger("bma-factory.oauth")

# Drive access scoped to files the app creates — not full Drive read.
# openid + userinfo.email lets us display "Signed in as X" on the home page.
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

CLIENT_ID     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
# The redirect URI is set at deploy time so it matches the live URL exactly.
# Default below is the current live URL; override via env if the URL changes.
REDIRECT_URI  = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "https://bma-factory-demo-bxbgttiecq-df.a.run.app/auth/callback",
)

# Token storage.
#
# The container filesystem is EPHEMERAL — Cloud Run scales to zero, and every
# cold start wipes /tmp. A refresh token in /tmp means re-signing-in every
# time the console sleeps, which is unusable for a daily work console.
#
# So the token lives in Secret Manager when the runtime service account can
# reach it (VF_TOKEN_SECRET + GOOGLE_APPLICATION_CREDENTIALS), with the local
# file as a fallback for the demo.
TOKEN_DIR  = Path("/tmp/bma-factory-demo/tokens")
TOKEN_FILE = TOKEN_DIR / "user.json"

# Name of the Secret Manager secret holding the token JSON.
SECRET_NAME = os.environ.get("VF_TOKEN_SECRET", "")
PROJECT_ID  = os.environ.get("GOOGLE_CLOUD_PROJECT", "bma-factory")
_SECRET_API = "https://secretmanager.googleapis.com/v1/projects"


def is_configured() -> bool:
    """True if the OAuth client ID + secret are set in the env."""
    return bool(CLIENT_ID) and bool(CLIENT_SECRET)


def _sa_access_token() -> Optional[str]:
    """Access token for the app's own service account, or None."""
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return None
    try:
        import google.auth
        import google.auth.transport.requests as tr
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if not creds.valid:
            creds.refresh(tr.Request())
        return creds.token
    except Exception as e:  # noqa: BLE001
        log.warning("service-account auth unavailable: %s", e)
        return None


def _secret_read() -> Optional[str]:
    """Read the token JSON from Secret Manager."""
    if not SECRET_NAME:
        return None
    token = _sa_access_token()
    if not token:
        return None
    import base64
    import urllib.request
    url = f"{_SECRET_API}/{PROJECT_ID}/secrets/{SECRET_NAME}/versions/latest:access"
    req = urllib.request.Request(
        url, method="POST", data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            import json as _json
            payload = _json.loads(r.read())
        return base64.b64decode(payload["payload"]["data"]).decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info("no token in Secret Manager yet (secret %s)", SECRET_NAME)
        else:
            log.warning("Secret Manager read failed: %s %s", e.code, e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Secret Manager read failed: %s", e)
        return None


def _secret_ensure() -> bool:
    """Create the secret if it does not exist yet. True if it exists after."""
    if not SECRET_NAME:
        return False
    token = _sa_access_token()
    if not token:
        return False
    import urllib.request
    url = f"{_SECRET_API}/{PROJECT_ID}/secrets/{SECRET_NAME}"
    # Does it already exist?
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False
    # Create it.
    body = json.dumps({"replication": {"automatic": {}}}).encode("utf-8")
    req = urllib.request.Request(
        url, method="POST", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        log.info("created Secret Manager secret %s", SECRET_NAME)
        return True
    except urllib.error.HTTPError as e:
        log.warning("could not create secret %s: %s %s", SECRET_NAME, e.code, e.reason)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("could not create secret %s: %s", SECRET_NAME, e)
        return False


def _secret_write(blob: str) -> bool:
    """Write the token JSON to Secret Manager as a new version."""
    if not SECRET_NAME:
        return False
    token = _sa_access_token()
    if not token:
        return False
    if not _secret_ensure():
        return False
    import base64
    import json as _json
    import urllib.request
    url = f"{_SECRET_API}/{PROJECT_ID}/secrets/{SECRET_NAME}:addVersion"
    body = _json.dumps({
        "payload": {"data": base64.b64encode(blob.encode("utf-8")).decode("ascii")}
    }).encode("utf-8")
    req = urllib.request.Request(
        url, method="POST", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        log.info("stored OAuth token in Secret Manager secret %s", SECRET_NAME)
        return True
    except urllib.error.HTTPError as e:
        log.warning("Secret Manager write failed: %s %s", e.code, e.reason)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("Secret Manager write failed: %s", e)
        return False


def secret_store_status() -> dict:
    """Diagnostic: what can the app do with Secret Manager right now?"""
    out: dict = {
        "secret_name": SECRET_NAME or None,
        "project": PROJECT_ID,
        "sa_credentials_present": bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")),
    }
    tok = _sa_access_token()
    out["sa_access_token"] = bool(tok)
    if not tok or not SECRET_NAME:
        out["ok"] = False
        out["reason"] = "no service-account identity or no secret name configured"
        return out
    import urllib.request
    url = f"{_SECRET_API}/{PROJECT_ID}/secrets/{SECRET_NAME}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        out["secret_exists"] = True
        out["can_read_secret_meta"] = True
    except urllib.error.HTTPError as e:
        out["secret_exists"] = e.code != 404
        out["can_read_secret_meta"] = e.code != 403
        if e.code == 403:
            out["reason"] = "no permission on Secret Manager"
    except Exception as e:  # noqa: BLE001
        out["reason"] = str(e)
    out["can_write"] = _secret_write(json.dumps({"probe": True}))
    out["ok"] = bool(out.get("can_write"))
    return out


def get_flow(code_verifier: Optional[str] = None) -> "Flow":  # type: ignore
    """Build the OAuth flow.

    ``code_verifier`` must be the PKCE verifier that was used for the
    authorization request, otherwise the token exchange fails with
    ``invalid_grant``. google_auth_oauthlib auto-generates one per Flow
    instance, so the value has to be carried across the redirect.
    """
    from google_auth_oauthlib.flow import Flow
    if not is_configured():
        raise RuntimeError(
            "OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID and "
            "GOOGLE_OAUTH_CLIENT_SECRET in the container env."
        )
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    if code_verifier:
        flow.code_verifier = code_verifier
    return flow


# ---------------------------------------------------------------------------
# Signed cookie carrying the PKCE verifier + state between /auth/login and
# /auth/callback. Stdlib only — no extra dependency.
# ---------------------------------------------------------------------------

_COOKIE_NAME = "bma_oauth_ctx"
_COOKIE_TTL = 600  # 10 minutes is plenty for a consent screen


def _sign(blob: bytes) -> str:
    import hmac
    import hashlib
    key = (CLIENT_SECRET or CLIENT_ID or "bma-factory").encode("utf-8")
    sig = hmac.new(key, blob, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(sig).decode("ascii")


def make_auth_context(code_verifier: str, state: str) -> str:
    """Build the signed, URL-safe context value for the cookie."""
    raw = json.dumps({"v": code_verifier, "s": state}).encode("utf-8")
    return f"{base64.urlsafe_b64encode(raw).decode('ascii')}.{_sign(raw)}"


def read_auth_context(value: str) -> Optional[dict]:
    """Verify and decode the context cookie. None if tampered or expired."""
    import hmac
    import time
    try:
        b64, sig = value.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        if not hmac.compare_digest(sig, _sign(raw)):
            log.warning("OAuth context cookie failed signature check")
            return None
        data = json.loads(raw)
        if not data.get("ts"):
            data["ts"] = time.time()
        if time.time() - float(data["ts"]) > _COOKIE_TTL:
            log.warning("OAuth context cookie expired")
            return None
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("could not read OAuth context cookie: %s", e)
        return None


def save_credentials(creds: Credentials) -> None:
    """Persist the credentials to Secret Manager (preferred) or /tmp."""
    blob = creds.to_json()
    if _secret_write(blob):
        # Keep a local copy too — it lets the running instance use the token
        # immediately without a round trip, and survives a Secret Manager blip.
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(blob)
        TOKEN_FILE.chmod(0o600)
        return
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(blob)
    TOKEN_FILE.chmod(0o600)
    log.info("saved OAuth credentials locally (Secret Manager unavailable)")


def load_credentials() -> Optional[Credentials]:
    """Load the stored credentials, refreshing if expired."""
    blob = _secret_read()
    if blob is None:
        if not TOKEN_FILE.exists():
            return None
        blob = TOKEN_FILE.read_text()
    try:
        import json as _json
        creds = Credentials.from_authorized_user_info(_json.loads(blob), SCOPES)
    except Exception as e:
        log.warning("failed to load stored credentials: %s", e)
        return None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
        except Exception as e:
            log.warning("failed to refresh credentials: %s", e)
            return None
    return creds


def clear_credentials() -> None:
    """Sign out — drop the local copy and destroy the Secret Manager version."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    if SECRET_NAME:
        token = _sa_access_token()
        if token:
            import urllib.request
            url = (f"{_SECRET_API}/{PROJECT_ID}/secrets/{SECRET_NAME}"
                   ":destroySecretVersion")
            req = urllib.request.Request(
                url, method="POST", data=b"{}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
            except Exception as e:  # noqa: BLE001
                log.warning("could not destroy secret version: %s", e)


def get_drive():
    """Drive client using Bobby's OAuth credentials. None if not signed in."""
    creds = load_credentials()
    if creds is None:
        raise RuntimeError(
            "Not signed in. Visit /auth/login to sign in with Google."
        )
    return gbuild("drive", "v3", credentials=creds, cache_discovery=False)


def get_user_info() -> Optional[dict]:
    """Return the signed-in user's basic profile, or None if not signed in."""
    creds = load_credentials()
    if creds is None:
        return None
    try:
        info = gbuild("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
        return {"email": info.get("email"), "name": info.get("name"), "picture": info.get("picture")}
    except Exception as e:
        log.warning("failed to fetch userinfo: %s", e)
        return None
