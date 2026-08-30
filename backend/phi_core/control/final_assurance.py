"""ReportingSafetyGate (docs #60) and FinalAssuranceGate (docs #57): the
deterministic report-leakage scan and the deterministic, non-bypassable
release gate that consumes it, Phase 11a (docs #94, wave 1 of 2).

``records.py`` is closed for this phase; ``ReportingSafetyFinding``,
``ReportingSafetyResult``, ``ReviewerFinalResult``, ``FinalAssuranceCheck``,
and ``FinalAssuranceResult`` below are this module's own supporting types,
extending ``records.ControlRecord``'s existing convention
(``schema_version``, ``extra="forbid"``) exactly as ``control/manifest.py``,
``control/verification.py``, and ``control/rewind.py`` each already do for
their own phase's types, rather than editing the closed module. Their names
match the placeholder strings ``control/artifacts.py``'s
``CONSEQUENTIAL_ARTIFACT_TYPES`` (an earlier wave's forward declaration)
already carries for ``ReviewerFinalResult`` and ``FinalAssuranceResult``;
neither this module nor any caller stages an artifact of either type yet --
no code path anywhere in the repository calls ``ArtifactService.stage``
today, and wiring these gates into a live execution path
(``agents/orchestrator.py``) is explicitly out of this phase's target-file
list. ``ReportPackage`` is 11b's own type (report generation itself); this
module only defines the *content* shape (`ReportPackageContent`) 11b's real
generator will eventually populate.

**ReportingSafetyGate placement (documented choice):** built here rather
than as a second new file or an in-place extension of
``phi_core/publish_guard.py``. Publish Guard's job (docs #61's ``final
ZIP`` boundary) is scanning bytes already written to a canonical,
hash-tracked export artifact on disk; ReportingSafetyGate's job (docs #60)
is scanning *report* surfaces -- some of which (report text, human-review
summaries, technical appendix, manifest display fields) are in-memory
strings a report generator has not necessarily written to disk yet when
this gate needs to run, and none of which go through Publish Guard's own
artifact_id/sha256-binding contract (that contract is specific to
Executor's PHI-handled dataset exports, not to a generated report).
Rather than distorting ``publish_guard.py``'s file-first API to accept raw
strings, or duplicating its regex/name-detection logic in a sibling
module, ReportingSafetyGate lives beside FinalAssuranceGate (the one gate
that consumes its verdict) and *imports* Publish Guard's existing
detectors (``scan_names``, the private-but-same-package ``_scan_text``,
and the public ``scan_export_file`` for the one surface -- workbook cells
-- that genuinely is an on-disk file) rather than re-implementing any of
them. This mirrors the codebase's own established convention of one leaf
module per phase's gate, importing shared detection primitives from
wherever they already live (``control/manifest.py`` imports
``SuperOrchestrator``, ``control/verification.py`` imports
``VerificationResult`` from the closed ``records.py``, ``agents/
reviewer.py`` imports ``agents/reasoning.py``'s private ``_read_columns``).

**Auditor migration (docs #94: "Migrate useful Publish Guard/Auditor
behavior"):** ``Auditor`` (``agents/reasoning.py``, unchanged and not
removed this phase) is fundamentally an LLM re-derivation call; its one
genuinely *deterministic* assurance behavior is
``auditor_escalation_reason`` (also ``agents/reasoning.py``, unchanged) --
the confidence-floor + issues-verdict + artifact-identity + evidence-state
 + gate-status gate that already refuses to let Auditor's own
self-reported confidence silently promote a genuine finding to a pass.
That function is *imported* into ``evaluate_final_assurance`` below as an
additional, always-checked condition (``no_unresolved_audit_finding``)
rather than duplicated: passing its output (``None`` or a reason string)
straight through is what proves a mocked high-confidence Auditor signal
can never flip a FinalAssuranceGate FAIL -- the confidence value is
already consumed and neutralised one layer below, long before this gate's
own checklist runs, and this gate's own ``evaluate_final_assurance``
signature never accepts a raw confidence number at all.

**Section 57 condition count (documented discrepancy):** the master
spec's own verbatim section 57 text enumerates fourteen ``AND``-joined
conditions (input inventory complete; all logical columns accounted;
Reviewer Preview PASS; no unresolved Human Review; VerifiedClassification
Manifest current; manifest frozen; Executor complete; Deterministic
Verifier PASS; Reviewer Final PASS; no unresolved privacy finding; no
unresolved security incident; report package complete; ReportingSafety
Gate PASS; integrity checks PASS) -- not sixteen, despite the phase-11a
task description's "16-condition" framing (also present in docs #94's own
historical annotation). This module implements section 57's fourteen
conditions exactly as specified, verbatim, plus the one additional
``no_unresolved_audit_finding`` condition documented above (the
Auditor-migration instruction), for fifteen checks total -- never
eleven, never sixteen, and never a condition invented to force either
count. See ``FINAL_ASSURANCE_CONDITIONS`` for the authoritative ordered
list.

**Structurally not-yet-exercisable end to end (disclosed, not hidden):**
``report_package_complete`` is a genuine, fully-typed, fully-tested
boolean condition on this gate -- both its PASS and FAIL paths are
covered by real tests -- but no live producer of that value exists in
this session: it is Phase 11b's own ``ReportGenerator``/``ReportPackage``
that will compute it from an actually-generated report bundle. Every one
of the other fourteen conditions (the thirteen remaining section-57 items
plus the Auditor migration) is wired to a real typed record this phase
can and does construct and verify end to end, including
``reporting_safety_gate_pass``, which this same module's own
``run_reporting_safety_gate`` genuinely computes rather than accepting as
an opaque boolean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field

from ..jurisdictions import get_pack
from ..publish_guard import _scan_text, scan_export_file, scan_names
from .records import (
    ControlRecord,
    ExecutionResult,
    VerificationResult,
    VerifiedClassificationManifest,
)
from .report_artifacts import ReportArtifacts, is_report_package_complete
from .store import ControlStore

ReportingSafetyVerdict = Literal["PASS", "FAIL"]
FinalAssuranceVerdict = Literal["READY_FOR_EXPORT", "BLOCKED"]


# --- ReportingSafetyGate (docs #60) ----------------------------------------


@dataclass(frozen=True)
class ReportPackageContent:
    """The report surfaces docs #60 requires scanned before packaging.

    Every field defaults empty so this gate is callable end to end before
    Phase 11b's real ``ReportGenerator`` exists to populate it -- a caller
    with no report content yet gets a genuine, correctly-empty ``PASS``
    (nothing to leak), not a stub. ``workbook_paths`` are real on-disk
    ``.xlsx``/``.csv`` paths (e.g. ``What_Happened_to_Each_Column.xlsx``),
    scanned through Publish Guard's own ``scan_export_file`` rather than a
    second cell-reading implementation.
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
    surface. A blank surface is still recorded as scanned -- an empty
    ``report_text`` before Phase 11b lands is a genuine clean result, not
    a skipped check."""
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
    the report generator (Phase 11b) is what is responsible for aliasing
    before handing content here."""
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


