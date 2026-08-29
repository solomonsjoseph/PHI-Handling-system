"""Phase R-a contract tests: the 9 absent section-84 records, FailureClass,
and the EvidenceVerificationResult rename.

These assert contract existence and exact field sets (extra="forbid") for
the records this wave adds to control/records.py. Field lists are taken from
docs/MASTER_ARCHITECTURE_V2.md sections 39, 43, 46, 50-53, 54, 63, 73, 105
and are asserted verbatim, not inferred.
"""
from __future__ import annotations

import pytest
from phi_core.control import records

# section 105 failure taxonomy: the 26 machine-readable failure classes.
FAILURE_CLASS_MEMBERS = (
    "INPUT_ERROR", "FILE_SAFETY_ERROR",
    "HEADER_SENSITIVE_CONTENT", "SOURCE_SENSITIVE_CONTENT",
    "SPECIALIST_INTERPRETATION_ERROR",
    "REGULATION_ERROR", "METHOD_ERROR", "EVIDENCE_ERROR",
    "CLASSIFICATION_ERROR", "REVIEW_CONFLICT",
    "HUMAN_INPUT_REQUIRED", "HUMAN_REVIEW_REQUIRED",
    "POLICY_BLOCK",
    "AUTHORIZATION_ERROR", "CROSS_RUN_ACCESS_ERROR", "PROVIDER_POLICY_ERROR",
    "EXECUTOR_CODE_ERROR", "EXECUTION_ERROR", "VERIFICATION_ERROR", "SANDBOX_ERROR",
    "OUTPUT_ERROR", "REPORTING_SAFETY_ERROR",
    "TRACE_SANITIZATION_ERROR", "LEARNING_SANITIZATION_ERROR", "CLEANUP_ERROR",
    "SECURITY_BOUNDARY_VIOLATION",
)


def test_failure_class_has_exactly_the_26_section_105_members():
    assert len(records.FailureClass.__args__) == 26
    assert set(records.FailureClass.__args__) == set(FAILURE_CLASS_MEMBERS)


# Exact field sets (excluding the inherited schema_version) for each absent
# record, taken verbatim from the cited master-spec sections.
_ABSENT_RECORD_FIELDS = {
    "EvidenceRecord": {
        "schema_version", "evidence_id", "evidence_type", "jurisdiction",
        "authority", "source", "title", "publisher", "publication_date",
        "effective_date", "retrieved_at", "content_hash", "source_material_ref",
        "interpretation_ref", "verification_status", "verified_by",
        "supports_decision_ids", "limitations",
    },
    "HumanReviewPacket": {
        "schema_version", "review_item_id", "safe_artifact_column_reference",
        "reason", "judge_proposal", "judge_rationale", "reviewer_concern",
        "evidence_refs", "previous_attempts", "remaining_uncertainty",
        "available_actions", "recommendation_if_appropriate",
    },
    "HumanDecision": {
        "schema_version", "decision_id", "action", "principal", "role",
        "decided_at", "version", "reviewer_principals_sha256",
    },
    "ExecutionTask": {
        "schema_version", "task_id", "run_id", "attempt_id", "manifest_id",
        "manifest_version", "input_artifact_version", "output_artifact_version",
        "decision_refs", "method_refs", "state", "created_at",
    },
    "ExecutionResult": {
        "schema_version", "task_id", "run_id", "attempt_id", "manifest_id",
        "manifest_version", "input_artifact_version", "output_artifact_version",
        "output_artifact_id", "success", "failure_class", "error_code",
        "detail", "created_at",
    },
    "LearningCase": {
        "schema_version", "case_id", "run_id", "source", "abstract",
        "sanitized", "phi_pii_scan_passed", "reconstruction_check_passed",
        "policy_validation_passed", "detail", "created_at",
    },
    "RunManifest": {
        "schema_version", "manifest_id", "run_id", "repository_commit",
        "application_version", "workflow_version", "agent_role_versions",
        "prompt_template_versions", "model_versions", "provider_versions",
        "run_privacy_policy_version", "method_registry_versions",
        "validator_versions", "transformation_hashes", "feature_flags",
        "trace_root_hash", "created_at",
    },
    "ReviewFinding": {
        "schema_version", "finding_id", "verdict", "detail", "file_id",
        "column", "kind",
    },
    "VerificationResult": {
        "schema_version", "verification_id", "task_id", "run_id", "attempt_id",
        "manifest_id", "manifest_version", "input_artifact_version",
        "output_artifact_version", "manifest_coverage_percent", "failed_checks",
        "passed", "detail", "created_at",
    },
}


