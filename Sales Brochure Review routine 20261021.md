# Sales Brochure Review — the routine

**Why it is quarterly.** Section 17(1) of the Residential Properties
(First-hand Sales) Ordinance (Cap. 621) requires the sales brochure of a
development to have been examined, and if necessary revised, **within the
previous three months**. The review cycle is set by law, not by habit.

The three items that change are the **Location Plan**, the **Aerial Photo**
and the **OZP**. The Layout Plan is the developer's own and does not
change, so it is not part of the check.

---

## The routine, per development, per round

### Stage 0 — Confirming the project deadline

| Field | Where it comes from |
|---|---|
| Last round's Actual Upload Date | The platform's record of the previous upload. Confirmed once per round; everything else is then worked out from it. |
| Upload Deadline | **Calculated** — the last round's actual upload date plus three calendar months. |
| Target Upload Date | **The preceding Friday to the Upload Deadline.** A proposal, not a rule — it is agreed with the client, so it can be changed. |
| Actual Upload Date | To be confirmed. When it is filled in, it becomes the next round's last round's actual upload date. |

**Worked example — La Aldea (SRPE devId 7825)**

```
Last round's actual upload    07/08/2026  (a Friday)
Upload deadline               07/11/2026  =  +3 calendar months  (a Saturday)
Target upload date            06/11/2026  =  the preceding Friday
Actual upload date            to be confirmed
```

When a month is short, the deadline falls on its last day: 31 August plus
three months is **30 November**, not 3 December.

If the deadline itself is a Friday, that Friday is the target — the upload
can be filed on the day.

---

### Stage 1 — Checking which of the three items need updating

An item is checked by **recording what was found**, not by typing a date
into a box. Each piece of evidence carries three things — the reference,
its title, and its date — and the screenshot that proves it.

The **latest** piece of evidence is what the brochure has to reflect.

#### OZP — TPB, Outlying Zoning Plan

Open the sheet. Its own page carries the version date:

> *Gazetted under Section 9(1)(a) on DD/MM/YYYY*

**Check every sheet that covers the site.** A development often sits on
more than one. The Tong Yan San Tsuen / Ping Shan / Hung Shui Kiu area
sits on three:

| Sheet | Title | Gazetted |
|---|---|---|
| `S/YL-TYST/14` | Tong Yan San Tsuen | 20/08/2021 |
| `S/HSK/4` | Hung Shui Kiu and Ha Tsuen | **09/10/2026** |
| `S/YL-PS/22` | Ping Shan | 22/05/2026 |

A brochure carrying 20/08/2021 is **out of date** here, because
`S/HSK/4` was gazetted on 09/10/2026. The latest of the three governs —
not the first one found, and not the oldest.

#### Location Plan — HK Map Service, Map Searching

Search the site, then use the **By Date** dropdown in the result list.
The top entry is the current plan. Record the **Sheet No. / Title** and
its **Date**.

A site can have more than one plan sheet — e.g. `TG-NW-A` and `16-NW-B`,
both dated 27/09/2026. Record each one.

#### Aerial Photo — HK Map Service

On the map at the site, click the **flight line** — the straight line
across the photograph. The pop-up gives the **Flight Date** and the
flying height (6,000 for the 09/06/2025 run).

One flight covers the site, so one date is enough.

---

## The result of a check

| Verdict | Meaning |
|---|---|
| **No update needed** | the source has not moved on |
| **Update needed** | the source is newer than what the brochure carries |
| **Not yet checked** | nothing has been compared yet |

The app never says "no update needed" when it has not actually looked,
because that is the one answer that must not come out of a check.

When the brochure is re-issued with the new version, one press records
that the brochure now carries it, and the item returns to "no update
needed".

---

## The links

| | |
|---|---|
| The development on the platform | `https://www.srpe.gov.hk/opip/selected_dev?devId=<devId>` |
| Location Plan and Aerial Photo | `https://www.hkmapservice.gov.hk/OneStopSystem/map-search` |
| OZP | `https://www.ozp.tpb.gov.hk/` |

The **SRPE development id** is the `devId` in the platform's own link. With
it, the review opens on the right page every time.

---

## What carries forward, and what does not

When a round is opened:

| Carries forward | Does not |
|---|---|
| The last round's actual upload date | The verdict |
| The version each brochure carries | The source version |
| The evidence itself, and its screenshots | |

The reason for the second one: the source version is an **observation at a
point in time**. Carrying it forward would let last quarter's reading pass
for this quarter's, which is exactly the mistake a quarterly check exists
to prevent.

The evidence does carry forward, because it is a record of what was found
and it stays auditable — but the new round has to look again.

---

## Where the record lives

`bma-factory/Valuation/SalesBrochure/developments.json` in the Drive, with
the app as the editor. The screenshots are in
`bma-factory/Valuation/SalesBrochure/<development>/Evidence/`.

The reviews travel with the files, and a round is a record — what happened
in it does not change because a website did.

The application is at **BMA Factory → Sales Brochure Review**.
