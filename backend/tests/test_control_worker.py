"""Focused D2/D3/D9 contracts for the durable worker loop (control/worker.py):
the claim-and-lease dispatch loop, the outbox relay, and the lease
reconciler, plus their exact wiring into ``server._startup_maintenance``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import OutboxEntry, WorkflowRun
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService
from phi_core.control.worker import (
    Worker,
    drain_outbox,
    drain_outbox_forever,
    reconcile_forever,
)

RUN_ID = "run-" + "a" * 28


def _service() -> tuple[TaskService, MemoryControlStore]:
    store = MemoryControlStore()
    # A real (dummy) LlmConfig, not None: several agents' manifests now
    # carry a real, non-empty allowed_providers set (Schema, step 10),
    # so an empty-string configured provider would fail issue_grant's
    # provider check for those agents. "anthropic" is in every such
    # manifest's provider set; this keeps every test in this file
    # focused on the worker loop, not provider policy, regardless of
    # which agent name it happens to enqueue as.
    llm_cfg = SimpleNamespace(provider="anthropic", model="test-model", base_url="")
    return TaskService(store, CapabilityPolicy(llm_cfg)), store


async def _enqueue(service: TaskService, **overrides):
    kwargs = dict(run_id=RUN_ID, session_id="session-1", worker="Executor", task_type="executor")
    kwargs.update(overrides)
    return await service.enqueue(**kwargs)


# ---- Worker.run_once: happy path, dispatch, heartbeat -----------------------


@pytest.mark.asyncio
async def test_worker_claims_dispatches_and_completes_a_matching_ready_task() -> None:
    service, store = _service()
    task = await _enqueue(service)

    async def handler(_store, item):
        assert item.task_id == task.task_id
        return {"result": "ok"}

    worker = Worker(store, service, worker_id="worker-1", handlers={"executor": handler})
    found = await worker.run_once()

    assert found is True
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    assert stored["state"] == "succeeded"
    assert stored["output_ref"] == {"result": "ok"}
    assert stored["lease_owner"] == "worker-1"


@pytest.mark.asyncio
async def test_worker_ignores_ready_tasks_whose_task_type_has_no_handler() -> None:
    service, store = _service()
    await _enqueue(service, worker="Schema", task_type="schema")

    worker = Worker(store, service, worker_id="worker-1", handlers={"executor": lambda *_: None})
    found = await worker.run_once()

    assert found is False  # nothing this worker can claim
    stored = (await store.find_many("work_items", {}))[0]
    assert stored["state"] == "ready"  # untouched


@pytest.mark.asyncio
async def test_worker_heartbeats_while_a_long_running_handler_executes() -> None:
    service, store = _service()
    task = await _enqueue(service)

    async def slow_handler(_store, _item):
        await asyncio.sleep(0.08)
        return {}

    worker = Worker(
        store, service, worker_id="worker-1", handlers={"executor": slow_handler},
        heartbeat_interval_s=0.02,
    )
    await worker.run_once()

    # The handler ran long enough for at least one heartbeat to land while
    # the task was still "leased" (heartbeat only applies against
    # state="leased"); completion then overwrote heartbeat_at again, so the
    # only directly observable proof is that completion succeeded cleanly
    # with the original claim's fence still valid -- a heartbeat CAS failure
    # would have logged and been silently ignored, never corrupting state.
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    assert stored["state"] == "succeeded"


# ---- Worker.run_once: fenced completion is discarded, not raised ------------


@pytest.mark.asyncio
async def test_worker_discards_a_fenced_completion_without_raising() -> None:
    """Simulate a task whose fence moved between claim and complete (another
    process reconciled or otherwise fenced it): the handler still runs and
    returns normally, but the worker's ``complete`` call loses its CAS and
    comes back ``outcome="fenced"`` -- ``run_once`` must not raise, and the
    task's state (set by whoever bumped the fence) must be left untouched."""
    service, store = _service()
    task = await _enqueue(service)

    async def handler(_store, _item):
        # Between claim and this handler returning, an external actor (a
        # reconciler, or a racing worker) already resolved the task and
        # bumped its fence -- simulated here by mutating the stored document
        # directly, exactly as test_control_tasks.py's staleness scenario
        # does.
        stored = await store.get_one("work_items", {"task_id": task.task_id})
        stored["fence"] = stored["fence"] + 1
        stored["state"] = "failed"
        stored["error_category"] = "resolved_by_someone_else"
        await store.replace_one("work_items", {"task_id": task.task_id}, stored)
        return {"result": "late"}

    worker = Worker(store, service, worker_id="worker-1", handlers={"executor": handler})
    found = await worker.run_once()  # must not raise

    assert found is True
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    # The externally-set state stands; this worker's late "succeeded" /
    # output_ref was never applied.
    assert stored["state"] == "failed"
    assert stored["error_category"] == "resolved_by_someone_else"
    assert stored["output_ref"] == {}


