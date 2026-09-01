"""Tests for the reportlab-built PDF report surfaces (Phase 11b, docs
#58): each PDF is a real, non-empty, parseable file with a genuine
digital text layer -- verified by re-extracting through the same
``pypdf``-backed ``read_pdf`` path ZIPBuilder itself uses.
"""
from __future__ import annotations

from pathlib import Path

from phi_core.control.column_ledger import ColumnLedgerRow
from phi_core.control.records import (
    EvidenceRecord,
    ExecutionResult,
    HumanDecision,
    HumanReviewEvent,
    ResolutionEntry,
    RunManifest,
    VerificationResult,
    VerifiedClassificationManifest,
)
from phi_core.control.report_pdf import (
    build_audit_report_pdf,
    build_human_review_summary_pdf,
    build_technical_appendix_pdf,
)
from phi_core.control.reporting_safety import ReviewerFinalResult
from phi_core.file_readers import read_pdf


def _row(**overrides) -> ColumnLedgerRow:
    base = dict(
        dataset_file="dataset_a", column="Visit Date", what_it_means="Clinical visit date",
        sensitivity_classification="restricted_phi", initial_proposed_action="keep",
        final_approved_action="date_shift", what_we_did="Shifted dates by a per-subject random offset",
        why="Dates identify patients", applicable_rule="HIPAA Safe Harbor", method_used="Date Shift",
        method_version="1", reviewer_result="PASS", correction_made="No", human_review="No",
        human_decision="", executor_result="succeeded", final_verification="passed",
        decision_id="d1", evidence_references="ev1; ev2",
    )
    base.update(overrides)
    return ColumnLedgerRow(**base)


def _manifest(**overrides) -> VerifiedClassificationManifest:
    base = dict(run_id="r1", preview_review_id="pr1", unresolved_items=0, status="verified_for_execution")
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def test_audit_report_pdf_is_real_non_empty_and_parseable(tmp_path: Path):
    manifest = _manifest()
    execution_result = ExecutionResult(task_id="t1", run_id="r1", manifest_id=manifest.manifest_id, success=True)
    verification_result = VerificationResult(run_id="r1", passed=True, manifest_coverage_percent=100)
    path, text = build_audit_report_pdf(
        manifest=manifest, rows=[_row()], execution_result=execution_result,
        verification_result=verification_result, reviewer_final=ReviewerFinalResult(verdict="PASS"),
        path=tmp_path / "PHI_Handling_Audit_Report.pdf",
    )
    assert path.exists()
    assert path.stat().st_size > 0
    extracted = read_pdf(path)
    assert "PHI Handling Audit Report" in extracted
    assert manifest.run_id in extracted
    assert "PASSED" in extracted
    assert "Visit Date" in extracted
    assert text  # the returned plain-text summary is non-empty too


def test_audit_report_pdf_reports_did_not_pass_when_verification_failed(tmp_path: Path):
    manifest = _manifest()
    execution_result = ExecutionResult(task_id="t1", run_id="r1", manifest_id=manifest.manifest_id, success=True)
    verification_result = VerificationResult(run_id="r1", passed=False, failed_checks=["coverage_gap"],
                                               manifest_coverage_percent=80)
    _, text = build_audit_report_pdf(
        manifest=manifest, rows=[_row()], execution_result=execution_result,
        verification_result=verification_result, reviewer_final=ReviewerFinalResult(verdict="PASS"),
        path=tmp_path / "audit.pdf",
    )
    assert "DID NOT PASS" in text
    assert "80% column coverage" in text


def test_audit_report_pdf_handles_zero_rows_without_crashing(tmp_path: Path):
    manifest = _manifest()
    execution_result = ExecutionResult(task_id="t1", run_id="r1", manifest_id=manifest.manifest_id, success=True)
    verification_result = VerificationResult(run_id="r1", passed=True, manifest_coverage_percent=100)
    path, _ = build_audit_report_pdf(
        manifest=manifest, rows=[], execution_result=execution_result,
        verification_result=verification_result, reviewer_final=ReviewerFinalResult(verdict="PASS"),
        path=tmp_path / "audit_empty.pdf",
    )
    extracted = read_pdf(path)
    assert "No columns were processed" in extracted


