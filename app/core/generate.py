"""The one generation path.

UI, REST and MCP all call `generate()`. If you ever find yourself writing a
second implementation for a different front door, that is the bug -- two code
paths drift, and the determinism guarantee dies quietly.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ledger, registry
from .schemas import Severity, validate


class GenerationRefused(Exception):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


@dataclass
class Artifact:
    filename: str
    path: Path
    size: int
    sha256: str
    type: str


@dataclass
class GenerationResult:
    run_id: str
    module_id: str
    template_version: str
    input_hash: str
    artifacts: list[Artifact]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "module": self.module_id,
            "template_version": self.template_version,
            "input_hash": self.input_hash,
            "warnings": self.warnings,
            "files": [
                {
                    "filename": a.filename,
                    "type": a.type,
                    "size": a.size,
                    "sha256": a.sha256,
                }
                for a in self.artifacts
            ],
        }


def _output_dir() -> Path:
    out = Path("data/outputs")
    out.mkdir(parents=True, exist_ok=True)
    return out


def generate(
    module_id: str,
    payload: dict[str, Any],
    caller: str = "unknown",
    out_dir: Path | None = None,
) -> GenerationResult:
    try:
        loaded = registry.get_module(module_id)
    except KeyError:
        raise GenerationRefused([f"unknown module '{module_id}'"]) from None

    manifest = loaded.manifest
    schema = manifest.schema()

    # 1. typed validation -- the gate that makes inputs deterministic
    vr = validate(schema, payload)
    if not vr.ok:
        raise GenerationRefused([str(i) for i in vr.errors])
    warnings = [str(i) for i in vr.warnings]

    # 2. module business rules (house style, cross-field checks)
    mod_validators = loaded.validators()
    if mod_validators is not None:
        for issue in mod_validators(vr.values):
            warnings.append(str(issue))
            if getattr(issue, "severity", None) is Severity.ERROR:
                raise GenerationRefused(warnings)

    # 3. hash the inputs *including* the template version, so a template change
    #    shows up as a different input hash even when the data is identical
    ihash = ledger.input_hash(module_id, manifest.version, vr.values)

    run_id = uuid.uuid4().hex[:16]
    dest = out_dir or (_output_dir() / run_id)
    dest.mkdir(parents=True, exist_ok=True)

    # 4. generate
    produced = loaded.generator().generate(vr.values, dest)
    if not produced:
        shutil.rmtree(dest, ignore_errors=True)
        raise GenerationRefused(["generator produced no files"])

    # 5. hash the outputs
    artifacts: list[Artifact] = []
    for item in produced:
        path = Path(item["path"])
        if not path.exists():
            raise GenerationRefused([f"generator reported {path.name} but it does not exist"])
        artifacts.append(
            Artifact(
                filename=path.name,
                path=path,
                size=path.stat().st_size,
                sha256=ledger.file_hash(path),
                type=item.get("type", path.suffix.lstrip(".")),
            )
        )

    # 6. record
    combined = "".join(a.sha256 for a in artifacts)
    ledger.record(
        run_id=run_id,
        module_id=module_id,
        template_version=manifest.version,
        ihash=ihash,
        ohash=combined,
        filename=";".join(a.filename for a in artifacts),
        caller=caller,
    )

    return GenerationResult(
        run_id=run_id,
        module_id=module_id,
        template_version=manifest.version,
        input_hash=ihash,
        artifacts=artifacts,
        warnings=warnings,
    )