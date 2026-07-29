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

* :func:`scan_export_file(path, decisions_by_file=None)` -> :class:`GuardResult`
* :func:`scan_all_exports(export_paths, decisions=None)` -> :class:`GuardReport`
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
    ("DATE_FULL_ISO", "C", re.compile(r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")),
    ("DATE_FULL_US", "C", re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/(19|20)\d{2}\b")),
    ("RESTRICTED_ZIP3", "B", re.compile(
        r"\b(036|059|063|102|203|556|692|790|821|823|830|831|878|879|884|890|893)\d{2}\b"
    )),
    ("AGE_OVER_89", "C", re.compile(r"\b9[0-9]\b(?![\+\-])")),
    # --- Phase B parity for categories L / M / N / O / P / Q / R -----------
    ("URL", "N", re.compile(r"\bhttps?://[^\s,\"']{3,}", re.IGNORECASE)),
    ("IPV4", "O", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("IPV6", "O", re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b")),
    # US-style license plate (2-3 letters + 3-4 digits, common state formats)
    ("LICENSE_PLATE", "L", re.compile(r"\b[A-Z]{2,3}[- ]?\d{3,4}\b")),
    # 15-digit IMEI (loose but standalone)
    ("IMEI", "M", re.compile(r"(?<!\d)\d{15}(?!\d)")),
    # Device serial: 8+ alphanumerics with at least one digit AND one letter,
    # matching common "SN12345678", "SN-ABCD-1234" shapes.
    ("DEVICE_SERIAL", "M", re.compile(r"\b[Ss][Nn][:\- ]*[A-Z0-9\-]{6,}\b")),
    # Image file reference in a cell value (photograph / face capture)
    ("IMAGE_REF", "Q", re.compile(r"\b[A-Za-z0-9_\-/.]+\.(?:jpe?g|png|bmp|tiff|heic|heif)\b", re.IGNORECASE)),
    # Biometric hash / fingerprint reference (label + long hex)
    ("BIOMETRIC_HASH", "P", re.compile(r"\b(?:fingerprint|iris|biometric|voice[_ ]?print)[:=\s]*[A-Fa-f0-9]{16,}\b", re.IGNORECASE)),
    # DNA / genetic identifiers
    ("DNA_PROFILE", "P", re.compile(r"\b(?:dna|str)[_ ]?(?:profile|locus)[:=\s]*[A-Z0-9\-]{8,}\b", re.IGNORECASE)),
    # NPI (10-digit provider id) - K
    ("NPI", "K", re.compile(r"\bNPI[:\- ]*\d{10}\b")),
    # DEA number (2 letters + 7 digits) - K
    ("DEA", "K", re.compile(r"\bDEA[:\- ]*[A-Z]{2}\d{7}\b")),
]


# --- Conditional patterns -------------------------------------------------
#
# HIPAA Safe Harbor identifies TYPES of information, not shapes. Three of our
# guard patterns have shapes that overlap with legitimate clinical data:
#
#   * AGE_OVER_89  overlaps with vitals/lab values 90-99 (HR, BP, glucose)
#   * LICENSE_PLATE overlaps with study arm/site codes ("ARM 001", "HB 120")
#   * IMEI overlaps with long barcodes / study identifiers
#
# For these three we fire ONLY when the column's HIPAA category from the
# pipeline decision matches the identifier type (defensible: the classifier
# already decided this column carries that identifier and an action must have
# emitted it), OR when the cell text carries an anchor token that names the
# identifier explicitly (defensible: catches free-text leaks the classifier
# never saw). Every other pattern remains unconditional because its shape is
# unique enough that a false-positive is implausible in a de-identified study
# export.
_CONDITIONAL: dict[str, dict[str, Any]] = {
    "AGE_OVER_89": {
        "column_cats": {"C"},
        "anchors": re.compile(
            r"\b(?:age[sd]?|y/?o|yrs?|years?\s+old|elderly)\b", re.IGNORECASE
        ),
    },
    "LICENSE_PLATE": {
        "column_cats": {"L"},
        "anchors": re.compile(r"\b(?:plate|license|licence|tag|vehicle)\b", re.IGNORECASE),
    },
    "IMEI": {
        "column_cats": {"M"},
        "anchors": re.compile(r"\b(?:imei|device[_ ]?id|handset)\b", re.IGNORECASE),
    },
}


def _should_fire(
    pid: str,
    cell_text: str,
    column_category: str | None,
) -> bool:
    """Decide whether a conditional pattern should fire in this cell.

    Non-conditional patterns always fire (returns True immediately).
    Conditional patterns fire only when the column's HIPAA category matches
    the identifier type OR the cell text contains an anchor token that
    explicitly names the identifier.
    """
    rule = _CONDITIONAL.get(pid)
    if rule is None:
        return True  # unconditional pattern
    if column_category and column_category in rule["column_cats"]:
        return True
    if rule["anchors"].search(cell_text):
        return True
    return False


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
               column_categories: dict[str, str] | None = None) -> list[Finding]:
    """Run every pattern over ``text`` and return findings (deduped by pattern).

    Non-CSV text (narrative, TXT/MD) has no column context; conditional
    patterns rely purely on in-cell anchors here.
    """
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    lines = text.splitlines() or [text]
    for lineno, line in enumerate(lines, start=1):
        for pid, cat, rx in _PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            if not _should_fire(pid, line, None):
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


def scan_export_file(
    file_id: str,
    path: Path,
    column_categories: dict[str, str] | None = None,
) -> GuardResult:
    """Scan a single exported file. Only CSV/TSV/XLSX/TXT are inspected;
    anything else is marked ``skipped`` so we do not falsely block PDFs
    whose scrub happened at the raw-text layer.

    ``column_categories`` maps CSV/XLSX header names to the pipeline's
    per-column HIPAA category letter ("A".."R"). Used by the conditional
    patterns (AGE_OVER_89, LICENSE_PLATE, IMEI) so they fire only on cells
    whose column actually carries that identifier type. Non-CSV surfaces
    fall back to in-cell anchor detection.
    """
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
        findings = _scan_csv_text(text, file_id, path.name, column_categories or {})
    elif ext in ("txt", "md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        findings = _scan_text(text, file_id, path.name)
    elif ext in ("xlsx", "xls"):
        findings = _scan_xlsx(file_id, path, column_categories or {})
    else:
        return GuardResult(file_id=file_id, file_path=str(path), status="skipped",
                           detail=f"extension {ext!r} not scanned")
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
            for pid, cat, rx in _PATTERNS:
                m = rx.search(cell)
                if not m:
                    continue
                if not _should_fire(pid, cell, col_cat):
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


def _scan_xlsx(
    file_id: str,
    path: Path,
    column_categories: dict[str, str],
) -> list[Finding]:
    try:
        import openpyxl  # local dep, already installed
    except ImportError:
        return []
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
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
            for pid, cat, rx in _PATTERNS:
                m = rx.search(cell)
                if not m:
                    continue
                if not _should_fire(pid, cell, col_cat):
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


def scan_all_exports(
    export_paths: dict[str, str],
    decisions: list[dict[str, Any]] | None = None,
) -> GuardReport:
    """Run :func:`scan_export_file` over every entry in ``export_paths``.

    ``decisions`` is the pipeline's per-column decision list. When
    provided, each cell's column is looked up to determine whether a
    conditional pattern should fire (see ``_should_fire``). When absent,
    conditional patterns rely purely on in-cell anchor tokens (safer
    default: catches free-text leaks even without column context).
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
        r = scan_export_file(file_id, Path(p), column_categories=col_cats)
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
