"""Versioned records for the PHI assurance control plane.

These models deliberately contain only the fields assigned by D3.  Mongo
serialization belongs to ``control.store`` so records remain transport-neutral.

Phase 1 addendum (target-architecture reconciliation, local reference doc
docs/MASTER_ARCHITECTURE_V2.md #84, never committed): a read-only fork audit
found this module already implements most of that document's "shared
architecture contracts" list under different names -- ArtifactRecord,
EvidenceSource/EvidenceClaim, WorkItem, HumanReviewRequest/Event, and
TraceEvent (already hash-chained, exceeding that document's own spec) are
canonical here and were not rebuilt. ``RunPrivacyPolicy``, ``ColumnDecision``,
``StudyKnowledgePackage``, ``RegulatoryFinding``, ``MethodFinding``,
``MethodRecord``, ``VerifiedClassificationManifest``, and ``CleanupManifest``
below are the types that audit found genuinely missing; they extend this
module's existing conventions (schema_version, frozen grant records, plain
mutable working records) rather than introducing a second schema.

Phase 3 addendum (target-architecture reconciliation, local reference doc
docs/MASTER_ARCHITECTURE_V2.md, never committed): the Phase 1 note above
said no HandoffEnvelope/HandoffResult record existed because Manager/
Manager sequences every agent today (ADR 0006). That is still
true of the running pipeline, but ``control.handoff.HandoffGateway`` now
builds the standalone validation module a later phase will wire agents
through, so ``HandoffEnvelope``/``HandoffResult`` below are the typed
request/response pair it uses. Neither is referenced from
``phi_core/agents/`` yet.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .workflow import RUN_LIFECYCLE_STATES


def _id() -> str:
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ControlRecord(BaseModel):
    """Base model that rejects schema drift in durable control records."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1


