"""Phase 14 (scale and resilience): backend restart mid-pipeline, and
Human Review pause/resume across a restart.

Extends the restart/resume pattern Phase 9 and Phase 12 already proved
(``get_db.cache_clear()`` forces a brand-new ``AsyncIOMotorClient``, and
therefore a brand-new ``MongoControlStore``/``SuperOrchestrator``
instance sharing only the durable Mongo state -- see
``test_control_phase12_cleanup_wiring.py``'s
``test_export_survives_a_simulated_process_restart_between_begin_and_confirm``
and ``test_security_incident.py``'s identical technique) to two new
scenarios neither existing use of that pattern covers: a run genuinely
mid-pipeline (not already at a post-completion export/download step),
and a run paused at ``awaiting_human_review`` that is resolved only
*after* the simulated restart.

``test_control_superorchestrator_lifecycle.py`` already proves
``resume()``'s orphaned-task-retry and node/state-reporting behavior
against a shared ``MemoryControlStore`` instance -- a weaker restart
simulation (the store object itself never actually goes away). This file
proves the same behavior holds against a genuinely fresh Motor client
and Mongo-backed store, the real production restart mechanism.
"""
from __future__ import annotations

import uuid

import pytest
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import ResourceBudget
from phi_core.control.store import MongoControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService
from phi_core.db import get_db


@pytest.fixture(autouse=True)
def _fresh_motor_client_per_test():
    """Matches ``test_control_migrate.py``/``test_control_phase12_cleanup_wiring.py``'s
    identical fixture: ``get_db`` is ``@lru_cache``'d process-wide against
    whichever event loop was running at first construction, and
    ``asyncio_mode=strict`` gives each ``@pytest.mark.asyncio`` test its
    own loop."""
    get_db.cache_clear()
    yield
    get_db.cache_clear()


def _sid() -> str:
    return uuid.uuid4().hex


def _mongo_orch() -> tuple[SuperOrchestrator, TaskService, MongoControlStore]:
    """A fresh ``SuperOrchestrator``/``TaskService``/``MongoControlStore``
    trio against the *current* ``get_db()`` instance -- callers restart
    by calling ``get_db.cache_clear()`` and this helper again."""
    store = MongoControlStore(get_db())
    tasks = TaskService(store, CapabilityPolicy(None))
    return SuperOrchestrator(store, tasks), tasks, store


async def _cleanup(store: MongoControlStore, run_id: str) -> None:
    db = get_db()
    for collection in (
        "workflow_runs", "work_items", "capability_grants",
        "human_review_requests", "human_review_events", "trace_events",
    ):
        await getattr(db, collection).delete_many({"run_id": run_id})


# ---- restart mid-pipeline: not yet terminal, not post-completion ----------


@pytest.mark.asyncio
async def test_resume_after_a_real_mongo_restart_reports_the_same_mid_pipeline_node_and_state() -> None:
    """A run genuinely mid-pipeline (``node="decide"``, ``state="running"``,
    never reaching a terminal or post-completion node at all) must report
    identically from a brand-new orchestrator instance built against a
    brand-new Motor client after a simulated restart -- widening
    ``test_control_superorchestrator_lifecycle.py``'s
    ``test_resume_on_a_fresh_instance_reports_the_same_node_and_state_as_the_original``
    (proven only against a shared ``MemoryControlStore``) to the real
    Mongo-backed restart mechanism."""
    orch, _tasks, store = _mongo_orch()
    session_id = _sid()
    run = await orch.start_run(session_id=session_id, principal="operator-1")
    run_id = run.run_id
    try:
        advanced = await orch.advance(run_id=run_id, outcome="ok")
        assert advanced.node not in ("complete", "failed", "cancelled", "blocked", "partially_complete")

        # Simulate a process restart.
        get_db.cache_clear()
        fresh_orch, _fresh_tasks, fresh_store = _mongo_orch()

        plan = await fresh_orch.resume(run_id=run_id)

        assert plan["node"] == advanced.node
        assert plan["state"] == "running"
        assert plan["is_terminal"] is False
    finally:
        await _cleanup(store, run_id)