def test_absent_records_exist_and_have_the_exact_field_set():
    for name, expected in _ABSENT_RECORD_FIELDS.items():
        model = getattr(records, name)
        assert set(model.model_fields) == expected, name
        # extra="forbid" is the module-wide ControlRecord contract.
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_fields["schema_version"].default == 1, name


def test_evidence_record_is_composed_and_evidence_source_claim_split_is_kept():
    # EvidenceRecord is the composed public contract (section 39); the
    # EvidenceSource/EvidenceClaim storage split remains internal.
    assert set(records.EvidenceSource.model_fields) == {
        "schema_version", "source_id", "claim_id", "url", "normalized_domain",
        "final_redirect_url", "publisher", "retrieved_at", "query", "tool",
        "tool_request_id", "provider_request_id", "content_hash", "locator",
        "snapshot_artifact_id", "verifications",
    }


def test_execution_verification_record_is_named_verification_result():
    # The execution-verification record owns the VerificationResult name; the
    # old evidence-dimension record is renamed EvidenceVerificationResult.
    assert hasattr(records, "VerificationResult")
    assert hasattr(records, "EvidenceVerificationResult")
    # The evidence record carries dimension/state/reason/checked_at; the
    # execution record carries the idempotency spine instead.
    assert set(records.EvidenceVerificationResult.model_fields) == {
        "schema_version", "dimension", "state", "reason", "checked_at",
    }


# --- Lifecycle single-sourcing (section 78) ---

SECTION_78_STATES = (
    "created", "sandbox_creating", "sandbox_ready", "uploading",
    "intake_validating", "awaiting_user_clarification", "specialists_running",
    "study_knowledge_ready", "judge_triage", "research_pending",
    "research_running", "classification_draft", "preview_review",
    "correction_required", "human_review_pending", "classification_verified",
    "executor_preparing", "code_validating", "executing", "execution_verifying",
    "final_review", "rewinding", "final_assurance", "package_building",
    "ready_for_export", "export_confirmed", "learning_sanitizing",
    "destroying", "session_destroyed", "blocked", "cancelled",
    "security_incident",
)
_TRANSITIONAL = ("pending", "running", "paused", "cancelling", "awaiting_human_review")
_D9_TERMINAL = ("complete", "partially_complete", "failed")


def test_run_state_is_the_section_78_lifecycle_plus_transitional_and_d9_terminal():
    import phi_core.control.records as r
    members = set(r.RunState.__args__)
    assert len(members) == 40
    assert set(SECTION_78_STATES) <= members
    assert set(_TRANSITIONAL) <= members
    assert set(_D9_TERMINAL) <= members


def test_run_lifecycle_states_is_the_single_source_in_workflow():
    from phi_core.control import workflow
    assert tuple(workflow.RUN_LIFECYCLE_STATES) == SECTION_78_STATES


def test_session_status_is_derived_from_run_state_not_an_independent_enum():
    from phi_core import models
    # Session.status is typed by RunState (the single lifecycle source), not
    # an independent 13-value SessionStatus Literal.
    assert hasattr(models, "Session")
    assert models.Session.model_fields["status"].annotation is records.RunState
    # The old SessionStatus enum is gone; only the display projection remains.
    assert callable(models.session_status_display)


# --- Partial-contract completion: TraceEvent and AgentManifest ---

SECTION_65_MISSING = (
    "agent_role", "attempt_id", "event_type", "input_artifact_refs",
    "output_artifact_refs", "tool_call_id", "decision", "policy_checks",
    "human_review_ref",
)

