"""D15 acceptance tests: ``TraceEventStore`` (seq allocation, hash chaining,
the terminal-outcome fence check, sealing, and purge) and ``EventBroker``
(per-subscriber fan-out, overflow resync, and the ``__end__`` bucket
teardown that replaces the single shared queue's leak)."""
from __future__ import annotations

import pytest
from phi_core.control.events import EventAppendError, EventBroker, TraceEventStore
from phi_core.control.records import TraceEvent, WorkflowRun, WorkItem
from phi_core.control.store import MemoryControlStore

RUN_ID = "run-" + "a" * 28
SESSION_ID = "session-" + "b" * 24


def _event(**overrides) -> TraceEvent:
    kwargs = dict(run_id=RUN_ID, seq=0, session_id=SESSION_ID, input_class="internal", output_class="internal")
    kwargs.update(overrides)
    return TraceEvent(**kwargs)


async def _seed_run(store: MemoryControlStore) -> None:
    await store.insert("workflow_runs", WorkflowRun(run_id=RUN_ID, session_id=SESSION_ID))


async def _seed_work_item(store: MemoryControlStore, *, task_id: str, fence: int) -> None:
    await store.insert(
        "work_items",
        WorkItem(
            task_id=task_id, run_id=RUN_ID, session_id=SESSION_ID, worker="Executor", task_type="executor",
            fence=fence, idempotency_key="k",
        ),
    )


# ---- seq allocation and hash chaining --------------------------------------


@pytest.mark.asyncio
async def test_append_allocates_sequential_seq_and_chains_hashes() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    first = await trace.append(_event(agent="Lexicon"))
    second = await trace.append(_event(agent="Statute"))
    third = await trace.append(_event(agent="Praxis"))

    assert [e.seq for e in (first, second, third)] == [1, 2, 3]
    assert first.prev_hash == ""
    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash
    assert len({first.hash, second.hash, third.hash}) == 3  # no collisions
    assert first.run_id == RUN_ID and first.session_id == SESSION_ID


@pytest.mark.asyncio
async def test_append_without_a_durable_run_falls_back_to_seq_zero_every_time() -> None:
    """No ``workflow_runs`` row exists for this run_id (a unit-test
    fixture, or a pre-D9 legacy session) -- best-effort ``seq=0`` on every
    call rather than refusing to trace at all, matching the writer this
    replaces."""
    store = MemoryControlStore()
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    first = await trace.append(_event())
    second = await trace.append(_event())

    assert first.seq == 0
    assert second.seq == 0


# ---- terminal-outcome fence check -------------------------------------------


@pytest.mark.asyncio
async def test_terminal_outcome_requires_a_fence() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    with pytest.raises(EventAppendError) as exc:
        await trace.append(_event(outcome="complete", task_id="t1"))
    assert exc.value.reason == "fence_required"


@pytest.mark.asyncio
async def test_terminal_outcome_requires_a_task_id() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    with pytest.raises(EventAppendError) as exc:
        await trace.append(_event(outcome="failed"), fence=0)
    assert exc.value.reason == "task_id_required_for_terminal_event"


@pytest.mark.asyncio
async def test_terminal_outcome_rejects_a_missing_work_item() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    with pytest.raises(EventAppendError) as exc:
        await trace.append(_event(outcome="cancelled", task_id="ghost-task"), fence=0)
    assert exc.value.reason == "work_item_missing"


@pytest.mark.asyncio
async def test_terminal_outcome_rejects_a_stale_fence_but_accepts_the_current_one() -> None:
    """The exact race D15 describes: a worker whose lease was reconciled
    away (bumping the work item's fence) cannot publish the terminal event
    for a task another worker has since completed."""
    store = MemoryControlStore()
    await _seed_run(store)
    await _seed_work_item(store, task_id="root-task", fence=5)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    with pytest.raises(EventAppendError) as exc:
        await trace.append(_event(outcome="complete", task_id="root-task"), fence=4)
    assert exc.value.reason == "stale_fence"

    accepted = await trace.append(_event(outcome="complete", task_id="root-task"), fence=5)
    assert accepted.outcome == "complete"
    assert accepted.fence == 5


@pytest.mark.asyncio
async def test_non_terminal_outcome_ignores_fence_entirely() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    appended = await trace.append(_event(outcome="ok", task_id="whatever"))
    assert appended.fence == 0


# ---- sealing and purge -------------------------------------------------------


@pytest.mark.asyncio
async def test_seal_range_and_purge_range_round_trip() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)
    for _ in range(3):
        await trace.append(_event())

    segment = await trace.seal_range(from_seq=1, to_seq=3)
    assert segment["segment_hash"]

    removed = await trace.purge_range(from_seq=1, to_seq=3, principal="lead_reviewer-1", reason="retention")
    assert removed == 3
    assert await store.find_many("trace_events", {"run_id": RUN_ID}) == []
    tombstones = await store.find_many("trace_purge_tombstones", {"run_id": RUN_ID})
    assert len(tombstones) == 1
    assert tombstones[0]["segment_hash"] == segment["segment_hash"]
    assert tombstones[0]["purged_by"] == "lead_reviewer-1"


