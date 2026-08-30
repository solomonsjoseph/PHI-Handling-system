"""Tests for FinalAssuranceGate and ReportingSafetyGate (Phase 11a, docs
#57/#60): the deterministic, non-bypassable release gate and the
report-leakage scan it consumes. Every one of the fifteen
``FINAL_ASSURANCE_CONDITIONS`` has a dedicated failing-path test here, plus
the shared all-pass ``READY_FOR_EXPORT`` case and two dedicated
non-bypassability tests proving a mocked high-confidence Auditor signal can
never flip a genuinely failing condition.
"""
from __future__ import annotations

import asyncio

import openpyxl
from phi_core.agents.reviewer import Reviewer
from phi_core.control.final_assurance import (
    FINAL_ASSURANCE_CONDITIONS,
    ReportPackageContent,
    ReviewerFinalResult,
    evaluate_final_assurance,
    run_integrity_checks,
    run_reporting_safety_gate,
)
from phi_core.control.records import (
    ExecutionResult,
    VerificationResult,
    VerifiedClassificationManifest,
)
from phi_core.control.testing import make_ctx

# --- shared fixtures --------------------------------------------------------


def _manifest(**overrides) -> VerifiedClassificationManifest:
    base = dict(
        run_id="r1", preview_review_id="pr1",
        source_artifact_versions={"f1": 1, "f2": 1},
        decision_refs=["f1:id", "f2:ssn"], unresolved_items=0,
        status="verified_for_execution",
    )
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def _execution_result(**overrides) -> ExecutionResult:
    base = dict(task_id="t1", run_id="r1", manifest_id="m1", success=True)
    base.update(overrides)
    return ExecutionResult(**base)


def _verification_result(**overrides) -> VerificationResult:
    base = dict(run_id="r1", passed=True, failed_checks=[], manifest_coverage_percent=100)
    base.update(overrides)
    return VerificationResult(**base)


def _reviewer_final(**overrides) -> ReviewerFinalResult:
    base = dict(verdict="PASS")
    base.update(overrides)
    return ReviewerFinalResult(**base)


def _reporting_safety_pass() -> "object":
    return run_reporting_safety_gate(ReportPackageContent())


def _evaluate(**overrides):
    defaults = dict(
        expected_file_ids=["f1", "f2"],
        manifest=_manifest(),
        reviewer_preview_verdict="PASS",
        execution_result=_execution_result(),
        verification_result=_verification_result(),
        reviewer_final=_reviewer_final(),
        privacy_findings_unresolved=0,
        security_incident_active=False,
        report_package_complete=True,
        reporting_safety=_reporting_safety_pass(),
        integrity_checks_passed=True,
    )
    defaults.update(overrides)
    return evaluate_final_assurance(**defaults)


def _checks_by_name(result) -> dict[str, "object"]:
    return {c.name: c for c in result.checks}


# --- FinalAssuranceGate: the all-pass case ---------------------------------


def test_all_conditions_pass_yields_ready_for_export():
    result = _evaluate()
    assert result.verdict == "READY_FOR_EXPORT"
    assert result.failed_conditions == []
    assert {c.name for c in result.checks} == set(FINAL_ASSURANCE_CONDITIONS)
    assert all(c.passed for c in result.checks)


def test_fifteen_conditions_evaluated_every_call():
    """FINAL_ASSURANCE_CONDITIONS is the authoritative fixed set -- section
    57's fourteen verbatim conditions plus the one Auditor-migration
    addition, never eleven, never sixteen."""
    assert len(FINAL_ASSURANCE_CONDITIONS) == 15
    result = _evaluate()
    assert len(result.checks) == 15


# --- FinalAssuranceGate: one dedicated failing-path test per condition ----


def test_missing_input_file_blocks_input_inventory_complete():
    result = _evaluate(expected_file_ids=["f1", "f2", "f3"])
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["input_inventory_complete"].passed is False
    assert "f3" in by_name["input_inventory_complete"].detail
    assert "input_inventory_complete" in result.failed_conditions