def test_audit_report_pdf_escapes_markup_characters_without_crashing(tmp_path: Path):
    manifest = _manifest()
    execution_result = ExecutionResult(task_id="t1", run_id="r1", manifest_id=manifest.manifest_id, success=True)
    verification_result = VerificationResult(run_id="r1", passed=True, manifest_coverage_percent=100)
    row = _row(why='Contains <angle> & "quoted" characters')
    path, _ = build_audit_report_pdf(
        manifest=manifest, rows=[row], execution_result=execution_result,
        verification_result=verification_result, reviewer_final=ReviewerFinalResult(verdict="PASS"),
        path=tmp_path / "audit_escaped.pdf",
    )
    extracted = read_pdf(path)
    assert "Contains" in extracted and "angle" in extracted and "quoted" in extracted


def test_technical_appendix_pdf_includes_provenance_and_evidence(tmp_path: Path):
    manifest = _manifest()
    verification_result = VerificationResult(run_id="r1", passed=True, manifest_coverage_percent=100)
    run_manifest = RunManifest(run_id="r1", repository_commit="abc123", application_version="2.0.0")
    evidence = [EvidenceRecord(
        evidence_type="regulation", jurisdiction="us", authority="HHS", source="https://hhs.gov/x",
        title="HIPAA Safe Harbor", publisher="HHS", verification_status="VERIFIED",
    )]
    path, text = build_technical_appendix_pdf(
        manifest=manifest, rows=[_row()], evidence_records=evidence, run_manifest=run_manifest,
        verification_result=verification_result, path=tmp_path / "Technical_Appendix.pdf",
    )
    extracted = read_pdf(path)
    assert "Technical Appendix" in extracted
    assert "d1" in extracted  # decision_id
    assert "HIPAA Safe Harbor" in extracted  # evidence title
    assert "abc123" in extracted  # repository_commit
    assert "d1" in text


def test_technical_appendix_pdf_notes_when_no_evidence_recorded(tmp_path: Path):
    manifest = _manifest()
    verification_result = VerificationResult(run_id="r1", passed=True, manifest_coverage_percent=100)
    run_manifest = RunManifest(run_id="r1")
    path, _ = build_technical_appendix_pdf(
        manifest=manifest, rows=[_row()], evidence_records=[], run_manifest=run_manifest,
        verification_result=verification_result, path=tmp_path / "appendix_no_evidence.pdf",
    )
    extracted = read_pdf(path)
    assert "No evidence sources were recorded" in extracted


def test_human_review_summary_pdf_includes_decisions_and_resolutions(tmp_path: Path):
    events = [HumanReviewEvent(
        request_id="req1", run_id="r1", session_id="s1", workflow_version="1", task_id="t1",
        seq=1, client_event_id="c1", principal="dr.smith", kind="resolution", body_hash="h",
        resolutions=[ResolutionEntry(file_id="dataset_a", column="col_1", mode="approve", comment="looks right")],
    )]
    decisions = [HumanDecision(action="APPROVE", principal="dr.smith", role="reviewer")]
    path, text = build_human_review_summary_pdf(
        run_id="r1", human_review_events=events, human_decisions=decisions,
        path=tmp_path / "Human_Review_Summary.pdf",
    )
    extracted = read_pdf(path)
    assert "Human Review Summary" in extracted
    assert "dr.smith" in extracted
    assert "APPROVE" in extracted
    assert "looks right" in extracted
    assert "dr.smith" in text


def test_human_review_summary_pdf_notes_when_no_resolutions_recorded(tmp_path: Path):
    path, _ = build_human_review_summary_pdf(
        run_id="r1", human_review_events=[], human_decisions=[], path=tmp_path / "no_resolutions.pdf",
    )
    extracted = read_pdf(path)
    assert "No per-column resolutions were recorded" in extracted
