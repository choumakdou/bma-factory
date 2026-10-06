# Valuation Forge

Deterministic Word / Excel / PowerPoint generation for valuation reporting.

Same input → same bytes, every time. Not "a good answer" — a file you can hash.

---

## Why this exists

A chat assistant will give you a slightly different report each time you ask.
That is fine for drafting and fatal for a valuation file. This project replaces
generation with a **function call**: typed input + fixed template + rules →
a file whose SHA-256 you can record.

Three things make it hold:

1. **Typed inputs.** `HK$1,250`, `1250` and `1.25k` are coerced to the same
   number before anything else runs. A field that cannot be coerced is a hard
   error, never a silent zero.
2. **Pinned metadata.** Word/Excel/PowerPoint embed created/modified timestamps.
   They are pinned so two runs are byte-identical, not merely equivalent.
3. **A ledger.** Every run records `input_hash → output_hash` plus the template
   version, so "did this change, and because of what?" is answerable.

---

## Layout

```
app/core/
  schemas.py    typed fields + coercion + validation
  registry.py   module discovery (drop a folder in, get a module)
  ledger.py     determinism ledger, idempotency, metadata pinning
  auth.py       scoped bearer auth, rate limiting
  generate.py   THE generation path — UI, REST and MCP all come through here
app/api/main.py HTTP surface
modules/
  hk-rental-comps/     manifest.json + generator.py
tests/                 standard-library unittest, no dependencies
```

Adding a project type = add `modules/<name>/` with a `manifest.json` and a
`generator.py` exposing `generate(values, out_dir)` and optionally
`validate(values)`. No application code changes.

---

## Writing a module

```python
# modules/my-thing/generator.py
def validate(values):        # optional; returns list[Issue]
    return []

def generate(values, out_dir):
    # values are already coerced and typed
    ...
    return [{"path": str(out_dir / "Report.docx"), "type": "docx"}]
```

The manifest declares fields, types and sections; the UI and the API schema are
both derived from it, so they can never disagree.

---

## Running locally

```bash
python3 -m unittest discover -s tests -v
python3 -m app.dev            # generates a sample workbook into data/outputs
```

No third-party dependency beyond the Office libraries. Manifests are JSON
specifically so the core has no YAML dependency.

---

## Deploying to Cloud Run

```bash
gcloud run deploy valuation-forge \
  --source . \
  --region asia-east2 \
  --no-allow-unauthenticated \
  --set-env-vars VF_API_KEYS="claude:catalog:read|case:write,me:catalog:read|case:write|case:read" \
  --set-env-vars VF_SCRIPT_SCOPES="catalog:read,case:write,case:read,admin" \
  --set-env-vars VF_RATE_LIMIT=60
```

Free tier: 180,000 vCPU-seconds / 360,000 GiB-seconds / 2M requests per month,
scaling to zero. Set a budget alert.

---

## Authentication

Two caller kinds, deliberately not sharing an identity:

| Caller | Credential | Scope |
|---|---|---|
| Apps Script | Google-signed identity token | `VF_SCRIPT_SCOPES` (default: all) |
| AI assistant / script | static bearer key from `VF_API_KEYS` | per key |

`VF_API_KEYS` format: `name:scope1|scope2, other:scope1`

```
VF_API_KEYS="claude:catalog:read|case:write"
```

Scopes: `catalog:read`, `case:write`, `case:read`, `admin`.

**With `VF_API_KEYS` unset, the only accepted caller is the Apps Script identity
token.** That is the safe default: a fresh install has exactly one way in.

Notes:
- An unknown scope in config fails at startup rather than silently granting
  nothing.
- `case:read` lets an agent see only runs it created itself.
- An agent's job ends at *proposing* values. The typed schema and validators do
  the guaranteeing — never let a model choose the numbers that get stamped onto
  a report.

---

## Calling it from Apps Script

```javascript
function generateReport(payload) {
  const idToken = ScriptApp.getIdentityToken();
  const res = UrlFetchApp.fetch('https://<service>.run.app/v1/generate/hk-rental-comps', {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + idToken,
      'Idempotency-Key': Utilities.getUuid(),
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  return JSON.parse(res.getContentText());
}
```

Cloud Run must have the script's client ID configured as an allowed audience.
Use `X-Serverless-Authorization` if the app needs the `Authorization` header
for its own checks.

`Idempotency-Key` matters: an agent that times out will retry, and a retry must
not create a second case.

---

## API

| Method | Path | Scope |
|---|---|---|
| GET | `/healthz` | none |
| GET | `/v1/modules` | `catalog:read` |
| GET | `/v1/modules/{id}/schema` | `catalog:read` |
| POST | `/v1/generate/{id}` | `case:write` |
| GET | `/v1/runs` | `case:read` |
| GET | `/v1/runs/{run_id}` | `case:read` |
| GET | `/v1/runs/{run_id}/verify` | `case:read` |
| GET | `/v1/runs/{run_id}/files/{name}` | `case:read` |

`/verify` re-hashes what is on disk against the ledger — useful before sending
a file out.

---

## Adding the AI tier

The REST API is the universal path. MCP (the standard integration surface for AI
assistants) is a thin wrapper over the same `generate()` call — expose
`generate`, `list_modules`, `get_schema` and `get_run` as tools and nothing else.

ChatGPT custom MCP connectors require an admin-enabled Business/Enterprise/Edu
workspace; other assistants accept a bearer key against the REST API directly.

---

## Not done yet

- DOCX and PPTX generators (the module interface supports them; only the XLSX
  module is built)
- Drive write-back (the API returns files; pushing into a Drive folder is a
  short adapter on top of the same service account)
- The browser UI (schema-derived forms; the API is the contract)