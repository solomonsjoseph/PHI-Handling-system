"""D5 resource-bound and aggregate-team policy contracts.

Each resource ceiling gets its own test as the corresponding enforcement
lands. Per-task/enqueue-time bounds are proven against
`SuperOrchestrator.create_child_work`; per-task gateway-time bounds
(wall/tokens/cost/tool-calls/input-output bytes) and run-level aggregates
(tokens/cost/tool-calls/artifact-bytes/wall across the whole run) are
proven against `ProviderGateway.complete` and `ArtifactService.finalize`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace

import pytest
from phi_core.control import limits
from phi_core.control import policy as policy_module
from phi_core.control import superorchestrator as so_module
from phi_core.control.artifacts import ArtifactError
from phi_core.control.gateway import GatewayRequest, ProviderGateway
from phi_core.control.policy import MANIFESTS, POLICY_VERSION, TEAMS, CapabilityDenied, CapabilityPolicy
from phi_core.control.records import ResourceBudget, ResourceUsage, WorkflowRun
from phi_core.control.store import MemoryControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService

SESSION_ID = "b" * 32


def test_teams_are_the_exact_five_non_authoritative_budget_groups() -> None:
    assert TEAMS == {
        "regulatory_evidence": frozenset({"RegulationsExpert", "PHIMethodsExpert", "CorpusResearcher"}),
        "data_and_instrument": frozenset({"Lexicon", "Schema", "Instrument"}),
        "proposal_and_challenge": frozenset({"Judge", "Reviewer"}),
        "verification_and_audit": frozenset({"Executor", "Operator", "Reviewer"}),
        "publication_and_reporting": frozenset(
            {"Scout", "Ledger", "Ledger.Compare", "Ledger.Aggregate", "Herald", "Herald.Abstract", "Herald.Sections"}
        ),
    }


def _rig() -> tuple[SuperOrchestrator, TaskService, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    return SuperOrchestrator(store, tasks), tasks, store


async def _started_run(orch: SuperOrchestrator):
    return await orch.start_run(session_id=SESSION_ID, principal="operator-1")


async def _root_task(store: MemoryControlStore, run_id: str):
    docs = await store.find_many("work_items", {"run_id": run_id})
    assert len(docs) == 1
    return docs[0]


def _patched_manifests(**overrides) -> MappingProxyType:
    """Return MANIFESTS with "Pipeline" replaced, carrying `overrides` plus
    a grant to "executor" child work (executor's own manifest has no
    provider restriction, so CapabilityPolicy(None) can issue its grant)."""
    return MappingProxyType(
        {
            **MANIFESTS,
            "Pipeline": MANIFESTS["Pipeline"].model_copy(
                update={"allowed_child_task_types": frozenset({"executor"}), **overrides}
            ),
        }
    )


# ---- max_delegation_depth ---------------------------------------------


@pytest.mark.asyncio
async def test_max_delegation_depth_enforced(monkeypatch) -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    root = await _root_task(store, run.run_id)
    monkeypatch.setattr(so_module, "MANIFESTS", _patched_manifests())
    monkeypatch.setattr(policy_module, "MANIFESTS", so_module.MANIFESTS)
    monkeypatch.setattr(limits, "MAX_DELEGATION_DEPTH", 0)

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_delegation_depth_env_var_overrides_the_default(monkeypatch) -> None:
    """Every D5 bound reads through this same `_int_env` helper (a direct
    call, not a module reload, so consumers that name-bound the constant
    at import time -- e.g. tasks.py's `from .limits import
    MAX_ATTEMPTS_PER_TASK` -- are never left holding a stale value after
    this test)."""
    monkeypatch.setenv("MAX_DELEGATION_DEPTH", "9")
    assert limits._int_env("MAX_DELEGATION_DEPTH", 3) == 9


# ---- max_children_per_task ---------------------------------------------


@pytest.mark.asyncio
async def test_max_children_per_task_enforced(monkeypatch) -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    root = await _root_task(store, run.run_id)
    monkeypatch.setattr(so_module, "MANIFESTS", _patched_manifests(max_children=1))
    monkeypatch.setattr(policy_module, "MANIFESTS", so_module.MANIFESTS)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_PARENT", 100)  # isolate this bound

    first = await orch.create_child_work(
        run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
        input_ref={}, budget=ResourceBudget(),
    )
    assert first.state == "ready"

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


# ---- max_parallel_tasks_per_parent --------------------------------------


@pytest.mark.asyncio
async def test_max_parallel_tasks_per_parent_enforced(monkeypatch) -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    root = await _root_task(store, run.run_id)
    monkeypatch.setattr(so_module, "MANIFESTS", _patched_manifests(max_children=100))
    monkeypatch.setattr(policy_module, "MANIFESTS", so_module.MANIFESTS)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_PARENT", 1)

    await orch.create_child_work(
        run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
        input_ref={}, budget=ResourceBudget(),
    )

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


# ---- max_tasks_per_run ---------------------------------------------------


@pytest.mark.asyncio
async def test_max_tasks_per_run_enforced(monkeypatch) -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    root = await _root_task(store, run.run_id)
    monkeypatch.setattr(so_module, "MANIFESTS", _patched_manifests(max_children=100))
    monkeypatch.setattr(policy_module, "MANIFESTS", so_module.MANIFESTS)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_PARENT", 100)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_RUN", 100)
    monkeypatch.setattr(limits, "MAX_TASKS_PER_RUN", 1)  # the root task alone already fills it

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


# ---- max_parallel_tasks_per_run ------------------------------------------


@pytest.mark.asyncio
async def test_max_parallel_tasks_per_run_enforced(monkeypatch) -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    root = await _root_task(store, run.run_id)
    monkeypatch.setattr(so_module, "MANIFESTS", _patched_manifests(max_children=100))
    monkeypatch.setattr(policy_module, "MANIFESTS", so_module.MANIFESTS)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_PARENT", 100)
    monkeypatch.setattr(limits, "MAX_TASKS_PER_RUN", 100)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_RUN", 1)  # the still-ready root already fills it

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


# ---- max_attempts_per_task ------------------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_per_task_enforced() -> None:
    """A task past its retry ceiling is failed, not returned to `ready`, by
    lease reconciliation -- the actual max_attempts enforcement point."""

    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    task = await tasks.enqueue(run_id="r" * 32, session_id=SESSION_ID, worker="Executor", task_type="executor")
    await tasks.claim(task_id=task.task_id, lease_owner="worker-a")
    doc = await store.get_one("work_items", {"task_id": task.task_id})
    doc["max_attempts"] = 1
    doc["attempt"] = 1
    doc["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await store.replace_one("work_items", {"task_id": task.task_id}, doc)

    reconciled = await tasks.reconcile_leases(run_id="r" * 32)

    assert len(reconciled) == 1
    assert reconciled[0].state == "failed"
    assert reconciled[0].error_category == "lease_expired_retry_ceiling"


def test_max_attempts_per_task_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_ATTEMPTS_PER_TASK", "7")
    assert limits._int_env("MAX_ATTEMPTS_PER_TASK", 3) == 7


# ---- per-agent manifest override -----------------------------------------


def test_per_agent_manifest_budget_overrides_the_global_default() -> None:
    """A manifest may tighten a bound below the global default; the issued
    grant always reflects the tighter of the two (D5), never the wider.
    Scout's manifest tightens `max_attempts` to 1 (research calls are
    expensive; one try, no supervised retry)."""
    policy = CapabilityPolicy(SimpleNamespace(provider="anthropic", model="claude", base_url=""))
    grant = policy.issue_grant(run_id="r" * 32, task_id="t" * 32, agent="Scout", task_type="scout")

    assert grant.budget.max_attempts == MANIFESTS["Scout"].budget.max_attempts
    assert grant.budget.max_attempts < limits.MAX_ATTEMPTS_PER_TASK


def test_manifest_cannot_widen_a_grant_past_the_global_ceiling(monkeypatch) -> None:
    """The inverse: a manifest requesting more than the global ceiling is
    clamped down to it, never issued as requested."""
    patched = MappingProxyType(
        {
            **MANIFESTS,
            "Executor": MANIFESTS["Executor"].model_copy(
                update={"budget": MANIFESTS["Executor"].budget.model_copy(
                    update={"wall_seconds": limits.MAX_TASK_WALL_S + 1000.0}
                )}
            ),
        }
    )
    monkeypatch.setattr(policy_module, "MANIFESTS", patched)
    policy = CapabilityPolicy(None)

    grant = policy.issue_grant(run_id="r" * 32, task_id="t" * 32, agent="Executor", task_type="executor")

    assert grant.budget.wall_seconds == limits.MAX_TASK_WALL_S


# ---- gateway-time and run-level bound test rig ---------------------------

RUN_ID = "r" * 32
TASK_ID = "t" * 32


def _llm_cfg() -> SimpleNamespace:
    return SimpleNamespace(provider="anthropic", model="claude-test", base_url="")


def _patched_pipeline(*, budget_overrides: dict | None = None, tools: dict[str, int] | None = None):
    """`CapabilityPolicy._validate_grant` re-derives the expected budget
    from the *current* "Pipeline" manifest on every call (issuance AND
    every later `check_provider`/`check_data_class`/`check_tool`), and
    refuses any grant whose stored ``budget``/``tools`` no longer match --
    a grant can never be tampered with by widening it after issuance. So
    a tight per-task ceiling must come from patching the manifest itself
    (mirroring `_patched_manifests` above), never from `grant.model_copy`.
    Pipeline's own manifest also grants no provider at all (it never
    calls a provider in production -- every real agent activation opens
    its own separate grant), so every gateway test patches in "anthropic"
    to make `check_provider` satisfiable."""
    pipeline = MANIFESTS["Pipeline"].model_copy(update={"allowed_providers": frozenset({"anthropic"})})
    if budget_overrides:
        pipeline = pipeline.model_copy(update={"budget": pipeline.budget.model_copy(update=budget_overrides)})
    if tools is not None:
        pipeline = pipeline.model_copy(update={"allowed_tools": tools})
    return MappingProxyType({**MANIFESTS, "Pipeline": pipeline})


async def _issue_gateway_grant(store: MemoryControlStore, monkeypatch, *,
                                budget_overrides: dict | None = None, tools: dict[str, int] | None = None):
    monkeypatch.setattr(policy_module, "MANIFESTS", _patched_pipeline(budget_overrides=budget_overrides, tools=tools))
    policy = CapabilityPolicy(_llm_cfg())
    grant = policy.issue_grant(run_id=RUN_ID, task_id=TASK_ID, agent="Pipeline", task_type="pipeline_run")
    await store.insert("capability_grants", grant)
    return grant


async def _open_gateway_run(store: MemoryControlStore, *, run_budget: ResourceBudget | None = None,
                             run_usage: ResourceUsage | None = None, started_at: str | None = None) -> WorkflowRun:
    run = WorkflowRun(
        run_id=RUN_ID, session_id=SESSION_ID, workflow_version="wf/1", policy_version=POLICY_VERSION,
        run_type="study", state="running", node="charter",
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        budget=run_budget or ResourceBudget(), usage=run_usage or ResourceUsage(),
    )
    await store.insert("workflow_runs", run)
    return run


def _gateway_request(*, grant_id: str, **overrides) -> GatewayRequest:
    fields: dict = dict(
        session_id=SESSION_ID, run_id=RUN_ID, task_id=TASK_ID, agent="Pipeline", attempt=1,
        purpose="pipeline", input_class="internal", grant_id=grant_id,
        provider="anthropic", model="claude-test", endpoint="",
        system_prompt="system", user_prompt="user", coaching_note=None, tool_results=(),
        allowed_tools={}, response_schema="no_provider_output",
        timeout_s=30.0, max_tokens=100, max_cost_usd=0.01, policy_version=POLICY_VERSION,
    )
    fields.update(overrides)
    return GatewayRequest(**fields)


# ---- max_task_wall_s -------------------------------------------------------


@pytest.mark.asyncio
async def test_max_task_wall_s_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store, monkeypatch, budget_overrides={"wall_seconds": 10.0})
    req = _gateway_request(grant_id=grant.grant_id, timeout_s=20.0)  # exceeds the grant's 10s

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_TASK_WALL_S" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_task_wall_s_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_TASK_WALL_S", "42")
    assert limits._float_env("MAX_TASK_WALL_S", 180.0) == 42.0


# ---- max_tokens_per_task ---------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_per_task_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store, monkeypatch, budget_overrides={"max_tokens": 50})
    req = _gateway_request(grant_id=grant.grant_id, max_tokens=100)  # exceeds the grant's 50

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_TOKENS_PER_TASK" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_tokens_per_task_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_TOKENS_PER_TASK", "1234")
    assert limits._int_env("MAX_TOKENS_PER_TASK", 8000) == 1234


# ---- max_cost_per_task_usd -------------------------------------------------


@pytest.mark.asyncio
async def test_max_cost_per_task_usd_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store, monkeypatch, budget_overrides={"max_cost_usd": 0.05})
    req = _gateway_request(grant_id=grant.grant_id, max_cost_usd=0.10)  # exceeds the grant's 0.05

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_COST_PER_TASK_USD" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_cost_per_task_usd_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_COST_PER_TASK_USD", "1.5")
    assert limits._float_env("MAX_COST_PER_TASK_USD", 0.16) == 1.5


# ---- max_tool_calls_per_task -----------------------------------------------


@pytest.mark.asyncio
async def test_max_tool_calls_per_task_enforced_at_gateway(monkeypatch) -> None:
    """The aggregate ceiling on `grant.budget.max_tool_calls`, distinct
    from `check_tool`'s per-tool-name grant (`grant.tools[tool]`, which a
    3-use request against a 3-use grant satisfies on its own)."""
    store = MemoryControlStore()
    run = await _open_gateway_run(store)
    grant = await _issue_gateway_grant(
        store, monkeypatch, budget_overrides={"max_tool_calls": 1}, tools={"web_search": 3},
    )
    req = _gateway_request(grant_id=grant.grant_id, allowed_tools={"web_search": 3})  # 3 > the grant's aggregate 1

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_TOOL_CALLS_PER_TASK" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_tool_calls_per_task_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_TOOL_CALLS_PER_TASK", "9")
    assert limits._int_env("MAX_TOOL_CALLS_PER_TASK", 3) == 9


# ---- max_input_bytes -------------------------------------------------------


@pytest.mark.asyncio
async def test_max_input_bytes_enforced_at_gateway(monkeypatch) -> None:
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.limits, "MAX_INPUT_BYTES", 10)
    store = MemoryControlStore()
    run = await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store, monkeypatch)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="this prompt is far longer than 10 bytes")

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_INPUT_BYTES" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_input_bytes_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_INPUT_BYTES", "4096")
    assert limits._int_env("MAX_INPUT_BYTES", 262144) == 4096


# ---- max_output_bytes ------------------------------------------------------


@pytest.mark.asyncio
async def test_max_output_bytes_enforced_at_gateway(monkeypatch) -> None:
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.limits, "MAX_OUTPUT_BYTES", 5)

    class _FakeResponse:
        choices = [SimpleNamespace(message=SimpleNamespace(content="way more than five bytes"))]
        usage = {"total_tokens": 12}
        provider = "anthropic"
        model = "claude-test"
        id = "resp-1"

    monkeypatch.setattr(gateway_module.litellm, "completion", lambda **kwargs: _FakeResponse())
    store = MemoryControlStore()
    run = await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store, monkeypatch)
    req = _gateway_request(grant_id=grant.grant_id)

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_OUTPUT_BYTES" in result.denial_reason
    assert result.text == ""  # the oversized content itself never ships
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1
    # Real consumption still happened and is still recorded, even though
    # the output itself is refused.
    updated_run = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert updated_run["usage"]["tokens"] == 12


def test_max_output_bytes_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_OUTPUT_BYTES", "4096")
    assert limits._int_env("MAX_OUTPUT_BYTES", 262144) == 4096


# ---- max_run_wall_s ---------------------------------------------------------


@pytest.mark.asyncio
async def test_max_run_wall_s_enforced_at_enqueue(monkeypatch) -> None:
    orch, _tasks, store = _rig()
    monkeypatch.setattr(so_module, "MANIFESTS", _patched_manifests())
    monkeypatch.setattr(policy_module, "MANIFESTS", so_module.MANIFESTS)
    monkeypatch.setattr(so_module.limits, "MAX_RUN_WALL_S", 5.0)
    run = await _started_run(orch)
    root = await _root_task(store, run.run_id)
    doc = await store.get_one("workflow_runs", {"run_id": run.run_id})
    doc["started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    await store.replace_one("workflow_runs", {"run_id": run.run_id}, doc)

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root["task_id"], task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


@pytest.mark.asyncio
async def test_max_run_wall_s_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(
        store, run_budget=ResourceBudget(wall_seconds=5.0),
        started_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    )
    grant = await _issue_gateway_grant(store, monkeypatch)
    req = _gateway_request(grant_id=grant.grant_id)

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_RUN_WALL_S" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_run_wall_s_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_RUN_WALL_S", "111")
    assert limits._float_env("MAX_RUN_WALL_S", 900.0) == 111.0


# ---- max_tokens_per_run / max_cost_per_run_usd / max_tool_calls_per_run ----


@pytest.mark.asyncio
async def test_max_tokens_per_run_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(
        store, run_budget=ResourceBudget(max_tokens=100), run_usage=ResourceUsage(tokens=90),
    )
    grant = await _issue_gateway_grant(store, monkeypatch)
    req = _gateway_request(grant_id=grant.grant_id, max_tokens=50)  # 90 + 50 > the run's 100

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_TOKENS_PER_RUN" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_tokens_per_run_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_TOKENS_PER_RUN", "1")
    assert limits._int_env("MAX_TOKENS_PER_RUN", 400000) == 1


@pytest.mark.asyncio
async def test_max_cost_per_run_usd_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(
        store, run_budget=ResourceBudget(max_cost_usd=1.0), run_usage=ResourceUsage(cost_usd=0.95),
    )
    grant = await _issue_gateway_grant(store, monkeypatch)
    req = _gateway_request(grant_id=grant.grant_id, max_cost_usd=0.10)  # 0.95 + 0.10 > the run's 1.0

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_COST_PER_RUN_USD" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_cost_per_run_usd_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_COST_PER_RUN_USD", "0.01")
    assert limits._float_env("MAX_COST_PER_RUN_USD", 8.0) == 0.01


@pytest.mark.asyncio
async def test_max_tool_calls_per_run_enforced_at_gateway(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await _open_gateway_run(
        store, run_budget=ResourceBudget(max_tool_calls=3), run_usage=ResourceUsage(tool_calls=2),
    )
    grant = await _issue_gateway_grant(store, monkeypatch, tools={"web_search": 3})
    req = _gateway_request(grant_id=grant.grant_id, allowed_tools={"web_search": 2})  # 2 + 2 > the run's 3

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_TOOL_CALLS_PER_RUN" in result.denial_reason
    events = await store.find_many("trace_events", {"run_id": run.run_id, "outcome": "budget_exceeded"})
    assert len(events) == 1


def test_max_tool_calls_per_run_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_TOOL_CALLS_PER_RUN", "5")
    assert limits._int_env("MAX_TOOL_CALLS_PER_RUN", 60) == 5


# ---- max_artifact_bytes_per_run ---------------------------------------------


@pytest.mark.asyncio
async def test_max_artifact_bytes_per_run_enforced(tmp_path, monkeypatch) -> None:
    from phi_core.control import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module, "_ROOT_DIRS", {"staging": tmp_path})
    store = MemoryControlStore()
    await _open_gateway_run(store, run_budget=ResourceBudget(max_artifact_bytes=10))
    service = artifacts_module.ArtifactService(store, session_id=SESSION_ID, run_id=RUN_ID)
    artifact_id, tmp = await service.stage("dataset_export", "dataset__export.csv", "restricted_metadata", "staging")
    tmp.write_bytes(b"this payload is much larger than ten bytes")

    with pytest.raises(ArtifactError) as excinfo:
        await service.finalize(artifact_id)

    assert excinfo.value.reason == "artifact_bytes_budget_exceeded"
    record = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert record["state"] == "provisional"  # never promoted
    assert tmp.is_file()  # tmp bytes untouched, nothing at a final path to promote


def test_max_artifact_bytes_per_run_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_ARTIFACT_BYTES_PER_RUN", "1024")
    assert limits._int_env("MAX_ARTIFACT_BYTES_PER_RUN", 2147483648) == 1024
