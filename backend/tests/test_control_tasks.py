"""Focused D3/D9 contracts for the CAS-fenced task lifecycle (control/tasks.py)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskOutcome, TaskService

RUN_ID = "run-" + "a" * 28


def _service() -> tuple[TaskService, MemoryControlStore]:
    store = MemoryControlStore()
    # "Executor" carries an empty ``allowed_providers`` in its manifest, so
    # issuing a grant needs no real ``LlmConfig`` -- keeps these tests
    # focused on task lifecycle, not provider policy.
    service = TaskService(store, CapabilityPolicy(None))
    return service, store


async def _enqueue(service: TaskService, **overrides):
    kwargs = dict(run_id=RUN_ID, session_id="session-1", worker="Executor", task_type="executor")
    kwargs.update(overrides)
    return await service.enqueue(**kwargs)


async def _expire_lease(store: MemoryControlStore, task_id: str) -> None:
    """Rewrite the stored lease_expires_at into the past, in place."""
    doc = await store.get_one("work_items", {"task_id": task_id})
    assert doc is not None
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    doc["lease_expires_at"] = past
    await store.replace_one("work_items", {"task_id": task_id}, doc)


# ---- enqueue --------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_creates_a_ready_work_item_with_budget_and_depth() -> None:
    service, store = _service()
    task = await _enqueue(service, depth=2, parent_task_id="parent-1")

    assert task.state == "ready"
    assert task.depth == 2
    assert task.parent_task_id == "parent-1"
    assert task.fence == 0
    assert task.budget.max_attempts > 0
    assert task.max_attempts == task.budget.max_attempts
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    assert stored["state"] == "ready"


# ---- claim ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_claims_race_safely_exactly_one_wins() -> None:
    service, _ = _service()
    task = await _enqueue(service)

    first, second = await asyncio.gather(
        service.claim(task_id=task.task_id, lease_owner="worker-a"),
        service.claim(task_id=task.task_id, lease_owner="worker-b"),
    )

    winners = [claim for claim in (first, second) if claim is not None]
    losers = [claim for claim in (first, second) if claim is None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].state == "leased"
    assert winners[0].lease_owner in ("worker-a", "worker-b")


@pytest.mark.asyncio
async def test_claim_does_not_bump_fence() -> None:
    service, _ = _service()
    task = await _enqueue(service)
    claimed = await service.claim(task_id=task.task_id, lease_owner="worker-a")
    assert claimed is not None
    assert claimed.fence == task.fence == 0
    assert claimed.attempt == 1


@pytest.mark.asyncio
async def test_claiming_an_already_leased_task_returns_none() -> None:
    service, _ = _service()
    task = await _enqueue(service)
    assert await service.claim(task_id=task.task_id, lease_owner="worker-a") is not None
    assert await service.claim(task_id=task.task_id, lease_owner="worker-b") is None


@pytest.mark.asyncio
async def test_claiming_an_unknown_task_returns_none() -> None:
    service, _ = _service()
    assert await service.claim(task_id="does-not-exist", lease_owner="worker-a") is None


# ---- heartbeat --------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_extends_the_lease_under_matching_owner_and_fence() -> None:
    service, _ = _service()
    task = await _enqueue(service)
    claimed = await service.claim(task_id=task.task_id, lease_owner="worker-a")

    outcome = await service.heartbeat(
        task_id=task.task_id, lease_owner="worker-a", fence=claimed.fence
    )

    assert outcome.ok is True
    assert outcome.outcome == "ok"
    assert outcome.task.lease_expires_at > claimed.lease_expires_at
    assert outcome.task.fence == claimed.fence  # heartbeat never bumps the fence


@pytest.mark.asyncio
async def test_heartbeat_with_wrong_owner_is_fenced_not_raised() -> None:
    service, _ = _service()
    task = await _enqueue(service)
    claimed = await service.claim(task_id=task.task_id, lease_owner="worker-a")

    outcome = await service.heartbeat(task_id=task.task_id, lease_owner="worker-b", fence=claimed.fence)

    assert isinstance(outcome, TaskOutcome)
    assert outcome.ok is False
    assert outcome.outcome == "fenced"
    assert outcome.task.lease_owner == "worker-a"  # untouched, not overwritten


@pytest.mark.asyncio
async def test_heartbeat_on_unknown_task_is_not_found() -> None:
    service, _ = _service()
    outcome = await service.heartbeat(task_id="does-not-exist", lease_owner="worker-a", fence=0)
    assert outcome.ok is False
    assert outcome.outcome == "not_found"
    assert outcome.task is None


# ---- complete / fail ----------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_bumps_fence_and_sets_succeeded() -> None:
    service, store = _service()
    task = await _enqueue(service)
    claimed = await service.claim(task_id=task.task_id, lease_owner="worker-a")

    outcome = await service.complete(
        task_id=task.task_id, lease_owner="worker-a", fence=claimed.fence, output_ref={"n": 1}
    )

    assert outcome.ok is True
    assert outcome.task.state == "succeeded"
    assert outcome.task.fence == claimed.fence + 1
    assert outcome.task.output_ref == {"n": 1}
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    assert stored["state"] == "succeeded"


@pytest.mark.asyncio
async def test_fail_bumps_fence_and_sets_failed() -> None:
    service, _ = _service()
    task = await _enqueue(service)
    claimed = await service.claim(task_id=task.task_id, lease_owner="worker-a")

    outcome = await service.fail(
        task_id=task.task_id, lease_owner="worker-a", fence=claimed.fence, error_category="boom"
    )

    assert outcome.ok is True
    assert outcome.task.state == "failed"
    assert outcome.task.fence == claimed.fence + 1
    assert outcome.task.error_category == "boom"


@pytest.mark.asyncio
async def test_complete_with_wrong_fence_is_fenced_and_leaves_state_untouched() -> None:
    service, store = _service()
    task = await _enqueue(service)
    claimed = await service.claim(task_id=task.task_id, lease_owner="worker-a")

    outcome = await service.complete(task_id=task.task_id, lease_owner="worker-a", fence=claimed.fence + 5)

    assert outcome.ok is False
    assert outcome.outcome == "fenced"
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    assert stored["state"] == "leased"  # never applied


@pytest.mark.asyncio
async def test_complete_on_unknown_task_is_not_found() -> None:
    service, _ = _service()
    outcome = await service.complete(task_id="does-not-exist", lease_owner="worker-a", fence=0)
    assert outcome.outcome == "not_found"


@pytest.mark.asyncio
async def test_stale_holder_cannot_complete_after_reconciliation_hands_the_task_to_a_new_owner() -> None:
    """The full staleness scenario: A claims, its lease is reconciled away
    (bumping the fence) while it is still "working", B claims the reopened
    task, and A's stale (lease_owner, fence) pair can no longer complete or
    fail the task -- it is fenced, and B's real credentials still can."""
    service, store = _service()
    task = await _enqueue(service)
    claimed_by_a = await service.claim(task_id=task.task_id, lease_owner="worker-a")
    assert claimed_by_a.fence == 0

    await _expire_lease(store, task.task_id)
    reconciled = await service.reconcile_leases(run_id=RUN_ID)
    assert len(reconciled) == 1
    assert reconciled[0].state == "ready"
    assert reconciled[0].fence == 1  # bumped out from under worker-a

    claimed_by_b = await service.claim(task_id=task.task_id, lease_owner="worker-b")
    assert claimed_by_b is not None
    assert claimed_by_b.lease_owner == "worker-b"
    assert claimed_by_b.fence == 1  # claim itself never bumps it further

    # worker-a's original, now-stale credentials can neither complete nor fail.
    stale_complete = await service.complete(
        task_id=task.task_id, lease_owner="worker-a", fence=claimed_by_a.fence
    )
    assert stale_complete.ok is False
    assert stale_complete.outcome == "fenced"
    stale_fail = await service.fail(task_id=task.task_id, lease_owner="worker-a", fence=claimed_by_a.fence)
    assert stale_fail.ok is False
    assert stale_fail.outcome == "fenced"

    # worker-b, the legitimate current holder, can.
    real_complete = await service.complete(
        task_id=task.task_id, lease_owner="worker-b", fence=claimed_by_b.fence
    )
    assert real_complete.ok is True
    assert real_complete.task.state == "succeeded"


