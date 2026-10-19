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

# Token storage — one file per user, keyed by email hash in v0.2.
# For single-user demo, a single file is fine.
TOKEN_DIR  = Path("/tmp/bma-factory-demo/tokens")
TOKEN_FILE = TOKEN_DIR / "user.json"


def is_configured() -> bool:
    """True if the OAuth client ID + secret are set in the env."""
    return bool(CLIENT_ID) and bool(CLIENT_SECRET)


def get_flow() -> "Flow":  # type: ignore
    from google_auth_oauthlib.flow import Flow
    if not is_configured():
        raise RuntimeError(
            "OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID and "
            "GOOGLE_OAUTH_CLIENT_SECRET in the container env."
        )
    return Flow.from_client_config(
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


def save_credentials(creds: Credentials) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    # Lock the file down — the refresh token is sensitive
    TOKEN_FILE.chmod(0o600)
    log.info("saved OAuth credentials for %s", creds.client_id)


def load_credentials() -> Optional[Credentials]:
    """Load the stored credentials, refreshing if expired."""
    if not TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
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
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


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
