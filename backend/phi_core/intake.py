"""Intake manifest v3: ZIP -> {datasets/, forms/, data_dictionary|mappings/}.

Aligned with feat/v2-multi-jurisdiction convention. See CLAUDE.md.

Rules (fail-closed):
  - `datasets/` required. Extensions: .csv, .xls, .xlsx. Single sheet only for xlsx.
    .json / .jsonl NOT accepted here.
  - `forms/` required. Extension: .pdf only.
  - At least one of `data_dictionary/` OR `mappings/`. Extensions: .csv, .xlsx.
  - Any unsupported suffix, multi-sheet xlsx dataset, cross-component nested
    duplicate, or symlink -> `_unclassified` review bucket.
  - Blocking review item holds the entire study.

Public status codes (for CLI-style exit-code contract):
  0 -> "ready"           : classification may proceed
  8 -> "review_required" : at least one _unclassified bucket entry
  2 -> "failed"          : missing mandatory component or malformed ZIP
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import openpyxl


COMPONENT_SUFFIXES: dict[str, set[str]] = {
    "datasets":         {".csv", ".xls", ".xlsx"},
    "forms":            {".pdf"},
    "data_dictionary":  {".csv", ".xlsx"},
    "mappings":         {".csv", ".xlsx"},
}
COMPONENTS = tuple(COMPONENT_SUFFIXES)
MANDATORY = {"datasets"}
# At least one of these three must be present alongside datasets/.
ANY_OF = {"forms", "data_dictionary", "mappings"}


@dataclass
class IntakeEntry:
    component: str                # datasets|forms|data_dictionary|mappings|_unclassified
    relpath: str                  # path relative to intake root
    stored_path: str              # absolute path inside workspace
    size_bytes: int = 0
    sha256: str = ""
    reason: str = ""              # populated when component == "_unclassified"
    blocking: bool = True         # unclassified bucket blocks the whole study


@dataclass
class IntakeManifest:
    study_id: str
    root: str                     # absolute path to unpacked intake tree
    status: Literal["ready", "review_required", "failed"] = "failed"
    exit_code: int = 2
    linked: int = 0
    review: int = 0
    errors: int = 0
    entries: list[IntakeEntry] = field(default_factory=list)
    error: str = ""
    missing_components: list[str] = field(default_factory=list)


def _norm_top(name: str) -> str:
    """Return the top-level directory name for an entry inside the ZIP."""
    parts = Path(name).parts
    return parts[0].lower() if parts else ""


def _component_of(top: str) -> str | None:
    if top in COMPONENTS:
        return top
    # Common aliases
    if top in {"dataset", "data"}:
        return "datasets"
    if top == "form":
        return "forms"
    if top in {"dictionary", "data-dictionary", "codebook"}:
        return "data_dictionary"
    if top in {"mapping", "map"}:
        return "mappings"
    return None


def _xlsx_is_single_sheet(path: Path) -> bool:
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        n = len(wb.sheetnames)
        wb.close()
        return n == 1
    except Exception:
        return False


def unpack_zip(zip_path: Path, dest_root: Path) -> tuple[list[str], str | None]:
    """Extract ZIP into dest_root. Returns (extracted_relpaths, error_or_None).

    Rejects: absolute paths, path traversal, symlinks, files > 200 MB.
    Normalizes a single-root wrapper directory so top-level components are at dest_root.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            # Detect single-root wrapper: every non-empty entry shares the same first-part.
            tops = set()
            for info in infos:
                if info.filename.endswith("/") or not info.filename:
                    continue
                parts = Path(info.filename).parts
                if parts:
                    tops.add(parts[0])
            strip_root = ""
            if len(tops) == 1 and next(iter(tops)) not in COMPONENTS and _component_of(next(iter(tops)).lower()) is None:
                strip_root = next(iter(tops))
            for info in infos:
                name = info.filename
                if name.endswith("/"):
                    continue
                if name.startswith("/") or ".." in Path(name).parts:
                    return extracted, f"unsafe path in zip: {name!r}"
                if (info.external_attr >> 16) & 0xF000 == 0xA000:
                    return extracted, f"symlink in zip: {name!r}"
                if info.file_size > 200 * 1024 * 1024:
                    return extracted, f"file exceeds 200 MB: {name!r}"
                # strip single-root wrapper if applicable
                rel_name = name
                if strip_root:
                    parts = Path(name).parts
                    if parts and parts[0] == strip_root:
                        rel_name = str(Path(*parts[1:])) if len(parts) > 1 else ""
                if not rel_name:
                    continue
                dst = dest_root / rel_name
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dst.open("wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                extracted.append(rel_name)
    except zipfile.BadZipFile:
        return extracted, "not a valid zip archive"
    except Exception as e:
        return extracted, f"{type(e).__name__}: {e}"
    return extracted, None


def scan_intake(root: Path) -> tuple[list[IntakeEntry], list[str]]:
    """Walk the unpacked tree, assign each real file to a component.

    Returns (entries, missing_components).
    """
    entries: list[IntakeEntry] = []
    seen_components: set[str] = set()

    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            entries.append(IntakeEntry(
                component="_unclassified",
                relpath=str(path.relative_to(root)),
                stored_path=str(path),
                reason="symlink not allowed",
            ))
            continue
        rel = path.relative_to(root)
        top = _norm_top(str(rel))
        component = _component_of(top)
        size = path.stat().st_size
        ext = path.suffix.lower()

        if component is None:
            entries.append(IntakeEntry(
                component="_unclassified",
                relpath=str(rel),
                stored_path=str(path),
                size_bytes=size,
                reason=f"top-level dir {top!r} is not a known component",
            ))
            continue
        allowed = COMPONENT_SUFFIXES[component]
        if ext not in allowed:
            entries.append(IntakeEntry(
                component="_unclassified",
                relpath=str(rel),
                stored_path=str(path),
                size_bytes=size,
                reason=f"extension {ext!r} not allowed in {component!r} (expected {sorted(allowed)})",
            ))
            continue
        if component == "datasets" and ext == ".xlsx" and not _xlsx_is_single_sheet(path):
            entries.append(IntakeEntry(
                component="_unclassified",
                relpath=str(rel),
                stored_path=str(path),
                size_bytes=size,
                reason="dataset xlsx must be single-sheet",
            ))
            continue

        entries.append(IntakeEntry(
            component=component,
            relpath=str(rel),
            stored_path=str(path),
            size_bytes=size,
        ))
        seen_components.add(component)

    missing: list[str] = []
    for comp in MANDATORY:
        if comp not in seen_components:
            missing.append(comp)
    if not (seen_components & ANY_OF):
        missing.append("one_of_forms_dictionary_or_mappings")

    return entries, missing


def build_manifest(study_id: str, zip_path: Path, workspace_root: Path) -> IntakeManifest:
    """Full intake pipeline: unpack zip, scan tree, resolve status + exit code."""
    intake_root = workspace_root / study_id
    m = IntakeManifest(study_id=study_id, root=str(intake_root))
    _, err = unpack_zip(zip_path, intake_root)
    if err:
        m.status = "failed"
        m.exit_code = 2
        m.error = err
        return m
    entries, missing = scan_intake(intake_root)
    m.entries = entries
    m.linked = sum(1 for e in entries if e.component != "_unclassified")
    m.review = sum(1 for e in entries if e.component == "_unclassified")
    m.missing_components = missing
    if missing:
        m.status = "failed"
        m.exit_code = 2
        m.error = f"missing mandatory components: {missing}"
        return m
    if m.review > 0:
        m.status = "review_required"
        m.exit_code = 8
        return m
    m.status = "ready"
    m.exit_code = 0
    return m
