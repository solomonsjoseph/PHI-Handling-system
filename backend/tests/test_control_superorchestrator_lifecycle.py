"""Wave 4a: SuperOrchestrator absorbs the Manager/D9 lifecycle
responsibilities master spec section 9/87 lists (run lifecycle, dependency
state, task supervision, handoff supervision, artifact validity, retry and
correction budgets, human-review lifecycle, manifest freeze authorization,
execution authorization, rewind routing, final release authorization,
export lifecycle, cleanup lifecycle, formal run closure) beyond what
test_control_superorchestrator.py already covers (start_run/cancel_run/
advance/create_child_work/request_human_review/consume_review_event/
accept_result/recover/authorize_publication -- run lifecycle and
human-review lifecycle are already fully owned there and are not
re-tested here).

The load-bearing exit criterion (docs #87: "Manager can safely resume
supported states after process restart") is proven by resume_plan tests
below that construct a genuinely FRESH SuperOrchestrator/TaskService pair
against a store a first instance already wrote to -- simulating a process
restart with zero carried-over Python state, not merely re-using the same
object.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from phi_core.control.artifacts import MANIFEST_COLLECTION, ArtifactService
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import (
    CleanupManifest,
    ExecutionTask,
    HandoffResult,
    VerifiedClassificationManifest,
)
from phi_core.control.store import MemoryControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService
from phi_core.control.workflow import WorkflowError

SESSION_ID = "a" * 32


def _rig() -> tuple[SuperOrchestrator, TaskService, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    return SuperOrchestrator(store, tasks), tasks, store


def _fresh_orchestrator(store: MemoryControlStore) -> SuperOrchestrator:
    """Simulates a process restart: a brand-new TaskService and
    SuperOrchestrator, sharing only the durable store."""
    return SuperOrchestrator(store, TaskService(store, CapabilityPolicy(None)))


async def _started_run(orch: SuperOrchestrator):
    return await orch.start_run(session_id=SESSION_ID, principal="operator-1")


# ---- resume: durable resume after restart (the load-bearing exit criterion) ----


@pytest.mark.asyncio
async def test_resume_on_a_fresh_instance_reports_the_same_node_and_state_as_the_original() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    await orch.advance(run_id=run.run_id, outcome="ok")

    fresh = _fresh_orchestrator(store)
    plan = await fresh.resume(run_id=run.run_id)

    assert plan["run_id"] == run.run_id
    assert plan["node"] == "research"
    assert plan["state"] == "running"
    assert plan["is_terminal"] is False


@pytest.mark.asyncio
async def test_resume_reports_every_live_task_without_re_dispatching_completed_work() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    completed = await tasks.claim(task_id=root["task_id"], lease_owner="worker-1")
    await tasks.complete(task_id=completed.task_id, lease_owner="worker-1", fence=completed.fence, output_ref={"ok": True})
    live_child = await tasks.enqueue(
        run_id=run.run_id, session_id=SESSION_ID, worker="Pipeline", task_type="pipeline_run",
    )

    fresh = _fresh_orchestrator(store)
    plan = await fresh.resume(run_id=run.run_id)

    assert completed.task_id not in plan["live_task_ids"]
    assert live_child.task_id in plan["live_task_ids"]


@pytest.mark.asyncio
async def test_resume_on_a_terminal_run_is_a_no_op_that_still_reports_terminal() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    outcomes = ["ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete"]
    for outcome in outcomes:
        run = await orch.advance(run_id=run.run_id, outcome=outcome)
    assert run.node == "complete"

    fresh = _fresh_orchestrator(store)
    plan = await fresh.resume(run_id=run.run_id)

    assert plan["is_terminal"] is True
    assert plan["node"] == "complete"


# ---- resume: the in-flight sandboxed-child restart scenario ----
#
# A sandboxed run_isolated dispatch keeps no independent durable record of
# its own (control/sandbox.py's SandboxRecord is held only in the caller's
# in-memory ActivationFactory._sandboxes dict, never persisted to
# ControlStore) -- the only durable trace that a worker was mid-dispatch is
# the WorkItem lease it held. A "process restart" with an in-flight
# sandboxed child therefore surfaces, at the control-plane level, as
# exactly one thing: a `leased` WorkItem whose lease has expired with no
# further heartbeat. TaskService.reconcile_leases is this codebase's
# existing, sole authority for what happens next -- retry (attempts
# remain) or fail (ceiling reached) -- and resume() must call it, since
# nothing else does on a bare recover().


@pytest.mark.asyncio
async def test_resume_after_restart_detects_an_orphaned_in_flight_task_and_marks_it_for_retry() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    claimed = await tasks.claim(task_id=root["task_id"], lease_owner="sandbox-worker-1")
    assert claimed is not None
    # Simulate the parent process (and the sandboxed child it dispatched
    # into run_isolated) dying mid-task: the lease is now in the past,
    # exactly what a restarted process would observe with no live worker
    # heartbeating it.
    expired = claimed.model_copy(update={
        "lease_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
    })
    await store.replace_one("work_items", {"task_id": claimed.task_id}, expired)

    fresh = _fresh_orchestrator(store)
    plan = await fresh.resume(run_id=run.run_id)

    assert claimed.task_id in plan["retried_task_ids"]
    stored = await store.get_one("work_items", {"task_id": claimed.task_id})
    assert stored["state"] == "ready"
    assert stored["lease_owner"] == ""


@pytest.mark.asyncio
async def test_resume_after_restart_fails_an_orphaned_task_once_its_attempt_ceiling_is_reached() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    task_id = root["task_id"]
    # Drive the same task through claim -> expire -> reconcile three times
    # (max_attempts) before the final restart, matching how a genuinely
    # unrecoverable sandboxed worker would exhaust its ceiling across
    # repeated crash/restart cycles rather than on the very first one.
    for owner in ("worker-1", "worker-2", "worker-3"):
        stored = await store.get_one("work_items", {"task_id": task_id})
        current = stored["state"]
        if current == "ready":
            claimed = await tasks.claim(task_id=task_id, lease_owner=owner)
            assert claimed is not None
            expired = claimed.model_copy(update={
                "lease_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            })
            await store.replace_one("work_items", {"task_id": task_id}, expired)
        fresh = _fresh_orchestrator(store)
        plan = await fresh.resume(run_id=run.run_id)

    final = await store.get_one("work_items", {"task_id": task_id})
    assert final["state"] == "failed"
    assert task_id in plan["retry_failed_task_ids"]


# ---- dependencies_satisfied: dependency-state responsibility ----


@pytest.mark.asyncio
async def test_dependencies_satisfied_is_true_with_no_children() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]

    assert await orch.dependencies_satisfied(run_id=run.run_id, task_id=root["task_id"]) is True


@pytest.mark.asyncio
async def test_dependencies_satisfied_is_false_while_a_child_is_still_live() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    child = await tasks.enqueue(
        run_id=run.run_id, session_id=SESSION_ID, worker="Pipeline", task_type="pipeline_run",
        parent_task_id=root["task_id"],
    )

    assert await orch.dependencies_satisfied(run_id=run.run_id, task_id=root["task_id"]) is False

    claimed = await tasks.claim(task_id=child.task_id, lease_owner="w1")
    await tasks.complete(task_id=child.task_id, lease_owner="w1", fence=claimed.fence, output_ref={})

    assert await orch.dependencies_satisfied(run_id=run.run_id, task_id=root["task_id"]) is True
