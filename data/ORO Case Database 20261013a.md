# ORO Case Database — schema and sample

Built 2026-10-13. Source of truth: the `MailMerge` registry in the master workbook
(`ORO Template 20260803b.xlsx`), which is a **declared mapping table** — every one of
its 88 rows names the sheet and cell holding that value. Ingestion therefore
resolves an existing contract rather than guessing at a layout.

## Entity model

Property is the primary entity; a case is a valuation of a property. Valuing the
same flat twice updates one `properties` row and increments `case_count` — which is
what makes value history possible.

```
properties 1──< cases 1──< case_owners
                     ├──< case_encumbrances
                     ├──< case_raw          (all 88 fields, untouched)
                     └──< ingest_problems   (the QC gate result)
```

| Table | Holds | Key |
|---|---|---|
| `properties` | The asset — address, lot, block, floor/flat, district, type, SA, HOS values | `lot_number + floor + flat` |
| `cases` | The valuation — ORO ref, CHFT ref, dates, bankrupt, valuer, fee | `oro_file_ref` |
| `case_owners` | Registered owners + capacity | case + ordinal |
| `case_encumbrances` | Memorial no, date, nature, parties, consideration | case + ordinal |
| `case_raw` | Every resolved field verbatim | case + field_name |
| `ingest_problems` | What the validator rejected, and why | case |

`lat` / `lng` are on `properties` and currently NULL — RVD coordinates are the
source to fill them, with manual entry for rural and village addresses.

## What comes next for the 400

Nothing new. Cases that follow the master's sheet structure parse with no code
change. Cases that do not are visible as a low `resolved` count and a red
`ingest_problems` row — not silently wrong.

## Sample record — ORO 116

| | |
|---|---|
| ORO ref | B10/5411/2008-MSMC/PW |
| CHFT ref | V/SCH/303860/2026 |
| Valuer | AHO |
| Fee | HK$850 |
| Property | Flat 17, 9/F, Tsui Wo House (Block 5), Tai Wo Estate, Tai Po |
| Chinese | 大埔太和路12號太和村翠和樓(第5座)9樓17室 |
| Lot | Tai Po Town Lot No. 176 (60/414,930 shares) |
| Type | Tenants Purchase Scheme (TPS) under Housing Authority |
| SA | 485 sq ft |
| HOS initial MV | 1,129,400 |
| Completed | 1989 |
| Lease | 50 years from 10 February 2000 |
| Owners | Wong Sing Kay 王成基 + Lam Fung Kwan 林鳳群 (Joint Tenants) |
| Encumbrance | TP657978 — Assignment (with plan), 2001-02-01, from HKHA |
| DMC | TP627775 — 2000-04-05 |

## QC gate fired on 2 of 88 fields

- 🔴 `GFA_SqFt` — resolved to prose ("Rating and Valuation Department, HKSAR
  (Assessment No. 695-7…") where a number is required. The registry points at a
  source-description cell. **This is the class of error that looks fine on screen.**
- 🟡 `Valuation_Date` — empty in the source workbook.

The 3 unresolved fields (`HKIS_Standards_Cited`, `RICS_Standards_Cited`, `IVS_Cited`)
are blank in the master by design, not a parse failure.
