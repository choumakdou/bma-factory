"""Sales Brochure Review — the quarterly re-examination routine.

Every residential development whose sales brochure is under the
Ordinance (Cap. 621) has to be re-examined and, if necessary, revised
and re-uploaded. Section 17(1) requires the examination to have
happened **within the previous three months**, so the review cycle is
quarterly by law, not by habit.

The routine per development, per round:

  Stage 0   Confirming the project deadline
              last round's actual upload date
              upload deadline          = that date + 3 calendar months
              target upload date       = the preceding Friday to the deadline,
                                         agreed with the client
              actual upload date       = to be confirmed, and it becomes the
                                         next round's last round's date

  Stage 1   Checking which of the three items need updating
              Location Plan   — HK Map Service
              Aerial Photo    — HK Map Service
              OZP             — TPB Outlying Zoning Plan

A round is a **record**, not a live view. Once a round is opened, what
happened in it does not change because a website did. The stages and
items are a registry, so a new stage is a new entry rather than another
branch.

The dates are the part that must not be wrong, so the arithmetic lives
here, in one place, and is tested:

  deadline = add_months(last_actual, 3)
  target   = the last Friday on or before the deadline

7 August 2026 (a Friday) -> deadline 7 November 2026 (a Saturday) ->
target 6 November 2026. The target is a **proposal**: it is agreed with
the client, so the field is editable and the computed value is only the
default.
"""

from __future__ import annotations

import calendar
import io
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

log = logging.getLogger("bma-factory.brochure")

#: Where the reviews live, relative to the app's root path.
FOLDER = "SalesBrochure"
DATA_FILE = "developments.json"

