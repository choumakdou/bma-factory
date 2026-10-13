# Annual refresh pipeline — design

Replaces the yearly rebuild. Feed two files, get updated workbooks.

```
[paid source transactions .xlsx]  ─┐
[RVD time trend CSVs]              ─┼──▶ ingest ──▶ align ──▶ recompute ──▶ new workbook
[last year's workbook]             ─┘                 │                      + change report
                                                      └── QC gate
```

## The three inputs, and what each is for

| Input | Stability | Role |
|---|---|---|
| Paid-source transactions pull | **High** — fixed export format | Refreshes the comps set |
| RVD time trend CSVs | **High** — published on a fixed cadence | Recomputes every time adjustment |
| Last year's workbook | Varies — this is where drift lives | Carries the property forward |

The last one is an **input, not just an output**. That is the whole "incorporate
my existing properties" requirement: the prior year's adopted comps, subject
data and conclusion are read back in, refreshed, and re-emitted — with the
difference preserved so the change is explainable.

## What is automated vs what is not

**Automated** — the arithmetic and the refresh:
- ingest and align the new data pull
- recompute every time adjustment from the RVD indices
- re-run the adjustment ladder and unit rates
- carry forward subject data that has not changed
- surface what changed and what no longer has evidence

**Not automated** — the judgement:
- which comparables to use, and which outliers to strip
- the adopted rate
- anything where the two approaches disagree

That split is deliberate. Automating the second list would produce a
confidently wrong workbook, which is worse than a slow one.

## Surviving column drift

Every workbook follows the same backbone — subject section, data pull, comps,
adjustments, adopted conclusion — and individual files drift by **insertion and
annotation**, not by restructuring.

So nothing matches by position. `ingest/anchor.py` matches by **label**, and
resolves the value *relative to the match*:

```
find "Adopted Unit Rate"  →  whichever cell carries that text
value                     →  the cell to its right
```

Insert a column anywhere and the anchor does not move, because text does not
move the way an index does. Verified in `tests/test_anchor.py`:

| Drift | Result |
|---|---|
| Column inserted above the block | values identical |
| Empty column inserted between label and value | values identical, shift **reported** |
| Rows reordered | values identical |
| Label reworded ("Rent-free period" → "Rent Free Months") | values identical |
| Extra remark column between fields | values identical |
| Required field genuinely removed | reported as `missing`, not silently absent |

A shift is recorded in `extraction.shifted` rather than absorbed. A silently
wrong read is the one outcome worth engineering against.

## Annotations are data

Cell comments are captured, not discarded. In a valuation workbook they are
where the judgement lives — why a comparable was excluded, when a rent was
verified, what a landlord confirmed. They exist in no other system, and a
parser that reads numbers and throws comments away has captured the arithmetic
and lost the reasoning.

Each annotation is stored against the case, with the value it annotates and its
author.

## Stable identity across years

A property is keyed `lot_number + floor + flat`. Re-valuing the same flat
updates one row and increments `case_count` — which is what makes year-on-year
movement answerable rather than a manual diff.

**Worth checking:** do your institutional clients (China Life, Chevalier, Guoco,
Yuexiu…) assign their own asset reference numbers? If they do, that is a
better key than lot + floor + flat, because it survives a re-numbering or a
redevelopment.

## Not yet built

- Comps-table extraction end to end (`find_header_row` / `column_map` exist and
  are tested, but the row reader is not wired)
- The change report — what moved since last year and why
- The recompute step (time adjustment, ladder, unit rate) reading from the
  database rather than a template
- Geocoding onto `properties.lat` / `lng` (RVD coordinates are the source)
- The map

## One thing to watch

The shop template derives its time adjustment with:

```
OFFSET(RVD!$A$1, MATCH($A2, RVD!$R:$R, 1)-1, 19)
```

`OFFSET` is 1-based from column A, so offset 19 lands on **column T**. If T is
not the index column, this returns a plausible number with no error. Since this
step is about to be automated across every client file, it is worth confirming
— a `SUMIFS` against a date column has no offset arithmetic to get wrong.
