"""Drive layout tests — path resolution and the tidy-up.

The Drive API is mocked with a small in-memory tree. What is being
tested is the app's *addressing* of folders: it must mean the same
folder every time, and the tidy-up must move only what it is supposed
to move.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="vf-drive-"))
os.environ["VF_DB"] = str(_TMP / "ledger.sqlite3")
os.environ["VF_OUTPUT_DIR"] = str(_TMP / "outputs")

from demo import drive as D  # noqa: E402


# ---------------------------------------------------------------------------
# A tiny fake Drive
# ---------------------------------------------------------------------------

class FakeDrive:
    """Just enough of the Drive API for the folder code.

    Nodes are dicts: {id, name, mimeType, parents: [ids], trashed: bool}
    and children are tracked by scanning parents.
    """

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self._n = 0
        self.updates: list[dict] = []
        # My Drive root, as the API reports it on this account: a real id
        # in `parents`, not an empty list.
        self.root_id = "0ROOT"

    def _new_id(self) -> str:
        self._n += 1
        return f"F{self._n}"

    def add(self, name: str, parent: str | None = None, mime: str = D.FOLDER_MIME) -> str:
        nid = self._new_id()
        self.nodes[nid] = {
            "id": nid, "name": name, "mimeType": mime,
            "parents": [parent if parent is not None else self.root_id],
            "trashed": False,
        }
        return nid

    # -- the API surface the app uses -------------------------------

    def files(self):
        return self

    def list(self, q="", **kwargs):
        out = []
        for n in self.nodes.values():
            if n["trashed"]:
                continue
            if f"mimeType='{D.FOLDER_MIME}'" in q and n["mimeType"] != D.FOLDER_MIME:
                continue
            m = re.search(r"name='([^']*)'", q)
            if m and n["name"] != m.group(1):
                continue
            # The parent clause is the LAST quoted id in the query.
            pm = re.findall(r"'([^']*)' in parents", q)
            if pm and pm[-1] not in n["parents"]:
                continue
            out.append({
                "id": n["id"], "name": n["name"],
                "parents": list(n["parents"]),
            })
        return _FakeResult(out)

    def get(self, fileId=None, **kwargs):
        if fileId == "root":
            return _FakeResult({"id": self.root_id})
        n = self.nodes[fileId]
        return _FakeResult({
            "id": n["id"], "name": n["name"], "parents": list(n["parents"]),
        })

    def create(self, body=None, **kwargs):
        body = body or {}
        nid = self.add(body.get("name", "untitled"),
                       (body.get("parents") or [None])[0],
                       body.get("mimeType", D.FOLDER_MIME))
        return _FakeResult({"id": nid, "name": body.get("name")})

    def update(self, fileId=None, body=None, addParents=None,
               removeParents=None, **kwargs):
        n = self.nodes[fileId]
        if addParents:
            for p in addParents.split(","):
                if p and p not in n["parents"]:
                    n["parents"].append(p)
        if removeParents:
            for p in removeParents.split(","):
                if p in n["parents"]:
                    n["parents"].remove(p)
        if body and body.get("trashed"):
            n["trashed"] = True
        self.updates.append({"id": fileId, "add": addParents, "remove": removeParents})
        return _FakeResult({"id": n["id"], "name": n["name"],
                            "parents": list(n["parents"])})


class _FakeResult:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        if isinstance(self._payload, dict):
            return self._payload
        return {"files": self._payload}


class DriveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDrive()
        self._real_get_drive = D._get_drive
        D._get_drive = lambda: self.fake
        D._folder_ids.clear()
        self._real_path = D.DRIVE_ROOT_PATH
        D.DRIVE_ROOT_PATH = "bma-factory/Valuation"
        D._root_id_cache = None

    def tearDown(self):
        D._get_drive = self._real_get_drive
        D._folder_ids.clear()
        D._root_id_cache = None
        D._cache_loaded = False
        D.DRIVE_ROOT_PATH = self._real_path

    # -- path resolution ---------------------------------------------

    def test_resolve_path_creates_each_segment(self):
        root = D.resolve_path("bma-factory/Valuation")
        self.assertIn(root, self.fake.nodes)
        self.assertEqual(self.fake.nodes[root]["name"], "Valuation")
        parent = self.fake.nodes[root]["parents"][0]
        self.assertEqual(self.fake.nodes[parent]["name"], "bma-factory")

    def test_resolve_path_is_idempotent(self):
        a = D.resolve_path("bma-factory/Valuation")
        D._folder_ids.clear()
        b = D.resolve_path("bma-factory/Valuation")
        self.assertEqual(a, b)

    def test_resolve_path_does_not_find_when_asked_not_to_create(self):
        with self.assertRaises(KeyError):
            D.resolve_path("bma-factory/Valuation", create=False)

    def test_same_name_in_two_places_does_not_collide(self):
        """The whole point of addressing by path."""
        top = self.fake.add("Intake")                 # at Drive root
        other = self.fake.add("Other")
        nested = self.fake.add("Intake", other)       # elsewhere
        D._folder_ids.clear()
        # Root path created fresh, so its own Intake is a third one.
        target = D.app_folder("Intake")
        self.assertNotIn(target, (top, nested))
        self.assertEqual(self.fake.nodes[target]["name"], "Intake")
        # and the root one is still where it was
        self.assertEqual(self.fake.nodes[top]["parents"], [self.fake.root_id])

    def test_app_folder_if_exists_does_not_create(self):
        self.assertIsNone(D.app_folder_if_exists("Intake"))
        self.assertEqual(len(self.fake.nodes), 0)

    # -- the tidy-up -------------------------------------------------

    def _seed_root_level(self):
        return {n: self.fake.add(n) for n in D.APP_FOLDERS}

    def test_organise_moves_the_four_folders(self):
        seeded = self._seed_root_level()
        report = D.organise_drive()
        self.assertEqual(
            sorted(m["folder"] for m in report["moved"]), sorted(D.APP_FOLDERS)
        )
        self.assertEqual(report["errors"], [])
        root_id = report["root_id"]
        for name, fid in seeded.items():
            self.assertEqual(self.fake.nodes[fid]["parents"], [root_id],
                             f"{name} was not moved under the root path")

    def test_organise_is_idempotent(self):
        self._seed_root_level()
        D.organise_drive()
        D._folder_ids.clear()
        second = D.organise_drive()
        self.assertEqual(second["moved"], [])
        self.assertEqual(sorted(second["already_in_place"]), sorted(D.APP_FOLDERS))

    def test_organise_leaves_other_folders_alone(self):
        self._seed_root_level()
        stranger = self.fake.add("Intake")   # a second one, at the root
        report = D.organise_drive()
        # One entry per folder name. Of the two "Intake" folders, exactly
        # one was moved; the other was left where it was.
        moved_intake = [m for m in report["moved"] if m.get("folder") == "Intake"]
        self.assertEqual(len(moved_intake), 1)
        # The second "Intake" was not touched — it stays at the Drive root.
        self.assertNotIn(stranger, [m["id"] for m in moved_intake])
        self.assertEqual(self.fake.nodes[stranger]["parents"], [self.fake.root_id])

    def test_organise_merges_when_one_is_already_in_place(self):
        """A same-named folder at the root and one in place → merge, not
        a second folder with the same name side by side."""
        real = self.fake.add("Intake")
        D._folder_ids.clear()
        root_id = D.resolve_path(D.DRIVE_ROOT_PATH)
        placeholder = self.fake.add("Intake", root_id)
        child = self.fake.add("a-file.xlsx", real, mime="application/vnd.ms-excel")

        report = D.organise_drive()
        self.assertIn("Intake", report["already_in_place"])
        self.assertEqual(len(report["merged"]), 1)
        self.assertEqual(report["merged"][0]["children_moved"], 1)
        # the file moved into the folder that is in place
        self.assertEqual(self.fake.nodes[child]["parents"], [placeholder])
        # and the emptied source was trashed
        self.assertTrue(self.fake.nodes[real]["trashed"])

    def test_organise_reports_a_missing_folder(self):
        self.fake.add("Intake")
        report = D.organise_drive()
        self.assertEqual(sorted(report["missing"]), ["ORO", "Outputs", "Templates"])

    def test_a_folder_at_my_drive_root_is_recognised(self):
        """Drive reports the root as a real id in `parents` on this account,
        not as an empty list. A folder sitting in My Drive must still be
        seen as 'at the root' and be movable."""
        self.assertEqual(self.fake.nodes[self.fake.add("Intake")]["parents"],
                         [self.fake.root_id])
        self.assertTrue(D.is_at_drive_root([self.fake.root_id]))
        self.assertTrue(D.is_at_drive_root([]))
        self.assertFalse(D.is_at_drive_root(["SOMEOtherFOLDER"]))

        report = D.organise_drive()
        self.assertEqual(len(report["moved"]), 1)
        self.assertEqual(report["moved"][0]["folder"], "Intake")
        self.assertEqual(report["errors"], [])

    def test_describe_layout_reports_position(self):
        self._seed_root_level()
        d = D.describe_layout()
        self.assertEqual(d["root_path"], "bma-factory/Valuation")
        self.assertFalse(d["root_exists"])
        for entry in d["folders"]:
            self.assertEqual(entry["status"], "at Drive root")
            self.assertFalse(entry["in_place"])

    def test_describe_layout_reports_in_place(self):
        self._seed_root_level()
        D.organise_drive()
        D._folder_ids.clear()
        d = D.describe_layout()
        self.assertTrue(d["root_exists"])
        for entry in d["folders"]:
            self.assertEqual(entry["status"], "in place")
            self.assertTrue(entry["in_place"])


if __name__ == "__main__":
    unittest.main()