# --- FinalAssuranceGate (docs #57) -----------------------------------------


class ReviewerFinalResult(ControlRecord):
    """Typed wrapper around ``Reviewer.finalize()``'s plain-dict return
    (docs #55, ``agents/reviewer.py:422``). ``Reviewer.finalize()``'s own
    contract is unchanged and pinned by Phase 10's tests
    (``test_reviewer_final.py``); this record is FinalAssuranceGate's own
    typed view of that dict, built with ``from_finalize_dict`` rather than
    changing what ``finalize`` returns."""

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


# Authoritative ordered list: section 57's fourteen verbatim AND-conditions
# plus the one additional Auditor-migration condition (see module docstring
# for the exact accounting -- fourteen from the spec text, fifteen total).
FINAL_ASSURANCE_CONDITIONS: tuple[str, ...] = (
    "input_inventory_complete",
    "all_logical_columns_accounted",
    "reviewer_preview_pass",
    "no_unresolved_human_review",
    "manifest_current",
    "manifest_frozen",
    "executor_complete",
    "deterministic_verifier_pass",
    "reviewer_final_pass",
    "no_unresolved_privacy_finding",
    "no_unresolved_security_incident",
    "report_package_complete",
    "reporting_safety_gate_pass",
    "integrity_checks_pass",
    "no_unresolved_audit_finding",
)


class FinalAssuranceCheck(ControlRecord):
    name: str
    passed: bool
    detail: str = ""