class FrozenControlRecord(ControlRecord):
    """Immutable control record used for authority-bearing grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)


RunState = Literal[
    *RUN_LIFECYCLE_STATES,
    # Transition-in-flight values the live pipeline still emits
    # (server.py/events.py), kept alongside the section-78 vocabulary.
    "pending", "running", "paused", "cancelling", "awaiting_human_review",
    # D9 terminal node names, which ``Manager.advance`` stamps
    # onto ``WorkflowRun.state`` directly (a terminal node's name is always
    # a valid RunState).
    "complete", "partially_complete", "failed",
]

TaskState = Literal[
    "ready",
    "leased",
    "running",
    "awaiting_acceptance",
    "accepted",
    "rejected",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
]

DataClass = Literal["public", "internal", "restricted_metadata", "restricted_phi"]
EvidenceState = Literal["UNKNOWN", "UNVERIFIED", "VERIFIED", "CONTRADICTED", "REJECTED"]
VerificationDimension = Literal[
    "retrieval_authenticity",
    "source_authority",
    "claim_support",
    "freshness",
    "contradiction",
]
ArtifactState = Literal[
    "provisional",
    "staged",
    "accepted",
    "rejected",
    "promoted",
    "superseded",
    "deletion_pending",
    "deleted",
    "legal_hold",
]

FailureClass = Literal[
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
]


class ResourceBudget(FrozenControlRecord):
    wall_seconds: float = 0.0
    max_attempts: int = 0
    max_parallel: int = 0
    max_tokens: int = 0
    max_cost_usd: float = 0.0
    max_tool_calls: int = 0
    max_children: int = 0
    max_input_bytes: int = 0
    max_output_bytes: int = 0
    max_artifact_bytes: int = 0


class ResourceUsage(FrozenControlRecord):
    wall_seconds: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    children: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    artifact_bytes: int = 0


class OutboxEntry(ControlRecord):
    entry_id: str = Field(default_factory=_id)
    kind: Literal[
        "enqueue",
        "trace",
        "artifact_register",
        "artifact_promote",
        "publication_pointer",
        "review_resume",
        "cancel_subtree",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    attempts: int = 0
    last_error: str = ""


class ScopeSpec(FrozenControlRecord):
    session: bool = False
    run: bool = False
    file_ids: frozenset[str] | None = None
    collections: frozenset[str] = Field(default_factory=frozenset)
    artifact_roots: frozenset[str] = Field(default_factory=frozenset)


class AgentManifest(FrozenControlRecord):
    agent: str
    manifest_version: str
    purpose: str
    task_types: frozenset[str]
    accepted_input_classes: frozenset[DataClass]
    data_class_ceiling: DataClass
    output_schema: str
    allowed_tools: Mapping[str, int]
    network_domains: frozenset[str]
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    scope: ScopeSpec
    reads: frozenset[str]
    writes: frozenset[str]
    budget: ResourceBudget
    allowed_child_task_types: frozenset[str]
    max_depth: int
    max_children: int
    # Phase R-a (docs #12 AgentContract): the 14 contract fields not already
    # present under this module's names. ``allowed_tools`` above already
    # covers the section-12 ``allowed_tools``; the rest are additive, with
    # defaults so ``policy._manifest`` (which constructs manifests under the
    # earlier field set) continues to build unchanged.
    agent_id: str = ""
    role: str = ""
    permitted_senders: frozenset[str] = Field(default_factory=frozenset)
    permitted_recipients: frozenset[str] = Field(default_factory=frozenset)
    allowed_input_data_classes: frozenset[DataClass] = Field(default_factory=frozenset)
    forbidden_input_data_classes: frozenset[DataClass] = Field(default_factory=frozenset)
    forbidden_tools: frozenset[str] = Field(default_factory=frozenset)
    network_permissions: frozenset[str] = Field(default_factory=frozenset)
    child_agent_permissions: frozenset[str] = Field(default_factory=frozenset)
    expected_output_schema: str = ""
    timeout_policy: str = ""
    retry_policy: str = ""
    acceptance_criteria: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("allowed_tools", mode="after")
    @classmethod
    def _freeze_allowed_tools(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        return MappingProxyType(dict(value))

    @field_serializer("allowed_tools")
    def _serialize_allowed_tools(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)


class WorkflowRun(ControlRecord):
    run_id: str = Field(default_factory=_id)
    session_id: str
    schema_version: int = 1
    workflow_version: str = ""
    policy_version: str = ""
    run_type: Literal["study", "intake", "maintenance", "warmup"] = "study"
    state: RunState = "pending"
    terminal_outcome: str = ""
    node: str = ""
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    checkpoint_version: int = 0
    created_at: str = Field(default_factory=_now)
    started_at: str = ""
    updated_at: str = Field(default_factory=_now)
    paused_at: str = ""
    resumed_at: str = ""
    cancelled_at: str = ""
    completed_at: str = ""
    cancel_requested: bool = False
    cancel_requested_at: str = ""
    correlation_id: str = ""
    decision_version: int = 0
    publication_generation: int = 0
    hold: str = ""
    event_seq: int = 0
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    opaque_map: dict[str, str] = Field(default_factory=dict)
    outbox: list[OutboxEntry] = Field(default_factory=list)
    # Phase 2C (v3 #68): rollup of the trace hash chain, written by
    # ``events.seal_range``/``seal_and_archive_range`` once a range is
    # sealed. Phase R-a correction: a separate ``RunManifest`` record now
    # exists below (docs #63) as the exported reproducibility projection of
    # this run, carrying its own copy of this hash; this field remains the
    # live, in-place value ``RunManifest`` is projected from.
    trace_root_hash: str = ""


class WorkItem(ControlRecord):
    task_id: str = Field(default_factory=_id)
    run_id: str
    session_id: str
    parent_task_id: str = ""
    depth: int = 0
    worker: str
    worker_version: str = ""
    task_type: str
    state: TaskState = "ready"
    attempt: int = 0
    max_attempts: int = 0
    next_eligible_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    fence: int = 0
    idempotency_key: str
    effect_key: str = ""
    input_ref: dict[str, Any] = Field(default_factory=dict)
    output_ref: dict[str, Any] = Field(default_factory=dict)
    grant_id: str = ""
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    cancel_requested: bool = False
    error_category: str = ""
    correlation_id: str = ""
    created_at: str = Field(default_factory=_now)
    claimed_at: str = ""
    started_at: str = ""
    updated_at: str = Field(default_factory=_now)
    completed_at: str = ""
    outbox: list[OutboxEntry] = Field(default_factory=list)


class CapabilityGrant(FrozenControlRecord):
    grant_id: str = Field(default_factory=_id)
    run_id: str
    task_id: str
    agent: str
    manifest_version: str
    policy_version: str
    issued_at: str = Field(default_factory=_now)
    expires_at: str = ""
    tools: Mapping[str, int] = Field(default_factory=dict)
    tools_used: Mapping[str, int] = Field(default_factory=dict)
    data_class_ceiling: DataClass
    providers: frozenset[str] = Field(default_factory=frozenset)
    models: frozenset[str] = Field(default_factory=frozenset)
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    scope: ScopeSpec = Field(default_factory=ScopeSpec)
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    usage: ResourceUsage = Field(default_factory=ResourceUsage)

    @field_validator("tools", "tools_used", mode="after")
    @classmethod
    def _freeze_tools(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        return MappingProxyType(dict(value))

    @field_serializer("tools", "tools_used")
    def _serialize_tools(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)


class EvidenceVerificationResult(ControlRecord):
    """One verification-dimension check on an ``EvidenceSource`` (D12)."""

    dimension: VerificationDimension
    state: EvidenceState
    reason: str = ""
    checked_at: str = Field(default_factory=_now)


class EvidenceSource(ControlRecord):
    source_id: str = Field(default_factory=_id)
    claim_id: str
    url: str = ""
    normalized_domain: str = ""
    final_redirect_url: str = ""
    publisher: str = ""
    retrieved_at: str = ""
    query: str = ""
    tool: str = ""
    tool_request_id: str = ""
    provider_request_id: str = ""
    content_hash: str = ""
    locator: str = Field(default="", max_length=500)
    snapshot_artifact_id: str = ""
    verifications: list[EvidenceVerificationResult] = Field(default_factory=list)


class EvidenceClaim(ControlRecord):
    claim_id: str = Field(default_factory=_id)
    run_id: str
    task_id: str
    subject: str
    statement: str
    state: EvidenceState = "UNKNOWN"
    required_state: EvidenceState = "VERIFIED"
    source_ids: list[str] = Field(default_factory=list)
    freshness_required_after: str = ""
    contradicted_by: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class GateResult(ControlRecord):
    gate_id: str = Field(default_factory=_id)
    run_id: str
    task_id: str
    gate: str
    gate_version: str
    status: Literal["pass", "fail", "blocked", "not_applicable"]
    subject: str = ""
    detail: str = ""
    inputs_digest: str = ""
    created_at: str = Field(default_factory=_now)


class ArtifactRecord(ControlRecord):
    artifact_id: str = Field(default_factory=_id)
    session_id: str
    run_id: str
    producer_task_id: str
    scope: Literal["run", "session", "shared"]
    type: str
    root: Literal["intake", "staging", "evidence", "reversal", "published", "cache"]
    rel_path: str
    sha256: str = ""
    size_bytes: int = 0
    state: ArtifactState = "provisional"
    data_class: DataClass
    retention_class: str
    parents: list[str] = Field(default_factory=list)
    gate_result_ids: list[str] = Field(default_factory=list)
    generation: int = 0
    created_at: str = Field(default_factory=_now)
    promoted_at: str = ""
    expires_at: str = ""
    rejection_reason: str = ""
    deleted_at: str = ""
    delete_attempts: int = 0
    delete_error: str = ""
    hold: str = ""


class HumanReviewRequest(ControlRecord):
    request_id: str = Field(default_factory=_id)
    run_id: str
    session_id: str
    workflow_version: str
    task_id: str
    node: str
    reason_codes: list[str] = Field(default_factory=list)
    decision_version: int = 0
    audit_version: str = ""
    evidence_version: str = ""
    required_role: str = ""
    state: Literal["open", "resolved", "superseded", "cancelled"] = "open"
    created_at: str = Field(default_factory=_now)
    resolved_at: str = ""
    # D13 ("only supersede closes it, recording principal, reason, and
    # policy_version"): populated only when state == "superseded", never
    # for a normal resolve via consume_review_event.
    superseded_by: str = ""
    superseded_reason: str = ""
    superseded_policy_version: str = ""


class ResolutionEntry(ControlRecord):
    file_id: str
    column: str
    mode: Literal["approve", "comment", "defer"]
    comment: str = ""


class HumanReviewEvent(ControlRecord):
    event_id: str = Field(default_factory=_id)
    request_id: str
    run_id: str
    session_id: str
    workflow_version: str
    task_id: str
    seq: int
    client_event_id: str
    principal: str
    submitted_at: str = Field(default_factory=_now)
    kind: Literal["resolution", "confirmation", "audit_confidence_confirmation", "defer", "supersede"]
    body_hash: str
    resolutions: list[ResolutionEntry] = Field(default_factory=list)
    actual_knowledge_ack: bool = False
    delivered_files: list[str] = Field(default_factory=list)
    decision_version: int = 0
    audit_version: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class PublicationPointer(ControlRecord):
    pointer_id: str = Field(default_factory=_id)
    session_id: str
    run_id: str
    generation: int = 0
    artifact_ids: list[str] = Field(default_factory=list)
    gate_result_ids: list[str] = Field(default_factory=list)
    certified_at: str = ""
    certified_by_task_id: str = ""
    fence: int = 0



class LearningProposal(ControlRecord):
    """D16: a proposed change to a prompt, manifest, or threshold.
    ``redacted_input_digest`` is a content hash of whatever evidence
    motivated the proposal -- never the evidence itself -- so a proposal
    record can be inspected without reconstructing restricted content."""

    proposal_id: str = Field(default_factory=_id)
    kind: str
    target: str
    baseline_version: str
    proposed_version: str
    redacted_input_digest: str
    rationale: str = ""
    created_at: str = Field(default_factory=_now)
    created_by_task_id: str = ""
    state: Literal["proposed", "evaluated", "approved", "rejected", "activated", "superseded"] = "proposed"


class LearningEvaluation(ControlRecord):
    evaluation_id: str = Field(default_factory=_id)
    proposal_id: str
    fixture_set: str
    adversarial: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    passed: bool = False
    evaluated_at: str = Field(default_factory=_now)


class LearningActivation(ControlRecord):
    activation_id: str = Field(default_factory=_id)
    proposal_id: str
    version: str
    approved_by: str
    approved_at: str = Field(default_factory=_now)
    rollout: Literal["shadow", "canary", "full"] = "shadow"
    monitor_status: Literal["pending", "passing", "tripped"] = "pending"
    activated_at: str = ""
    rolled_back_at: str = ""
    rollback_reason: str = ""


class TraceEvent(ControlRecord):
    event_id: str = Field(default_factory=_id)
    run_id: str
    seq: int
    session_id: str
    task_id: str = ""
    parent_task_id: str = ""
    depth: int = 0
    attempt: int = 0
    span_id: str = ""
    # Phase 7 (D15 agent_log migration): the AgentMessage-shaped fields
    # `session_agent_trace`/`session_bundle`/`corpus_study_benchmark` need
    # to reconstruct the "Agent trace" panel's full prompt/reply display
    # without a separate, unaudited `agent_log` collection. `parent_msg_id`
    # is deliberately distinct from `parent_task_id`: it links one logical
    # call's own request/reply/info chain (set by `Agent._log`'s caller),
    # not the WorkItem hierarchy, and can point at a message emitted by
    # the same task_id (Praxis's 17 per-category calls all share one
    # task_id but chain through distinct parent_msg_id values).
    phase: str = ""
    direction: str = ""
    parent_msg_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    agent: str = ""
    agent_version: str = ""
    workflow_version: str = ""
    prompt_version: str = ""
    policy_version: str = ""
    rule_version: str = ""
    manifest_version: str = ""
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    provider_request_id: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    tool_requested: str = ""
    tool_policy_decision: str = ""
    tool_executed: str = ""
    tool_result_status: str = ""
    input_class: DataClass
    output_class: DataClass
    evidence_ids: list[str] = Field(default_factory=list)
    gate_ids: list[str] = Field(default_factory=list)
    review_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    outcome: str = ""
    retry_category: str = ""
    error_correlation_id: str = ""
    status_text: str = ""
    egress_digest: str = ""
    gateway_decision: str = ""
    fence: int = 0
    prev_hash: str = ""
    hash: str = ""
    ts: str = Field(default_factory=_now)
    # Phase 2C (v3 #65 schema-gap closure): genuinely missing fields, added
    # rather than renaming any field above -- this codebase's existing
    # names (outcome/retry_category/gateway_decision/agent_version/etc.)
    # stay canonical; see the module docstring's Phase 1 addendum.
    trace_id: str = ""
    model_version: str = ""
    prompt_template_version: str = ""
    handoff_id: str = ""
    sanitized_rationale: str = ""
    alternatives_considered: list[str] = Field(default_factory=list)
    authorization_result: str = ""
    failure_class: str = ""
    error_code: str = ""
    correction_number: int = 0
    previous_state: str = ""
    new_state: str = ""
    # Phase R-a (v3 #65 remaining gaps): the last nine section-65 field
    # names with no existing equivalent. Additive only, like the Phase 2C
    # block above; no existing field is renamed (and none is renamed to the
    # still-Experimental OpenTelemetry gen_ai.* convention).
    agent_role: str = ""
    attempt_id: str = ""
    event_type: str = ""
    input_artifact_refs: list[str] = Field(default_factory=list)
    output_artifact_refs: list[str] = Field(default_factory=list)
    tool_call_id: str = ""
    decision: str = ""
    policy_checks: list[str] = Field(default_factory=list)
    human_review_ref: str = ""


class RunPrivacyPolicy(FrozenControlRecord):
    """One run's frozen privacy/de-identification policy (docs #15).

    ``version`` bump means a new policy_id, not an in-place mutation --
    frozen like every other authority-bearing grant record in this module.
    """

    policy_id: str = Field(default_factory=_id)
    run_id: str
    jurisdictions: list[str] = Field(default_factory=list)
    applicable_regimes: list[str] = Field(default_factory=list)
    intended_use: str = ""
    intended_release_context: str = ""
    recipient_context_if_relevant: str = ""
    privacy_or_deidentification_path: str = ""
    human_authorization_requirements: list[str] = Field(default_factory=list)
    policy_source_refs: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)


ColumnOperation = Literal[
    "keep", "drop", "pseudonymize", "shift", "date_shift",
    "jitter", "generalize", "cap", "redact", "other_approved_action",
]


class ColumnDecision(ControlRecord):
    """Judge's typed per-column output (docs #41), replacing the current
    ``output_schema="judge_decisions"`` plain dict."""

    decision_id: str = Field(default_factory=_id)
    run_id: str
    file_id: str
    dataset_part_id: str = ""
    column_id: str
    safe_display_name: str
    semantic_meaning: str = ""
    semantic_evidence_refs: list[str] = Field(default_factory=list)
    sensitivity_classification: str = ""
    applicable_rule: str = ""
    regulatory_evidence_refs: list[str] = Field(default_factory=list)
    operation: ColumnOperation
    method_id: str = ""
    method_version: int = 0
    method_parameters: dict[str, Any] = Field(default_factory=dict)
    research_utility_reason: str = ""
    plain_language_reason: str = ""
    technical_rationale: str = ""
    decision_status: Literal["draft", "under_review", "correction_required", "verified", "superseded"] = "draft"
    superseded_by: str = ""
    created_at: str = Field(default_factory=_now)


class StudyKnowledgePackage(ControlRecord):
    """Unifies Schema/Lexicon/Instrument findings into one versioned package
    (docs #28), instead of concatenating specialist prose into one prompt."""

    package_id: str = Field(default_factory=_id)
    run_id: str
    datasets: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    schema_findings: list[dict[str, Any]] = Field(default_factory=list)
    lexicon_findings: list[dict[str, Any]] = Field(default_factory=list)
    instrument_findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    superseded_by: str = ""


class RegulatoryFinding(ControlRecord):
    """Statute's typed output (docs #35), replacing its current plain dict."""

    finding_id: str = Field(default_factory=_id)
    run_id: str
    hipaa_category: str
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=_now)