#: How often a review round repeats. Three months, because the
#: Ordinance says so.
CYCLE_MONTHS = 3


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_date(value: Any) -> Optional[date]:
    """A date from a string, a date, or a datetime. None if it is not one."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # The usual forms, most likely first.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def fmt_date(d: Optional[date]) -> str:
    """The display form — day first, the Hong Kong way."""
    if d is None:
        return ""
    return d.strftime("%d/%m/%Y")


def add_months(d: date, months: int) -> date:
    """Calendar months, keeping the day of the month where it exists.

    31 August plus three months is 30 November, not 3 December, and
    certainly not 28 November. The deadline is a real date that a real
    document is filed on.
    """
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def preceding_friday(d: date) -> date:
    """The last Friday on or before this date.

    If the date is itself a Friday, that Friday is the answer — the
    upload can be filed on the day.
    """
    return date.fromordinal(d.toordinal() - ((d.weekday() - 4) % 7))


def days_between(a: date, b: date) -> int:
    return (b - a).days


# ---------------------------------------------------------------------------
# The stages and the items
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """One thing to check inside a stage."""
    id: str
    name: str
    #: Where the valuer looks. Deep-linked per development where the
    #: site allows it.
    source: str
    source_label: str = ""
    #: What the check actually compares.
    compares: str = ""
    #: True when the app can read the source itself.
    auto: bool = False


#: The three items of Stage 1, and where each is checked.
CHECK_ITEMS: list[Item] = [
    Item(
        id="location_plan",
        name="Location Plan",
        source="https://www.hkmapservice.gov.hk/OneStopSystem/map-search",
        source_label="HK Map Service — Map Searching",
        compares="the plan reference and date the brochure carries, against the "
                 "current plan at the Map Service",
    ),
    Item(
        id="aerial_photo",
        name="Aerial Photo",
        source="https://www.hkmapservice.gov.hk/OneStopSystem/map-search",
        source_label="HK Map Service — Map Searching",
        compares="the capture date the brochure carries, against the current "
                 "aerial photograph at the Map Service",
    ),
    Item(
        id="ozp",
        name="OZP",
        source="https://www.ozp.tpb.gov.hk/",
        source_label="TPB — Outlying Zoning Plan",
        compares="the OZP sheet number and revision date the brochure carries, "
                 "against the current sheet at TPB",
    ),
]


@dataclass
class Stage:
    """One step of the routine."""
    number: int
    name: str
    #: "dates" for Stage 0, "items" for Stage 1.
    kind: str
    description: str = ""


STAGES: list[Stage] = [
    Stage(
        number=0,
        name="Confirming Project Deadline",
        kind="dates",
        description="The three-month clock under section 17(1) of the "
                    "Residential Properties (First-hand Sales) Ordinance. "
                    "The sales brochure must have been examined, and if "
                    "necessary revised, within the previous three months.",
    ),
    Stage(
        number=1,
        name="Checking Which Items Need Updating",
        kind="items",
        description="The Location Plan, the Aerial Photo and the OZP are the "
                    "three that change. The Layout Plan is the developer's "
                    "own and does not.",
    ),
]


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------

@dataclass
class Round:
    """One quarterly review of one development."""
    number: int
    opened: str = ""                 # when this round was started
    #: Stage 0
    last_actual: str = ""            # the previous round's actual upload date
    target: str = ""                 # the target upload date (editable)
    actual: str = ""                 # this round's actual upload date
    stage0_note: str = ""
    #: Stage 1 — item id -> state
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Any other stage's free text, by stage number.
    notes: dict[str, str] = field(default_factory=dict)
    #: Set when the round is closed and the actual upload date is known.
    closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "opened": self.opened,
            "last_actual": self.last_actual,
            "target": self.target,
            "actual": self.actual,
            "stage0_note": self.stage0_note,
            "items": self.items,
            "notes": self.notes,
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Round":
        return cls(
            number=int(d.get("number") or 1),
            opened=str(d.get("opened") or ""),
            last_actual=str(d.get("last_actual") or ""),
            target=str(d.get("target") or ""),
            actual=str(d.get("actual") or ""),
            stage0_note=str(d.get("stage0_note") or ""),
            items=dict(d.get("items") or {}),
            notes=dict(d.get("notes") or {}),
            closed=bool(d.get("closed", False)),
        )

    # --- Stage 0, computed -----------------------------------------

    @property
    def last_actual_date(self) -> Optional[date]:
        return parse_date(self.last_actual)

    @property
    def deadline(self) -> Optional[date]:
        """Last round's actual upload date plus three calendar months."""
        base = self.last_actual_date
        return add_months(base, CYCLE_MONTHS) if base else None

    @property
    def default_target(self) -> Optional[date]:
        dl = self.deadline
        return preceding_friday(dl) if dl else None

    @property
    def target_date(self) -> Optional[date]:
        """The target upload date — the agreed one, or the computed default."""
        return parse_date(self.target) or self.default_target

    @property
    def actual_date(self) -> Optional[date]:
        return parse_date(self.actual)

    def days_to_deadline(self, today: Optional[date] = None) -> Optional[int]:
        dl = self.deadline
        if not dl:
            return None
        return days_between(today or date.today(), dl)

    # --- Stage 1, computed -----------------------------------------

    def item_state(self, item: Item) -> dict[str, Any]:
        """The state of one item, with the verdict worked out."""
        raw = self.items.get(item.id) or {}
        brochure = parse_date(raw.get("brochure_version"))
        source = parse_date(raw.get("source_version"))
        if source and brochure:
            verdict = "update" if source > brochure else "ok"
        elif source:
            verdict = "unknown"
        else:
            verdict = "unchecked"

        return {
            "id": item.id,
            "name": item.name,
            "source": item.source,
            "source_label": item.source_label,
            "compares": item.compares,
            "auto": item.auto,
            "brochure_version": raw.get("brochure_version", ""),
            "source_version": raw.get("source_version", ""),
            "checked_at": raw.get("checked_at", ""),
            "checked_by": raw.get("checked_by", ""),
            "note": raw.get("note", ""),
            "verdict": verdict,
            "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        }


VERDICT_LABELS = {
    "unchecked": "Not yet checked",
    "ok": "No update needed",
    "update": "Update needed",
    "unknown": "Checked — the brochure's version is not on file",
}


@dataclass
class Development:
    """A development under review."""
    id: str
    name: str
    #: The SRPE development id, used to build the link into the platform.
    dev_id: str = ""
    address: str = ""
    district: str = ""
    client: str = ""
    business_line: str = ""
    rounds: list[Round] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "dev_id": self.dev_id,
            "address": self.address,
            "district": self.district,
            "client": self.client,
            "business_line": self.business_line,
            "rounds": [r.to_dict() for r in self.rounds],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Development":
        return cls(
            id=str(d.get("id") or new_id()),
            name=str(d.get("name") or "Unnamed development"),
            dev_id=str(d.get("dev_id") or ""),
            address=str(d.get("address") or ""),
            district=str(d.get("district") or ""),
            client=str(d.get("client") or ""),
            business_line=str(d.get("business_line") or ""),
            rounds=[Round.from_dict(r) for r in (d.get("rounds") or [])],
        )

    def round(self, number: int) -> Optional[Round]:
        for r in self.rounds:
            if r.number == number:
                return r
        return None

    @property
    def current_round(self) -> Optional[Round]:
        """The open round, or the last one."""
        if not self.rounds:
            return None
        for r in self.rounds:
            if not r.closed:
                return r
        return self.rounds[-1]

    def next_round_number(self) -> int:
        return max((r.number for r in self.rounds), default=0) + 1

    def open_round(self, today: Optional[date] = None) -> Round:
        """Open the next round, carrying the dates forward.

        The carry-forward is the whole point: the new round's "last
        round's actual upload date" is the round before it, and the new
        round's "actual upload date" starts empty and is to be confirmed.
        """
        previous = self.current_round
        number = self.next_round_number()
        rnd = Round(number=number, opened=(today or date.today()).isoformat())
        if previous is not None:
            carried = previous.actual_date or previous.last_actual_date
            if carried:
                rnd.last_actual = carried.isoformat()
            # What the brochure carries carries forward — that is a
            # fact about the brochure, and it does not change by
            # opening a new round. What the *source* was at the last
            # check does not: that is an observation at a point in
            # time, and the whole point of the new round is to make a
            # fresh one. Carrying it forward would let an old reading
            # pass for a new one.
            for item in CHECK_ITEMS:
                state = previous.items.get(item.id) or {}
                if state:
                    rnd.items[item.id] = {
                        "brochure_version": state.get("brochure_version", ""),
                        "source_version": "",
                        "note": state.get("note", ""),
                        "checked_at": "",
                    }
        self.rounds.append(rnd)
        return rnd


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(name or "")).strip("-").lower()
    return s or new_id()