@pytest.mark.asyncio
async def test_resume_after_a_real_mongo_restart_detects_an_orphaned_in_flight_task_and_marks_it_for_retry() -> None:
    """Widens ``test_control_superorchestrator_lifecycle.py``'s
    ``test_resume_after_restart_detects_an_orphaned_in_flight_task_and_marks_it_for_retry``
    to a genuinely fresh Mongo-backed store: a leased-but-never-completed
    child task (the durable trace a mid-dispatch worker leaves behind,
    per ``resume()``'s own docstring) must still be found and returned to
    ``ready`` for retry from a brand-new orchestrator instance after a
    real restart, not merely a shared in-memory store."""
    orch, tasks, store = _mongo_orch()
    session_id = _sid()
    run = await orch.start_run(session_id=session_id, principal="operator-1")
    run_id = run.run_id
    try:
        root_docs = await store.find_many("work_items", {"run_id": run_id})
        root_task_id = root_docs[0]["task_id"]
        child = await orch.create_child_work(
            run_id=run_id, parent_task_id=root_task_id, task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
        claimed = await tasks.claim(task_id=child.task_id, lease_owner="worker-restart-test", lease_seconds=1)
        assert claimed is not None
        # Force the lease into the past without waiting -- the same
        # technique test_control_tasks.py's own `_expire_lease` uses,
        # applied through the real MongoControlStore API.
        import datetime as _dt
        doc = await store.get_one("work_items", {"task_id": child.task_id})
        past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat()
        doc["lease_expires_at"] = past
        await store.replace_one("work_items", {"task_id": child.task_id}, doc)

        # Simulate a process restart.
        get_db.cache_clear()
        fresh_orch, _fresh_tasks, fresh_store = _mongo_orch()

        plan = await fresh_orch.resume(run_id=run_id)

        assert child.task_id in plan["retried_task_ids"]
        stored = await fresh_store.get_one("work_items", {"task_id": child.task_id})
        assert stored["state"] == "ready"
        assert stored["lease_owner"] == ""
    finally:
        await _cleanup(store, run_id)


# ---- human review pause, restart, then resolve -----------------------------


@pytest.mark.asyncio
async def test_human_review_pause_survives_a_real_mongo_restart_and_resolves_to_running() -> None:
    """A run paused at ``awaiting_human_review`` (via
    ``request_human_review``, the real production pause mechanism) is
    resolved only *after* a simulated restart -- proving the pause state
    itself, the open ``HumanReviewRequest``, and the resolution path
    (``consume_review_event`` + ``advance``) all survive independently of
    any in-process orchestrator/task-service instance, using only the
    durable Mongo document, matching Phase 12's own
    ``get_db.cache_clear()`` restart technique but for the
    awaiting-human-review lifecycle state Phase 12 never exercised."""
    from phi_core.control.records import HumanReviewEvent
    from phi_core.control.workflow import TERMINAL_NODES

    orch, tasks, store = _mongo_orch()
    session_id = _sid()
    run = await orch.start_run(session_id=session_id, principal="operator-1")
    run_id = run.run_id
    try:
        root_docs = await store.find_many("work_items", {"run_id": run_id})
        root_task_id = root_docs[0]["task_id"]

        request = await orch.request_human_review(
            run_id=run_id, task_id=root_task_id, node="human_review_decisions",
            reason_codes=["decision_routed_human_review"],
            decision_version=0,
        )
        paused = await store.get_one("workflow_runs", {"run_id": run_id})
        assert paused["state"] == "awaiting_human_review"
        assert paused["node"] == "human_review_decisions"
        assert request.state == "open"

        # Simulate a process restart -- the paused state is entirely
        # durable; nothing about it lived only in the pre-restart
        # orchestrator/store instances.
        get_db.cache_clear()
        fresh_orch, fresh_tasks, fresh_store = _mongo_orch()

        # A resume() against a paused-for-human-review run must not
        # itself disturb the pause -- resuming is not the same as
        # resolving.
        plan = await fresh_orch.resume(run_id=run_id)
        assert plan["state"] == "awaiting_human_review"
        assert plan["node"] == "human_review_decisions"

        # Resolve the review on the fresh instance -- the request_id came
        # from the pre-restart call, proving it is a durable identity,
        # not something the old in-process object still needs to be
        # alive to reference.
        event = HumanReviewEvent(
            run_id=run_id, request_id=request.request_id, session_id=session_id,
            workflow_version="wf/1", task_id="", seq=1, client_event_id="c1",
            principal="reviewer-1", kind="resolution", body_hash="deadbeef",
            resolutions=[],
        )
        resumed_run = await fresh_orch.consume_review_event(run_id=run_id, event=event)
        assert resumed_run.state == "running"

        final_run = await fresh_orch.advance(run_id=run_id, outcome="resolved")
        assert final_run.node != "human_review_decisions"
        # Every subsequent read (including from a THIRD post-resolution
        # instance) sees the same durable, non-paused state.
        get_db.cache_clear()
        _third_orch, _third_tasks, third_store = _mongo_orch()
        durable = await third_store.get_one("workflow_runs", {"run_id": run_id})
        assert durable["state"] == "running"
        assert durable["node"] not in TERMINAL_NODES or durable["node"] == final_run.node

        stored_request = await third_store.get_one(
            "human_review_requests", {"request_id": request.request_id}
        )
        assert stored_request["state"] == "resolved"
    finally:
        await _cleanup(store, run_id)
