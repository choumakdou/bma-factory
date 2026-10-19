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

From Cloud Shell (or any laptop with `gcloud` authed):

```bash
git clone https://github.com/choumakdou/bma-factory
cd bma-factory
bash deploy.sh
```

The script:
1. Builds the container via Cloud Build
2. Deploys to Cloud Run in `asia-east2`
3. Mounts the `engine-sa-key` secret as `GOOGLE_APPLICATION_CREDENTIALS`
4. Runs as the `service-ac1@bma-factory.iam.gserviceaccount.com` runtime SA
5. Prints the URL

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