def test_incomplete_coverage_blocks_all_logical_columns_accounted():
    result = _evaluate(verification_result=_verification_result(manifest_coverage_percent=80))
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["all_logical_columns_accounted"].passed is False
    assert "80" in by_name["all_logical_columns_accounted"].detail


def test_reviewer_preview_not_pass_blocks():
    result = _evaluate(reviewer_preview_verdict="HUMAN_REVIEW_REQUIRED")
    assert result.verdict == "BLOCKED"
    assert "reviewer_preview_pass" in result.failed_conditions


def test_unresolved_human_review_blocks():
    """VerifiedClassificationManifest's own closed-schema validator
    forbids status="verified_for_execution" together with
    unresolved_items != 0 (see records.py's
    _unresolved_items_blocks_verified), so the only way to construct a
    manifest with unresolved_items > 0 is status="invalidated" -- meaning
    no_unresolved_human_review can only ever fail alongside
    manifest_current/manifest_frozen, never in isolation. Documented, not
    hidden: this test constructs exactly that (real, schema-enforced)
    combined-failure state and asserts all three fail together."""
    result = _evaluate(manifest=_manifest(status="invalidated", unresolved_items=2))
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["no_unresolved_human_review"].passed is False
    assert "unresolved_items=2" in by_name["no_unresolved_human_review"].detail
    assert by_name["manifest_current"].passed is False
    assert by_name["manifest_frozen"].passed is False


def test_invalidated_manifest_blocks_manifest_current_and_frozen():
    """VerifiedClassificationManifest.status is a closed two-value Literal
    (``verified_for_execution``/``invalidated``): the only way to fail
    manifest_current is the same value that also fails manifest_frozen --
    there is no third, representable 'current but not frozen' state.
    Both conditions correctly co-fail on it."""
    result = _evaluate(manifest=_manifest(status="invalidated", unresolved_items=0))
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["manifest_current"].passed is False
    assert by_name["manifest_frozen"].passed is False
    assert {"manifest_current", "manifest_frozen"} <= set(result.failed_conditions)


def test_executor_incomplete_blocks():
    result = _evaluate(execution_result=_execution_result(success=False, detail="worker crashed"))
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["executor_complete"].passed is False
    assert by_name["executor_complete"].detail == "worker crashed"


def test_deterministic_verifier_fail_blocks():
    result = _evaluate(verification_result=_verification_result(
        passed=False, failed_checks=["f1:ssn:drop"], manifest_coverage_percent=100,
    ))
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["deterministic_verifier_pass"].passed is False
    assert "f1:ssn:drop" in by_name["deterministic_verifier_pass"].detail


def test_reviewer_final_not_pass_blocks():
    result = _evaluate(reviewer_final=_reviewer_final(verdict="FAIL"))
    assert result.verdict == "BLOCKED"
    assert "reviewer_final_pass" in result.failed_conditions


def test_unresolved_privacy_finding_blocks():
    result = _evaluate(privacy_findings_unresolved=3)
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["no_unresolved_privacy_finding"].passed is False
    assert "privacy_findings_unresolved=3" in by_name["no_unresolved_privacy_finding"].detail


def test_security_incident_blocks():
    result = _evaluate(security_incident_active=True)
    assert result.verdict == "BLOCKED"
    assert "no_unresolved_security_incident" in result.failed_conditions


def test_report_package_incomplete_blocks():
    """This condition has no live upstream producer until Phase 11b lands
    (documented in the module docstring and PHASE_STATUS.md), but the gate
    condition itself is fully real: both PASS (see the all-pass test above,
    which passes report_package_complete=True) and FAIL are genuinely
    exercised here."""
    result = _evaluate(report_package_complete=False)
    assert result.verdict == "BLOCKED"
    assert "report_package_complete" in result.failed_conditions