class MethodFinding(ControlRecord):
    """Praxis's typed output (docs #37), replacing its current plain dict."""

    finding_id: str = Field(default_factory=_id)
    run_id: str
    hipaa_category: str
    recommended_method_id: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=_now)


MethodLifecycle = Literal["researched", "candidate", "validated", "approved", "deprecated"]


class MethodRecord(ControlRecord):
    """MethodRegistry entry (docs #38): research discovery alone never grants
    execution permission -- only ``lifecycle == "approved"`` does."""

    method_id: str = Field(default_factory=_id)
    hipaa_category: str
    name: str
    lifecycle: MethodLifecycle = "researched"
    evidence_refs: list[str] = Field(default_factory=list)
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


class VerifiedClassificationManifest(ControlRecord):
    """Frozen once Judge + Reviewer Preview + Human Review all clear (docs
    #49). Not a ``FrozenControlRecord``: ``status`` transitions to
    ``invalidated`` in place when an upstream artifact changes (docs #30),
    rather than minting a new manifest_id for the same decision set."""

    manifest_id: str = Field(default_factory=_id)
    run_id: str
    source_artifact_versions: dict[str, int] = Field(default_factory=dict)
    decision_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    preview_review_id: str
    human_review_refs: list[str] = Field(default_factory=list)
    unresolved_items: int = 0
    status: Literal["verified_for_execution", "invalidated"] = "verified_for_execution"
    created_at: str = Field(default_factory=_now)

    @field_validator("status", mode="after")
    @classmethod
    def _unresolved_items_blocks_verified(cls, value: str, info: Any) -> str:
        unresolved = info.data.get("unresolved_items", 0)
        if value == "verified_for_execution" and unresolved:
            raise ValueError("cannot be verified_for_execution with unresolved_items != 0")
        return value


