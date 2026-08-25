"""Code-defined, deny-by-default capability policy.

Manifests are immutable authority records.  Grants are derived from them and
are never accepted from an agent as authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping

from phi_core.security import allowed_providers

from . import limits
from .records import AgentManifest, CapabilityGrant, DataClass, ResourceBudget, ScopeSpec

POLICY_VERSION = "policy/1"
_MANIFEST_VERSION = "manifest/1"


class CapabilityDenied(PermissionError):
    """Raised when a request falls outside a manifest-derived grant."""


OUTPUT_SCHEMAS: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {
        "header_analysis": MappingProxyType({"type": "object"}),
        "research_evidence": MappingProxyType({"type": "object"}),
        "decision_proposal": MappingProxyType({"type": "object"}),
        "judge_decisions": MappingProxyType({"type": "array"}),
        "audit_report": MappingProxyType({"type": "object"}),
        "report": MappingProxyType({"type": "object"}),
        "no_provider_output": MappingProxyType({"type": "object"}),
    }
)

_DATA_CLASS_ORDER: Mapping[DataClass, int] = MappingProxyType(
    {"public": 0, "internal": 1, "restricted_metadata": 2, "restricted_phi": 3}
)

# These are manifest maxima, not mutable environment limits.  A configured
# global limit can make a grant narrower, never wider than this authority.
_MANIFEST_MAX_BUDGET = ResourceBudget(
    wall_seconds=180.0,
    max_attempts=3,
    max_parallel=6,
    max_tokens=8000,
    max_cost_usd=0.16,
    max_tool_calls=3,
    max_children=8,
    max_input_bytes=262144,
    max_output_bytes=262144,
    max_artifact_bytes=2147483648,
)

_GLOBAL_PROVIDER_SET = frozenset(allowed_providers())
_SESSION_RUN_SCOPE = ScopeSpec(session=True, run=True)
_RESEARCH_TOOLS: Mapping[str, int] = MappingProxyType({"web_search": 3})


def _task_type(agent: str) -> str:
    return agent.lower().replace(".", "_")


def _manifest(
    agent: str,
    *,
    purpose: str,
    input_class: DataClass,
    output_schema: str,
    providers: frozenset[str] | None = None,
    tools: Mapping[str, int] | None = None,
    max_attempts: int | None = None,
) -> AgentManifest:
    if output_schema not in OUTPUT_SCHEMAS:
        raise RuntimeError(f"unknown output schema {output_schema!r}")
    budget_values = _MANIFEST_MAX_BUDGET.model_dump()
    if max_attempts is not None:
        budget_values["max_attempts"] = max_attempts
    return AgentManifest(
        agent=agent,
        manifest_version=_MANIFEST_VERSION,
        purpose=purpose,
        task_types=frozenset({_task_type(agent)}),
        accepted_input_classes=frozenset({"public", "internal", input_class}),
        data_class_ceiling=input_class,
        output_schema=output_schema,
        allowed_tools=tools or {},
        network_domains=frozenset({"api.anthropic.com"}) if tools else frozenset(),
        allowed_providers=_GLOBAL_PROVIDER_SET if providers is None else providers,
        allowed_models=frozenset(),
        scope=_SESSION_RUN_SCOPE,
        reads=frozenset(),
        writes=frozenset(),
        budget=ResourceBudget(**budget_values),
        allowed_child_task_types=frozenset(),
        max_depth=3,
        max_children=8,
    )


MANIFESTS: Mapping[str, AgentManifest] = MappingProxyType(
    {
        "Lexicon": _manifest("Lexicon", purpose="Classify dataset headers.", input_class="restricted_metadata", output_schema="header_analysis"),
        "Schema": _manifest("Schema", purpose="Inspect file structure.", input_class="restricted_metadata", output_schema="no_provider_output", providers=frozenset()),
        "Instrument": _manifest("Instrument", purpose="Classify instruments and form metadata.", input_class="restricted_metadata", output_schema="header_analysis"),
        "Statute": _manifest("Statute", purpose="Research jurisdictional rules.", input_class="internal", output_schema="research_evidence", tools=_RESEARCH_TOOLS),
        "Praxis": _manifest("Praxis", purpose="Research de-identification practice.", input_class="internal", output_schema="research_evidence", tools=_RESEARCH_TOOLS),
        "Judge": _manifest("Judge", purpose="Propose column decisions.", input_class="restricted_metadata", output_schema="judge_decisions"),
        "Sentinel": _manifest("Sentinel", purpose="Challenge proposed decisions.", input_class="restricted_metadata", output_schema="decision_proposal"),
        "Executor": _manifest("Executor", purpose="Apply deterministic decisions.", input_class="internal", output_schema="no_provider_output", providers=frozenset()),
        "Auditor": _manifest("Auditor", purpose="Audit staged output metadata.", input_class="restricted_metadata", output_schema="audit_report"),
        "Scout": _manifest("Scout", purpose="Research reporting context.", input_class="internal", output_schema="research_evidence", tools=_RESEARCH_TOOLS, max_attempts=1),
        "Ledger": _manifest("Ledger", purpose="Build a reporting ledger.", input_class="internal", output_schema="report"),
        "Ledger.Compare": _manifest("Ledger.Compare", purpose="Compare reporting inputs.", input_class="internal", output_schema="report"),
        "Ledger.Aggregate": _manifest("Ledger.Aggregate", purpose="Aggregate reporting inputs.", input_class="internal", output_schema="report"),
        "Herald": _manifest("Herald", purpose="Prepare a report.", input_class="internal", output_schema="report"),
        "Herald.Abstract": _manifest("Herald.Abstract", purpose="Prepare a report abstract.", input_class="internal", output_schema="report"),
        "Herald.Sections": _manifest("Herald.Sections", purpose="Prepare report sections.", input_class="internal", output_schema="report"),
        "Manager": _manifest("Manager", purpose="Supervise bounded calls.", input_class="internal", output_schema="no_provider_output", providers=frozenset()),
        "Operator": _manifest("Operator", purpose="Verify deterministic transformations.", input_class="internal", output_schema="no_provider_output", providers=frozenset()),
        "Reviewer": _manifest("Reviewer", purpose="Review export coverage.", input_class="internal", output_schema="no_provider_output", providers=frozenset()),
        "CorpusResearcher": _manifest("CorpusResearcher", purpose="Research corpus sources.", input_class="internal", output_schema="research_evidence", tools=_RESEARCH_TOOLS),
        # The top-level `TaskService`-enqueued unit `session_handle`/
        # `session_human_review` submit (Phase 4 step 2/4): itself makes no
        # provider call and activates no grant of its own beyond bookkeeping
        # -- every real agent activation inside `run_agent_pipeline`/
        # `execute_decisions` opens its own separate `make_ctx`-issued
        # grant. `task_types` needs both literals since one manifest
        # covers a fresh run and a human-review resume alike.
        "Pipeline": AgentManifest(
            agent="Pipeline",
            manifest_version=_MANIFEST_VERSION,
            purpose="Own the top-level pipeline_run/pipeline_resume TaskService unit.",
            task_types=frozenset({"pipeline_run", "pipeline_resume"}),
            accepted_input_classes=frozenset({"public", "internal"}),
            data_class_ceiling="internal",
            output_schema="no_provider_output",
            allowed_tools={},
            network_domains=frozenset(),
            allowed_providers=frozenset(),
            allowed_models=frozenset(),
            scope=_SESSION_RUN_SCOPE,
            reads=frozenset(),
            writes=frozenset(),
            budget=ResourceBudget(**{**_MANIFEST_MAX_BUDGET.model_dump(), "wall_seconds": 900.0}),
            allowed_child_task_types=frozenset(),
            max_depth=3,
            max_children=8,
        ),
    }
)

TEAMS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "regulatory_evidence": frozenset({"Statute", "Praxis", "CorpusResearcher"}),
        "data_and_instrument": frozenset({"Lexicon", "Schema", "Instrument"}),
        "proposal_and_challenge": frozenset({"Judge", "Sentinel"}),
        "verification_and_audit": frozenset({"Executor", "Operator", "Reviewer", "Auditor"}),
        "publication_and_reporting": frozenset(
            {"Scout", "Ledger", "Ledger.Compare", "Ledger.Aggregate", "Herald", "Herald.Abstract", "Herald.Sections"}
        ),
    }
)


def _global_budget() -> ResourceBudget:
    return ResourceBudget(
        wall_seconds=limits.MAX_TASK_WALL_S,
        max_attempts=limits.MAX_ATTEMPTS_PER_TASK,
        max_parallel=limits.MAX_PARALLEL_TASKS_PER_RUN,
        max_tokens=limits.MAX_TOKENS_PER_TASK,
        max_cost_usd=limits.MAX_COST_PER_TASK_USD,
        max_tool_calls=limits.MAX_TOOL_CALLS_PER_TASK,
        max_children=limits.MAX_CHILDREN_PER_TASK,
        max_input_bytes=limits.MAX_INPUT_BYTES,
        max_output_bytes=limits.MAX_OUTPUT_BYTES,
        max_artifact_bytes=limits.MAX_ARTIFACT_BYTES_PER_RUN,
    )


def _bounded_budget(manifest: ResourceBudget) -> ResourceBudget:
    global_budget = _global_budget()
    return ResourceBudget(
        wall_seconds=min(manifest.wall_seconds, global_budget.wall_seconds),
        max_attempts=min(manifest.max_attempts, global_budget.max_attempts),
        max_parallel=min(manifest.max_parallel, global_budget.max_parallel),
        max_tokens=min(manifest.max_tokens, global_budget.max_tokens),
        max_cost_usd=min(manifest.max_cost_usd, global_budget.max_cost_usd),
        max_tool_calls=min(manifest.max_tool_calls, global_budget.max_tool_calls),
        max_children=min(manifest.max_children, global_budget.max_children),
        max_input_bytes=min(manifest.max_input_bytes, global_budget.max_input_bytes),
        max_output_bytes=min(manifest.max_output_bytes, global_budget.max_output_bytes),
        max_artifact_bytes=min(manifest.max_artifact_bytes, global_budget.max_artifact_bytes),
    )

class CapabilityPolicy:
    """Issues and verifies immutable grants against ``MANIFESTS``."""

    def __init__(self, llm_config: Any | None = None) -> None:
        self._llm_config = llm_config

    def issue_grant(self, *, run_id: str, task_id: str, agent: str, task_type: str) -> CapabilityGrant:
        manifest = self._manifest_for(agent, task_type)
        budget = _bounded_budget(manifest.budget)
        issued_at = datetime.now(timezone.utc)
        provider = str(getattr(self._llm_config, "provider", "") or "")
        model = str(getattr(self._llm_config, "model", "") or "")
        endpoint = str(getattr(self._llm_config, "base_url", "") or "")
        if manifest.allowed_providers and provider not in manifest.allowed_providers:
            raise CapabilityDenied(f"configured provider {provider!r} is not granted to {agent!r}")
        if manifest.allowed_models and model not in manifest.allowed_models:
            raise CapabilityDenied(f"configured model {model!r} is not granted to {agent!r}")
        return CapabilityGrant(
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            manifest_version=manifest.manifest_version,
            policy_version=POLICY_VERSION,
            issued_at=issued_at.isoformat(),
            expires_at=(issued_at + timedelta(seconds=budget.wall_seconds)).isoformat(),
            tools=dict(manifest.allowed_tools),
            tools_used={tool: 0 for tool in manifest.allowed_tools},
            data_class_ceiling=manifest.data_class_ceiling,
            providers=manifest.allowed_providers,
            models=manifest.allowed_models,
            provider=provider,
            model=model,
            endpoint=endpoint,
            scope=manifest.scope,
            budget=budget,
        )

    def check_tool(self, grant: CapabilityGrant, tool: str, *, uses: int = 1) -> None:
        self._validate_grant(grant)
        if uses <= 0 or tool not in grant.tools or grant.tools[tool] < uses:
            raise CapabilityDenied(f"tool {tool!r} is not granted")
        if grant.tools_used.get(tool, 0) + uses > grant.tools[tool]:
            raise CapabilityDenied(f"tool {tool!r} budget is exhausted")

    def check_provider(self, grant: CapabilityGrant, provider: str, model: str = "", endpoint: str = "") -> None:
        self._validate_grant(grant)
        if (provider, model, endpoint) != (grant.provider, grant.model, grant.endpoint):
            raise CapabilityDenied("provider, model, or endpoint differs from the grant")
        if provider not in grant.providers or provider not in allowed_providers():
            raise CapabilityDenied(f"provider {provider!r} is not granted")
        if grant.models and model not in grant.models:
            raise CapabilityDenied(f"model {model!r} is not granted")
    def check_data_class(self, grant: CapabilityGrant, data_class: DataClass) -> None:
        self._validate_grant(grant)
        if data_class == "restricted_phi":
            raise CapabilityDenied("restricted_phi cannot leave the deployment boundary")
        manifest = MANIFESTS[grant.agent]
        if data_class not in manifest.accepted_input_classes:
            raise CapabilityDenied(f"input class {data_class!r} is not accepted")
        if _DATA_CLASS_ORDER[data_class] > _DATA_CLASS_ORDER[grant.data_class_ceiling]:
            raise CapabilityDenied(f"input class {data_class!r} exceeds the grant ceiling")

    def check_child(self, grant: CapabilityGrant, task_type: str, *, depth: int, children: int) -> None:
        self._validate_grant(grant)
        manifest = MANIFESTS[grant.agent]
        if task_type not in manifest.allowed_child_task_types:
            raise CapabilityDenied(f"child task {task_type!r} is not granted")
        if depth > manifest.max_depth or depth > limits.MAX_DELEGATION_DEPTH:
            raise CapabilityDenied("child depth exceeds the grant")
        if children >= min(manifest.max_children, grant.budget.max_children):
            raise CapabilityDenied("child count exceeds the grant")

    @staticmethod
    def _manifest_for(agent: str, task_type: str) -> AgentManifest:
        manifest = MANIFESTS.get(agent)
        if manifest is None:
            raise CapabilityDenied(f"agent {agent!r} has no manifest")
        if task_type not in manifest.task_types:
            raise CapabilityDenied(f"task type {task_type!r} is not granted to {agent!r}")
        return manifest

    def _validate_grant(self, grant: CapabilityGrant) -> AgentManifest:
        manifest = MANIFESTS.get(grant.agent)
        if manifest is None or grant.manifest_version != manifest.manifest_version:
            raise CapabilityDenied("grant does not match an active manifest")
        if grant.policy_version != POLICY_VERSION:
            raise CapabilityDenied("grant has an inactive policy version")
        expected_budget = _bounded_budget(manifest.budget)
        if (
            grant.tools != manifest.allowed_tools
            or grant.data_class_ceiling != manifest.data_class_ceiling
            or grant.providers != manifest.allowed_providers
            or grant.models != manifest.allowed_models
            or grant.scope != manifest.scope
            or grant.budget != expected_budget
        ):
            raise CapabilityDenied("grant exceeds or differs from manifest authority")
        if self._llm_config is not None and (
            grant.provider != str(getattr(self._llm_config, "provider", "") or "")
            or grant.model != str(getattr(self._llm_config, "model", "") or "")
            or grant.endpoint != str(getattr(self._llm_config, "base_url", "") or "")
        ):
            raise CapabilityDenied("grant provider selection differs from configured provider")
        if set(grant.tools_used) != set(grant.tools) or any(
            used < 0 or used > grant.tools[tool] for tool, used in grant.tools_used.items()
        ):
            raise CapabilityDenied("grant tool usage is invalid")
        return manifest
