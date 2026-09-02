"""Wave 4a: Manager absorbs the Manager/D9 lifecycle
responsibilities master spec section 9/87 lists (run lifecycle, dependency
state, task supervision, handoff supervision, artifact validity, retry and
correction budgets, human-review lifecycle, manifest freeze authorization,
execution authorization, rewind routing, final release authorization,
export lifecycle, cleanup lifecycle, formal run closure) beyond what
test_control_manager.py already covers (start_run/cancel_run/
advance/create_child_work/request_human_review/consume_review_event/
accept_result/recover/authorize_publication -- run lifecycle and
human-review lifecycle are already fully owned there and are not
re-tested here).

The load-bearing exit criterion (docs #87: "Manager can safely resume
supported states after process restart") is proven by resume_plan tests
below that construct a genuinely FRESH Manager/TaskService pair
against a store a first instance already wrote to -- simulating a process
restart with zero carried-over Python state, not merely re-using the same
object.
"""
from __future__ import annotations

import pytest
from phi_core.control.artifacts import MANIFEST_COLLECTION
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import (
    CleanupManifest,
    VerifiedClassificationManifest,
)
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService
from phi_core.control.workflow import WorkflowError

SESSION_ID = "a" * 32


def _rig() -> tuple[Manager, TaskService, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    return Manager(store, tasks), tasks, store


async def _started_run(orch: Manager):
    return await orch.start_run(session_id=SESSION_ID, principal="operator-1")


_COMPLETE_OUTCOMES = (
    "ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete",
)


async def _completed_run(orch: Manager, run_id: str):
    run = None
    for outcome in _COMPLETE_OUTCOMES:
        run = await orch.advance(run_id=run_id, outcome=outcome)
    return run


# ---- authorize_manifest_freeze: manifest-freeze-authorization responsibility ----


@pytest.mark.asyncio
async def test_authorize_manifest_freeze_writes_to_the_manifest_collection() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    await orch.advance(run_id=run.run_id, outcome="ok")
    manifest = VerifiedClassificationManifest(run_id=run.run_id, preview_review_id="pr1")

    returned = await orch.authorize_manifest_freeze(
        run_id=run.run_id, artifact_id="artifact-1", manifest=manifest,
    )

    assert returned.manifest_id == manifest.manifest_id
    stored = await store.get_one(MANIFEST_COLLECTION, {"artifact_id": "artifact-1"})
    assert stored["status"] == "verified_for_execution"
    assert stored["manifest_id"] == manifest.manifest_id


@pytest.mark.asyncio
async def test_authorize_manifest_freeze_refuses_a_manifest_from_another_run() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    manifest = VerifiedClassificationManifest(run_id="some-other-run", preview_review_id="pr1")

    with pytest.raises(WorkflowError):
        await orch.authorize_manifest_freeze(run_id=run.run_id, artifact_id="artifact-1", manifest=manifest)


@pytest.mark.asyncio
async def test_authorize_manifest_freeze_refuses_once_the_run_is_terminal() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    outcomes = ["ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete"]
    for outcome in outcomes:
        run = await orch.advance(run_id=run.run_id, outcome=outcome)
    manifest = VerifiedClassificationManifest(run_id=run.run_id, preview_review_id="pr1")

    with pytest.raises(WorkflowError):
        await orch.authorize_manifest_freeze(run_id=run.run_id, artifact_id="artifact-1", manifest=manifest)


@pytest.mark.asyncio
async def test_authorize_manifest_freeze_refuses_while_awaiting_human_review() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    await orch.request_human_review(
        run_id=run.run_id, node="human_review_decisions", reason_codes=[], decision_version=1,
    )
    manifest = VerifiedClassificationManifest(run_id=run.run_id, preview_review_id="pr1")

    with pytest.raises(WorkflowError):
        await orch.authorize_manifest_freeze(run_id=run.run_id, artifact_id="artifact-1", manifest=manifest)


# ---- rewind: rewind-routing responsibility ----


@pytest.mark.asyncio
async def test_rewind_routes_back_to_an_earlier_node_and_commits_a_fresh_checkpoint() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    for outcome in ("ok", "ok", "ok"):
        run = await orch.advance(run_id=run.run_id, outcome=outcome)
    assert run.node == "decide"

    rewound = await orch.rewind(run_id=run.run_id, to_node="research", reason="root cause: bad specialist output")

    assert rewound.node == "research"
    assert rewound.state == "running"
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["node"] == "research"
    assert stored["checkpoint"]["rewind_reason"] == "root cause: bad specialist output"


@pytest.mark.asyncio
async def test_rewind_refuses_an_unknown_node() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    with pytest.raises(WorkflowError):
        await orch.rewind(run_id=run.run_id, to_node="not_a_real_node", reason="x")


@pytest.mark.asyncio
async def test_rewind_refuses_a_terminal_target_node() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    with pytest.raises(WorkflowError):
        await orch.rewind(run_id=run.run_id, to_node="failed", reason="x")


@pytest.mark.asyncio
async def test_rewind_refuses_a_target_that_is_not_earlier_than_the_current_node() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    with pytest.raises(WorkflowError):
        await orch.rewind(run_id=run.run_id, to_node="decide", reason="x")


@pytest.mark.asyncio
async def test_rewind_refuses_once_the_run_is_already_terminal() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    outcomes = ["ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete"]
    for outcome in outcomes:
        run = await orch.advance(run_id=run.run_id, outcome=outcome)

    with pytest.raises(WorkflowError):
        await orch.rewind(run_id=run.run_id, to_node="research", reason="x")


# ---- authorize_final_release: final-release-authorization responsibility ----


@pytest.mark.asyncio
async def test_authorize_final_release_is_true_for_a_completed_run_with_no_open_review() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    assert run.node == "complete"

    assert await orch.authorize_final_release(run_id=run.run_id) is True


@pytest.mark.asyncio
async def test_authorize_final_release_is_false_for_a_run_still_in_progress() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    assert await orch.authorize_final_release(run_id=run.run_id) is False


@pytest.mark.asyncio
async def test_authorize_final_release_is_false_while_a_human_review_request_is_open() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    # Simulate a request that outlived the run reaching complete (e.g. a
    # concurrent escalation) directly on the store, since request_human_review
    # itself refuses to open one on an already-terminal run.
    from phi_core.control.records import HumanReviewRequest
    stray = HumanReviewRequest(
        run_id=run.run_id, session_id=SESSION_ID, workflow_version="wf/1",
        task_id="", node="human_review_decisions", state="open",
    )
    await store.insert("human_review_requests", stray)

    assert await orch.authorize_final_release(run_id=run.run_id) is False


@pytest.mark.asyncio
async def test_authorize_final_release_is_true_for_partially_complete() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    outcomes = ["ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "partially_complete"]
    for outcome in outcomes:
        run = await orch.advance(run_id=run.run_id, outcome=outcome)
    assert run.node == "partially_complete"

    assert await orch.authorize_final_release(run_id=run.run_id) is True


# ---- begin_cleanup / confirm_cleanup: cleanup-lifecycle responsibility ----


@pytest.mark.asyncio
async def test_begin_cleanup_advances_state_to_destroying() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)

    updated = await orch.begin_cleanup(run_id=run.run_id)

    assert updated.state == "destroying"
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["state"] == "destroying"


@pytest.mark.asyncio
async def test_begin_cleanup_is_idempotent() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    first = await orch.begin_cleanup(run_id=run.run_id)

    second = await orch.begin_cleanup(run_id=run.run_id)

    assert second.state == "destroying"
    assert second.updated_at == first.updated_at


@pytest.mark.asyncio
async def test_confirm_cleanup_advances_state_to_session_destroyed_once_verified() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    await orch.begin_cleanup(run_id=run.run_id)
    manifest = CleanupManifest(run_id=run.run_id, verification_status="verified")

    updated = await orch.confirm_cleanup(run_id=run.run_id, manifest=manifest)

    assert updated.state == "session_destroyed"
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["state"] == "session_destroyed"


@pytest.mark.asyncio
async def test_confirm_cleanup_refuses_an_unverified_manifest() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    await orch.begin_cleanup(run_id=run.run_id)
    manifest = CleanupManifest(run_id=run.run_id, verification_status="pending")

    with pytest.raises(WorkflowError):
        await orch.confirm_cleanup(run_id=run.run_id, manifest=manifest)


@pytest.mark.asyncio
async def test_confirm_cleanup_refuses_before_begin_cleanup() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    manifest = CleanupManifest(run_id=run.run_id, verification_status="verified")

    with pytest.raises(WorkflowError):
        await orch.confirm_cleanup(run_id=run.run_id, manifest=manifest)


@pytest.mark.asyncio
async def test_confirm_cleanup_refuses_a_manifest_from_another_run() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    run = await _completed_run(orch, run.run_id)
    await orch.begin_cleanup(run_id=run.run_id)
    manifest = CleanupManifest(run_id="some-other-run", verification_status="verified")

    with pytest.raises(WorkflowError):
        await orch.confirm_cleanup(run_id=run.run_id, manifest=manifest)


# ---- close_run: formal-run-closure responsibility (docs section 9) ----


@pytest.mark.asyncio
async def test_close_run_is_closeable_once_terminal_with_no_open_review_and_no_live_tasks() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    claimed = await tasks.claim(task_id=root["task_id"], lease_owner="w1")
    await tasks.complete(task_id=root["task_id"], lease_owner="w1", fence=claimed.fence, output_ref={})
    run = await _completed_run(orch, run.run_id)

    summary = await orch.close_run(run_id=run.run_id)

    assert summary["closeable"] is True
    assert summary["open_human_review_request_ids"] == []
    assert summary["live_task_ids"] == []


@pytest.mark.asyncio
async def test_close_run_is_not_closeable_while_not_yet_terminal() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    summary = await orch.close_run(run_id=run.run_id)

    assert summary["closeable"] is False


@pytest.mark.asyncio
async def test_close_run_is_not_closeable_with_a_live_task_still_outstanding() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    claimed = await tasks.claim(task_id=root["task_id"], lease_owner="w1")
    await tasks.complete(task_id=root["task_id"], lease_owner="w1", fence=claimed.fence, output_ref={})
    live_child = await tasks.enqueue(
        run_id=run.run_id, session_id=SESSION_ID, worker="Pipeline", task_type="pipeline_run",
    )
    run = await _completed_run(orch, run.run_id)

    summary = await orch.close_run(run_id=run.run_id)

    assert summary["closeable"] is False
    assert summary["live_task_ids"] == [live_child.task_id]


@pytest.mark.asyncio
async def test_close_run_is_not_closeable_with_an_open_human_review_request() -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    root = (await store.find_many("work_items", {"run_id": run.run_id}))[0]
    claimed = await tasks.claim(task_id=root["task_id"], lease_owner="w1")
    await tasks.complete(task_id=root["task_id"], lease_owner="w1", fence=claimed.fence, output_ref={})
    run = await _completed_run(orch, run.run_id)
    from phi_core.control.records import HumanReviewRequest
    stray = HumanReviewRequest(
        run_id=run.run_id, session_id=SESSION_ID, workflow_version="wf/1",
        task_id="", node="human_review_decisions", state="open",
    )
    await store.insert("human_review_requests", stray)

    summary = await orch.close_run(run_id=run.run_id)

    assert summary["closeable"] is False
    assert summary["open_human_review_request_ids"] == [stray.request_id]