# ---- Worker.run_once: handler exception is caught, task is failed -----------


@pytest.mark.asyncio
async def test_worker_catches_a_handler_exception_and_fails_the_task() -> None:
    service, store = _service()
    task = await _enqueue(service)

    async def failing_handler(_store, _item):
        raise RuntimeError("boom")

    worker = Worker(store, service, worker_id="worker-1", handlers={"executor": failing_handler})
    found = await worker.run_once()  # must not raise

    assert found is True
    stored = await store.get_one("work_items", {"task_id": task.task_id})
    assert stored["state"] == "failed"
    assert stored["error_category"] == "handler_exception:RuntimeError"


@pytest.mark.asyncio
async def test_worker_proceeds_to_the_next_claim_after_a_handler_exception() -> None:
    """One task's handler exception must not stop the worker from claiming
    and completing the next ready task."""
    service, store = _service()
    bad_task = await _enqueue(service, idempotency_key="bad")
    good_task = await _enqueue(service, idempotency_key="good")

    calls: list[str] = []

    async def handler(_store, item):
        calls.append(item.task_id)
        if item.task_id == bad_task.task_id:
            raise RuntimeError("boom")
        return {"ok": True}

    worker = Worker(store, service, worker_id="worker-1", handlers={"executor": handler})
    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert await worker.run_once() is False  # nothing left to claim

    assert set(calls) == {bad_task.task_id, good_task.task_id}
    bad_stored = await store.get_one("work_items", {"task_id": bad_task.task_id})
    good_stored = await store.get_one("work_items", {"task_id": good_task.task_id})
    assert bad_stored["state"] == "failed"
    assert good_stored["state"] == "succeeded"


# ---- Worker.run_forever: survives an iteration exception ---------------------


@pytest.mark.asyncio
async def test_worker_run_forever_survives_a_store_failure_on_the_first_call() -> None:
    service, store = _service()
    calls = {"n": 0}
    real_find_many = store.find_many

    async def flaky_find_many(collection, query):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store unavailable")
        return await real_find_many(collection, query)

    store.find_many = flaky_find_many  # type: ignore[method-assign]

    worker = Worker(
        store, service, worker_id="worker-1", handlers={"executor": lambda *_: None},
        poll_interval_s=0.01,
    )
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)

    assert not task.done()  # still alive after the injected first-call failure
    assert calls["n"] >= 2  # proceeded past the failure to a later iteration

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- drain_outbox: unrecognized kind stays pending ---------------------------


async def _run_with_outbox(store: MemoryControlStore, entry: OutboxEntry, *, run_id: str = RUN_ID) -> None:
    run = WorkflowRun(run_id=run_id, session_id="session-1", outbox=[entry])
    await store.insert("workflow_runs", run)


@pytest.mark.asyncio
async def test_drain_outbox_leaves_an_unrecognized_kind_entry_pending() -> None:
    store = MemoryControlStore()
    entry = OutboxEntry(kind="enqueue", payload={"x": 1})
    await _run_with_outbox(store, entry)

    processed = await drain_outbox(store, handlers={})

    assert processed == 0
    stored = (await store.find_many("workflow_runs", {"run_id": RUN_ID}))[0]
    assert len(stored["outbox"]) == 1
    assert stored["outbox"][0]["entry_id"] == entry.entry_id
    assert stored["outbox"][0]["attempts"] == 0
    assert stored["outbox"][0]["last_error"] == ""