# ---------------------------------------------------------------------------
# Storage — in the valuer's Drive
# ---------------------------------------------------------------------------

def _folder_path() -> str:
    from demo.drive import DRIVE_ROOT_PATH
    return f"{DRIVE_ROOT_PATH}/{FOLDER}"


def load_developments() -> list[Development]:
    """Every development under review. Empty when there are none."""
    try:
        from demo.drive import app_folder_if_exists
        from demo.oauth import get_drive
        folder_id = app_folder_if_exists(FOLDER)
        if not folder_id:
            return []
        drive = get_drive()
        r = drive.files().list(
            q=f"'{folder_id}' in parents and name='{DATA_FILE}' and trashed=false",
            fields="files(id,modifiedTime)", pageSize=5, supportsAllDrives=True,
        ).execute()
        files = r.get("files", [])
        if not files:
            return []
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, drive.files().get_media(fileId=files[0]["id"]))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = json.loads(buf.getvalue().decode("utf-8"))
        raw = data.get("developments") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        return [Development.from_dict(d) for d in raw if isinstance(d, dict)]
    except Exception as e:  # noqa: BLE001
        log.info("could not load the brochure reviews: %s", e)
        return []


def save_developments(devs: list[Development]) -> tuple[bool, str]:
    """Write the reviews back to Drive. Returns (ok, message)."""
    try:
        from demo.drive import app_folder
        from demo.oauth import get_drive
        from googleapiclient.http import MediaIoBaseUpload
        folder_id = app_folder(FOLDER)
        drive = get_drive()
        payload = json.dumps(
            {"version": 1, "developments": [d.to_dict() for d in devs]},
            indent=2, ensure_ascii=False,
        ).encode("utf-8")
        existing = drive.files().list(
            q=f"'{folder_id}' in parents and name='{DATA_FILE}' and trashed=false",
            fields="files(id)", pageSize=5, supportsAllDrives=True,
        ).execute().get("files", [])
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )
        if existing:
            drive.files().update(
                fileId=existing[0]["id"], media_body=media,
                fields="id,name", supportsAllDrives=True,
            ).execute()
        else:
            drive.files().create(
                body={"name": DATA_FILE, "parents": [folder_id]},
                media_body=media, fields="id,name", supportsAllDrives=True,
            ).execute()
        return True, "saved"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# The view the pages need
# ---------------------------------------------------------------------------

def summarise(dev: Development, today: Optional[date] = None) -> dict[str, Any]:
    """A development and where it stands."""
    today = today or date.today()
    rnd = dev.current_round
    out: dict[str, Any] = {
        "id": dev.id,
        "name": dev.name,
        "dev_id": dev.dev_id,
        "address": dev.address,
        "district": dev.district,
        "client": dev.client,
        "business_line": dev.business_line,
        "rounds": len(dev.rounds),
        "has_rounds": bool(dev.rounds),
    }
    if rnd is None:
        out.update({
            "round_number": None, "deadline": "", "target": "", "last_actual": "",
            "actual": "", "days_to_deadline": None, "closed": False,
            "items": [], "items_done": 0, "items_total": len(CHECK_ITEMS),
            "items_needing_update": 0,
        })
        return out

    items = [rnd.item_state(i) for i in CHECK_ITEMS]
    out.update({
        "round_number": rnd.number,
        "last_actual": fmt_date(rnd.last_actual_date),
        "deadline": fmt_date(rnd.deadline),
        "target": fmt_date(rnd.target_date),
        "actual": fmt_date(rnd.actual_date),
        "days_to_deadline": rnd.days_to_deadline(today),
        "closed": rnd.closed,
        "items": items,
        "items_done": sum(1 for i in items if i["verdict"] != "unchecked"),
        "items_total": len(items),
        "items_needing_update": sum(1 for i in items if i["verdict"] == "update"),
    })
    return out


def by_business_line(devs: list[Development]) -> list[tuple[str, list[Development]]]:
    """The developments grouped by business line, in a stable order."""
    groups: dict[str, list[Development]] = {}
    for d in devs:
        groups.setdefault(d.business_line or "Unassigned", []).append(d)
    out = [(k, sorted(v, key=lambda x: x.name.lower())) for k, v in groups.items()]
    return sorted(out, key=lambda kv: kv[0].lower())