SECTION_12_CONTRACT = {
    "agent_id", "role", "permitted_senders", "permitted_recipients",
    "allowed_input_data_classes", "forbidden_input_data_classes",
    "allowed_tools", "forbidden_tools", "network_permissions",
    "child_agent_permissions", "expected_output_schema", "timeout_policy",
    "retry_policy", "acceptance_criteria",
}


def test_trace_event_has_the_9_missing_section_65_fields():
    fields = set(records.TraceEvent.model_fields)
    assert set(SECTION_65_MISSING) <= fields


def test_agent_manifest_carries_all_14_section_12_contract_fields():
    fields = set(records.AgentManifest.model_fields)
    assert SECTION_12_CONTRACT <= fields


def test_trace_event_fields_are_additive_only():
    # None of the pre-existing canonical TraceEvent fields were renamed away.
    for name in ("outcome", "retry_category", "status_text", "gateway_decision",
                 "agent_version", "artifact_ids", "evidence_ids", "review_ids",
                 "latency_ms", "usage", "ts", "phase", "agent"):
        assert name in records.TraceEvent.model_fields, name


# --- Pre-adds later waves need (fields/constants added now, unused this wave) ---


def test_sandbox_record_pre_added_fields():
    fields = records.SandboxRecord.model_fields
    assert "memory_limit_enforced" in fields
    assert "max_output_bytes" in fields
    assert fields["memory_limit_enforced"].default is False


def test_handoff_envelope_pre_added_fields():
    fields = records.HandoffEnvelope.model_fields
    assert "attempt_number" in fields and fields["attempt_number"].default == 1
    assert "correction_number" in fields and fields["correction_number"].default == 0


def test_pre_added_limit_constants_exist():
    from phi_core.control import limits
    assert limits.MAX_SANDBOX_OUTPUT_BYTES > 0
    assert isinstance(limits.HANDOFF_ATTEMPT_BUDGET, dict)
    assert limits.MAX_UNCERTAIN_HEADERS_PER_RUN == 50


# --- ReviewFinding wired into agents/reviewer.py ---


def test_reviewer_findings_carry_the_review_finding_shape(tmp_path):
    import asyncio
    import csv

    from phi_core.agents.reviewer import Reviewer
    from phi_core.control.testing import make_ctx

    src = tmp_path / "out.csv"
    with src.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name"])
        writer.writerow(["1", "Jane"])

    exports = {"f1": str(src)}
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "name", "action": "keep", "phi_category": "NONE", "citation": ""},
    ]
    operator_result = {
        "verdicts": [{
            "file_id": "f1", "column": "id", "violation": {"phi_category": "NONE", "citation": ""},
            "method": "keep", "checks": [], "verdict": "pass", "problem": "", "performed": "",
        }],
        "failed_file_ids": [],
        "status": "clean",
    }

    reviewer = Reviewer(make_ctx("Reviewer"))
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    missing = [f for f in result["findings"] if f["kind"] == "missing_operator_verdict"]
    assert len(missing) == 1
    finding = missing[0]
    # Backward-compatible dict shape (test_reviewer.py indexes by these keys)
    # plus the ReviewFinding contract fields (docs #43), proving the finding
    # was constructed as a ReviewFinding, not a hand-rolled dict.
    assert finding["file_id"] == "f1"
    assert finding["column"] == "name"
    assert finding["verdict"] == "CORRECTION_REQUIRED"
    assert "finding_id" in finding
    assert "schema_version" in finding


# --- Duplicate-schema debt marker (resolved in Phase 7) ---


@pytest.mark.xfail(
    strict=True,
    reason="Phase 7: Judge's output_schema is still the legacy 'judge_decisions' "
           "registration; ColumnDecision's typed contract uses 'column_decision'. "
           "Both sides of the duplicate-schema debt must move together.",
)
def test_judge_output_schema_matches_column_decision_contract():
    from phi_core.control.policy import MANIFESTS, OUTPUT_SCHEMAS
    assert MANIFESTS["Judge"].output_schema == "column_decision"
    assert "column_decision" in OUTPUT_SCHEMAS


