# bma-factory demo

The single-page Bel-Air M2 (ORO 120) case review. Proves the architecture
end-to-end on a real HK office valuation, no auth, one button.

## What the demo shows

- **Case header** — Island Place Tower, 510 King's Road, 13,812 sf NFA,
  adopted value HK$76,000,000, valuation date 15 Sep 2026
- **11 ranked comparables** extracted from `CHFT Map ver 8.0 20260921.xlsm`
  Sheet1, run through the existing `ingest.relevance.rank()` engine
- **QC flags** as inline labels (PRESS, CONNECTED, APPROX, NO_GFA) — the
  valuer never sees a comp hidden by an upstream filter
- **Map placeholder** (the v8.0 contract is wired; the Leaflet view is v0.2)
- **Generate button** — runs the engine to produce the HK Rental Comps + T&R
  workbook, offers it as a download

## What's NOT in the demo (intentional, for v0.2)

- Auth (URL shared by link)
- Property picker (one case, hardcoded)
- Standing rules CRUD (read-only display)
- Diff view
- Modification generalisation
- Other modules (PV / Office / Industrial)
- The Windows EXE wrapper

## Files

```
demo/
├── app.py            # FastAPI app — 4 routes: / /case/bel-air-m2 /generate /download/<filename>
├── case_data.py      # Embedded case data (subject + 11 comps, lifted from CHFT Map Sheet1)
├── templates/
│   └── case_review.html
└── static/
    └── style.css
```

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Redirect to `/case/bel-air-m2` |
| `/case/bel-air-m2` | GET | Render the case review page |
| `/api/case/bel-air-m2` | GET | JSON view (for the map / MCP) |
| `/generate` | POST | Run the engine, return artifact + download URL |
| `/download/<filename>` | GET | Serve the generated workbook |
| `/healthz` | GET | Liveness probe |

## Running locally (laptop, no GCP)

```bash
pip install -r requirements.txt
uvicorn demo.app:app --reload --port 8080
# Open http://localhost:8080/case/bel-air-m2
```

## Deploying to Cloud Run

### Auto-deploy (recommended — no Cloud Shell needed)

The repo has a GitHub Actions workflow at `.github/workflows/deploy.yml`
that runs on every push to `master`. To enable it:

1. **Create a deploy service account** in the GCP project:
   - IAM & Admin → Service Accounts → **Create Service Account**
   - Name: `bma-factory-deploy`
   - Grant these four roles (one at a time, click "+ Add Another Role"):
     - `Cloud Build Editor` (roles/cloudbuild.builds.editor)
     - `Cloud Run Admin` (roles/run.admin)
     - `Service Account User` (roles/iam.serviceAccountUser)
     - `Storage Object Admin` (roles/storage.objectAdmin)
   - Click **Done**
2. **Download the JSON key:**
   - Click the SA you just created → **Keys** tab → **Add Key** →
     **Create new key** → **JSON** → **Create**
   - A `.json` file downloads. **Don't email it, don't paste it in chat.**
3. **Add it as a GitHub Actions secret:**
   - Open https://github.com/choumakdou/bma-factory/settings/secrets/actions
   - Click **New repository secret**
   - **Name:** `GCP_SA_KEY` (exact)
   - **Value:** paste the entire JSON contents
   - Click **Add secret**

That's it. From now on, every push to `master` auto-builds + auto-deploys.
The Actions tab at https://github.com/choumakdou/bma-factory/actions shows
each deploy in real time, and the run URL appears in the workflow's output.

You can also **manually trigger** a deploy without pushing: Actions tab →
"deploy" → **Run workflow** → green button.

### Manual deploy (fallback)

If the workflow is broken or you want to deploy from Cloud Shell:

```bash
git clone https://github.com/choumakdou/bma-factory
cd bma-factory
bash deploy.sh
```

## What the engine does on first boot

- Reads the case data (embedded) and ranks the 11 comps
- On Generate, produces the HK Rental Comps + T&R workbook
- Writes the file to `/tmp/bma-factory-demo/output/` in the container
  (the demo MVP does not push to Drive; v0.2 wires the Apps Script glue)

## v0.2 wiring checklist

When you want the real Drive round-trip:

1. Upload `ORO120 Bel-Air M2 20261006.xlsx` to `/ORO/` in your Drive
2. Confirm the folder is shared with `service-ac1@bma-factory.iam.gserviceaccount.com` (Editor)
3. Replace `demo/case_data.py` with a Drive-aware loader:
   ```python
   def load_case():
       # use the GOOGLE_APPLICATION_CREDENTIALS env var (set by Cloud Run)
       # call drive.files.get(fileId=<case workbook id>)
       # call ingest.oro_ingest.parse_case(workbook, defs)
       return subject, comps
   ```
4. Add the Apps Script glue (4 functions: Memfus pull, RVD refresh, Drive push, file-watcher)
5. Re-deploy with `bash deploy.sh`

That's the path to a real production case, from the demo.