SandboxState = Literal["active", "destroyed", "destroy_failed"]


class SandboxRecord(ControlRecord):
    """Per-run isolated raw-processing workspace (docs #21, local reference
    doc, never committed): what ``control.sandbox.SandboxManager`` hands
    back from ``create_sandbox``/``destroy_sandbox``. Not a
    ``FrozenControlRecord``: ``state``/``destroyed_at`` update in place as
    the same sandbox is torn down, rather than minting a new id per
    transition."""

    sandbox_id: str = Field(default_factory=_id)
    run_id: str
    workspace_path: str
    state: SandboxState = "active"
    max_cpu_seconds: int
    max_memory_bytes: int
    max_wall_seconds: int
    network_denied: bool = True
    created_at: str = Field(default_factory=_now)
    destroyed_at: str = ""
    failure_details: str = ""
    # Phase R-a pre-adds (unused this wave; this module closes after it):
    # whether the memory ceiling is actually enforced, and the per-run
    # sandbox output byte ceiling (distinct from the task-queue
    # ``MAX_OUTPUT_BYTES`` budget).
    memory_limit_enforced: bool = False
    max_output_bytes: int = 0


class CleanupManifest(ControlRecord):
    """Session-destruction verification record (docs #77): never transitions
    a run to SESSION_DESTROYED until this reports ``verified``."""

    run_id: str
    cleanup_started_at: str = Field(default_factory=_now)
    cleanup_completed_at: str = ""
    destroyed_categories: list[str] = Field(default_factory=list)
    retained_safe_categories: list[str] = Field(default_factory=list)
    credentials_revoked: bool = False
    keys_destroyed: bool = False
    sandbox_destroyed: bool = False
    storage_sanitization_status: Literal["pending", "complete", "failed"] = "pending"
    verification_status: Literal["pending", "verified", "failed"] = "pending"
    failure_details: str = ""


