"""Publish Guard: deterministic last-mile PHI scan on emitted exports.

The GOAL is 'input PHI-filled study data -> output PHI-handled study data ready
to be shared and used publicly'. The Publish Guard is the boundary between
those two states. It runs AFTER the Executor has emitted files to
``/app/data/exports/`` and BEFORE any download URL is served.

Regulatory basis:
    HIPAA Privacy Rule 45 CFR 164.514(b)(2)(i) identifies TYPES of
    information. A pattern whose SHAPE overlaps with legitimate clinical
    data (a bare "95" that is a heart-rate, an "ARM 001" study code that
    resembles a license plate, a 15-digit barcode that resembles an IMEI)
    must therefore be gated by column semantics from Judge's decision, or
    by an in-cell anchor token, before it fires. Otherwise the guard
    over-blocks real de-identified exports.

Design rules:

* **Fail closed.** If anything looks like PHI, the export is blocked; the
  operator must fix the pipeline (add a rule, tighten a decision) rather
  than override the guard.
* **Regulation-aware.** Conditional patterns consult the per-column HIPAA
  category and in-cell anchor tokens before firing.
* **Deterministic.** No LLM in this path. Presidio + regex + explicit
  denylists. Every finding cites the pattern that matched.
* **Cheap.** Runs synchronously on already-redacted files so the overhead
  is negligible relative to the 12-agent pipeline.

Public API:

* :func:`scan_export_file(file_id, path, column_categories=None, jurisdiction="us")` -> :class:`GuardResult`
* :func:`scan_all_exports(export_paths, decisions=None, jurisdiction="us")` -> :class:`GuardReport`
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .jurisdictions import GuardPattern, get_pack

MAX_FINDINGS_PER_FILE = 20


def should_fire(p: GuardPattern, matched: str, cell_text: str,
                column_category: str | None) -> bool:
    """Decide whether a conditional pattern should fire in this cell.

    Non-conditional patterns always fire (returns True immediately).
    Conditional patterns fire only when the column's HIPAA category matches
    the identifier type OR the cell text contains an anchor token that
    explicitly names the identifier. A ``validator`` (e.g. Luhn) further
    narrows a pattern regardless of conditional status.
    """
    if p.validator is not None and not p.validator(matched):
        return False
    if not p.conditional:
        return True
    if column_category and column_category in p.column_categories:
        return True
    return bool(p.cell_anchors and p.cell_anchors.search(cell_text))


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

def _scan_text(text: str, file_id: str, path: str,
                patterns: tuple[GuardPattern, ...]) -> list[Finding]:
    """Run every pattern over ``text`` and return findings (deduped by pattern).

    Non-CSV text (narrative, TXT/MD) has no column context; conditional
    patterns rely purely on in-cell anchors here.
    """
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    lines = text.splitlines() or [text]
    for lineno, line in enumerate(lines, start=1):
        for p in patterns:
            m = p.regex.search(line)
            if not m:
                continue
            if not should_fire(p, m.group(0), line, None):
                continue
            key = (p.pid, m.group(0))
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(
                file=path, pattern_id=p.pid, hipaa_category=p.category,
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


def scan_export_file(
    file_id: str,
    path: Path,
    column_categories: dict[str, str] | None = None,
    jurisdiction: str = "us",
) -> GuardResult:
    """Scan a single exported file. Only CSV/TSV/XLSX/TXT are inspected.
    Files that cannot be scanned are blocked.

    ``column_categories`` maps CSV/XLSX header names to the pipeline's
    per-column HIPAA category letter ("A".."R"). Used by conditional
    patterns (AGE_OVER_89, LICENSE_PLATE, IMEI, and others) so they fire
    only on cells whose column actually carries that identifier type.
    Non-CSV surfaces fall back to in-cell anchor detection. ``jurisdiction``
    selects which pack's pattern table this file is scanned against.
    """
    patterns = get_pack(jurisdiction).patterns
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
        findings = _scan_csv_text(text, file_id, path.name, column_categories or {}, patterns)
    elif ext in ("txt", "md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                               detail=f"read failed: {e}", findings=[])
        findings = _scan_text(text, file_id, path.name, patterns)
    elif ext in ("xlsx", "xls"):
        findings = _scan_xlsx(file_id, path, column_categories or {}, patterns)
    else:
        return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                           detail=f"extension {ext!r} cannot be scanned")
    if findings:
        return GuardResult(file_id=file_id, file_path=str(path), status="blocked",
                           findings=[asdict(f) for f in findings],
                           detail=f"{len(findings)} residual PHI finding(s)")
    return GuardResult(file_id=file_id, file_path=str(path), status="clean")


def _scan_csv_text(
    text: str,
    file_id: str,
    filename: str,
    column_categories: dict[str, str],
    patterns: tuple[GuardPattern, ...],
) -> list[Finding]:
    """CSV-aware scan: skip the header row and consult column semantics
    for conditional patterns so a clinical value 90-99 in a
    heart-rate column does not trip AGE_OVER_89."""
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    reader = csv.reader(text.splitlines())
    headers: list[str] = []
    for lineno, row in enumerate(reader, start=1):
        if lineno == 1:
            headers = [str(h) for h in row]
            continue
        for col_idx, cell in enumerate(row):
            col_name = headers[col_idx] if col_idx < len(headers) else ""
            col_cat = column_categories.get(col_name)
            for p in patterns:
                m = p.regex.search(cell)
                if not m:
                    continue
                if not should_fire(p, m.group(0), cell, col_cat):
                    continue
                key = (p.pid, m.group(0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(
                    file=filename, pattern_id=p.pid, hipaa_category=p.category,
                    sample=_sanitise_sample(m.group(0)), line=lineno,
                ))
                if len(findings) >= MAX_FINDINGS_PER_FILE:
                    return findings
    return findings


def _scan_xlsx(
    file_id: str,
    path: Path,
    column_categories: dict[str, str],
    patterns: tuple[GuardPattern, ...],
) -> list[Finding]:
    unavailable = [Finding(
        file=path.name, pattern_id="GUARD_UNAVAILABLE", hipaa_category="",
        sample="", line=0,
    )]
    try:
        import openpyxl  # local dep, already installed
    except ImportError:
        return unavailable
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:
        return unavailable

    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    try:
        for ws in wb.worksheets:
            headers: list[str] = []
            for lineno, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if lineno == 1:
                    headers = ["" if v is None else str(v) for v in row]
                    continue
                for col_idx, cell in enumerate(row):
                    if cell is None:
                        continue
                    cell = str(cell)
                    col_name = headers[col_idx] if col_idx < len(headers) else ""
                    col_cat = column_categories.get(col_name)
                    for p in patterns:
                        m = p.regex.search(cell)
                        if not m:
                            continue
                        if not should_fire(p, m.group(0), cell, col_cat):
                            continue
                        key = (p.pid, m.group(0))
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(Finding(
                            file=path.name, pattern_id=p.pid, hipaa_category=p.category,
                            sample=_sanitise_sample(m.group(0)), line=lineno,
                        ))
                        if len(findings) >= MAX_FINDINGS_PER_FILE:
                            break
                    if len(findings) >= MAX_FINDINGS_PER_FILE:
                        break
                if len(findings) >= MAX_FINDINGS_PER_FILE:
                    break
            if len(findings) >= MAX_FINDINGS_PER_FILE:
                break
    except Exception:
        try:
            wb.close()
        except Exception:
            pass
        return unavailable
    try:
        wb.close()
    except Exception:
        return unavailable
    return findings


def scan_all_exports(
    export_paths: dict[str, str],
    decisions: list[dict[str, Any]] | None = None,
    jurisdiction: str = "us",
) -> GuardReport:
    """Run :func:`scan_export_file` over every entry in ``export_paths``.

    ``decisions`` is the pipeline's per-column decision list. When
    provided, each cell's column is looked up to determine whether a
    conditional pattern should fire (see ``should_fire``). When absent,
    conditional patterns rely purely on in-cell anchor tokens (safer
    default: catches free-text leaks even without column context).
    ``jurisdiction`` selects which pack's pattern table every file in this
    session is scanned against.
    """
    # Build (file_id -> {column_name: hipaa_category}) from decisions
    per_file_col_cats: dict[str, dict[str, str]] = {}
    for d in decisions or []:
        fid = d.get("file_id") or ""
        col = d.get("column") or ""
        # Judge emits `phi_category`; older decisions may use `hipaa_category`
        # or plain `category`. Accept any of them.
        cat = d.get("hipaa_category") or d.get("phi_category") or d.get("category") or ""
        if fid and col and cat:
            per_file_col_cats.setdefault(fid, {})[col] = cat

    results: list[GuardResult] = []
    blocked = 0
    scanned = 0
    for file_id, p in (export_paths or {}).items():
        if not p:
            results.append(GuardResult(file_id=file_id, file_path="", status="skipped",
                                       detail="path unavailable"))
            continue
        col_cats = per_file_col_cats.get(file_id, {})
        r = scan_export_file(file_id, Path(p), column_categories=col_cats, jurisdiction=jurisdiction)
        results.append(r)
        if r.status == "blocked":
            blocked += 1
        if r.status != "skipped":
            scanned += 1
    if not export_paths:
        results.append(GuardResult(file_id="", file_path="", status="blocked",
                                   detail="no exports to scan"))
    status = "clean" if (blocked == 0 and scanned > 0) else "blocked"
    return GuardReport(
        status=status,
        results=[r.to_dict() for r in results],
        scanned=scanned,
        blocked=blocked,
    )
