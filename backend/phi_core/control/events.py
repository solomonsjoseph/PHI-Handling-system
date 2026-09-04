"""D15: the sole authorized writer of ``trace_events``, and the process-local
SSE fan-out broker.

``TraceEventStore.append`` is the only place in the codebase that inserts
into ``trace_events`` -- proven by
``test_architecture_boundaries.py::test_only_trace_event_store_writes_trace_events``.
It owns ``seq`` allocation (an atomic ``$inc`` on
``workflow_runs.event_seq``), the
``prev_hash``/``hash`` chain, and the fence check that keeps a superseded
worker from publishing a terminal event for a run it no longer owns.

``seq`` comes from an atomic single-field ``$inc`` on
``workflow_runs.event_seq``, so two concurrent ``append`` calls for the
same run can never compute the same ``seq`` and collide on the underlying
``(run_id, seq)`` unique index. This previously used a read-then-CAS loop
and the claim of structural safety did not hold: ``compare_and_set``
replaces the whole document, so any other lifecycle writer working from an
older snapshot rewound ``event_seq`` and the next allocation reused a
number already inserted. Three specialists starting together reproduced it
on every run. D15 describes retrying a duplicate-key insert; incrementing
the counter atomically removes the race instead of catching it afterwards.

``EventBroker`` replaces the single shared ``asyncio.Queue`` per session
(``server.py``'s former ``_progress_queues``) that silently split each
event between whichever concurrent subscriber happened to call
``queue.get()`` next, rather than delivering it to every subscriber. Each
subscriber gets its own bounded queue; a slow consumer is resynced and
dropped rather than allowed to silently miss events or block every other
subscriber.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .records import TraceEvent
from .store import ControlStore
from .trace_sanitizer import sanitize_payload, sanitize_status_text

# TraceEvent.outcome values that mean "this run just reached a state from
# which no further trace event for it will ever be appended." Mirrors the
# terminal RunState values plus the two legacy session-status strings
# (`intake_failed`, `expired_awaiting_review`) that predate WorkflowRun and
# are never reachable as a RunState. A terminal event must be attributed to
# the work item that produced it and carry its fence, so a worker whose
# lease was reconciled away out from under it (a zombie still running past
# its lease) cannot publish the winning terminal event after a different
# worker has already completed the same task.
TERMINAL_RUN_OUTCOMES = frozenset({
    "complete", "failed", "cancelled", "blocked", "partially_complete",
    "awaiting_human_review", "intake_failed", "expired_awaiting_review",
})


class EventAppendError(RuntimeError):
    """Raised with a fixed, testable ``reason`` on any append refusal."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic JSON serialization used for the hash chain: sorted
    keys, no whitespace, ``str()`` for anything ``json`` cannot encode
    natively (mirrors the digest family already used for egress hashing)."""
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


class TraceEventStore:
    """Append-only, hash-chained ``trace_events`` writer scoped to one
    ``(run_id, session_id)``."""

    def __init__(self, store: ControlStore, *, run_id: str, session_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._session_id = session_id

    async def _allocate_seq(self) -> int:
        """Atomically ``$inc`` ``workflow_runs.event_seq``. A run with no
        durable ``WorkflowRun`` document (a unit-test fixture, or a pre-D9
        legacy session) allocates ``seq=0`` every time -- best-effort,
        matching the writer this replaces, rather than refusing to trace
        at all.

        A read-then-compare-and-set loop is not safe for this counter, and
        that is not theoretical: ``compare_and_set`` replaces the whole
        ``workflow_runs`` document, so an unrelated lifecycle write built
        from a snapshot taken before the last allocation puts ``event_seq``
        back, and the next allocation reissues a sequence number that
        ``trace_events``' unique ``(run_id, seq)`` index already holds. The
        three specialists running in parallel at t=0 hit exactly that and
        every one of them died with a DuplicateKeyError. A single-field
        ``$inc`` never hands out the same number twice.
        """
        seq = await self._store.increment_field(
            "workflow_runs", {"run_id": self._run_id}, "event_seq"
        )
        if seq is None:
            return 0
        return seq

    async def _checked_fence(self, event: TraceEvent, fence: int | None) -> int:
        if event.outcome not in TERMINAL_RUN_OUTCOMES:
            return fence or 0
        if fence is None:
            raise EventAppendError("fence_required", event.outcome)
        if not event.task_id:
            raise EventAppendError("task_id_required_for_terminal_event", event.outcome)
        work_item = await self._store.get_one("work_items", {"task_id": event.task_id})
        if work_item is None:
            raise EventAppendError("work_item_missing", event.task_id)
        if int(work_item.get("fence", 0)) != fence:
            raise EventAppendError(
                "stale_fence", f"event fence={fence} work_item fence={work_item.get('fence')}"
            )
        return fence

    async def append(self, event: TraceEvent, *, fence: int | None = None) -> TraceEvent:
        """Allocate ``seq``, check the fence for a terminal outcome, chain
        the hash, and insert. Returns the event actually persisted (with
        ``run_id``/``session_id``/``seq``/``fence``/``prev_hash``/``hash``
        filled in, regardless of what the caller passed for them)."""
        checked_fence = await self._checked_fence(event, fence)
        seq = await self._allocate_seq()
        prev = await self._store.get_one("trace_events", {"run_id": self._run_id, "seq": seq - 1}) if seq else None
        prev_hash = prev["hash"] if prev else ""
        # D66/D3/D4: sanitize before hashing/insertion, never raw-then-redact --
        # the sanitized payload/status_text/retry_category is what gets
        # chained, so the hash itself attests to the sanitized content, not
        # the pre-sanitized input. status_text and retry_category are plain
        # free-text fields real callers already interpolate raw study
        # content into (a dictionary entry name, a column name, a raw
        # exception str(exc) forwarded out of a sandboxed child process),
        # so they get the same scrub_persisted_text pass every other
        # persisted free-text field in this codebase already gets.
        candidate = event.model_copy(update={
            "run_id": self._run_id, "session_id": self._session_id, "seq": seq,
            "fence": checked_fence, "prev_hash": prev_hash, "hash": "",
            "payload": sanitize_payload(event.payload),
            "status_text": sanitize_status_text(event.status_text),
            "retry_category": sanitize_status_text(event.retry_category),
        })
        payload = candidate.model_dump(mode="json")
        payload.pop("hash")
        event_hash = hashlib.sha256((prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()
        final = candidate.model_copy(update={"hash": event_hash})
        await self._store.insert("trace_events", final)
        return final

    async def seal_range(self, *, from_seq: int, to_seq: int, archive_artifact_id: str = "") -> dict[str, Any]:
        """Record a ``trace_segments`` row proving the hash chain from
        ``from_seq`` to ``to_seq`` (inclusive) is intact, so a later purge
        of that range can be authorized against it. ``segment_hash`` is the
        chained ``hash`` of the event at ``to_seq`` -- the chain already
        covers every earlier event in the run back to genesis, so that one
        value is a complete proof of the whole prefix."""
        from datetime import datetime, timezone

        last = await self._store.get_one("trace_events", {"run_id": self._run_id, "seq": to_seq})
        if last is None:
            raise EventAppendError("segment_end_missing", f"run_id={self._run_id} seq={to_seq}")
        segment = {
            "schema_version": 1,
            "segment_id": uuid4().hex,
            "run_id": self._run_id,
            "from_seq": from_seq,
            "to_seq": to_seq,
            "segment_hash": last["hash"],
            "sealed_at": datetime.now(timezone.utc).isoformat(),
            "archive_artifact_id": archive_artifact_id,
        }
        await self._store.insert("trace_segments", segment)
        # D68: roll the sealed segment's proof up onto WorkflowRun
        # (this codebase's RunManifest) as `trace_root_hash`. Best-effort,
        # matching `_allocate_seq`'s own posture toward a run with no
        # durable WorkflowRun document (unit-test fixture / pre-D9 run):
        # sealing still succeeds, it just has nowhere to roll the hash up
        # to.
        run = await self._store.get_one("workflow_runs", {"run_id": self._run_id})
        if run is not None:
            updated_run = dict(run)
            updated_run["trace_root_hash"] = last["hash"]
            await self._store.compare_and_set(
                "workflow_runs", {"run_id": self._run_id},
                {"trace_root_hash": run.get("trace_root_hash", "")}, updated_run,
            )
        return segment

    async def seal_and_archive_range(self, *, from_seq: int, to_seq: int, artifact_service: Any) -> dict[str, Any]:
        """D15 step 4c: materialize every event in ``[from_seq, to_seq]``
        to a ``trace_archive`` artifact through ``artifact_service``'s own
        stage/finalize two-phase commit, then seal the range with that
        artifact's id recorded -- so an archived trace is registry-owned
        like every other artifact *before* ``purge_range`` can ever
        delete the events it captures. ``artifact_service`` is typed
        loosely (an ``ArtifactService``) to avoid this leaf module
        depending on ``control/artifacts.py``.

        Refuses (``empty_range``) rather than sealing and archiving
        nothing when the range has no events."""
        all_events = await self._store.find_many("trace_events", {"run_id": self._run_id})
        in_range = sorted(
            (e for e in all_events if from_seq <= e.get("seq", -1) <= to_seq),
            key=lambda e: e["seq"],
        )
        if not in_range:
            raise EventAppendError("empty_range", f"run_id={self._run_id} [{from_seq},{to_seq}]")
        payload = "\n".join(canonical_json(e) for e in in_range).encode("utf-8")
        artifact_id, tmp_path = await artifact_service.stage(
            "trace_archive", f"trace-{self._run_id}-{from_seq}-{to_seq}.jsonl", "internal", "long",
        )
        tmp_path.write_bytes(payload)
        await artifact_service.finalize(artifact_id)
        return await self.seal_range(from_seq=from_seq, to_seq=to_seq, archive_artifact_id=artifact_id)

    async def purge_range(self, *, from_seq: int, to_seq: int, principal: str, reason: str) -> int:
        """Delete every event in an already-sealed ``[from_seq, to_seq]``
        range, writing a ``trace_purge_tombstones`` row first so an
        authorized deletion is always distinguishable from tampering (a
        gap in the sequence with no matching tombstone). Refuses
        (``segment_not_sealed``) when no ``trace_segments`` row covers
        exactly this range -- a purge can only ever act on a range that
        was deliberately sealed first, never an arbitrary slice."""
        from datetime import datetime, timezone

        segment = await self._store.get_one(
            "trace_segments", {"run_id": self._run_id, "from_seq": from_seq, "to_seq": to_seq},
        )
        if segment is None:
            raise EventAppendError("segment_not_sealed", f"run_id={self._run_id} [{from_seq},{to_seq}]")
        tombstone = {
            "schema_version": 1,
            "tombstone_id": uuid4().hex,
            "run_id": self._run_id,
            "from_seq": from_seq,
            "to_seq": to_seq,
            "segment_hash": segment["segment_hash"],
            "purged_by": principal,
            "purged_reason": reason,
            "purged_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._store.insert("trace_purge_tombstones", tombstone)
        return await self._store.delete_many(
            "trace_events",
            {"run_id": self._run_id, "seq": {"$gte": from_seq, "$lte": to_seq}},
        )


@dataclass
class Subscription:
    """A single SSE client's own bounded queue. ``overflowed`` is set once
    this subscriber has fallen behind and been sent a resync frame; the
    generator consuming ``queue`` closes the stream on the next item it
    reads regardless of that item's content once ``overflowed`` is true,
    since the resync frame is always the last thing queued."""

    subscriber_id: str
    run_id: str
    queue: "asyncio.Queue[dict[str, Any]]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    overflowed: bool = False


class EventBroker:
    """Process-local, run_id-scoped SSE fan-out. One instance is shared
    process-wide (mirrors the old module-level ``_progress_queues`` dict it
    replaces)."""

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscriptions: dict[str, dict[str, Subscription]] = {}

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscriptions.get(run_id, {}))

    def subscribe(self, run_id: str) -> Subscription:
        sub = Subscription(subscriber_id=uuid4().hex, run_id=run_id,
                            queue=asyncio.Queue(maxsize=self._queue_maxsize))
        self._subscriptions.setdefault(run_id, {})[sub.subscriber_id] = sub
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Drop one subscriber. Removes the run's entry entirely once it
        was the last one, so a run nobody is watching leaves nothing
        behind (the leak the old ``_release_stream`` could not reach when
        the pipeline itself never emitted anything for it)."""
        bucket = self._subscriptions.get(sub.run_id)
        if bucket is None:
            return
        bucket.pop(sub.subscriber_id, None)
        if not bucket:
            self._subscriptions.pop(sub.run_id, None)

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every current subscriber of ``run_id``. A
        subscriber whose queue is full is sent one
        ``{"phase": "__resync__"}`` frame instead (dropping its own
        backlog to make room, since the frame's whole point is "stop
        trusting what you have and refetch"), then marked ``overflowed``
        so its generator closes the stream after delivering it -- a slow
        consumer never silently loses an event instead of being told to
        catch up. Publishing ``phase == "__end__"`` fans the end frame out
        the same way and then drops the run's bucket entirely, even when
        no subscriber ever connected, so a pipeline that finishes before
        anyone opened a stream leaves nothing pinned."""
        bucket = self._subscriptions.get(run_id, {})
        for sub in list(bucket.values()):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                while not sub.queue.empty():
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                sub.overflowed = True
                sub.queue.put_nowait({"phase": "__resync__", "cursor": event.get("seq")})
        if event.get("phase") == "__end__":
            self._subscriptions.pop(run_id, None)
