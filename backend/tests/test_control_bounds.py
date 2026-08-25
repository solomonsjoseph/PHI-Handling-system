"""D5 resource-bound and aggregate-team policy contracts.

Each resource ceiling gets its own test as the corresponding enforcement
lands. Bounds without a real enforcement point yet (wall-clock ceilings,
running per-run token/cost/tool-call/artifact-byte totals) are not covered
here -- see docs/adr/0006-super-orchestrator.md's Consequences for the
tracked gap; a test asserting behavior that does not exist would be a
fake, not a contract.
"""
from __future__ import annotations

from types import MappingProxyType

import pytest
from phi_core.control import limits
from phi_core.control import policy as policy_module
from phi_core.control import superorchestrator as so_module
from phi_core.control.policy import MANIFESTS, TEAMS, CapabilityDenied, CapabilityPolicy
from phi_core.control.records import ResourceBudget
from phi_core.control.store import MemoryControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService

SESSION_ID = "b" * 32


def test_teams_are_the_exact_five_non_authoritative_budget_groups() -> None:
    assert TEAMS == {
        "regulatory_evidence": frozenset({"Statute", "Praxis", "CorpusResearcher"}),
        "data_and_instrument": frozenset({"Lexicon", "Schema", "Instrument"}),
        "proposal_and_challenge": frozenset({"Judge", "Sentinel"}),
        "verification_and_audit": frozenset({"Executor", "Operator", "Reviewer", "Auditor"}),
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


# ---- max_attempts_per_task ------------------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_per_task_enforced() -> None:
    """A task past its retry ceiling is failed, not returned to `ready`, by
    lease reconciliation -- the actual max_attempts enforcement point."""
    from datetime import datetime, timedelta, timezone

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
    from types import SimpleNamespace

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