# Phase 2E (target-architecture reconciliation, local reference doc
# docs/MASTER_ARCHITECTURE_V2.md section 7 "headers and variable names" and
# section 22 "SourceProjectionGateway", never committed): the audit found no
# header-specific sensitivity classification and no staged
# extraction/classification/safety-check/normalization/projection pipeline
# for study-derived content. These two records are the typed output of that
# gap's deterministic gate/pipeline (``control.source_projection``); neither
# is wired into the live agent pipeline yet (later phase's scope).

SourceContentType = Literal["header", "dictionary", "mapping", "form", "comment"]


class HeaderClassification(ControlRecord):
    """One header's disposition from the deterministic HEADER SAFETY GATE
    (v3 section 7). ``sensitive``/``uncertain`` headers are never projected
    to the agent plane under their literal text; ``opaque_token`` (minted by
    ``control.opaque.OpaqueMap`` with kind ``"header"``) is what Schema sees
    instead, matching the doc's own ``SENSITIVE_HEADER_004`` example."""

    header: str
    disposition: Literal["safe", "sensitive", "uncertain"]
    reasons: list[str] = Field(default_factory=list)
    opaque_token: str = ""


class SourceProjectionResult(ControlRecord):
    """One document's pass through the staged SourceProjectionGateway
    pipeline (v3 section 22): classification, PHI/PII/secret safety check,
    untrusted-content normalization, and the resulting purpose-specific
    projection. ``blocked`` is set (and ``projected_text`` left empty) when
    an anchored credential/secret shape survives the PHI scrub -- that
    content is never handed to an agent, full stop, regardless of
    ``content_type``."""

    content_type: SourceContentType
    run_id: str
    disposition: Literal["safe", "sensitive", "uncertain"]
    reasons: list[str] = Field(default_factory=list)
    projected_text: str = ""
    blocked: bool = False