# ---- reconcile_leases ---------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_leases_returns_expired_leased_task_to_ready_for_reclaim() -> None:
    service, store = _service()
    task = await _enqueue(service)
    await service.claim(task_id=task.task_id, lease_owner="worker-a")
    await _expire_lease(store, task.task_id)

    reconciled = await service.reconcile_leases(run_id=RUN_ID)

    assert len(reconciled) == 1
    assert reconciled[0].state == "ready"
    assert reconciled[0].lease_owner == ""
    reclaim = await service.claim(task_id=task.task_id, lease_owner="worker-c")
    assert reclaim is not None
    assert reclaim.lease_owner == "worker-c"


@pytest.mark.asyncio
async def test_reconcile_leases_ignores_leases_that_have_not_expired() -> None:
    service, _ = _service()
    task = await _enqueue(service)
    await service.claim(task_id=task.task_id, lease_owner="worker-a")

    reconciled = await service.reconcile_leases(run_id=RUN_ID)

    assert reconciled == []


@pytest.mark.asyncio
async def test_reconcile_leases_fails_a_task_past_its_retry_ceiling() -> None:
    service, store = _service()
    task = await _enqueue(service)
    await service.claim(task_id=task.task_id, lease_owner="worker-a")
    # Force the ceiling low and pretend this is the final permitted attempt.
    doc = await store.get_one("work_items", {"task_id": task.task_id})
    doc["max_attempts"] = 1
    doc["attempt"] = 1
    await store.replace_one("work_items", {"task_id": task.task_id}, doc)
    await _expire_lease(store, task.task_id)

    reconciled = await service.reconcile_leases(run_id=RUN_ID)

    assert len(reconciled) == 1
    assert reconciled[0].state == "failed"
    assert reconciled[0].error_category == "lease_expired_retry_ceiling"
    # a task failed by the retry ceiling cannot be reclaimed
    assert await service.claim(task_id=task.task_id, lease_owner="worker-d") is None