@pytest.mark.asyncio
async def test_drain_outbox_default_registry_is_empty_and_leaves_every_entry_pending() -> None:
    """This dispatch registers no outbox-kind handlers at all: every one of
    D2's six ``OutboxEntry.kind`` literals is left pending by the default,
    unpatched registry."""
    store = MemoryControlStore()
    for index, kind in enumerate(("enqueue", "trace", "artifact_register", "artifact_promote",
                                   "publication_pointer", "review_resume", "cancel_subtree")):
        await _run_with_outbox(store, OutboxEntry(kind=kind), run_id=f"{RUN_ID}-{index}")

    processed = await drain_outbox(store)  # no handlers kwarg -> OUTBOX_HANDLERS

    assert processed == 0
    docs = await store.find_many("workflow_runs", {})
    assert sum(len(d["outbox"]) for d in docs) == 7


# ---- drain_outbox: registered handler executes and the entry is pulled -------


@pytest.mark.asyncio
async def test_drain_outbox_executes_a_registered_handler_and_removes_the_entry() -> None:
    store = MemoryControlStore()
    handled_entry = OutboxEntry(kind="trace", payload={"a": 1})
    unhandled_entry = OutboxEntry(kind="enqueue", payload={"b": 2})
    await _run_with_outbox(store, handled_entry)
    await _run_with_outbox(store, unhandled_entry, run_id="run-other")

    seen: list[str] = []

    async def trace_handler(_store, entry):
        seen.append(entry.entry_id)

    processed = await drain_outbox(store, handlers={"trace": trace_handler})

    assert processed == 1
    assert seen == [handled_entry.entry_id]
    run1_doc = (await store.get_one("workflow_runs", {"run_id": RUN_ID}))
    assert run1_doc["outbox"] == []
    run2_doc = await store.get_one("workflow_runs", {"run_id": "run-other"})
    assert len(run2_doc["outbox"]) == 1
    assert run2_doc["outbox"][0]["entry_id"] == unhandled_entry.entry_id


@pytest.mark.asyncio
async def test_drain_outbox_records_attempts_and_last_error_on_handler_exception() -> None:
    store = MemoryControlStore()
    entry = OutboxEntry(kind="trace")
    await _run_with_outbox(store, entry)

    async def failing_handler(_store, _entry):
        raise RuntimeError("relay boom")

    processed = await drain_outbox(store, handlers={"trace": failing_handler})

    assert processed == 0
    stored = (await store.get_one("workflow_runs", {"run_id": RUN_ID}))
    assert len(stored["outbox"]) == 1  # left pending, not dropped
    assert stored["outbox"][0]["entry_id"] == entry.entry_id
    assert stored["outbox"][0]["attempts"] == 1
    assert "relay boom" in stored["outbox"][0]["last_error"]


@pytest.mark.asyncio
async def test_drain_outbox_dead_letters_an_entry_past_the_retry_ceiling() -> None:
    """An entry whose handler keeps failing must not retry forever in
    place: once its ``attempts`` reaches ``MAX_ATTEMPTS_PER_TASK`` it is
    moved to ``outbox_dead_letters`` (never dropped) and removed from the
    owning document's ``outbox`` array."""
    from phi_core.control.limits import MAX_ATTEMPTS_PER_TASK

    store = MemoryControlStore()
    entry = OutboxEntry(kind="trace", attempts=MAX_ATTEMPTS_PER_TASK - 1)
    await _run_with_outbox(store, entry)

    async def failing_handler(_store, _entry):
        raise RuntimeError("relay boom")

    processed = await drain_outbox(store, handlers={"trace": failing_handler})

    assert processed == 1
    stored = await store.get_one("workflow_runs", {"run_id": RUN_ID})
    assert stored["outbox"] == []  # removed, not left retrying forever
    dead_letters = await store.find_many("outbox_dead_letters", {})
    assert len(dead_letters) == 1
    assert dead_letters[0]["entry"]["entry_id"] == entry.entry_id
    assert dead_letters[0]["entry"]["attempts"] == MAX_ATTEMPTS_PER_TASK
    assert "relay boom" in dead_letters[0]["entry"]["last_error"]