# Phase 3 (target-architecture reconciliation, local reference doc
# docs/MASTER_ARCHITECTURE_V2.md, never committed): the typed request/
# response pair for ``control.handoff.HandoffGateway``, the Phase 1
# docstring addendum's deferred item. The gateway itself, the allowed-
# topology table, and the per-edge payload schemas live in
# ``control/handoff.py`` -- these two records are the durable shape a
# handoff attempt and its verdict take, matching this module's existing
# split (canonical record here, enforcement logic in its own leaf module).

HandoffReasonCode = Literal[
    "",
    "sender_unregistered",
    "recipient_unregistered",
    "topology_blocked",
    "cross_run_reference",
    "data_class_forbidden",
    "not_minimum_necessary",
    "residual_phi_detected",
    "secret_detected",
    "tool_not_granted",
    "payload_schema_invalid",
]


class HandoffEnvelope(ControlRecord):
    """One agent-to-agent handoff request. ``payload`` is validated against
    the declared schema for ``(sender, recipient)`` -- never accepted as
    free-form dict past that boundary."""

    handoff_id: str = Field(default_factory=_id)
    run_id: str
    sender: str
    recipient: str
    data_class: DataClass
    requested_tool: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    # Phase R-a pre-adds: the per-edge attempt and correction counters the
    # retry/correction budget (docs #48) enforces.
    attempt_number: int = 1
    correction_number: int = 0


