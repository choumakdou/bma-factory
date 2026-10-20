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
import time
import urllib.error
import urllib.parse
import urllib.request
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

# Cloud Storage fallback for the token. The runtime service account already
# has project-level Storage Object Admin, so this needs no extra grant —
# unlike Secret Manager, which needs an admin role we do not have.
TOKEN_BUCKET = os.environ.get("VF_TOKEN_BUCKET", "") or os.environ.get(
    "GOOGLE_CLOUD_PROJECT", "bma-factory") + "_cloudbuild"
TOKEN_OBJECT = os.environ.get("VF_TOKEN_OBJECT", "bma-factory/app/oauth-token.json")
PROBE_OBJECT = TOKEN_OBJECT.replace(".json", ".probe.json")


def is_configured() -> bool:
    """True if the OAuth client ID + secret are set in the env."""
    return bool(CLIENT_ID) and bool(CLIENT_SECRET)


_SA_LAST_ERROR: Optional[str] = None
_SA_TOKEN_CACHE: tuple[Optional[str], float] = (None, 0.0)


def _gcs_write(blob: str) -> bool:
    """Write the token JSON to a private Cloud Storage object."""
    if not TOKEN_BUCKET:
        return False
    token = _sa_access_token()
    if not token:
        return False
    import urllib.parse
    import urllib.request
    obj = urllib.parse.quote(TOKEN_OBJECT, safe="")
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{TOKEN_BUCKET}"
           f"/o?uploadType=media&name={obj}")
    req = urllib.request.Request(
        url, method="POST", data=blob.encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log.info("stored OAuth token in gs://%s/%s", TOKEN_BUCKET, TOKEN_OBJECT)
        return True
    except urllib.error.HTTPError as e:
        log.warning("GCS write failed: %s %s", e.code, e.reason)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("GCS write failed: %s", e)
        return False


def _gcs_probe() -> bool:
    """Round-trip a probe through GCS without touching the real token object."""
    if not TOKEN_BUCKET:
        return False
    ok = _gcs_write_to(json.dumps({"probe": True}), PROBE_OBJECT)
    if not ok:
        return False
    try:
        _gcs_delete(PROBE_OBJECT)
    except Exception:  # noqa: BLE001
        pass
    return True


def _gcs_write_to(blob: str, obj_name: str) -> bool:
    if not TOKEN_BUCKET:
        return False
    token = _sa_access_token()
    if not token:
        return False
    import urllib.parse
    import urllib.request
    obj = urllib.parse.quote(obj_name, safe="")
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{TOKEN_BUCKET}"
           f"/o?uploadType=media&name={obj}")
    req = urllib.request.Request(
        url, method="POST", data=blob.encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        log.warning("GCS write failed: %s %s", e.code, e.reason)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("GCS write failed: %s", e)
        return False


def _gcs_delete(obj_name: str) -> bool:
    token = _sa_access_token()
    if not token or not TOKEN_BUCKET:
        return False
    import urllib.parse
    import urllib.request
    obj = urllib.parse.quote(obj_name, safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{TOKEN_BUCKET}/o/{obj}"
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("GCS delete failed: %s", e)
        return False


def _gcs_read() -> Optional[str]:
    """Read the token JSON from Cloud Storage."""
    if not TOKEN_BUCKET:
        return None
    token = _sa_access_token()
    if not token:
        return None
    import urllib.parse
    import urllib.request
    obj = urllib.parse.quote(TOKEN_OBJECT, safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{TOKEN_BUCKET}/o/{obj}?alt=media"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.warning("GCS read failed: %s %s", e.code, e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("GCS read failed: %s", e)
        return None


def _sa_credentials():
    """Build service-account credentials, handling BOTH forms of the env var.

    Cloud Run mounts a secret and injects its CONTENT into the env var when
    the var is declared as a secret reference, and a FILE PATH when it is
    declared as a normal env var. The raw-JSON form starts with ``{``.

    Returns ``(access_token, expires_at_epoch)``. We do the JWT exchange
    ourselves rather than going through ``google.auth`` because that
    library retries a failed refresh for up to two minutes, which used to
    push a cold start past Cloud Run's four-minute startup probe.
    """
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not set")
    if raw.lstrip().startswith("{"):
        info = json.loads(raw)
    else:
        info = json.loads(Path(raw).read_text())

    import urllib.parse
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(raw_bytes: bytes) -> str:
        return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": info["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": info.get("token_uri", "https://oauth2.googleapis.com/token"),
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        b64(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(
        info["private_key"].encode(), password=None
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    assertion = signing_input + "." + b64(signature)

    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode("utf-8")
    req = urllib.request.Request(
        info.get("token_uri", "https://oauth2.googleapis.com/token"),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read())
    return payload["access_token"], time.time() + int(payload.get("expires_in", 3600))


def _sa_access_token() -> Optional[str]:
    """Access token for the app's own service account, or None."""
    global _SA_LAST_ERROR, _SA_TOKEN_CACHE
    _SA_LAST_ERROR = None
    # Reuse the token until shortly before it expires — a service-account
    # token is valid for an hour and minting one costs a network round trip.
    now = time.time()
    cached, expiry = _SA_TOKEN_CACHE
    if cached and now < expiry - 60:
        return cached
    try:
        token, expires_at = _sa_credentials()
        _SA_TOKEN_CACHE = (token, expires_at)
        return token
    except Exception as e:  # noqa: BLE001
        _SA_LAST_ERROR = f"{type(e).__name__}: {str(e)[:300]}"
        log.warning("service-account auth unavailable: %s", _SA_LAST_ERROR)
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
    # Create it. If it already exists this returns 409, which is fine.
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
        if e.code == 409:
            return True  # already there
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
        if e.code == 404:
            # Secret does not exist yet — create it, then retry once.
            if _secret_ensure():
                return _secret_write(blob)
            return False
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
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path:
        p = Path(cred_path)
        raw = cred_path.lstrip()
        out["sa_credentials_form"] = "raw-json" if raw.startswith("{") else "file-path"
        if raw.startswith("{"):
            try:
                info = json.loads(cred_path)
                out["sa_client_email"] = info.get("client_email")
                out["sa_key_id"] = info.get("private_key_id")
            except Exception as e:  # noqa: BLE001
                out["sa_credentials_parse_error"] = str(e)[:120]
        else:
            out["sa_credentials_file_exists"] = p.exists()
    tok = _sa_access_token()
    out["sa_access_token"] = bool(tok)
    out["sa_error"] = _SA_LAST_ERROR
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
        out["secret_exists"] = None  # 403 hides existence; 404 means absent
        out["can_read_secret_meta"] = False
        out["secret_meta_http"] = e.code
        if e.code == 404:
            out["secret_exists"] = False
        else:
            out["reason"] = "no permission on Secret Manager"
    except Exception as e:  # noqa: BLE001
        out["reason"] = str(e)[:200]
    # Probe read (403 = no access, 404 = no version yet).
    try:
        vurl = f"{_SECRET_API}/{PROJECT_ID}/secrets/{SECRET_NAME}/versions/latest:access"
        vreq = urllib.request.Request(
            vurl, method="POST", data=b"{}",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(vreq, timeout=15) as r:
            r.read()
        out["can_read_version"] = True
    except urllib.error.HTTPError as e:
        out["can_read_version"] = False
        out["read_version_http"] = e.code
    except Exception as e:  # noqa: BLE001
        out["can_read_version"] = False
        out["read_version_error"] = str(e)[:120]
    out["can_write"] = _secret_write(json.dumps({"probe": True}))
    # Cloud Storage store — the one that actually works with the roles we have.
    out["gcs_bucket"] = TOKEN_BUCKET or None
    out["gcs_object"] = TOKEN_OBJECT if TOKEN_BUCKET else None
    if TOKEN_BUCKET:
        out["gcs_can_write"] = _gcs_probe()
        out["gcs_can_read"] = _gcs_read() is not None
    out["ok"] = bool(out.get("gcs_can_write") or out.get("can_write"))
    out["durable_store"] = (
        "gcs" if out.get("gcs_can_write")
        else "secretmanager" if out.get("can_write")
        else None
    )
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
    """Persist the credentials durably: GCS, then Secret Manager, then /tmp."""
    blob = creds.to_json()
    # Always keep a local copy — it lets this instance use the token straight
    # away without a round trip, and survives a remote-store blip.
    try:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(blob)
        TOKEN_FILE.chmod(0o600)
    except Exception as e:  # noqa: BLE001
        log.warning("could not write local token copy: %s", e)

    if _gcs_write(blob):
        return
    if _secret_write(blob):
        return
    log.warning("saved OAuth credentials locally only — no durable store available")


def load_credentials() -> Optional[Credentials]:
    """Load the stored credentials from any durable store, refreshing if expired."""
    blob = _gcs_read()
    if blob is None:
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
    """Sign out — drop the local copy and delete the durable copies."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    if TOKEN_BUCKET:
        token = _sa_access_token()
        if token:
            import urllib.parse
            import urllib.request
            obj = urllib.parse.quote(TOKEN_OBJECT, safe="")
            url = f"https://storage.googleapis.com/storage/v1/b/{TOKEN_BUCKET}/o/{obj}"
            req = urllib.request.Request(
                url, method="DELETE",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
            except Exception as e:  # noqa: BLE001
                log.warning("could not delete GCS token object: %s", e)
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