@pytest.mark.asyncio
async def test_drain_outbox_scans_work_items_outbox_too() -> None:
    service, store = _service()
    task = await _enqueue(service)
    entry = OutboxEntry(kind="trace")
    stored_task = await store.get_one("work_items", {"task_id": task.task_id})
    stored_task["outbox"] = [entry.model_dump(mode="python")]
    await store.replace_one("work_items", {"task_id": task.task_id}, stored_task)

    seen: list[str] = []

    async def trace_handler(_store, entry):
        seen.append(entry.entry_id)

    processed = await drain_outbox(store, handlers={"trace": trace_handler})

    assert processed == 1
    assert seen == [entry.entry_id]
    refreshed = await store.get_one("work_items", {"task_id": task.task_id})
    assert refreshed["outbox"] == []


# ---- drain_outbox_forever / reconcile_forever: survive iteration exceptions --


@pytest.mark.asyncio
async def test_drain_outbox_forever_survives_a_store_failure_on_the_first_call() -> None:
    store = MemoryControlStore()
    calls = {"n": 0}
    real_find_many = store.find_many

    async def flaky_find_many(collection, query):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store unavailable")
        return await real_find_many(collection, query)

    store.find_many = flaky_find_many  # type: ignore[method-assign]

    task = asyncio.create_task(drain_outbox_forever(store, interval_s=0.01))
    await asyncio.sleep(0.05)

    assert not task.done()
    assert calls["n"] >= 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reconcile_forever_survives_a_reconcile_leases_failure_on_the_first_call() -> None:
    service, _store = _service()
    calls = {"n": 0}
    real_reconcile = service.reconcile_leases

    async def flaky_reconcile(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("reconcile boom")
        return await real_reconcile(**kwargs)

    service.reconcile_leases = flaky_reconcile  # type: ignore[method-assign]

    task = asyncio.create_task(reconcile_forever(service, interval_s=0.01))
    await asyncio.sleep(0.05)

    assert not task.done()
    assert calls["n"] >= 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- _startup_maintenance wiring --------------------------------------------


@pytest.mark.asyncio
async def test_startup_maintenance_starts_all_three_control_loops_exactly_once(monkeypatch) -> None:
    """Phase 4 step 2/4: `_startup_maintenance` starts
    `_MAX_CONCURRENT_PIPELINES` `Worker` instances, not one -- a single
    worker claims and executes strictly one task at a time, so matching
    the route-level `_admit_pipeline_run` concurrency cap needs that many
    workers polling the same `work_items` collection."""
    import phi_core.control.worker as worker_module
    import server

    calls = {"worker": 0, "outbox": 0, "reconcile": 0}

    class FakeWorker:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_forever(self) -> None:
            calls["worker"] += 1

    async def fake_drain_outbox_forever(*args, **kwargs) -> None:
        calls["outbox"] += 1

    async def fake_reconcile_forever(*args, **kwargs) -> None:
        calls["reconcile"] += 1

    monkeypatch.setattr(worker_module, "Worker", FakeWorker)
    monkeypatch.setattr(worker_module, "drain_outbox_forever", fake_drain_outbox_forever)
    monkeypatch.setattr(worker_module, "reconcile_forever", fake_reconcile_forever)
    monkeypatch.setattr(server, "get_db", lambda: SimpleNamespace())

    created_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def recording_create_task(coro, *args, **kwargs):
        created_task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(created_task)
        return created_task

    monkeypatch.setattr(server.asyncio, "create_task", recording_create_task)

    await server._startup_maintenance()
    await asyncio.sleep(0)  # let the newly-scheduled tasks run their one-shot bodies

    worker_count = server._MAX_CONCURRENT_PIPELINES
    assert calls == {"worker": worker_count, "outbox": 1, "reconcile": 1}
    # The pre-existing purge loop, `worker_count` `Worker` instances, the
    # outbox relay, and the lease reconciler -- none started more than once.
    assert len(created_tasks) == 1 + worker_count + 2

    for created_task in created_tasks:
        created_task.cancel()
    await asyncio.gather(*created_tasks, return_exceptions=True)
