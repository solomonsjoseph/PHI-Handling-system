"""Integration tests for ReportGenerator (Phase 11b wave 1, docs #58):
end-to-end generation of every section-58 artifact from a real (test-
fixture) run's typed records, into the shared ``ReportArtifacts`` contract
Packaging/Integration's ``ZIPBuilder``/``IntegrityService`` consume.
"""
from __future__ import annotations

import json
from pathlib import Path

import openpyxl
from phi_core.control.column_ledger import COLUMN_LEDGER_HEADERS
from phi_core.control.final_assurance import ReviewerFinalResult
from phi_core.control.records import (
    ColumnDecision,
    EvidenceRecord,
    ExecutionResult,
    HumanDecision,
    HumanReviewEvent,
    ResolutionEntry,
    ReviewFinding,
    RunManifest,
    VerificationResult,
    VerifiedClassificationManifest,
)
from phi_core.control.report_artifacts import ReportArtifacts, is_report_package_complete
from phi_core.control.report_generator import ReportGenerator, RunReportInputs
from phi_core.file_readers import read_pdf


def _decision(**overrides) -> ColumnDecision:
    base = dict(
        run_id="run-1", file_id="dataset_a", column_id="visit_date", safe_display_name="Visit Date",
        semantic_meaning="Clinical visit date", sensitivity_classification="restricted_phi",
        applicable_rule="HIPAA Safe Harbor", operation="date_shift", method_id="date_shift_v1",
        method_version=1, plain_language_reason="Dates can identify patients when combined with other data.",
        decision_status="verified", semantic_evidence_refs=["ev1"], regulatory_evidence_refs=["ev2"],
    )
    base.update(overrides)
    return ColumnDecision(**base)


def _build_inputs(tmp_path: Path, *, with_human_review: bool) -> RunReportInputs:
    manifest = VerifiedClassificationManifest(
        run_id="run-1", preview_review_id="preview-1",
        source_artifact_versions={"dataset_a": 1}, unresolved_items=0, status="verified_for_execution",
    )
    decisions = [
        _decision(),
        _decision(column_id="mrn", safe_display_name="MRN", operation="pseudonymize", method_id="pseudo_v1"),
        _decision(column_id="site", safe_display_name="Site", operation="keep", method_id="", method_version=0),
    ]
    execution_result = ExecutionResult(task_id="t1", run_id="run-1", manifest_id=manifest.manifest_id, success=True)
    verification_result = VerificationResult(
        run_id="run-1", manifest_id=manifest.manifest_id, passed=True, manifest_coverage_percent=100,
    )
    reviewer_final = ReviewerFinalResult(verdict="PASS")
    run_manifest = RunManifest(
        run_id="run-1", repository_commit="deadbeef", application_version="2.0.0",
        workflow_version="1.0", trace_root_hash="hash123",
    )
    evidence = [
        EvidenceRecord(evidence_id="ev1", evidence_type="regulation", jurisdiction="us", authority="HHS",
                        source="https://hhs.gov/x", title="HIPAA Safe Harbor", publisher="HHS",
                        verification_status="VERIFIED"),
        EvidenceRecord(evidence_id="ev2", evidence_type="method", jurisdiction="us", authority="NIST",
                        source="https://nist.gov/x", title="Date Shift Guidance", publisher="NIST",
                        verification_status="VERIFIED"),
    ]
    review_findings = [ReviewFinding(verdict="PASS", file_id="dataset_a", column="visit_date")]

    human_review_events: list[HumanReviewEvent] = []
    human_decisions: list[HumanDecision] = []
    if with_human_review:
        human_review_events = [HumanReviewEvent(
            request_id="req1", run_id="run-1", session_id="s1", workflow_version="1", task_id="t1",
            seq=1, client_event_id="c1", principal="dr.smith", kind="resolution", body_hash="h",
            resolutions=[ResolutionEntry(file_id="dataset_a", column="mrn", mode="approve", comment="ok")],
        )]
        human_decisions = [HumanDecision(action="APPROVE", principal="dr.smith", role="reviewer")]

    return RunReportInputs(
        run_id="run-1", manifest=manifest, decisions=decisions, execution_result=execution_result,
        verification_result=verification_result, reviewer_final=reviewer_final, run_manifest=run_manifest,
        evidence_records=evidence, review_findings=review_findings,
        human_review_events=human_review_events, human_decisions=human_decisions,
        method_names={"date_shift_v1": "Date Shift", "pseudo_v1": "Pseudonymize"},
    )


