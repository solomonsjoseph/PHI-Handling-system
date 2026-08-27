"""Phase 1 exit criteria for the records added by the v3-reconciliation audit
(docs/MASTER_ARCHITECTURE_V2.md #84, local-only doc, never committed):
schema tests, serialization tests, versioning/invalidation tests, no
duplicate competing schemas (exact field-set drift guard, matching this
repo's existing test_control_records_policy.py convention)."""
from __future__ import annotations

import json

import pytest
from phi_core.control.records import (
    CleanupManifest,
    ColumnDecision,
    MethodFinding,
    MethodRecord,
    RegulatoryFinding,
    RunPrivacyPolicy,
    StudyKnowledgePackage,
    VerifiedClassificationManifest,
)
from pydantic import ValidationError

_RECORD_FIELDS = {
    RunPrivacyPolicy: {
        "schema_version", "policy_id", "run_id", "jurisdictions", "applicable_regimes",
        "intended_use", "intended_release_context", "recipient_context_if_relevant",
        "privacy_or_deidentification_path", "human_authorization_requirements",
        "policy_source_refs", "created_at",
    },
    ColumnDecision: {
        "schema_version", "decision_id", "run_id", "file_id", "dataset_part_id", "column_id",
        "safe_display_name", "semantic_meaning", "semantic_evidence_refs",
        "sensitivity_classification", "applicable_rule", "regulatory_evidence_refs",
        "operation", "method_id", "method_version", "method_parameters",
        "research_utility_reason", "plain_language_reason", "technical_rationale",
        "decision_status", "superseded_by", "created_at",
    },
    StudyKnowledgePackage: {
        "schema_version", "package_id", "run_id", "datasets", "columns", "schema_findings",
        "lexicon_findings", "instrument_findings", "evidence_refs", "conflicts",
        "unresolved_items", "created_at", "superseded_by",
    },
    RegulatoryFinding: {
        "schema_version", "finding_id", "run_id", "hipaa_category", "evidence_refs",
        "summary", "created_at",
    },
    MethodFinding: {
        "schema_version", "finding_id", "run_id", "hipaa_category", "recommended_method_id",
        "evidence_refs", "summary", "created_at",
    },
    MethodRecord: {
        "schema_version", "method_id", "hipaa_category", "name", "lifecycle",
        "evidence_refs", "parameters_schema", "created_at",
    },
    VerifiedClassificationManifest: {
        "schema_version", "manifest_id", "run_id", "source_artifact_versions",
        "decision_refs", "evidence_refs", "preview_review_id", "human_review_refs",
        "unresolved_items", "status", "created_at",
    },
    CleanupManifest: {
        "schema_version", "run_id", "cleanup_started_at", "cleanup_completed_at",
        "destroyed_categories", "retained_safe_categories", "credentials_revoked",
        "keys_destroyed", "sandbox_destroyed", "storage_sanitization_status",
        "verification_status", "failure_details",
    },
}

_REQUIRED = {
    RunPrivacyPolicy: {"run_id": "r1"},
    ColumnDecision: {
        "run_id": "r1", "file_id": "f1", "column_id": "dob",
        "safe_display_name": "DOB", "operation": "cap",
    },
    StudyKnowledgePackage: {"run_id": "r1"},
    RegulatoryFinding: {"run_id": "r1", "hipaa_category": "A"},
    MethodFinding: {"run_id": "r1", "hipaa_category": "A"},
    MethodRecord: {"hipaa_category": "A", "name": "cap_age_90"},
    VerifiedClassificationManifest: {"run_id": "r1", "preview_review_id": "p1"},
    CleanupManifest: {"run_id": "r1"},
}


@pytest.mark.parametrize("model,expected_fields", _RECORD_FIELDS.items())
def test_no_schema_drift_exact_field_set(model, expected_fields):
    assert set(model.model_fields) == expected_fields


@pytest.mark.parametrize("model", _RECORD_FIELDS)
def test_schema_instantiates_with_default_schema_version_1(model):
    instance = model(**_REQUIRED[model])
    assert instance.schema_version == 1


@pytest.mark.parametrize("model", _RECORD_FIELDS)
def test_serialization_round_trip(model):
    instance = model(**_REQUIRED[model])
    restored = model.model_validate(json.loads(instance.model_dump_json()))
    assert restored == instance


def test_run_privacy_policy_is_frozen():
    policy = RunPrivacyPolicy(run_id="r1", jurisdictions=["us"])
    with pytest.raises(ValidationError):
        policy.jurisdictions = ["eu"]


def test_verified_manifest_rejects_verified_status_with_unresolved_items():
    with pytest.raises(ValidationError):
        VerifiedClassificationManifest(
            run_id="r1", preview_review_id="p1", unresolved_items=1,
            status="verified_for_execution",
        )


def test_verified_manifest_allows_invalidated_with_unresolved_items():
    manifest = VerifiedClassificationManifest(
        run_id="r1", preview_review_id="p1", unresolved_items=1, status="invalidated",
    )
    assert manifest.unresolved_items == 1


def test_verified_manifest_invalidation_marks_status_not_mints_new_id():
    manifest = VerifiedClassificationManifest(run_id="r1", preview_review_id="p1")
    invalidated = manifest.model_copy(update={"status": "invalidated"})
    assert invalidated.manifest_id == manifest.manifest_id
    assert invalidated.status == "invalidated"


def test_method_record_lifecycle_defaults_to_researched_not_approved():
    # #38: "Research discovery does not grant execution permission."
    method = MethodRecord(hipaa_category="E", name="pseudonymize_mrn")
    assert method.lifecycle == "researched"


def test_study_knowledge_package_superseded_marks_stale():
    package = StudyKnowledgePackage(run_id="r1")
    assert package.superseded_by == ""
    superseded = package.model_copy(update={"superseded_by": "pkg-v2"})
    assert superseded.superseded_by == "pkg-v2"
