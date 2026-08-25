"""Durable worker loop (D2/D3/D9 plumbing): claim-and-lease dispatch, the
outbox relay, and lease reconciliation.

Three independent supervised loops, each started once from
``server._startup_maintenance``:

- ``Worker.run_forever`` polls ``work_items`` for a ``ready`` task whose
  ``task_type`` has a registered handler, claims it through
  ``TaskService.claim``, heartbeats while the handler runs, and reports the
  outcome through ``TaskService.complete`` / ``TaskService.fail``. A
  ``"fenced"`` outcome from either call means a different worker now owns
  the task by the time this one tried to report -- the result is discarded,
  never raised, and the loop moves on to the next claim.
- ``drain_outbox_forever`` runs ``drain_outbox`` on a fixed interval: every
  ``OutboxEntry`` still sitting in a ``workflow_runs`` or ``work_items``
  document's embedded ``outbox`` array (D2) is dispatched by ``kind`` to a
  registered handler and pulled from the array once the handler returns
  without raising. An entry whose ``kind`` has no registered handler is left
  in place -- pending, not dropped, not errored.
- ``reconcile_forever`` wraps ``TaskService.reconcile_leases`` on
  ``LEASE_RECONCILE_INTERVAL_S``.

This dispatch (Phase 4 step 2) registers no task-type or outbox-kind
handlers: ``TASK_HANDLERS`` and ``OUTBOX_HANDLERS`` are both empty. The three
loops are pure plumbing -- they claim, drain, and reconcile correctly with
nothing yet to dispatch to. Every loop catches and logs any exception raised
by one iteration and continues to the next; no unhandled exception from a
single claim, handler, drain pass, or reconciliation sweep is ever allowed
to end the loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from .limits import HEARTBEAT_INTERVAL_S, LEASE_RECONCILE_INTERVAL_S, LEASE_SECONDS, MAX_ATTEMPTS_PER_TASK
from .records import OutboxEntry, WorkflowRun, WorkItem
from .store import ControlStore
from .tasks import TaskService

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- task claim-and-lease loop --------------------------------------------

TaskHandler = Callable[[ControlStore, WorkItem], Awaitable[Optional[dict[str, Any]]]]

# Empty in this dispatch: no ``WorkItem.task_type`` has a registered handler
# yet. Populated by later phases as real pipeline work moves onto durable
# tasks; ``Worker`` itself needs no change when that happens -- a claimed
# task whose type has no handler is simply left alone (its lease expires and
# ``reconcile_leases`` returns it to ``ready``).
TASK_HANDLERS: Mapping[str, TaskHandler] = {}

DEFAULT_POLL_INTERVAL_S = 1.0


class Worker:
    """Claims ``ready`` work items whose ``task_type`` this worker handles,
    dispatches to the matching handler, heartbeats while it runs, and
    reports completion or failure through fenced ``TaskService`` calls.

    Every failure mode is contained at the task granularity: a lost claim
    race, a handler exception, and a fenced completion or failure report are
    all handled without raising out of ``run_once``, so ``run_forever``'s
    own exception guard is a second line of defence, not the only one.
    """

    def __init__(
        self,
        store: ControlStore,
        task_service: TaskService,
        *,
        worker_id: str,
        handlers: Mapping[str, TaskHandler] | None = None,
        lease_seconds: int = LEASE_SECONDS,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._store = store
        self._tasks = task_service
        self.worker_id = worker_id
        self._handlers: Mapping[str, TaskHandler] = (
            dict(handlers) if handlers is not None else TASK_HANDLERS
        )
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_s = heartbeat_interval_s
        self._poll_interval_s = poll_interval_s

    async def _next_candidate(self) -> WorkItem | None:
        """The first ``ready`` task whose ``task_type`` this worker handles
        and whose ``next_eligible_at`` backoff (if any) has elapsed, or
        ``None`` when there is nothing this worker can claim right now."""
        if not self._handlers:
            return None
        now = _now_iso()
        for document in await self._store.find_many("work_items", {"state": "ready"}):
            item = WorkItem.model_validate(document)
            if item.task_type not in self._handlers:
                continue
            if item.next_eligible_at and item.next_eligible_at > now:
                continue
            return item
        return None

    async def run_once(self) -> bool:
        """One claim-dispatch-report cycle.

        Returns ``True`` when a candidate task was found this tick
        (regardless of whether the claim race was won), ``False`` when there
        was nothing eligible to claim.
        """
        candidate = await self._next_candidate()
        if candidate is None:
            return False
        claimed = await self._tasks.claim(
            task_id=candidate.task_id, lease_owner=self.worker_id, lease_seconds=self._lease_seconds
        )
        if claimed is None:
            # Lost the claim race to another worker; nothing to report.
            return True
        await self._execute(claimed)
        return True

    async def _heartbeat_loop(self, claimed: WorkItem) -> None:
        """Extends the lease at ``heartbeat_interval_s`` while a handler
        runs. A heartbeat CAS failure only means the lease has already moved
        on (reconciled away, or the task otherwise fenced); it is logged and
        the loop keeps trying -- the eventual ``complete``/``fail`` call is
        what actually decides whether this worker's work was accepted."""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                await self._tasks.heartbeat(
                    task_id=claimed.task_id,
                    lease_owner=self.worker_id,
                    fence=claimed.fence,
                    lease_seconds=self._lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Worker %s: heartbeat failed for task %s", self.worker_id, claimed.task_id
                )

    async def _stop_heartbeat(self, heartbeat_task: "asyncio.Task[None]") -> None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    async def _execute(self, claimed: WorkItem) -> None:
        handler = self._handlers.get(claimed.task_type)
        if handler is None:
            # Claimed a task type this worker no longer (or never) handles --
            # the registry can shrink between listing and claiming. Leave the
            # lease alone; it expires and reconcile_leases returns it to
            # ready for a worker that does handle it.
            return
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(claimed))
        try:
            output_ref = await handler(self._store, claimed)
        except asyncio.CancelledError:
            await self._stop_heartbeat(heartbeat_task)
            raise
        except Exception as exc:
            logger.exception(
                "Worker %s: handler for task_type=%s task_id=%s failed",
                self.worker_id, claimed.task_type, claimed.task_id,
            )
            await self._stop_heartbeat(heartbeat_task)
            outcome = await self._tasks.fail(
                task_id=claimed.task_id,
                lease_owner=self.worker_id,
                fence=claimed.fence,
                error_category=f"handler_exception:{type(exc).__name__}",
            )
            if outcome.outcome == "fenced":
                logger.info(
                    "Worker %s: task %s fenced before its failure could be recorded; discarded",
                    self.worker_id, claimed.task_id,
                )
            return
        await self._stop_heartbeat(heartbeat_task)
        outcome = await self._tasks.complete(
            task_id=claimed.task_id,
            lease_owner=self.worker_id,
            fence=claimed.fence,
            output_ref=output_ref or {},
        )
        if outcome.outcome == "fenced":
            # A different worker now owns this task -- lease reconciled and
            # reclaimed, or a racing completion already landed. This
            # worker's result is stale and is discarded silently.
            logger.info(
                "Worker %s: task %s fenced before completion could be recorded; discarded",
                self.worker_id, claimed.task_id,
            )

    async def run_forever(self) -> None:
        """The supervised claim loop: never exits on an iteration failure.

        One bad claim, dispatch, or store error is logged and the loop polls
        again rather than dying and leaving every future work item unclaimed.
        """
        while True:
            try:
                found = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker %s: claim loop iteration failed", self.worker_id)
                found = False
            if not found:
                await asyncio.sleep(self._poll_interval_s)