def test_generate_produces_every_artifact_without_human_review(tmp_path: Path):
    inputs = _build_inputs(tmp_path, with_human_review=False)
    artifacts = ReportGenerator(tmp_path / "out").generate(inputs)

    assert isinstance(artifacts, ReportArtifacts)
    assert artifacts.human_review_summary_pdf is None
    for field_name in (
        "audit_report_pdf", "column_ledger_xlsx", "technical_appendix_pdf",
        "evidence_manifest_json", "verification_manifest_json", "run_manifest_json", "checksums_sha256",
    ):
        path = getattr(artifacts, field_name)
        assert path is not None and path.exists() and path.stat().st_size > 0, field_name

    assert is_report_package_complete(artifacts, human_review_occurred=False) is True
    assert is_report_package_complete(artifacts, human_review_occurred=True) is False


def test_generate_includes_human_review_summary_when_review_occurred(tmp_path: Path):
    inputs = _build_inputs(tmp_path, with_human_review=True)
    artifacts = ReportGenerator(tmp_path / "out").generate(inputs)

    assert artifacts.human_review_summary_pdf is not None
    assert artifacts.human_review_summary_pdf.exists()
    assert is_report_package_complete(artifacts, human_review_occurred=True) is True

    extracted = read_pdf(artifacts.human_review_summary_pdf)
    assert "dr.smith" in extracted


def test_column_ledger_has_nineteen_columns_and_every_logical_column(tmp_path: Path):
    inputs = _build_inputs(tmp_path, with_human_review=False)
    artifacts = ReportGenerator(tmp_path / "out").generate(inputs)

    wb = openpyxl.load_workbook(artifacts.column_ledger_xlsx)
    values = list(wb.active.iter_rows(values_only=True))
    assert list(values[0]) == list(COLUMN_LEDGER_HEADERS)
    assert len(values[0]) == 19
    data_rows = values[1:]
    assert len(data_rows) == 3  # visit_date, mrn, site
    columns_seen = {row[1] for row in data_rows}
    assert columns_seen == {"Visit Date", "MRN", "Site"}


def test_audit_and_appendix_pdfs_are_non_empty_and_parseable(tmp_path: Path):
    inputs = _build_inputs(tmp_path, with_human_review=False)
    artifacts = ReportGenerator(tmp_path / "out").generate(inputs)

    audit_text = read_pdf(artifacts.audit_report_pdf)
    assert "PHI Handling Audit Report" in audit_text
    assert "run-1" in audit_text

    appendix_text = read_pdf(artifacts.technical_appendix_pdf)
    assert "Technical Appendix" in appendix_text
    assert "HIPAA Safe Harbor" in appendix_text


def test_json_manifests_round_trip_and_carry_no_raw_study_values(tmp_path: Path):
    inputs = _build_inputs(tmp_path, with_human_review=False)
    artifacts = ReportGenerator(tmp_path / "out").generate(inputs)

    evidence_payload = json.loads(artifacts.evidence_manifest_json.read_text(encoding="utf-8"))
    assert len(evidence_payload["evidence"]) == 2
    assert {e["evidence_id"] for e in evidence_payload["evidence"]} == {"ev1", "ev2"}

    verification_payload = json.loads(artifacts.verification_manifest_json.read_text(encoding="utf-8"))
    assert VerificationResult(**verification_payload) == inputs.verification_result

    run_manifest_payload = json.loads(artifacts.run_manifest_json.read_text(encoding="utf-8"))
    assert RunManifest(**run_manifest_payload) == inputs.run_manifest
    # docs #63: only version/hash/flag metadata, never a raw study value.
    assert "repository_commit" in run_manifest_payload
    assert "trace_root_hash" in run_manifest_payload


def test_checksums_file_lists_every_packaged_artifact(tmp_path: Path):
    inputs = _build_inputs(tmp_path, with_human_review=True)
    artifacts = ReportGenerator(tmp_path / "out").generate(inputs)

    checksums_text = artifacts.checksums_sha256.read_text(encoding="utf-8")
    for field_name in (
        "audit_report_pdf", "column_ledger_xlsx", "technical_appendix_pdf", "human_review_summary_pdf",
        "evidence_manifest_json", "verification_manifest_json", "run_manifest_json",
    ):
        path = getattr(artifacts, field_name)
        assert path.name in checksums_text, f"{path.name} missing from CHECKSUMS.sha256"
    # checksums_sha256 itself is never listed inside its own file.
    assert artifacts.checksums_sha256.name not in checksums_text
