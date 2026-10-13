# CHFT Map ver 8.0 — what the extract revealed

Reviewed `CHFT Map ver 8.0 20260921.xlsm` on 2026-10-13, running the relevancy
scorer against the workbook's own data. Four findings, three of which are
defects the map cannot currently see.

## Architecture (confirmed from the VBA)

Two-step macro, with a human review gate between:

1. **Prepare Addresses** — reads `Sheet1`, writes unique locations to
   `PPA_Map_<timestamp>`, marks the last row `S` by default
2. *human reviews the yellow **Geocode Address** column*
3. **Generate Map** — builds the `locations` array, writes `PPA_Map_HHmmss.html`,
   opens it

- `Sheet1` = selected comparables (12 rows)
- `Sheet2` = the transaction pool (194 rows)
- Both carry the **Memfus Wong column set**, 27 columns A..AA
- `PPA_MapAddresses` = the review sheet (No / Building / Geocode Address /
  Transactions / Consideration / txn blob / source rows)
- Geocoding runs client-side: **Nominatim**, falling back to **Photon**
- Tiles: Esri World Topo / Street / Imagery. Leaflet 1.9.4
- The JS `locations` object is `{no, name, address, count, priceRange, srcRows,
  isSubject, txnText}` — the generated JSON already matches what
  `ingest/relevance.py` emits, so no change to the tool is needed.

## 1. 🔴 The pool in `Sheet2` does not match the subject

| | |
|---|---|
| Subject | Island Place Tower, 510 King's Road, **HNP** — 13,812 sf, Grade A office |
| `Sheet2` | **194 rows, 100% KMK** (Mong Kok), 302–3,378 sf, all `COM` |
| `Sheet1` | 10 × HNP + 2 × HCB — correctly matched |

`Sheet2` is a leftover from a different job. Run against it, the scorer picks
five Mong Kok street shops, averages to HK$26,383/sqft, and reports:

> **HK$364,407,415** for the subject

Against the correctly-matched evidence in `Sheet1` the same code returns
**HK$146m** — roughly 2.5× lower. Both are reproducible; only one is defensible.

**This is the single most important thing the QC gate does:** a district filter
plus a relevancy floor turns "here are five comps and a confident number" into
"there is no usable evidence in this extract". The extract you paste in
determines the answer, and nothing downstream can rescue a wrong pool.

## 2. 🔴 Four of the eleven selected comparables are not evidence

The map currently plots all of them.

| Row | Building | Problem |
|---|---|---|
| 7, 8 | ISLAND PLACE TOWER | `REMARK2 = "Connected Transaction"` — related party, not arm's length |
| 9, 10 | ISLAND PLACE TOWER | `NAT_INST = "news (unverified)"`, `REMARK2 = "信報"` / "transacted price not sure" |
| 12 | 9 KESWICK ST | `REMARK2 = "AREA APPROX ONLY"` |
| 5 | CAUSEWAY BAY COM BLDG | `TOTGFA = 0` — basement; `GROSS_PSF` unusable |

A connected transaction is not evidence of market value without adjustment or
exclusion. Two of these four are at the subject's own building, so they are
plotted very close to the subject pin and read as strong local evidence.

## 3. ⚠️ `CONSIDERTN` is in millions

`3.58` means HK$3,580,000. Reading it as dollars understates every unit rate by a
factor of a million. Verified: `CONSIDERTN × 1e6 / TOTNFA` reproduces the
workbook's own `NET_PSF` exactly (9,650 / 10,968 / 11,388 / 13,801).

## 4. ⚠️ Even the correct pool is weak evidence for this subject

`Sheet1`'s comps are 200–1,310 sf against a **13,812 sf** subject — 1–9% of its
size. `area_similarity` scores 0.01–0.06 on all of them. The HK$146m figure is
in a plausible range by luck rather than by comparability.

For a large Grade A office the needed evidence is large-floor-plate North Point
office transactions. Street retail is not a substitute, whatever the unit rate
suggests. The adopted set should carry a stated reason, and the score breakdown
is what shows that reason is missing.

## The gates now in place

- `CONSIDERTN` millions conversion, with a check against the workbook's own `NET_PSF`
- press-sourced price → **error**
- connected transaction → **error**
- `TOTNFA = 0` → **error** (no unit rate possible)
- `TOTGFA = 0` → **info** (basement; compare on net only)
- district filter, area band, distance — every exclusion recorded with its reason
- **relevancy floor** — below it, the answer is "no evidence", not a rate
