"""Focused contracts for Phase 2 control-plane records and capability policy."""
from __future__ import annotations

import asyncio

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.control import limits
from phi_core.control.policy import MANIFESTS, CapabilityDenied, CapabilityPolicy
from phi_core.control.records import (
    AgentManifest,
    ArtifactRecord,
    CapabilityGrant,
    EvidenceClaim,
    EvidenceSource,
    GateResult,
    HumanReviewEvent,
    HumanReviewRequest,
    OutboxEntry,
    PublicationPointer,
    ResolutionEntry,
    ResourceBudget,
    ResourceUsage,
    ScopeSpec,
    TraceEvent,
    VerificationResult,
    WorkflowRun,
    WorkItem,
)
from phi_core.control.store import MemoryControlStore
from pydantic import ValidationError

_RECORD_FIELDS = {
    ResourceBudget: {"schema_version", "wall_seconds", "max_attempts", "max_parallel", "max_tokens", "max_cost_usd", "max_tool_calls", "max_children", "max_input_bytes", "max_output_bytes", "max_artifact_bytes"},
    ResourceUsage: {"schema_version", "wall_seconds", "tokens", "cost_usd", "tool_calls", "children", "input_bytes", "output_bytes", "artifact_bytes"},
    OutboxEntry: {"schema_version", "entry_id", "kind", "payload", "created_at", "attempts", "last_error"},
    ScopeSpec: {"schema_version", "session", "run", "file_ids", "collections", "artifact_roots"},
    AgentManifest: {"schema_version", "agent", "manifest_version", "purpose", "task_types", "accepted_input_classes", "data_class_ceiling", "output_schema", "allowed_tools", "network_domains", "allowed_providers", "allowed_models", "scope", "reads", "writes", "budget", "allowed_child_task_types", "max_depth", "max_children"},
    WorkflowRun: {"run_id", "session_id", "schema_version", "workflow_version", "policy_version", "run_type", "state", "terminal_outcome", "node", "checkpoint", "checkpoint_version", "created_at", "started_at", "updated_at", "paused_at", "resumed_at", "cancelled_at", "completed_at", "cancel_requested", "cancel_requested_at", "correlation_id", "decision_version", "publication_generation", "hold", "event_seq", "budget", "usage", "opaque_map", "outbox"},
    WorkItem: {"task_id", "run_id", "session_id", "parent_task_id", "depth", "worker", "worker_version", "task_type", "state", "attempt", "max_attempts", "next_eligible_at", "lease_owner", "lease_expires_at", "heartbeat_at", "fence", "idempotency_key", "effect_key", "input_ref", "output_ref", "grant_id", "budget", "usage", "cancel_requested", "error_category", "correlation_id", "created_at", "claimed_at", "started_at", "updated_at", "completed_at", "outbox", "schema_version"},
    CapabilityGrant: {"grant_id", "run_id", "task_id", "agent", "manifest_version", "policy_version", "issued_at", "expires_at", "tools", "tools_used", "data_class_ceiling", "providers", "models", "provider", "model", "endpoint", "scope", "budget", "usage", "schema_version"},
    VerificationResult: {"schema_version", "dimension", "state", "reason", "checked_at"},
    EvidenceSource: {"schema_version", "source_id", "claim_id", "url", "normalized_domain", "final_redirect_url", "publisher", "retrieved_at", "query", "tool", "tool_request_id", "provider_request_id", "content_hash", "locator", "snapshot_artifact_id", "verifications"},
    EvidenceClaim: {"schema_version", "claim_id", "run_id", "task_id", "subject", "statement", "state", "required_state", "source_ids", "freshness_required_after", "contradicted_by", "created_at", "updated_at"},
    GateResult: {"schema_version", "gate_id", "run_id", "task_id", "gate", "gate_version", "status", "subject", "detail", "inputs_digest", "created_at"},
    ArtifactRecord: {"schema_version", "artifact_id", "session_id", "run_id", "producer_task_id", "scope", "type", "root", "rel_path", "sha256", "size_bytes", "state", "data_class", "retention_class", "parents", "gate_result_ids", "generation", "created_at", "promoted_at", "expires_at", "deleted_at", "delete_attempts", "delete_error", "hold"},
    HumanReviewRequest: {"schema_version", "request_id", "run_id", "session_id", "workflow_version", "task_id", "node", "reason_codes", "decision_version", "audit_version", "evidence_version", "required_role", "state", "created_at", "resolved_at"},
    ResolutionEntry: {"schema_version", "file_id", "column", "mode", "comment"},
    HumanReviewEvent: {"schema_version", "event_id", "request_id", "run_id", "session_id", "workflow_version", "task_id", "seq", "client_event_id", "principal", "submitted_at", "kind", "body_hash", "resolutions", "actual_knowledge_ack", "delivered_files", "decision_version", "audit_version", "result"},
    PublicationPointer: {"schema_version", "pointer_id", "session_id", "run_id", "generation", "artifact_ids", "gate_result_ids", "certified_at", "certified_by_task_id", "fence"},
    TraceEvent: {"schema_version", "event_id", "run_id", "seq", "session_id", "task_id", "parent_task_id", "depth", "attempt", "span_id", "agent", "agent_version", "workflow_version", "prompt_version", "policy_version", "rule_version", "manifest_version", "provider", "model", "endpoint", "provider_request_id", "usage", "cost_usd", "latency_ms", "tool_requested", "tool_policy_decision", "tool_executed", "tool_result_status", "input_class", "output_class", "evidence_ids", "gate_ids", "review_ids", "artifact_ids", "outcome", "retry_category", "error_correlation_id", "status_text", "egress_digest", "gateway_decision", "fence", "prev_hash", "hash", "ts"},
}