@pytest.mark.asyncio
async def test_reconcile_leases_scoped_to_run_id_leaves_other_runs_alone() -> None:
    service, store = _service()
    task_a = await _enqueue(service, run_id=RUN_ID)
    task_b = await _enqueue(service, run_id="run-" + "b" * 28)
    await service.claim(task_id=task_a.task_id, lease_owner="worker-a")
    await service.claim(task_id=task_b.task_id, lease_owner="worker-b")
    await _expire_lease(store, task_a.task_id)
    await _expire_lease(store, task_b.task_id)

    reconciled = await service.reconcile_leases(run_id=RUN_ID)

    assert [t.task_id for t in reconciled] == [task_a.task_id]
    other = await store.get_one("work_items", {"task_id": task_b.task_id})
    assert other["state"] == "leased"  # untouched


# ---- cancel_subtree -----------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_subtree_cancels_the_task_and_every_descendant() -> None:
    service, store = _service()
    parent = await _enqueue(service)
    child = await _enqueue(service, parent_task_id=parent.task_id, depth=1)
    grandchild = await _enqueue(service, parent_task_id=child.task_id, depth=2)
    # An unrelated sibling task must be left alone.
    unrelated = await _enqueue(service)

    cancelled = await service.cancel_subtree(run_id=RUN_ID, task_id=parent.task_id, reason="test")

    cancelled_ids = {t.task_id for t in cancelled}
    assert cancelled_ids == {parent.task_id, child.task_id, grandchild.task_id}
    for task_id in cancelled_ids:
        stored = await store.get_one("work_items", {"task_id": task_id})
        assert stored["state"] == "cancelled"
        assert stored["cancel_requested"] is True
        assert stored["error_category"] == "test"
    still_ready = await store.get_one("work_items", {"task_id": unrelated.task_id})
    assert still_ready["state"] == "ready"


@pytest.mark.asyncio
async def test_cancel_subtree_leaves_already_terminal_nodes_untouched() -> None:
    service, store = _service()
    parent = await _enqueue(service)
    child = await _enqueue(service, parent_task_id=parent.task_id, depth=1)
    claimed_child = await service.claim(task_id=child.task_id, lease_owner="worker-a")
    completed = await service.complete(task_id=child.task_id, lease_owner="worker-a", fence=claimed_child.fence)
    assert completed.ok is True

    cancelled = await service.cancel_subtree(run_id=RUN_ID, task_id=parent.task_id)

    by_id = {t.task_id: t for t in cancelled}
    assert by_id[parent.task_id].state == "cancelled"
    assert by_id[child.task_id].state == "succeeded"  # not clobbered
    stored_child = await store.get_one("work_items", {"task_id": child.task_id})
    assert stored_child["state"] == "succeeded"


@pytest.mark.asyncio
async def test_cancel_subtree_of_unknown_task_returns_empty() -> None:
    service, _ = _service()
    assert await service.cancel_subtree(run_id=RUN_ID, task_id="does-not-exist") == []


@pytest.mark.asyncio
async def test_cancel_subtree_fences_an_in_flight_leased_child() -> None:
    """A child mid-lease (not expired, not completed) still gets cancelled and
    fenced -- its holder's next complete/fail lands on a bumped fence."""
    service, store = _service()
    parent = await _enqueue(service)
    child = await _enqueue(service, parent_task_id=parent.task_id, depth=1)
    claimed_child = await service.claim(task_id=child.task_id, lease_owner="worker-a")

    await service.cancel_subtree(run_id=RUN_ID, task_id=parent.task_id)

    stale_complete = await service.complete(
        task_id=child.task_id, lease_owner="worker-a", fence=claimed_child.fence
    )
    assert stale_complete.ok is False
    assert stale_complete.outcome == "fenced"
    stored = await store.get_one("work_items", {"task_id": child.task_id})
    assert stored["state"] == "cancelled"
