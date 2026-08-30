"""ReportGenerator (Phase 11b wave 1, docs #58): produces every section-58
report artifact for one run from Phases 7-10's already-built typed
records, and returns the shared :class:`~.report_artifacts.ReportArtifacts`
contract Packaging/Integration's ``ZIPBuilder``/``IntegrityService``
consume.

This module owns file assembly only. It never calls an LLM, never scans
for reporting safety itself (``ZIPBuilder`` runs ``run_reporting_safety_gate``
over the packaged content before finalizing the ZIP, per docs #60), and
never invents cross-record joins the rest of the control plane does not
already support -- ``Executor Result``/``Final Verification`` in the
column ledger reflect the single per-run ``ExecutionResult``/
``VerificationResult`` records because those really are run/task-scoped
facts (docs #50-54), not per-column ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .column_ledger import build_column_ledger_rows, write_column_ledger_xlsx
from .final_assurance import ReviewerFinalResult
from .manifest_export import (
    export_evidence_manifest,
    export_run_manifest,
    export_verification_manifest,
    write_checksums,
)
from .records import (
    ColumnDecision,
    EvidenceRecord,
    ExecutionResult,
    HumanDecision,
    HumanReviewEvent,
    ReviewFinding,
    RunManifest,
    VerificationResult,
    VerifiedClassificationManifest,
)
from .report_artifacts import ReportArtifacts
from .report_pdf import build_audit_report_pdf, build_human_review_summary_pdf, build_technical_appendix_pdf

_AUDIT_REPORT_NAME = "PHI_Handling_Audit_Report.pdf"
_COLUMN_LEDGER_NAME = "What_Happened_to_Each_Column.xlsx"
_TECHNICAL_APPENDIX_NAME = "Technical_Appendix.pdf"
_HUMAN_REVIEW_SUMMARY_NAME = "Human_Review_Summary.pdf"
_EVIDENCE_MANIFEST_NAME = "Evidence_Manifest.json"
_VERIFICATION_MANIFEST_NAME = "Verification_Manifest.json"
_RUN_MANIFEST_NAME = "Run_Manifest.json"
_CHECKSUMS_NAME = "CHECKSUMS.sha256"


@dataclass
class RunReportInputs:
    """Everything one run's report bundle is built from -- every field is
    a real, already-typed record Phases 7-10 already produce. The caller
    (later: the live orchestration pipeline; today: tests and Packaging/
    Integration) is responsible for assembling these from the run's own
    state; this dataclass is the boundary contract, not a new store."""

    run_id: str
    manifest: VerifiedClassificationManifest
    decisions: Sequence[ColumnDecision]
    execution_result: ExecutionResult
    verification_result: VerificationResult
    reviewer_final: ReviewerFinalResult
    run_manifest: RunManifest
    evidence_records: Sequence[EvidenceRecord] = ()
    review_findings: Sequence[ReviewFinding] = ()
    human_review_events: Sequence[HumanReviewEvent] = ()
    human_decisions: Sequence[HumanDecision] = ()
    method_names: Mapping[str, str] = field(default_factory=dict)
    # None means "infer from human_review_events" (see ReportGenerator.generate).
    human_review_occurred: bool | None = None


class ReportGenerator:
    """Builds one run's full section-58 report bundle under ``output_dir``."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)

    def generate(self, inputs: RunReportInputs) -> ReportArtifacts:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        rows = build_column_ledger_rows(
            inputs.decisions,
            review_findings=inputs.review_findings,
            human_review_events=inputs.human_review_events,
            execution_result=inputs.execution_result,
            verification_result=inputs.verification_result,
            method_names=inputs.method_names,
        )

        ledger_path = write_column_ledger_xlsx(rows, self.output_dir / _COLUMN_LEDGER_NAME)

        audit_path, _ = build_audit_report_pdf(
            manifest=inputs.manifest, rows=rows, execution_result=inputs.execution_result,
            verification_result=inputs.verification_result, reviewer_final=inputs.reviewer_final,
            path=self.output_dir / _AUDIT_REPORT_NAME,
        )

        appendix_path, _ = build_technical_appendix_pdf(
            manifest=inputs.manifest, rows=rows, evidence_records=inputs.evidence_records,
            run_manifest=inputs.run_manifest, verification_result=inputs.verification_result,
            path=self.output_dir / _TECHNICAL_APPENDIX_NAME,
        )

        human_review_occurred = (
            inputs.human_review_occurred
            if inputs.human_review_occurred is not None
            else bool(inputs.human_review_events)
        )
        human_review_path: Path | None = None
        if human_review_occurred:
            human_review_path, _ = build_human_review_summary_pdf(
                run_id=inputs.run_id, human_review_events=inputs.human_review_events,
                human_decisions=inputs.human_decisions, path=self.output_dir / _HUMAN_REVIEW_SUMMARY_NAME,
            )

        evidence_path = export_evidence_manifest(inputs.evidence_records, self.output_dir / _EVIDENCE_MANIFEST_NAME)
        verification_path = export_verification_manifest(
            inputs.verification_result, self.output_dir / _VERIFICATION_MANIFEST_NAME,
        )
        run_manifest_path = export_run_manifest(inputs.run_manifest, self.output_dir / _RUN_MANIFEST_NAME)

        checksum_inputs = [ledger_path, audit_path, appendix_path, evidence_path, verification_path, run_manifest_path]
        if human_review_path is not None:
            checksum_inputs.append(human_review_path)
        checksums_path = write_checksums(checksum_inputs, self.output_dir / _CHECKSUMS_NAME)

        return ReportArtifacts(
            audit_report_pdf=audit_path,
            column_ledger_xlsx=ledger_path,
            technical_appendix_pdf=appendix_path,
            human_review_summary_pdf=human_review_path,
            evidence_manifest_json=evidence_path,
            verification_manifest_json=verification_path,
            run_manifest_json=run_manifest_path,
            checksums_sha256=checksums_path,
        )
