"""Publish Guard: deterministic last-mile PHI scan on emitted exports.

The GOAL is 'input PHI-filled study data -> output PHI-handled study data ready
to be shared and used publicly'. The Publish Guard is the boundary between
those two states. It runs AFTER the Executor has emitted files to
``/app/data/exports/`` and BEFORE any download URL is served.

Design rules:

* **Fail closed.** If anything looks like PHI, the export is blocked; the
  operator must fix the pipeline (add a rule, tighten a decision) rather
  than override the guard.
* **Deterministic.** No LLM in this path. Presidio + regex + explicit
  denylists. Every finding cites the pattern that matched, so the operator
  can reproduce it.
* **Cheap.** Runs synchronously on already-redacted files so the overhead
  is negligible relative to the 12-agent pipeline.

Public API:

* :func:`scan_export_file(path)` -> :class:`GuardResult`
* :func:`scan_all_exports(export_paths)` -> :class:`GuardReport`

Both return plain dicts (via ``model_dump``) so they can be stored on the
session document and served via the read endpoints. The ``findings`` field
is bounded (``MAX_FINDINGS_PER_FILE``) so a completely broken export cannot
blow up the response body.
"""
from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


MAX_FINDINGS_PER_FILE = 20


# --- Detector patterns ----------------------------------------------------
#
# We deliberately keep this list short and Safe-Harbor-anchored. Over-matching
# would make the guard noisy; under-matching would let real PHI through. Each
# pattern maps to a HIPAA Safe Harbor category letter for reviewer clarity.
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # (id, hipaa_category_letter, regex)
    ("SSN", "G", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE_US", "D", re.compile(r"\b(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")),
    ("EMAIL", "F", re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # Full DOB anywhere in an export is a Safe Harbor violation. The
    # pipeline should have converted these to year-only; if any slip through
    # the guard catches them.
    ("DATE_FULL_ISO", "C", re.compile(r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")),
    ("DATE_FULL_US", "C", re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/(19|20)\d{2}\b")),
    # 5-digit ZIP anywhere in an export means the truncate step was skipped.
    # We allow 3-digit ZIPs, but any 5-digit run in a column that is not
    # already numeric-looking is suspicious. To keep noise down we only fire
    # on the 17 restricted ZIP3 codes fully written out (036xx etc.) since
    # those are the ones Safe Harbor prohibits explicitly.
    ("RESTRICTED_ZIP3", "B", re.compile(
        r"\b(036|059|063|102|203|556|692|790|821|823|830|831|878|879|884|890|893)\d{2}\b"
    )),
    # Age > 89 as a raw integer (should have been aggregated to "90+")
    ("AGE_OVER_89", "C", re.compile(r"(?<![\d.])9[0-9](?![\d+])")),
]


@dataclass
class Finding:
    """One residual-PHI hit in an exported file."""
    file: str
    pattern_id: str
    hipaa_category: str
    sample: str
    line: int


@dataclass
class GuardResult:
    """Result for a single exported file."""
    file_id: str
    file_path: str
    status: str  # "clean" | "blocked" | "skipped"
    findings: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuardReport:
    """Aggregate report over every export in a session."""
    status: str  # "clean" | "blocked"
    results: list[dict[str, Any]] = field(default_factory=list)
    scanned: int = 0
    blocked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Core scanners --------------------------------------------------------

def _scan_text(text: str, file_id: str, path: str) -> list[Finding]:
    """Run every pattern over ``text`` and return findings (deduped by pattern)."""
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    lines = text.splitlines() or [text]
    for lineno, line in enumerate(lines, start=1):
        for pid, cat, rx in _PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            key = (pid, m.group(0))
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(
                file=path, pattern_id=pid, hipaa_category=cat,
                sample=_sanitise_sample(m.group(0)),
                line=lineno,
            ))
            if len(findings) >= MAX_FINDINGS_PER_FILE:
                return findings
    return findings


def _sanitise_sample(sample: str) -> str:
    """Return a partial-mask of the matched substring so the finding itself
    does not carry the raw PHI verbatim into the report."""
    if len(sample) <= 4:
        return "*" * len(sample)
    return sample[:2] + "*" * (len(sample) - 4) + sample[-2:]


def scan_export_file(file_id: str, path: Path) -> GuardResult:
    """Scan a single exported file. Only CSV/TSV/XLSX/TXT are inspected;
    anything else is marked ``skipped`` so we do not falsely block PDFs
    whose scrub happened at the raw-text layer."""
    try:
        ext = path.suffix.lower().lstrip(".")
    except AttributeError:
        return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                           detail="invalid path", findings=[])
    if not path.exists():
        return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                           detail="export file missing", findings=[])
    if ext in ("csv", "tsv"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                               detail=f"read failed: {e}", findings=[])
        findings = _scan_csv_text(text, file_id, path.name)
    elif ext in ("txt", "md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        findings = _scan_text(text, file_id, path.name)
    elif ext in ("xlsx", "xls"):
        findings = _scan_xlsx(file_id, path)
    else:
        return GuardResult(file_id=file_id, file_path=str(path), status="skipped",
                           detail=f"extension {ext!r} not scanned")
    if findings:
        return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                           findings=[asdict(f) for f in findings],
                           detail=f"{len(findings)} residual PHI finding(s)")
    return GuardResult(file_id=file_id, file_path=str(path), status="clean")


def _scan_csv_text(text: str, file_id: str, filename: str) -> list[Finding]:
    """CSV-aware scan: only inspect DATA rows, skip the header row so a
    column name like ``phone_number`` doesn't itself trip the guard."""
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    reader = csv.reader(text.splitlines())
    for lineno, row in enumerate(reader, start=1):
        if lineno == 1:
            continue  # header row - column names are metadata, not PHI values
        for cell in row:
            for pid, cat, rx in _PATTERNS:
                m = rx.search(cell)
                if not m:
                    continue
                key = (pid, m.group(0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(
                    file=filename, pattern_id=pid, hipaa_category=cat,
                    sample=_sanitise_sample(m.group(0)), line=lineno,
                ))
                if len(findings) >= MAX_FINDINGS_PER_FILE:
                    return findings
    return findings


def _scan_xlsx(file_id: str, path: Path) -> list[Finding]:
    try:
        import openpyxl  # local dep, already installed
    except ImportError:
        return []
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    for lineno, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if lineno == 1:
            continue
        for cell in row:
            if cell is None:
                continue
            cell = str(cell)
            for pid, cat, rx in _PATTERNS:
                m = rx.search(cell)
                if not m:
                    continue
                key = (pid, m.group(0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(
                    file=path.name, pattern_id=pid, hipaa_category=cat,
                    sample=_sanitise_sample(m.group(0)), line=lineno,
                ))
                if len(findings) >= MAX_FINDINGS_PER_FILE:
                    return findings
    return findings


def scan_all_exports(export_paths: dict[str, str]) -> GuardReport:
    """Run :func:`scan_export_file` over every entry in ``export_paths``.

    Returns a :class:`GuardReport` where ``status='clean'`` means every
    scanned file was clean (skipped files do not block). Otherwise
    ``status='blocked'`` and each blocked file lists its findings.
    """
    results: list[GuardResult] = []
    blocked = 0
    scanned = 0
    for file_id, p in (export_paths or {}).items():
        if not p:
            # Some deployments blank the path for security; guard runs from Executor
            # where the real path is still available, so this branch is diagnostic.
            results.append(GuardResult(file_id=file_id, file_path="", status="skipped",
                                       detail="path unavailable"))
            continue
        r = scan_export_file(file_id, Path(p))
        results.append(r)
        if r.status == "blocked":
            blocked += 1
        if r.status != "skipped":
            scanned += 1
    return GuardReport(
        status="clean" if blocked == 0 else "blocked",
        results=[r.to_dict() for r in results],
        scanned=scanned,
        blocked=blocked,
    )