# ---- outbox relay -----------------------------------------------------------

OutboxHandler = Callable[[ControlStore, OutboxEntry], Awaitable[None]]

# Empty in this dispatch: none of D2's ``OutboxEntry.kind`` values
# (``enqueue``, ``trace``, ``artifact_register``, ``artifact_promote``,
# ``publication_pointer``, ``review_resume``, ``cancel_subtree``) has a
# handler registered yet. ``drain_outbox`` leaves every entry pending rather
# than dropping or erroring it.
OUTBOX_HANDLERS: Mapping[str, OutboxHandler] = {}

DEFAULT_OUTBOX_DRAIN_INTERVAL_S = 5.0

# The two record types D3 gives an embedded ``outbox: list[OutboxEntry]``
# field, paired with the field that uniquely identifies each document, per
# D2 ("appended ... inside that same document" -- the document that owns the
# state transition).
_OUTBOX_OWNERS: tuple[tuple[str, type[WorkflowRun] | type[WorkItem], str], ...] = (
    ("workflow_runs", WorkflowRun, "run_id"),
    ("work_items", WorkItem, "task_id"),
)


async def drain_outbox(
    store: ControlStore, *, handlers: Mapping[str, OutboxHandler] | None = None
) -> int:
    """One pass over every outbox-owning document, executing and pulling
    every entry whose ``kind`` has a registered handler.

    Idempotent by construction: a handler is invoked at most once per entry
    per pass, and the entry is pulled from its owning document's ``outbox``
    array only after the handler returns without raising, under a
    compare-and-set against the exact array this pass read. A concurrent
    writer that changes the array first loses this pass's CAS; the entry is
    retried on a later pass rather than lost or double-applied. An entry
    whose ``kind`` has no registered handler is copied into the remaining
    array untouched -- it stays pending.

    Returns the number of entries executed and removed this pass.
    """
    active_handlers = OUTBOX_HANDLERS if handlers is None else handlers
    processed = 0
    for collection, model, id_field in _OUTBOX_OWNERS:
        for raw in await store.find_many(collection, {}):
            raw_outbox = raw.get("outbox") or []
            if not raw_outbox:
                continue
            owner = model.model_validate(raw)
            id_value = getattr(owner, id_field)
            remaining: list[OutboxEntry] = []
            changed = False
            for entry in owner.outbox:
                handler = active_handlers.get(entry.kind)
                if handler is None:
                    remaining.append(entry)  # pending: no handler registered yet
                    continue
                try:
                    await handler(store, entry)
                except Exception as exc:
                    logger.exception(
                        "drain_outbox: handler for kind=%s entry_id=%s (%s/%s) failed",
                        entry.kind, entry.entry_id, collection, id_value,
                    )
                    failed_entry = entry.model_copy(
                        update={"attempts": entry.attempts + 1, "last_error": str(exc)[:500]}
                    )
                    if failed_entry.attempts >= MAX_ATTEMPTS_PER_TASK:
                        # Past the retry ceiling: stop retrying forever in
                        # place. Move the entry to a dead-letter record
                        # (never dropped silently) and remove it from the
                        # owner's outbox so a permanently-failing handler
                        # cannot grow that array without bound.
                        await store.insert(
                            "outbox_dead_letters",
                            {
                                "collection": collection,
                                "owner_id": id_value,
                                "entry": failed_entry.model_dump(),
                                "dead_lettered_at": _now_iso(),
                            },
                        )
                        changed = True
                        processed += 1
                    else:
                        remaining.append(failed_entry)
                        changed = True
                    continue
                changed = True
                processed += 1
            if not changed:
                continue
            replacement = owner.model_copy(update={"outbox": remaining, "updated_at": _now_iso()})
            matched = await store.compare_and_set(
                collection, {id_field: id_value}, {"outbox": raw_outbox}, replacement
            )
            if not matched:
                logger.warning(
                    "drain_outbox: %s/%s outbox changed concurrently; retrying next pass",
                    collection, id_value,
                )
    return processed


async def drain_outbox_forever(
    store: ControlStore,
    *,
    handlers: Mapping[str, OutboxHandler] | None = None,
    interval_s: float = DEFAULT_OUTBOX_DRAIN_INTERVAL_S,
) -> None:
    """The supervised outbox relay: never exits on an iteration failure."""
    while True:
        try:
            await drain_outbox(store, handlers=handlers)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("drain_outbox_forever: outbox drain iteration failed")
        await asyncio.sleep(interval_s)


# ---- lease reconciler -------------------------------------------------------

async def reconcile_forever(
    task_service: TaskService,
    *,
    run_id: str | None = None,
    interval_s: float = LEASE_RECONCILE_INTERVAL_S,
) -> None:
    """The supervised lease reconciler: wraps ``TaskService.reconcile_leases``
    on a fixed interval, never exiting on an iteration failure."""
    while True:
        try:
            await task_service.reconcile_leases(run_id=run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reconcile_forever: lease reconciliation iteration failed")
        await asyncio.sleep(interval_s)