# --- Versioning and invalidation coverage for all 8 Phase 1 records ---
# (RunPrivacyPolicy, ColumnDecision, StudyKnowledgePackage, RegulatoryFinding,
# MethodFinding, MethodRecord, VerifiedClassificationManifest, CleanupManifest
# -- the 8 records test_control_phase1_contracts.py pins the exact field set
# of. Before this wave only 3 (RunPrivacyPolicy frozen, StudyKnowledgePackage
# superseded_by, VerifiedClassificationManifest invalidated status) had an
# invalidation/versioning test anywhere in the suite; this closes the other 5.)


def test_run_privacy_policy_version_bump_is_a_new_policy_id_never_a_mutation():
    from phi_core.control.records import RunPrivacyPolicy
    from pydantic import ValidationError

    policy = RunPrivacyPolicy(run_id="r1", jurisdictions=["us"])
    with pytest.raises(ValidationError):
        policy.jurisdictions = ["eu"]
    revised = RunPrivacyPolicy(run_id="r1", jurisdictions=["eu"])
    assert revised.policy_id != policy.policy_id
    assert policy.jurisdictions == ["us"]


def test_column_decision_supersede_marks_status_and_ref_without_mutating_original():
    from phi_core.control.records import ColumnDecision

    decision = ColumnDecision(run_id="r1", file_id="f1", column_id="dob",
                               safe_display_name="DOB", operation="cap")
    assert decision.decision_status == "draft"
    superseded = decision.model_copy(update={"decision_status": "superseded", "superseded_by": "dec-v2"})
    assert superseded.decision_status == "superseded"
    assert superseded.superseded_by == "dec-v2"
    assert decision.decision_status == "draft"
    assert decision.superseded_by == ""


def test_study_knowledge_package_supersede_marks_stale():
    from phi_core.control.records import StudyKnowledgePackage

    package = StudyKnowledgePackage(run_id="r1")
    superseded = package.model_copy(update={"superseded_by": "pkg-v2"})
    assert superseded.superseded_by == "pkg-v2"
    assert package.superseded_by == ""


def test_regulatory_finding_has_no_in_place_invalidation_field_new_version_is_new_id():
    from phi_core.control.records import RegulatoryFinding

    assert "superseded_by" not in RegulatoryFinding.model_fields
    finding = RegulatoryFinding(run_id="r1", hipaa_category="A")
    revised = RegulatoryFinding(run_id="r1", hipaa_category="A", summary="revised")
    assert revised.finding_id != finding.finding_id


def test_method_finding_has_no_in_place_invalidation_field_new_version_is_new_id():
    from phi_core.control.records import MethodFinding

    assert "superseded_by" not in MethodFinding.model_fields
    finding = MethodFinding(run_id="r1", hipaa_category="A")
    revised = MethodFinding(run_id="r1", hipaa_category="A", summary="revised")
    assert revised.finding_id != finding.finding_id


def test_method_record_lifecycle_invalidates_to_deprecated_without_mutating_original():
    from phi_core.control.records import MethodRecord

    method = MethodRecord(hipaa_category="A", name="cap_age_90")
    assert method.lifecycle == "researched"
    deprecated = method.model_copy(update={"lifecycle": "deprecated"})
    assert deprecated.lifecycle == "deprecated"
    assert method.lifecycle == "researched"


def test_verified_classification_manifest_invalidation_transitions_status_in_place():
    from phi_core.control.records import VerifiedClassificationManifest

    manifest = VerifiedClassificationManifest(run_id="r1", preview_review_id="p1")
    assert manifest.status == "verified_for_execution"
    invalidated = manifest.model_copy(update={"status": "invalidated", "unresolved_items": 1})
    assert invalidated.status == "invalidated"
    # docs #49: invalidation transitions status in place, never mints a new id.
    assert invalidated.manifest_id == manifest.manifest_id


def test_cleanup_manifest_verification_status_transitions_pending_to_verified_or_failed():
    from phi_core.control.records import CleanupManifest

    manifest = CleanupManifest(run_id="r1")
    assert manifest.verification_status == "pending"
    verified = manifest.model_copy(update={"verification_status": "verified"})
    assert verified.verification_status == "verified"
    failed = manifest.model_copy(update={"verification_status": "failed", "failure_details": "sandbox not destroyed"})
    assert failed.verification_status == "failed"
    assert failed.failure_details == "sandbox not destroyed"