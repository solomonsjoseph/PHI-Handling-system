"""ReportingSafetyGate (docs #60): the deterministic report-leakage scan.

Split out of ``control/final_assurance.py``: the report-surface scan stays
(it becomes a mandatory tool call inside OutputVerifier before packaging),
while the never-wired FinalAssuranceGate is deleted. The split preserves the
original module's placement reasoning verbatim-publish-worthily below:
Publish Guard's job (docs #61's ``final ZIP`` boundary) is scanning bytes
already written to a canonical, hash-tracked export artifact on disk;
ReportingSafetyGate's job is scanning *report* surfaces -- some of which
(report text, human-review summaries, technical appendix, manifest display
fields) are in-memory strings a report generator has not necessarily written
to disk yet when this gate needs to run, and none of which go through
Publish Guard's own artifact_id/sha256-binding contract. Rather than
distort ``publish_guard.py``'s file-first API to accept raw strings, or
duplicate its regex/name-detection logic, this module *imports* Publish
Guard's existing detectors (``scan_names``, the private-but-same-package
``_scan_text``, and the public ``scan_export_file`` for the one surface --
workbook cells -- that genuinely is an on-disk file) rather than
re-implementing any of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import Field

from ..jurisdictions import get_pack
from ..publish_guard import _scan_text, scan_export_file, scan_names
from .records import ControlRecord

ReportingSafetyVerdict = Literal["PASS", "FAIL"]


@dataclass(frozen=True)
class ReportPackageContent:
    """The report surfaces docs #60 requires scanned before packaging.

    Every field defaults empty so this gate is callable even when a run
    produced no report surface yet -- a caller with no report content gets a
    genuine, correctly-empty ``PASS`` (nothing to leak), not a stub.
    ``workbook_paths`` are real on-disk ``.xlsx``/``.csv`` paths (e.g.
    ``What_Happened_to_Each_Column.xlsx``), scanned through Publish Guard's
    own ``scan_export_file`` rather than a second cell-reading
    implementation.
    """

    report_text: str = ""
    human_review_summary_text: str = ""
    technical_appendix_text: str = ""
    filenames: tuple[str, ...] = ()
    safe_display_column_names: tuple[str, ...] = ()
    manifest_display_fields: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    workbook_paths: tuple[str, ...] = ()


class ReportingSafetyFinding(ControlRecord):
    """One residual-sensitive-value hit on a report surface (docs #60).
    Carries only the masked ``sample`` Publish Guard's own
    ``Finding``/``_sanitise_sample`` already produces -- never a raw value."""

    surface: str
    pattern_id: str
    hipaa_category: str
    sample: str
    line: int = 0


class ReportingSafetyResult(ControlRecord):
    """ReportingSafetyGate's verdict (docs #60): ``PASS`` only when every
    surface section 60 names -- report text, workbook cells, filenames,
    safe-display column names, human-review summaries, technical appendix,
    manifest display fields -- scans clean of both pattern-based and
    name-based (Presidio) detectors."""

    verdict: ReportingSafetyVerdict
    findings: list[ReportingSafetyFinding] = Field(default_factory=list)
    surfaces_scanned: list[str] = Field(default_factory=list)


def _scan_text_surface(
    surface: str, text: str, jurisdiction: str, findings: list[ReportingSafetyFinding],
) -> None:
    """Run Publish Guard's pattern scan (``_scan_text``, imported not
    duplicated) and name scan (``scan_names``) over one in-memory text
    surface. A blank surface is still eligible to be recorded as scanned by
    the caller -- an empty ``report_text`` is a genuine clean result, not a
    skipped check."""
    if not text:
        return
    patterns = get_pack(jurisdiction).patterns
    for f in _scan_text(text, surface, surface, patterns):
        findings.append(ReportingSafetyFinding(
            surface=surface, pattern_id=f.pattern_id, hipaa_category=f.hipaa_category,
            sample=f.sample, line=f.line,
        ))
    for f in scan_names(text, jurisdiction):
        findings.append(ReportingSafetyFinding(
            surface=surface, pattern_id=f.pattern_id, hipaa_category=f.hipaa_category,
            sample=f.sample, line=f.line,
        ))


def run_reporting_safety_gate(
    content: ReportPackageContent, *, jurisdiction: str = "us",
) -> ReportingSafetyResult:
    """The deterministic docs #60 scan: every report surface, before
    packaging. Sensitive source names must use aliases (docs #60) -- this
    gate cannot itself know which display name is an alias and which is a
    raw sensitive header, so it scans ``safe_display_column_names`` for
    residual PHI-shaped content exactly like every other text surface;
    the report generator is responsible for aliasing before handing content
    here."""
    findings: list[ReportingSafetyFinding] = []
    surfaces_scanned: list[str] = []

    text_surfaces: tuple[tuple[str, str], ...] = (
        ("report_text", content.report_text),
        ("human_review_summaries", content.human_review_summary_text),
        ("technical_appendix", content.technical_appendix_text),
        ("filenames", "\n".join(content.filenames)),
        ("safe_display_column_names", "\n".join(content.safe_display_column_names)),
        ("manifest_display_fields",
         "\n".join(f"{k}: {v}" for k, v in content.manifest_display_fields.items())),
    )
    for surface, text in text_surfaces:
        surfaces_scanned.append(surface)
        _scan_text_surface(surface, text, jurisdiction, findings)

    for path in content.workbook_paths:
        surfaces_scanned.append(f"workbook_cells:{path}")
        result = scan_export_file(path, Path(path), jurisdiction=jurisdiction)
        if result.status == "blocked":
            for raw in result.findings:
                findings.append(ReportingSafetyFinding(
                    surface="workbook_cells", pattern_id=raw.get("pattern_id", ""),
                    hipaa_category=raw.get("hipaa_category", ""), sample=raw.get("sample", ""),
                    line=raw.get("line", 0),
                ))

    verdict: ReportingSafetyVerdict = "FAIL" if findings else "PASS"
    return ReportingSafetyResult(verdict=verdict, findings=findings, surfaces_scanned=surfaces_scanned)


class ReviewerFinalResult(ControlRecord):
    """Typed wrapper around ``Reviewer.finalize()``'s plain-dict return
    (docs #55, ``agents/reviewer.py:422``). ``Reviewer.finalize()``'s own
    contract is unchanged and pinned by Phase 10's tests
    (``test_reviewer_final.py``); this record is a typed view of that dict,
    built with ``from_finalize_dict`` rather than changing what ``finalize``
    returns."""

    verdict: Literal["PASS", "FAIL", "HUMAN_REVIEW_REQUIRED"]
    checks: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    signal: dict[str, str] | None = None

    @classmethod
    def from_finalize_dict(cls, data: Mapping[str, Any]) -> "ReviewerFinalResult":
        return cls(
            verdict=data["verdict"],
            checks=list(data.get("checks") or []),
            findings=list(data.get("findings") or []),
            signal=data.get("signal"),
        )
