"""Intake manifest v3: ZIP -> {datasets/, forms/, dictionary/}.

Aligned with the goal at /app/memory/GOAL.md and Sir's clarification 2026-07-27:
three data elements only, not four. `data_dictionary/`, `mappings/`, `dictionary/`,
`mapping/`, and `codebook/` are all aliases for the same slot: the schema-defining
workbook. Datasets is always mandatory; at least one of dictionary or forms must
accompany it.

Rules (fail-closed):
  - `datasets/` required. Extensions: .csv, .xls, .xlsx. Single sheet only for xlsx.
    .json / .jsonl NOT accepted here.
  - At least one of `forms/` (.pdf) or `dictionary/` (.csv, .xlsx) must accompany datasets.
  - Any unsupported suffix, multi-sheet xlsx dataset, unreadable xls, empty
    file, cross-component duplicate content, or symlink -> `_unclassified`
    review bucket recording only `{path, reason, blocking}` (never row values).
  - Blocking review item holds the entire study.

Public status codes (CLI-style exit-code contract):
  0 -> "ready"           : classification may proceed
  8 -> "review_required" : at least one _unclassified bucket entry
  2 -> "failed"          : missing mandatory component or malformed ZIP
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import openpyxl


COMPONENT_SUFFIXES: dict[str, set[str]] = {
    "datasets":   {".csv", ".xls", ".xlsx"},
    "forms":      {".pdf"},
    "dictionary": {".csv", ".xlsx", ".xls"},
}
COMPONENTS = tuple(COMPONENT_SUFFIXES)
MANDATORY = {"datasets"}
# At least one of these must be present alongside datasets/.
ANY_OF = {"forms", "dictionary"}


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
    # All of these are aliases for the single "dictionary" slot per Sir's spec 2026-07-27.
    if top in {"data_dictionary", "data-dictionary", "codebook",
               "mapping", "mappings", "map", "workbook",
               "dictionary_mapping", "dictionary-mapping"}:
        return "dictionary"
    return None


def _xlsx_is_single_sheet(path: Path) -> tuple[bool, str]:
    """Return (is_single_sheet, error_reason)."""
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        n = len(wb.sheetnames)
        wb.close()
        return (n == 1, "" if n == 1 else f"xlsx has {n} sheets, single-sheet required")
    except Exception as e:
        return (False, f"unreadable xlsx: {type(e).__name__}")


def _csv_is_readable(path: Path) -> tuple[bool, str]:
    """Best-effort CSV validation: file has at least a header line."""
    try:
        with path.open("rb") as f:
            head = f.read(4096)
        if not head:
            return False, "empty file"
        # Reject files that decode as pure binary
        try:
            text = head.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            try:
                text = head.decode("latin-1")
            except Exception:
                return False, "not utf-8 or latin-1 decodable"
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if not first_line.strip():
            return False, "empty header line"
        return True, ""
    except Exception as e:
        return False, f"unreadable csv: {type(e).__name__}"


def _pdf_is_readable(path: Path) -> tuple[bool, str]:
    """PDF magic-bytes check."""
    try:
        with path.open("rb") as f:
            head = f.read(5)
        if head != b"%PDF-":
            return False, "not a PDF (missing %PDF- magic)"
        return True, ""
    except Exception as e:
        return False, f"unreadable pdf: {type(e).__name__}"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack_zip(zip_path: Path, dest_root: Path) -> tuple[list[str], str | None]:
    """Extract ZIP into dest_root. Returns (extracted_relpaths, error_or_None).

    Rejects: absolute paths, path traversal, symlinks, individual files > 200 MB.
    SEC-005 caps (env-overridable):
      * total decompressed size <= INTAKE_MAX_TOTAL_BYTES (default 1 GiB)
      * entry count <= INTAKE_MAX_ENTRIES (default 500)
      * per-entry compression ratio <= INTAKE_MAX_RATIO (default 100x)
    Normalizes a single-root wrapper directory so top-level components are at dest_root.
    """
    max_total = int(os.environ.get("INTAKE_MAX_TOTAL_BYTES", 1 << 30))          # 1 GiB
    max_entries = int(os.environ.get("INTAKE_MAX_ENTRIES", 500))
    max_ratio = int(os.environ.get("INTAKE_MAX_RATIO", 100))
    per_file_cap = 200 * 1024 * 1024

    dest_root.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                return extracted, f"zip has {len(infos)} entries; cap is {max_entries}"
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
                if info.file_size > per_file_cap:
                    return extracted, f"file exceeds 200 MB: {name!r}"
                # Compression-bomb guard: reject entries with an outrageous ratio.
                if info.compress_size > 0 and info.file_size // max(info.compress_size, 1) > max_ratio:
                    return extracted, (
                        f"suspicious compression ratio for {name!r}: "
                        f"{info.file_size}/{info.compress_size} (> {max_ratio}x)"
                    )
                total_bytes += info.file_size
                if total_bytes > max_total:
                    return extracted, f"total uncompressed size exceeds {max_total} bytes"
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
                # Also enforce a streaming cap on decompressed bytes per file so
                # a lying header (small file_size, huge stream) still trips.
                written = 0
                with zf.open(info) as src, dst.open("wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > per_file_cap:
                            return extracted, f"streamed size exceeded 200 MB for {name!r}"
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
    hash_to_component: dict[str, str] = {}  # for cross-component duplicate detection

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
        if size == 0:
            entries.append(IntakeEntry(
                component="_unclassified",
                relpath=str(rel),
                stored_path=str(path),
                size_bytes=0,
                reason="empty file (0 bytes)",
            ))
            continue

        # Format-specific validation
        reason = ""
        if component == "datasets" and ext == ".xlsx":
            ok, reason = _xlsx_is_single_sheet(path)
            if not ok:
                entries.append(IntakeEntry(
                    component="_unclassified", relpath=str(rel), stored_path=str(path),
                    size_bytes=size, reason=reason,
                ))
                continue
        elif ext == ".csv":
            ok, reason = _csv_is_readable(path)
            if not ok:
                entries.append(IntakeEntry(
                    component="_unclassified", relpath=str(rel), stored_path=str(path),
                    size_bytes=size, reason=reason,
                ))
                continue
        elif ext == ".pdf":
            ok, reason = _pdf_is_readable(path)
            if not ok:
                entries.append(IntakeEntry(
                    component="_unclassified", relpath=str(rel), stored_path=str(path),
                    size_bytes=size, reason=reason,
                ))
                continue

        # Hash + cross-component duplicate detection
        sha = _sha256_of(path)
        prior = hash_to_component.get(sha)
        if prior and prior != component:
            entries.append(IntakeEntry(
                component="_unclassified", relpath=str(rel), stored_path=str(path),
                size_bytes=size, sha256=sha,
                reason=f"duplicate content across components ({prior} <-> {component})",
            ))
            continue
        hash_to_component[sha] = component

        entries.append(IntakeEntry(
            component=component,
            relpath=str(rel),
            stored_path=str(path),
            size_bytes=size,
            sha256=sha,
        ))
        seen_components.add(component)

    missing: list[str] = []
    for comp in MANDATORY:
        if comp not in seen_components:
            missing.append(comp)
    if not (seen_components & ANY_OF):
        missing.append("one_of_forms_or_dictionary")

    return entries, missing


def build_manifest(study_id: str, zip_path: Path, workspace_root: Path) -> IntakeManifest:
    """Full intake pipeline: unpack zip, scan tree, resolve status + exit code."""
    intake_root = workspace_root / study_id
    # Re-intake: clean stale unpacked tree so residual entries don't linger.
    if intake_root.exists():
        import shutil
        shutil.rmtree(intake_root, ignore_errors=True)
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
    m.errors = 0
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
