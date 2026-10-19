# bma-factory

Deterministic HK valuation document generation. One engine, three doors (UI / REST / MCP), per-case data, no chat-tier lottery in the output.

## What's in the repo

- `app/` — FastAPI service, schemas, registry, ledger (idempotency), auth, generate
- `ingest/` — ORO MailMerge parser (88-field registry), Memfus Wong schema, relevancy ranking, standing rules, label-anchored extraction
- `modules/hk-rental-comps/` — first module: HK Rental Comps + Term & Reversion workbook
- `demo/` — single-page demo of the Bel-Air M2 (ORO 120) case
- `tests/` — 88 tests covering core, ingest, ranking, rules, memfus
- `Dockerfile` + `deploy.sh` — Cloud Run deploy

## Quick start

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests   # 88 tests, should be 0.5s
uvicorn demo.app:app --port 8080         # → http://localhost:8080/case/bel-air-m2
```

## Deploy to Cloud Run

```bash
bash deploy.sh
```

## Status

**Demo MVP** — the Bel-Air M2 case is wired end-to-end. The page renders the subject, ranks 11 comps via `ingest.relevance.rank()`, runs the engine to produce the HK Rental Comps + T&R workbook on Generate, and offers the file as a download.

**Production** (v0.2) is one wiring step away: replace the embedded `demo/case_data.py` with a Drive-aware loader that calls `ingest.oro_ingest.parse_case` on the user's `/ORO/` workbook. The engine, ranking, modules and UI are unchanged.

See `demo/README.md` for the full v0.2 wiring checklist.

## Conventions

- File deliverables use `<name>_<YYYYMMDD><letter>.<ext>`, spaces not underscores
- Same-day versioning: `20261006a.xlsx`, `20261006b.xlsx`, then `20261007a.xlsx`
- The engine is deterministic — same input → byte-identical output, every time
- Templates become build artifacts; never hand-edit a generated file, change the generator
- The valuer confirms, the AI assembles, the schema guarantees