class HandoffResult(ControlRecord):
    """``HandoffGateway.handoff``'s verdict. Denials are ordinary return
    values, never exceptions -- only a programming error (a malformed
    envelope, a store failure) raises."""

    handoff_id: str
    run_id: str
    sender: str
    recipient: str
    allowed: bool
    reason_code: HandoffReasonCode = ""
    detail: str = ""
    trace_event_id: str = ""


# --- Phase R-a: the nine section-84 contracts the reconciliation audit ---
# found genuinely absent. Field lists are taken verbatim from the cited
# master-spec sections; each extends the module's ControlRecord convention
# (schema_version, extra="forbid").


class EvidenceRecord(ControlRecord):
    """The composed evidence-registry read model (docs #39). The
    ``EvidenceSource``/``EvidenceClaim`` split above stays the storage
    shape; this record is the registry's public contract. ``source`` and
    ``interpretation_ref`` stay separate (``SOURCE`` vs ``MODEL
    INTERPRETATION``) as the spec requires."""

    evidence_id: str = Field(default_factory=_id)
    evidence_type: str
    jurisdiction: str
    authority: str
    source: str
    title: str
    publisher: str
    publication_date: str = ""
    effective_date: str = ""
    retrieved_at: str = ""
    content_hash: str = ""
    source_material_ref: str = ""
    interpretation_ref: str = ""
    verification_status: EvidenceState = "UNKNOWN"
    verified_by: str = ""
    supports_decision_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


ReviewVerdict = Literal["PASS", "CORRECTION_REQUIRED", "HUMAN_REVIEW_REQUIRED"]


class ReviewFinding(ControlRecord):
    """Reviewer Preview's typed finding (docs #43). ``verdict`` is the
    three-value output; ``file_id``/``column``/``kind``/``detail`` carry
    the affected decision and reason (docs #44), replacing the plain dicts
    ``agents/reviewer.py`` used to build."""

    finding_id: str = Field(default_factory=_id)
    verdict: ReviewVerdict
    detail: str = ""
    file_id: str = ""
    column: str = ""
    kind: str = ""


class HumanReviewPacket(ControlRecord):
    """The full human-review packet (docs #46) handed to an authorized
    human. Coexists with ``HumanReviewRequest`` (a thin routing record)."""

    review_item_id: str = Field(default_factory=_id)
    safe_artifact_column_reference: str
    reason: str
    judge_proposal: str = ""
    judge_rationale: str = ""
    reviewer_concern: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    previous_attempts: list[str] = Field(default_factory=list)
    remaining_uncertainty: str = ""
    available_actions: list[str] = Field(default_factory=list)
    recommendation_if_appropriate: str = ""


HumanDecisionAction = Literal[
    "APPROVE", "MODIFY", "OVERRIDE", "REQUEST_MORE_EVIDENCE", "DEFER", "HOLD",
]


