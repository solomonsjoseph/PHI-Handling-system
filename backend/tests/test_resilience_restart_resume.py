"""Phase 14 (scale and resilience): Human Review pause/resume across a
real backend restart.

Extends the restart/resume pattern Phase 9 and Phase 12 already proved
(``get_db.cache_clear()`` forces a brand-new ``AsyncIOMotorClient``, and
therefore a brand-new ``MongoControlStore``/``Manager``
instance sharing only the durable Mongo state -- see
``test_control_phase12_cleanup_wiring.py``'s
``test_export_survives_a_simulated_process_restart_between_begin_and_confirm``
and ``test_security_incident.py``'s identical technique) to a scenario
neither existing use of that pattern covers: a run paused at
``awaiting_human_review`` that is resolved only *after* the simulated
restart.
"""
from __future__ import annotations

import uuid

import pytest
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.store import MongoControlStore
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


def _mongo_orch() -> tuple[Manager, TaskService, MongoControlStore]:
    """A fresh ``Manager``/``TaskService``/``MongoControlStore``
    trio against the *current* ``get_db()`` instance -- callers restart
    by calling ``get_db.cache_clear()`` and this helper again."""
    store = MongoControlStore(get_db())
    tasks = TaskService(store, CapabilityPolicy(None))
    return Manager(store, tasks), tasks, store


async def _cleanup(store: MongoControlStore, run_id: str) -> None:
    db = get_db()
    for collection in (
        "workflow_runs", "work_items", "capability_grants",
        "human_review_requests", "human_review_events", "trace_events",
    ):
        await getattr(db, collection).delete_many({"run_id": run_id})


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
        fresh_orch, _fresh_tasks, _fresh_store = _mongo_orch()

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