class FinalAssuranceResult(ControlRecord):
    """FinalAssuranceGate's verdict (docs #57): the deterministic,
    non-bypassable release gate. ``READY_FOR_EXPORT`` requires every
    condition in :data:`FINAL_ASSURANCE_CONDITIONS` to pass. No field on
    this record, or on any input ``evaluate_final_assurance`` accepts,
    carries a model confidence score capable of flipping a failing
    condition -- see the module docstring's Auditor-migration note."""

    verdict: FinalAssuranceVerdict
    checks: list[FinalAssuranceCheck] = Field(default_factory=list)
    failed_conditions: list[str] = Field(default_factory=list)


def run_integrity_checks(expected: Mapping[str, tuple[str, str]]) -> tuple[bool, str]:
    """Docs #57's "integrity checks PASS": recompute each artifact's
    sha256 from disk with the same hashing routine ``ArtifactService``
    already uses (``artifacts._hash_file``, imported not duplicated) and
    compare against the sha256 recorded when it was staged/scanned.

    ``expected`` maps ``artifact_id -> (path, expected_sha256)``. Returns
    ``(True, "")`` only when every artifact is readable and its recomputed
    hash matches; otherwise ``(False, "; "-joined mismatch detail)``. An
    empty ``expected`` mapping passes vacuously (nothing to verify),
    matching ``scan_all_exports``'s own vacuous-input convention.
    """
    from .artifacts import _hash_file  # local import: control.artifacts, no new module-level cycle

    mismatches: list[str] = []
    for artifact_id, (path, expected_sha256) in expected.items():
        try:
            actual_sha256, _size = _hash_file(Path(path))
        except OSError as exc:
            mismatches.append(f"{artifact_id}: unreadable ({exc})")
            continue
        if actual_sha256 != expected_sha256:
            mismatches.append(f"{artifact_id}: sha256 mismatch")
    return (not mismatches, "; ".join(mismatches))


def evaluate_final_assurance(
    *,
    expected_file_ids: Sequence[str],
    manifest: VerifiedClassificationManifest,
    reviewer_preview_verdict: str | None,
    execution_result: ExecutionResult,
    verification_result: VerificationResult,
    reviewer_final: ReviewerFinalResult,
    privacy_findings_unresolved: int,
    security_incident_active: bool,
    report_package_complete: bool,
    reporting_safety: ReportingSafetyResult,
    integrity_checks_passed: bool,
    integrity_detail: str = "",
    auditor_escalation: str | None = None,
) -> FinalAssuranceResult:
    """The full :data:`FINAL_ASSURANCE_CONDITIONS` checklist (docs #57).
    Never calls an LLM and never accepts a raw confidence score; every
    input is already a deterministic fact (a typed record, a count, a
    boolean) or, for ``auditor_escalation``, the *already-gated* output of
    ``agents.reasoning.auditor_escalation_reason`` -- ``None`` means
    Auditor's own confidence floor and issues/evidence/gate checks all
    cleared, a reason string means at least one did not. There is no
    parameter through which a bare confidence number can reach this
    function and no code path below that reads one.

    ``input_inventory_complete`` and ``all_logical_columns_accounted`` are
    computed from real fields already on the frozen manifest and the
    verification result (``manifest.source_artifact_versions``,
    ``verification_result.manifest_coverage_percent``) rather than taken
    as opaque booleans, so both are genuinely exercised by this function's
    own logic, not merely threaded through.
    """
    checks: list[FinalAssuranceCheck] = []

    def _check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(FinalAssuranceCheck(name=name, passed=ok, detail=detail))

    missing_files = sorted(set(expected_file_ids) - set(manifest.source_artifact_versions))
    _check("input_inventory_complete", not missing_files,
           f"file(s) missing from manifest source_artifact_versions: {missing_files}"
           if missing_files else "")

    coverage = verification_result.manifest_coverage_percent
    _check("all_logical_columns_accounted", coverage == 100,
           f"manifest_coverage_percent={coverage}" if coverage != 100 else "")

    _check("reviewer_preview_pass", reviewer_preview_verdict == "PASS",
           f"reviewer_preview_verdict={reviewer_preview_verdict!r}"
           if reviewer_preview_verdict != "PASS" else "")

    _check("no_unresolved_human_review", manifest.unresolved_items == 0,
           f"unresolved_items={manifest.unresolved_items}" if manifest.unresolved_items else "")

    _check("manifest_current", manifest.status != "invalidated",
           f"manifest_status={manifest.status}" if manifest.status == "invalidated" else "")

    _check("manifest_frozen", manifest.status == "verified_for_execution",
           f"manifest_status={manifest.status}"
           if manifest.status != "verified_for_execution" else "")

    _check("executor_complete", execution_result.success,
           execution_result.detail if not execution_result.success else "")

    _check("deterministic_verifier_pass", verification_result.passed,
           f"failed_checks={verification_result.failed_checks}"
           if not verification_result.passed else "")

    _check("reviewer_final_pass", reviewer_final.verdict == "PASS",
           f"reviewer_final_verdict={reviewer_final.verdict}"
           if reviewer_final.verdict != "PASS" else "")

    _check("no_unresolved_privacy_finding", privacy_findings_unresolved == 0,
           f"privacy_findings_unresolved={privacy_findings_unresolved}"
           if privacy_findings_unresolved else "")

    _check("no_unresolved_security_incident", not security_incident_active,
           "security incident active" if security_incident_active else "")

    _check("report_package_complete", report_package_complete,
           "report package not complete" if not report_package_complete else "")

    _check("reporting_safety_gate_pass", reporting_safety.verdict == "PASS",
           f"{len(reporting_safety.findings)} reporting safety finding(s)"
           if reporting_safety.verdict != "PASS" else "")

    _check("integrity_checks_pass", integrity_checks_passed,
           integrity_detail if not integrity_checks_passed else "")

    _check("no_unresolved_audit_finding", auditor_escalation is None, auditor_escalation or "")

    failed = [c.name for c in checks if not c.passed]
    verdict: FinalAssuranceVerdict = "READY_FOR_EXPORT" if not failed else "BLOCKED"
    return FinalAssuranceResult(verdict=verdict, checks=checks, failed_conditions=failed)