@pytest.mark.asyncio
async def test_seal_and_archive_range_registers_a_trace_archive_artifact_before_purge() -> None:
    from phi_core.control.artifacts import ArtifactService

    hex_session_id = "b" * 32
    hex_run_id = "c" * 32
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=hex_run_id, session_id=hex_session_id))
    trace = TraceEventStore(store, run_id=hex_run_id, session_id=hex_session_id)
    for agent in ("Lexicon", "Statute", "Praxis"):
        await trace.append(TraceEvent(
            run_id=hex_run_id, seq=0, session_id=hex_session_id, agent=agent,
            input_class="internal", output_class="internal",
        ))
    service = ArtifactService(store, session_id=hex_session_id, run_id=hex_run_id)

    segment = await trace.seal_and_archive_range(from_seq=1, to_seq=3, artifact_service=service)

    assert segment["archive_artifact_id"]
    artifact = await store.get_one("artifacts", {"artifact_id": segment["archive_artifact_id"]})
    assert artifact is not None
    assert artifact["type"] == "trace_archive"
    assert artifact["state"] == "staged"

    # The archive is registered *before* purge, so it survives the events
    # it captured being deleted.
    await trace.purge_range(from_seq=1, to_seq=3, principal="lead_reviewer-1", reason="retention")
    assert await store.find_many("trace_events", {"run_id": hex_run_id}) == []
    assert await store.get_one("artifacts", {"artifact_id": segment["archive_artifact_id"]}) is not None


@pytest.mark.asyncio
async def test_seal_and_archive_range_refuses_an_empty_range() -> None:
    from phi_core.control.artifacts import ArtifactService

    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)
    service = ArtifactService(store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(EventAppendError) as exc:
        await trace.seal_and_archive_range(from_seq=1, to_seq=3, artifact_service=service)
    assert exc.value.reason == "empty_range"


@pytest.mark.asyncio
async def test_purge_refuses_an_unsealed_range() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)
    await trace.append(_event())

    with pytest.raises(EventAppendError) as exc:
        await trace.purge_range(from_seq=0, to_seq=0, principal="p", reason="r")
    assert exc.value.reason == "segment_not_sealed"
    assert await store.find_many("trace_events", {"run_id": RUN_ID}) != []


# ---- EventBroker fan-out ------------------------------------------------------


def test_broker_fans_out_the_same_event_to_every_subscriber() -> None:
    broker = EventBroker()
    a = broker.subscribe(RUN_ID)
    b = broker.subscribe(RUN_ID)

    broker.publish(RUN_ID, {"phase": "agent_phase:x"})

    assert a.queue.get_nowait() == {"phase": "agent_phase:x"}
    assert b.queue.get_nowait() == {"phase": "agent_phase:x"}


def test_broker_overflow_evicts_backlog_sends_resync_and_marks_overflowed() -> None:
    broker = EventBroker(queue_maxsize=1)
    sub = broker.subscribe(RUN_ID)

    broker.publish(RUN_ID, {"phase": "one", "seq": 1})
    broker.publish(RUN_ID, {"phase": "two", "seq": 2})  # queue full -> resync

    assert sub.overflowed is True
    assert sub.queue.qsize() == 1
    assert sub.queue.get_nowait() == {"phase": "__resync__", "cursor": 2}


def test_broker_end_drops_the_run_bucket_and_stops_further_delivery() -> None:
    broker = EventBroker()
    sub = broker.subscribe(RUN_ID)

    broker.publish(RUN_ID, {"phase": "__end__"})

    assert sub.queue.get_nowait() == {"phase": "__end__"}
    assert broker.subscriber_count(RUN_ID) == 0


def test_broker_end_with_no_subscribers_is_a_harmless_no_op() -> None:
    broker = EventBroker()
    broker.publish(RUN_ID, {"phase": "__end__"})  # nobody ever subscribed
    assert broker.subscriber_count(RUN_ID) == 0


def test_unsubscribe_removes_only_that_subscriber() -> None:
    broker = EventBroker()
    a = broker.subscribe(RUN_ID)
    broker.subscribe(RUN_ID)
    assert broker.subscriber_count(RUN_ID) == 2

    broker.unsubscribe(a)

    assert broker.subscriber_count(RUN_ID) == 1


def test_unsubscribe_the_last_subscriber_drops_the_run_entry() -> None:
    broker = EventBroker()
    sub = broker.subscribe(RUN_ID)
    broker.unsubscribe(sub)
    assert broker.subscriber_count(RUN_ID) == 0
    # Publishing to a run with no bucket at all must not raise.
    broker.publish(RUN_ID, {"phase": "anything"})