def test_reporting_safety_fail_blocks():
    unsafe = run_reporting_safety_gate(ReportPackageContent(report_text="SSN 123-45-6789"))
    assert unsafe.verdict == "FAIL"
    result = _evaluate(reporting_safety=unsafe)
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["reporting_safety_gate_pass"].passed is False
    assert "1 reporting safety finding" in by_name["reporting_safety_gate_pass"].detail


def test_integrity_check_fail_blocks():
    result = _evaluate(integrity_checks_passed=False, integrity_detail="artifact_a: sha256 mismatch")
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["integrity_checks_pass"].passed is False
    assert by_name["integrity_checks_pass"].detail == "artifact_a: sha256 mismatch"


def test_auditor_escalation_blocks():
    result = _evaluate(auditor_escalation="auditor_issues_verdict")
    assert result.verdict == "BLOCKED"
    by_name = _checks_by_name(result)
    assert by_name["no_unresolved_audit_finding"].passed is False
    assert by_name["no_unresolved_audit_finding"].detail == "auditor_issues_verdict"


# --- non-bypassability: model confidence can never override a FAIL --------


def test_model_confidence_cannot_override_a_failing_condition():
    """A genuinely failing deterministic condition (Executor did not
    complete) stays BLOCKED even alongside auditor_escalation=None -- the
    maximally 'confident, everything looks fine' signal Auditor's own
    deterministic gate (auditor_escalation_reason) can produce. There is no
    parameter on evaluate_final_assurance a raw confidence number could
    reach even if a caller tried to pass one."""
    result = _evaluate(
        execution_result=_execution_result(success=False, detail="worker crashed"),
        auditor_escalation=None,
    )
    assert result.verdict == "BLOCKED"
    assert "executor_complete" in result.failed_conditions
    assert "auditor_confidence" not in ",".join(result.failed_conditions)


def test_low_auditor_confidence_blocks_even_when_every_other_condition_passes():
    """Converse of the above: Auditor's confidence-floor escalation can add
    a block reason even when every one of the other fourteen conditions
    passes -- confidence can only ever tighten the gate, never loosen it."""
    result = _evaluate(auditor_escalation="auditor_confidence_below_floor:0.50")
    assert result.verdict == "BLOCKED"
    assert result.failed_conditions == ["no_unresolved_audit_finding"]


def test_evaluate_final_assurance_has_no_confidence_parameter():
    """Structural proof, not just a behavioral one: the gate's own
    signature carries no field named/shaped like a raw confidence score."""
    import inspect
    params = set(inspect.signature(evaluate_final_assurance).parameters)
    assert not any("confidence" in p for p in params)


# --- ReviewerFinalResult: genuine integration, not a hand-built mock ------


def test_reviewer_final_result_round_trips_a_real_reviewer_finalize_call():
    reviewer = Reviewer(make_ctx("Reviewer"))
    manifest = _manifest()
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f2", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    safe_output_metadata = {
        "column_counts": {"f1": {"decisions": 1, "verdicts": 1}, "f2": {"decisions": 1, "verdicts": 1}},
        "schema_valid": {"f1": True, "f2": True},
    }
    raw = asyncio.run(reviewer.finalize(
        manifest=manifest, execution_result=_execution_result(),
        verification_result=_verification_result(), decisions=decisions,
        human_decisions=[], safe_output_metadata=safe_output_metadata,
    ))
    assert raw["verdict"] == "PASS"
    wrapped = ReviewerFinalResult.from_finalize_dict(raw)
    assert wrapped.verdict == "PASS"
    assert wrapped.signal is None
    result = _evaluate(manifest=manifest, reviewer_final=wrapped)
    assert result.verdict == "READY_FOR_EXPORT"


# --- ReportingSafetyGate: positive-detection tests -------------------------


