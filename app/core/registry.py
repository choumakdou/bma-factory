"""Module discovery.

A module is a folder under `modules/` containing a `manifest.yaml` and a
`generator.py`. Adding a project type means adding a folder and committing --
no application code is touched. That is the whole extensibility story.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .schemas import Field, Schema

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULES_DIR = Path(os.environ.get("VF_MODULES_DIR", REPO_ROOT / "modules"))


@dataclass
class Manifest:
    id: str
    name: str
    description: str
    version: str
    fields: list[Field]
    outputs: list[dict[str, str]]
    folder: Path

    def schema(self) -> Schema:
        return Schema(fields=self.fields, name=self.name)

    def output_types(self) -> list[str]:
        return [o["type"] for o in self.outputs]


@dataclass
class LoadedModule:
    manifest: Manifest
    module: Any  # the imported generator module

    def generator(self):
        return self.module

    def validators(self):
        return getattr(self.module, "validate", None)


def _fields_from_raw(raw: list[dict] | None) -> list[Field]:
    fields: list[Field] = []
    for f in raw or []:
        fields.append(
            Field(
                key=f["key"],
                label=f.get("label", f["key"]),
                type=f.get("type", "str"),
                required=bool(f.get("required", True)),
                section=f.get("section", "General"),
                unit=f.get("unit", ""),
                choices=tuple(f.get("choices", ()) or ()),
                minimum=f.get("minimum"),
                maximum=f.get("maximum"),
                help=f.get("help", ""),
            )
        )
    return fields


def _load_one(folder: Path) -> Manifest | None:
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not data.get("id"):
        return None
    return Manifest(
        id=data["id"],
        name=data.get("name", data["id"]),
        description=data.get("description", ""),
        version=str(data.get("version", "0.0.0")),
        fields=_fields_from_raw(data.get("fields")),
        outputs=data.get("outputs", []),
        folder=folder,
    )


def _import_generator(folder: Path, module_id: str):
    path = folder / "generator.py"
    if not path.exists():
        raise ImportError(f"module '{module_id}' has no generator.py")
    folder_str = str(folder)
    if folder_str not in sys.path:
        sys.path.insert(0, folder_str)
    spec = importlib.util.spec_from_file_location(f"vf_module_{module_id}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load generator for '{module_id}'")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=1)
def _registry() -> dict[str, LoadedModule]:
    found: dict[str, LoadedModule] = {}
    if not MODULES_DIR.exists():
        return found
    for folder in sorted(p for p in MODULES_DIR.iterdir() if p.is_dir()):
        manifest = _load_one(folder)
        if manifest is None:
            continue
        found[manifest.id] = LoadedModule(manifest=manifest, module=_import_generator(folder, manifest.id))
    return found


def list_modules() -> list[Manifest]:
    return [m.manifest for m in _registry().values()]


def get_module(module_id: str) -> LoadedModule:
    reg = _registry()
    if module_id not in reg:
        raise KeyError(module_id)
    return reg[module_id]


def module_ids() -> list[str]:
    return list(_registry().keys())