class HumanDecision(ControlRecord):
    """The typed content of an authoritative human decision (docs #46).
    ``HumanReviewEvent`` remains the append-only storage row; this record is
    its typed ``result`` payload. ``reviewer_principals_sha256`` is the
    sha256 of the raw ``REVIEWER_PRINCIPALS`` env-var value as it stood at
    decision time, which makes the authorization reconstructible at audit."""

    decision_id: str = Field(default_factory=_id)
    action: HumanDecisionAction
    principal: str
    role: str = ""
    decided_at: str = Field(default_factory=_now)
    version: int = 1
    reviewer_principals_sha256: str = ""


ExecutionTaskState = Literal["submitted", "running", "succeeded", "failed", "cancelled"]


class ExecutionTask(ControlRecord):
    """The validated operation plan Executor submits to the raw-data worker
    (docs #50). The idempotency spine (docs #53: task_id/attempt_id/...
    output_artifact_version) makes a retry duplicate-protected."""

    task_id: str = Field(default_factory=_id)
    run_id: str
    attempt_id: str = ""
    manifest_id: str
    manifest_version: str = ""
    input_artifact_version: int = 0
    output_artifact_version: int = 0
    decision_refs: list[str] = Field(default_factory=list)
    method_refs: list[str] = Field(default_factory=list)
    state: ExecutionTaskState = "submitted"
    created_at: str = Field(default_factory=_now)


class ExecutionResult(ControlRecord):
    """The structured result the raw-data worker returns to Executor
    (docs #50-53). Carries the same idempotency spine as ``ExecutionTask``."""

    task_id: str
    run_id: str
    attempt_id: str = ""
    manifest_id: str
    manifest_version: str = ""
    input_artifact_version: int = 0
    output_artifact_version: int = 0
    output_artifact_id: str = ""
    success: bool = False
    failure_class: str = ""
    error_code: str = ""
    detail: str = ""
    created_at: str = Field(default_factory=_now)


class VerificationResult(ControlRecord):
    """DeterministicVerifier's post-execution verdict (docs #54). The
    evidence-dimension record of the same name is ``EvidenceVerificationResult``
    above; this record owns the execution-verification meaning."""

    verification_id: str = Field(default_factory=_id)
    task_id: str = ""
    run_id: str
    attempt_id: str = ""
    manifest_id: str = ""
    manifest_version: str = ""
    input_artifact_version: int = 0
    output_artifact_version: int = 0
    manifest_coverage_percent: int = 100
    failed_checks: list[str] = Field(default_factory=list)
    passed: bool = False
    detail: str = ""
    created_at: str = Field(default_factory=_now)


LearningCaseSource = Literal[
    "judge_mistake", "reviewer_correction", "human_decision",
    "research_failure", "method_failure", "executor_failure",
    "verification_failure", "successful_recovery",
]


class LearningCase(ControlRecord):
    """A sanitized learning candidate (docs #73) that feeds the existing
    ``LearningProposal``/``LearningEvaluation``/``LearningActivation``
    pipeline, not a replacement for it. Only validated sources may produce
    a candidate; the pipeline stage flags record the sanitize/scan/reconstruct
    checks the spec's candidate flow requires before persistence."""

    case_id: str = Field(default_factory=_id)
    run_id: str
    source: LearningCaseSource
    abstract: str = ""
    sanitized: bool = False
    phi_pii_scan_passed: bool = False
    reconstruction_check_passed: bool = False
    policy_validation_passed: bool = False
    detail: str = ""
    created_at: str = Field(default_factory=_now)


class RunManifest(ControlRecord):
    """The exported reproducibility projection of a ``WorkflowRun``
    (docs #63): safe non-PHI run configuration metadata plus the trace
    root hash. No secrets and no raw study values."""

    manifest_id: str = Field(default_factory=_id)
    run_id: str
    repository_commit: str = ""
    application_version: str = ""
    workflow_version: str = ""
    agent_role_versions: dict[str, str] = Field(default_factory=dict)
    prompt_template_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    provider_versions: dict[str, str] = Field(default_factory=dict)
    run_privacy_policy_version: str = ""
    method_registry_versions: dict[str, str] = Field(default_factory=dict)
    validator_versions: dict[str, str] = Field(default_factory=dict)
    transformation_hashes: dict[str, str] = Field(default_factory=dict)
    feature_flags: dict[str, str] = Field(default_factory=dict)
    trace_root_hash: str = ""
    created_at: str = Field(default_factory=_now)
