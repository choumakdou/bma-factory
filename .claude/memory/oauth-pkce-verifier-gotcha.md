---
name: oauth-pkce-verifier-gotcha
description: google_auth_oauthlib auto-generates a PKCE code_verifier per Flow instance — building a fresh Flow in the OAuth callback causes invalid_grant. Carries the verifier in a signed cookie.
metadata:
  type: reference
---

# OAuth PKCE: the callback must reuse the login Flow's code_verifier

**Symptom:** user grants consent, Google redirects back, and the token
exchange fails with `InvalidGrantError`. The app then bounces to the home
page still asking to sign in. Two *different* auth codes, both rejected —
so it is not code reuse.

**Cause:** `google_auth_oauthlib.flow.Flow` auto-generates a PKCE
`code_verifier` per instance (`autogenerate_code_verifier=True` by
default). `authorization_url()` sends the matching `code_challenge`;
`fetch_token()` must send the same `code_verifier`. Building a **second**
Flow in the callback handler gives a fresh, mismatched verifier → Google
rejects the exchange with `invalid_grant`.

**Fix (implemented in `demo/oauth.py` + `demo/app.py`):**
- `get_flow(code_verifier=None)` accepts and restores the verifier
- `make_auth_context(verifier, state)` / `read_auth_context(value)` carry
  `{v: verifier, s: state, ts: now}` in an HMAC-signed cookie — stdlib
  `hmac`/`hashlib`/`base64`/`json` only, 10-minute TTL
- `/auth/login` sets the cookie (`httponly`, `samesite=lax`, `secure`);
  `/auth/callback` verifies the signature, checks `state` (CSRF) and
  restores the verifier

**Why a cookie and not server-side state:** Cloud Run may route the
callback to a different instance than the one that served `/auth/login`.
The cookie travels with the browser, so it works regardless.

**Alternative (not used):** `Flow.from_client_config(..., autogenerate_code_verifier=False)`
disables PKCE — simpler, but PKCE protects the code even with a client
secret, and the cookie approach also buys CSRF protection for free.

**Verify locally without touching Google:** build a flow, call
`authorization_url()`, round-trip the context cookie, then assert
`get_flow(code_verifier=ctx["v"]).code_verifier == flow.code_verifier`
and that a *fresh* flow's verifier differs. See the test pattern in the
commit "Fix OAuth invalid_grant".

**Also note:** `google.auth`, `google_auth_oauthlib` and
`google-api-python-client` are NOT installed in the agent sandbox by
default — `pip install --break-system-packages --index-url
https://pypi.org/simple` works (the default index is unreachable, which
looks like "no matching distribution" rather than a network error).