def test_d3_records_have_only_the_planned_fields() -> None:
    for record, expected_fields in _RECORD_FIELDS.items():
        assert set(record.model_fields) == expected_fields
        assert record.model_fields["schema_version"].default == 1


def test_policy_has_the_exact_manifest_roles_and_web_search_research_boundary() -> None:
    assert set(MANIFESTS) == {
        "Lexicon", "Schema", "Instrument", "Statute", "Praxis", "Judge", "Sentinel", "Executor", "Auditor", "Scout", "Ledger", "Ledger.Compare", "Ledger.Aggregate", "Herald", "Herald.Abstract", "Herald.Sections", "Manager", "Operator", "Reviewer", "CorpusResearcher",
        "Pipeline",  # Phase 4 step 2/4: the TaskService-enqueued top-level pipeline_run/pipeline_resume unit
    }
    web_enabled = {name for name, manifest in MANIFESTS.items() if manifest.allowed_tools}
    assert web_enabled == {"Statute", "Praxis", "Scout", "CorpusResearcher"}
    assert all(MANIFESTS[name].allowed_tools == {"web_search": 3} for name in web_enabled)


def test_policy_issues_an_immutable_manifest_bounded_grant() -> None:
    policy = CapabilityPolicy(LlmConfig(provider="anthropic", model="test"))
    grant = policy.issue_grant(run_id="run", task_id="task", agent="Scout", task_type="scout")

    assert grant.budget.max_attempts == 1
    assert grant.provider == "anthropic"
    with pytest.raises(ValidationError):
        grant.agent = "Judge"
    with pytest.raises(TypeError):
        grant.tools["web_search"] = 4



def test_environment_limits_cannot_widen_a_manifest_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_ATTEMPTS_PER_TASK", 100)
    policy = CapabilityPolicy(LlmConfig(provider="anthropic", model="test"))
    grant = policy.issue_grant(run_id="run", task_id="task", agent="Scout", task_type="scout")
    assert grant.budget.max_attempts == 1

def test_policy_fails_closed_for_unknown_widened_and_restricted_requests() -> None:
    policy = CapabilityPolicy(LlmConfig(provider="anthropic", model="test"))
    with pytest.raises(CapabilityDenied):
        policy.issue_grant(run_id="run", task_id="task", agent="Unknown", task_type="unknown")

    grant = policy.issue_grant(run_id="run", task_id="task", agent="Judge", task_type="judge")
    widened = grant.model_copy(update={"tools": {"web_search": 99}})
    with pytest.raises(CapabilityDenied):
        policy.check_tool(widened, "web_search")
    with pytest.raises(CapabilityDenied):
        policy.check_data_class(grant, "restricted_phi")
    with pytest.raises(CapabilityDenied):
        policy.check_tool(grant, "web_search")


def test_memory_store_preserves_documents_and_enforces_compare_and_set() -> None:
    async def exercise() -> None:
        store = MemoryControlStore()
        await store.insert("workflow_runs", {"run_id": "run", "state": "pending"})
        assert await store.compare_and_set(
            "workflow_runs",
            {"run_id": "run"},
            {"state": "running"},
            {"run_id": "run", "state": "complete"},
        ) is False
        assert await store.compare_and_set(
            "workflow_runs",
            {"run_id": "run"},
            {"state": "pending"},
            {"run_id": "run", "state": "running"},
        ) is True
        assert await store.get_one("workflow_runs", {"run_id": "run"}) == {"run_id": "run", "state": "running"}

    asyncio.run(exercise())
