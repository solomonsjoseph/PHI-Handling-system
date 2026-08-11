"""Deterministic offline replay.

Runs the REAL deterministic layer of the pipeline -- the Sentinel hard-rule
table, the executor transforms, the Presidio/rule scrubber, and the publish
guard -- with no LLM and no Mongo, so the ladder can be executed and graded
before an LLM credential exists. It does not measure Judge or Sentinel's
LLM-driven fallback; every report it produces carries
``"mode": "deterministic_replay"`` (set by the caller, e.g. ``campaign.py``)
so no reader mistakes it for a full-pipeline number.
"""
from __future__ import annotations

import csv
import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phi_core.agents.reasoning import (
    apply_sentinel_hard_rules,
    apply_column_actions_to_dataset,
    PseudonymRegistry,
)
# Private by convention in phi_core; imported here (not promoted) because
# the point of the replay is to exercise the code under test, and
# promoting it to a public name would be an edit to phi_core, which this
# workstream does not make.
from phi_core.agents.reasoning import _redact_metadata_file
from phi_core.publish_guard import scan_all_exports

from .planters import CorpusArtifact


@dataclass
class ReplayResult:
    decisions: list[dict[str, Any]]
    export_paths: dict[str, str]
    guard_report: dict[str, Any]
    file_name_map: dict[str, str]
    llm_dependent_columns: list[dict[str, str]]
    elapsed_s: float


def _zip_names(zip_bytes: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return z.namelist()


def _read_header(path: Path) -> list[str]:
    # utf-8-sig strips a BOM when present and is a no-op otherwise, so
    # decisions are seeded with the SAME clean column names the ground
    # truth uses regardless of the scenario's realism profile. This is
    # deliberately more lenient than `apply_column_actions_to_dataset`'s
    # own plain-"utf-8" open, which does NOT strip a BOM: a hostile-profile
    # export whose first column carries a BOM can therefore still surface
    # a genuine SEC-004-fail-closed-vs-BOM interaction in scoring, because
    # only the executor's read is affected, not this decision-seeding step.
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        try:
            return next(csv.reader(f))
        except StopIteration:
            return []


def replay(artifact: CorpusArtifact, workdir: Path, *,
           unmatched: str = "human_review") -> ReplayResult:
    """Extract, decide, transform, and guard-scan one planted corpus.

    ``unmatched`` controls what happens to a column the 20-regex hard-rule
    table did not cover: ``"human_review"`` (default) keeps the deferral
    so the executor renders ``[HUMAN_REVIEW_PENDING]``; ``"oracle"``
    substitutes the ground-truth ``expected_action`` to isolate executor
    and guard behaviour from hard-rule coverage gaps; ``"drop"`` mirrors
    the SEC-004 fail-closed default.
    """
    if unmatched not in ("human_review", "oracle", "drop"):
        raise ValueError(f"unknown unmatched mode: {unmatched!r}")

    t0 = time.time()
    workdir = Path(workdir)
    src_dir = workdir / "src"
    export_dir = workdir / "exports"
    src_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(artifact.zip_bytes)) as z:
        z.extractall(src_dir)

    file_name_map: dict[str, str] = {}
    decisions: list[dict[str, Any]] = []
    dataset_files: list[str] = []
    dictionary_files: list[str] = []
    for name in sorted(_zip_names(artifact.zip_bytes)):
        if name.startswith("datasets/") and not name.endswith("/"):
            file_name = name.split("/", 1)[1]
            dataset_files.append(file_name)
            file_name_map[file_name] = file_name
            for col in _read_header(src_dir / name):
                decisions.append({"file_id": file_name, "column": col, "action": "human_review"})
        elif name.startswith("dictionary/") and not name.endswith("/"):
            dictionary_files.append(name.split("/", 1)[1])

    decisions, _overrides = apply_sentinel_hard_rules(decisions)

    expected_index: dict[tuple[str, str], str] = {}
    for cell in artifact.ground_truth.get("planted", []):
        expected_index.setdefault((cell.get("file_name", ""), cell.get("column", "")),
                                   cell.get("expected_action", ""))

    llm_dependent_columns: list[dict[str, str]] = []
    resolved: list[dict[str, Any]] = []
    for d in decisions:
        if d.get("action") != "human_review":
            resolved.append(d)
            continue
        llm_dependent_columns.append({"file": d.get("file_id", ""), "column": d.get("column", "")})
        if unmatched == "human_review":
            resolved.append(d)
        elif unmatched == "drop":
            nd = dict(d)
            nd["action"] = "drop"
            resolved.append(nd)
        else:  # "oracle"
            nd = dict(d)
            nd["action"] = expected_index.get((d.get("file_id", ""), d.get("column", "")), "human_review")
            resolved.append(nd)
    decisions = resolved

    registry = PseudonymRegistry(salt="replay")
    decisions_by_file: dict[str, list[dict[str, Any]]] = {}
    for d in decisions:
        decisions_by_file.setdefault(d.get("file_id", ""), []).append(d)

    export_paths: dict[str, str] = {}
    for file_name in dataset_files:
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "csv"
        src = src_dir / "datasets" / file_name
        dst = export_dir / file_name
        apply_column_actions_to_dataset(src, dst, ext, decisions_by_file.get(file_name, []),
                                         registry=registry)
        export_paths[file_name] = str(dst)

    for dict_name in dictionary_files:
        src = src_dir / "dictionary" / dict_name
        dst = export_dir / dict_name
        _redact_metadata_file(src, dst)
        # Dictionary exports are not scored by verify(); intentionally
        # excluded from export_paths, which is planted-cell scoped.

    guard_report = scan_all_exports(export_paths, decisions=decisions, jurisdiction="us")

    return ReplayResult(
        decisions=decisions,
        export_paths=export_paths,
        guard_report=guard_report.to_dict(),
        file_name_map=file_name_map,
        llm_dependent_columns=llm_dependent_columns,
        elapsed_s=time.time() - t0,
    )
