"""Phase R-a contract tests: the 9 absent section-84 records, FailureClass,
and the EvidenceVerificationResult rename.

These assert contract existence and exact field sets (extra="forbid") for
the records this wave adds to control/records.py. Field lists are taken from
docs/MASTER_ARCHITECTURE_V2.md sections 39, 43, 46, 50-53, 54, 63, 73, 105
and are asserted verbatim, not inferred.
"""
from __future__ import annotations

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