def test_reporting_safety_gate_clean_content_passes():
    content = ReportPackageContent(
        report_text="Study arm A showed no adverse events.",
        human_review_summary_text="One column required manual review; approved.",
        technical_appendix_text="Checksums verified for all four exports.",
        filenames=("PHI_Handling_Audit_Report.pdf", "Technical_Appendix.pdf"),
        safe_display_column_names=("participant_alias", "visit_date_year"),
        manifest_display_fields={"run_id": "r1", "status": "verified_for_execution"},
    )
    result = run_reporting_safety_gate(content)
    assert result.verdict == "PASS"
    assert result.findings == []
    assert len(result.surfaces_scanned) == 6


def test_reporting_safety_gate_catches_planted_ssn_in_report_text():
    result = run_reporting_safety_gate(ReportPackageContent(
        report_text="Participant contact on file: SSN 123-45-6789.",
    ))
    assert result.verdict == "FAIL"
    assert any(f.pattern_id == "SSN" and f.surface == "report_text" for f in result.findings)
    # the masked sample never carries the raw digits
    assert not any(f.sample == "123-45-6789" for f in result.findings)


def test_reporting_safety_gate_catches_planted_name_in_filenames():
    result = run_reporting_safety_gate(ReportPackageContent(
        filenames=("Jane Doe.csv",),
    ))
    assert result.verdict == "FAIL"
    assert any(f.pattern_id == "PRESIDIO_PERSON_NAME" and f.surface == "filenames"
               for f in result.findings)


def test_reporting_safety_gate_catches_planted_value_in_manifest_display_fields():
    result = run_reporting_safety_gate(ReportPackageContent(
        manifest_display_fields={"contact_email": "researcher@example.edu"},
    ))
    assert result.verdict == "FAIL"
    assert any(f.pattern_id == "EMAIL" and f.surface == "manifest_display_fields"
               for f in result.findings)


def test_reporting_safety_gate_catches_planted_ssn_in_workbook_cell(tmp_path):
    workbook_path = tmp_path / "What_Happened_to_Each_Column.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["column", "note"])
    wb.active.append(["dob", "kept in error"])
    wb.active.append(["ssn_backup", "111-22-3333"])
    wb.save(workbook_path)

    result = run_reporting_safety_gate(ReportPackageContent(
        workbook_paths=(str(workbook_path),),
    ))
    assert result.verdict == "FAIL"
    assert any(f.pattern_id == "SSN" and f.surface == "workbook_cells" for f in result.findings)


def test_reporting_safety_gate_empty_content_is_a_genuine_pass_not_a_skip():
    result = run_reporting_safety_gate(ReportPackageContent())
    assert result.verdict == "PASS"
    assert len(result.surfaces_scanned) == 6


# --- run_integrity_checks ---------------------------------------------------


def test_run_integrity_checks_all_match(tmp_path):
    import hashlib

    p = tmp_path / "artifact_a"
    p.write_bytes(b"deterministic PHI-handled bytes")
    expected_sha = hashlib.sha256(p.read_bytes()).hexdigest()

    ok, detail = run_integrity_checks({"artifact_a": (str(p), expected_sha)})
    assert ok is True
    assert detail == ""


def test_run_integrity_checks_detects_mismatch(tmp_path):
    p = tmp_path / "artifact_a"
    p.write_bytes(b"deterministic PHI-handled bytes")

    ok, detail = run_integrity_checks({"artifact_a": (str(p), "0" * 64)})
    assert ok is False
    assert "artifact_a" in detail and "mismatch" in detail


def test_run_integrity_checks_detects_unreadable_artifact(tmp_path):
    missing = tmp_path / "does_not_exist"
    ok, detail = run_integrity_checks({"artifact_a": (str(missing), "0" * 64)})
    assert ok is False
    assert "unreadable" in detail


def test_run_integrity_checks_vacuous_empty_input_passes():
    ok, detail = run_integrity_checks({})
    assert ok is True
    assert detail == ""