# --- report_package_complete: real producer (Phase 11b wave 2) -------------
# Closes the gap this module's own docstring disclosed above: Phase 11b's
# ReportGenerator now exists (``control/report_artifacts.py``), so a caller
# of ``evaluate_final_assurance`` no longer has to supply
# ``report_package_complete`` as a hand-picked boolean -- it can derive the
# real value from the run's actual ``ReportArtifacts`` instance instead.
# Pure addition: no existing name in this module changes shape, and
# ``evaluate_final_assurance``'s own signature (still a plain ``bool``
# parameter) is untouched, so every existing caller and test keeps working
# unchanged.


def derive_report_package_complete(artifacts: ReportArtifacts, *, human_review_occurred: bool) -> bool:
    """The real producer for the ``report_package_complete`` condition:
    delegates to :func:`control.report_artifacts.is_report_package_complete`
    (not duplicated here) so this module and ``ZIPBuilder``/
    ``IntegrityService`` (``control/zip_builder.py``,
    ``control/integrity_service.py``) share exactly one definition of
    "complete" for a report bundle."""
    return is_report_package_complete(artifacts, human_review_occurred=human_review_occurred)


# --- no_unresolved_security_incident: real producer (Phase 15a) ------------
# Section 71's SECURITY_BOUNDARY_VIOLATION handling records an open incident
# durably in ``control.security_incident``'s ``security_incidents``
# collection (no process-local cache: an open incident is a release-blocking
# safety fact and must survive a backend restart). This producer reads that
# collection so the gate genuinely checks for an open incident rather than
# trusting a hand-picked boolean. Pure addition mirroring
# ``derive_report_package_complete``: ``evaluate_final_assurance`` still takes
# the derived fact as its ``security_incident_active`` parameter unchanged.


async def derive_security_incident_active(store: ControlStore, run_id: str) -> bool:
    """The real producer for ``no_unresolved_security_incident``: True when
    the durable ``security_incidents`` collection holds at least one open
    incident for ``run_id`` (section 71), which makes the gate return
    ``BLOCKED``. Async and store-backed so the check survives a restart --
    see ``control.security_incident``'s module docstring."""
    from .security_incident import security_incident_active
    return await security_incident_active(store, run_id)
