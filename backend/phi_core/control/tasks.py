"""Durable, CAS-fenced task lifecycle (D3/D9): ``TaskService``.

Every consequential ``work_items`` transition is one ``ControlStore.compare_and_set``
call whose filter includes the fields that prove the caller still holds
authority over the document: ``state`` alone for ``claim`` (the state flip
from ``ready`` is itself the uniqueness guard -- exactly one of two racing
claimants can win the CAS), and ``lease_owner`` plus ``fence`` for every
transition made by an already-claimed holder (``heartbeat``, ``complete``,
``fail``). ``complete``, ``fail``, and ``reconcile_leases`` bump ``fence``;
``claim`` and ``heartbeat`` do not. A caller whose ``fence`` (or
``lease_owner``) no longer matches the stored document loses the CAS and gets
back a ``TaskOutcome`` with ``outcome="fenced"`` -- never an exception, and
never a silent no-op that looks like success.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from .limits import LEASE_SECONDS, MAX_ATTEMPTS_PER_TASK
from .policy import CapabilityPolicy
from .records import WorkItem
from .store import ControlStore

# Terminal ``TaskState`` values: once here, no further transition applies.
# ``cancel_subtree`` and ``reconcile_leases`` both treat membership in this
# set as "leave it alone".
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "rejected", "superseded"})

TaskOutcomeKind = Literal["ok", "fenced", "not_found"]


@dataclass(frozen=True)
class TaskOutcome:
    """Result of one fenced ``work_items`` transition attempt.

    ``ok`` is ``True`` only for ``outcome="ok"``. A stale ``lease_owner`` or
    ``fence``, a task that already left the expected state, or a missing
    ``task_id`` are all reported here rather than raised, so a worker can
    branch on ``outcome`` instead of wrapping every call in ``try/except``.
    """

    ok: bool
    outcome: TaskOutcomeKind
    task: WorkItem | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


class TaskService:
    """Creates, leases, and retires ``work_items`` records with a persisted,
    immutable ``CapabilityGrant`` per task."""

    def __init__(self, store: ControlStore, policy: CapabilityPolicy) -> None:
        self._store = store
        self._policy = policy

    @property
    def policy(self) -> CapabilityPolicy:
        """Read-only access to the issuing policy, so a collaborator that
        needs to re-validate a grant (``SuperOrchestrator.create_child_work``
        checking ``check_child`` against the parent's grant) does not need
        its own separately constructed ``CapabilityPolicy`` instance."""
        return self._policy

    # ---- enqueue --------------------------------------------------------

    async def enqueue(
        self,
        *,
        run_id: str,
        session_id: str,
        worker: str,
        task_type: str,
        parent_task_id: str = "",
        depth: int = 0,
        input_ref: dict | None = None,
        idempotency_key: str | None = None,
        correlation_id: str = "",
    ) -> WorkItem:
        """Create one ``ready`` ``WorkItem`` plus its immutable capability grant."""
        task_id = uuid4().hex
        grant = self._policy.issue_grant(
            run_id=run_id, task_id=task_id, agent=worker, task_type=task_type
        )
        task = WorkItem(
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            parent_task_id=parent_task_id,
            depth=depth,
            worker=worker,
            worker_version=grant.manifest_version,
            task_type=task_type,
            max_attempts=grant.budget.max_attempts,
            idempotency_key=idempotency_key or f"{run_id}:{task_id}",
            input_ref=input_ref or {},
            grant_id=grant.grant_id,
            budget=grant.budget,
            correlation_id=correlation_id or run_id,
        )
        await self._store.insert("capability_grants", grant)
        await self._store.insert("work_items", task)
        return task

    # ---- claim ------------------------------------------------------------

    async def claim(
        self, *, task_id: str, lease_owner: str, lease_seconds: int = LEASE_SECONDS
    ) -> WorkItem | None:
        """CAS-transition ``ready`` -> ``leased`` under a fresh lease.

        Two concurrent callers racing the same ``task_id`` both read
        ``state="ready"``, but only one ``compare_and_set`` can win: the
        loser's filter (``state="ready"``) no longer matches once the winner
        has flipped it, so it gets ``None`` back cleanly, never a
        mid-transition exception. ``fence`` is read but left unchanged --
        claiming does not, by itself, invalidate a previously issued fence
        token; only ``complete``, ``fail``, and ``reconcile_leases`` do that.
        """
        document = await self._store.get_one("work_items", {"task_id": task_id})
        if document is None:
            return None
        task = WorkItem.model_validate(document)
        if task.state != "ready":
            return None
        now = _now()
        claimed = task.model_copy(
            update={
                "state": "leased",
                "attempt": task.attempt + 1,
                "lease_owner": lease_owner,
                "lease_expires_at": _iso(now + timedelta(seconds=lease_seconds)),
                "heartbeat_at": _iso(now),
                "claimed_at": _iso(now),
                "started_at": task.started_at or _iso(now),
                "updated_at": _iso(now),
            }
        )
        matched = await self._store.compare_and_set(
            "work_items", {"task_id": task_id}, {"state": "ready", "fence": task.fence}, claimed
        )
        if not matched:
            return None
        return claimed

    # ---- fenced transitions shared by heartbeat/complete/fail -------------

    async def _fenced_transition(
        self,
        *,
        task_id: str,
        lease_owner: str,
        fence: int,
        expected_state: str,
        updates: dict[str, Any],
    ) -> TaskOutcome:
        document = await self._store.get_one("work_items", {"task_id": task_id})
        if document is None:
            return TaskOutcome(ok=False, outcome="not_found")
        current = WorkItem.model_validate(document)
        replacement = current.model_copy(update=updates)
        matched = await self._store.compare_and_set(
            "work_items",
            {"task_id": task_id},
            {"lease_owner": lease_owner, "fence": fence, "state": expected_state},
            replacement,
        )
        if not matched:
            stale = await self._store.get_one("work_items", {"task_id": task_id})
            return TaskOutcome(
                ok=False,
                outcome="fenced",
                task=WorkItem.model_validate(stale) if stale is not None else None,
            )
        return TaskOutcome(ok=True, outcome="ok", task=replacement)

    async def heartbeat(
        self, *, task_id: str, lease_owner: str, fence: int, lease_seconds: int = LEASE_SECONDS
    ) -> TaskOutcome:
        """Extend a held lease. CAS-gated on ``lease_owner`` and ``fence``;
        does not itself bump ``fence`` -- a stale caller loses its match the
        moment ``complete``, ``fail``, or ``reconcile_leases`` moves the
        fence out from under it, without needing every renewal to also
        advance the token."""
        now = _now()
        return await self._fenced_transition(
            task_id=task_id,
            lease_owner=lease_owner,
            fence=fence,
            expected_state="leased",
            updates={
                "lease_expires_at": _iso(now + timedelta(seconds=lease_seconds)),
                "heartbeat_at": _iso(now),
                "updated_at": _iso(now),
            },
        )

    async def complete(
        self,
        *,
        task_id: str,
        lease_owner: str,
        fence: int,
        output_ref: dict[str, Any] | None = None,
    ) -> TaskOutcome:
        """CAS-transition ``leased`` -> ``succeeded``, bumping ``fence``.
        A stale ``lease_owner``/``fence`` never applies the result -- it comes
        back ``outcome="fenced"`` so the caller can discard its output rather
        than have it silently accepted or silently overwrite a newer holder."""
        now = _now()
        return await self._fenced_transition(
            task_id=task_id,
            lease_owner=lease_owner,
            fence=fence,
            expected_state="leased",
            updates={
                "state": "succeeded",
                "fence": fence + 1,
                "output_ref": output_ref or {},
                "completed_at": _iso(now),
                "updated_at": _iso(now),
            },
        )

    async def fail(
        self,
        *,
        task_id: str,
        lease_owner: str,
        fence: int,
        error_category: str = "",
    ) -> TaskOutcome:
        """CAS-transition ``leased`` -> ``failed``, bumping ``fence``. Same
        fenced-not-raised contract as ``complete``."""
        now = _now()
        return await self._fenced_transition(
            task_id=task_id,
            lease_owner=lease_owner,
            fence=fence,
            expected_state="leased",
            updates={
                "state": "failed",
                "fence": fence + 1,
                "error_category": error_category,
                "completed_at": _iso(now),
                "updated_at": _iso(now),
            },
        )

    # ---- cancel_subtree -----------------------------------------------------

    async def cancel_subtree(
        self, *, run_id: str, task_id: str, reason: str = ""
    ) -> list[WorkItem]:
        """Mark ``task_id`` and every descendant (by ``parent_task_id``,
        transitively) ``cancelled``.

        Best-effort: there is no live coroutine here to await (that is
        ``control/worker.py``'s job, out of scope for this module), so
        "awaiting or fencing children" means a CAS-guarded state flip per
        node. A node already in a terminal state is left untouched and
        reported as-is; a node whose CAS loses a race with a concurrent
        ``heartbeat``/``complete``/``fail`` is re-read and reported with
        its actual current state rather than assumed cancelled -- the
        fence bump this issues still makes any in-flight holder's next
        transition fail once it does land.
        """
        documents = await self._store.find_many("work_items", {"run_id": run_id})
        by_id = {d["task_id"]: WorkItem.model_validate(d) for d in documents}
        if task_id not in by_id:
            return []
        by_parent: dict[str, list[str]] = {}
        for item in by_id.values():
            by_parent.setdefault(item.parent_task_id, []).append(item.task_id)

        subtree_ids: list[str] = []
        seen: set[str] = set()
        queue = [task_id]
        while queue:
            current_id = queue.pop(0)
            if current_id in seen or current_id not in by_id:
                continue
            seen.add(current_id)
            subtree_ids.append(current_id)
            queue.extend(by_parent.get(current_id, []))

        now = _now()
        results: list[WorkItem] = []
        for current_id in subtree_ids:
            node = by_id[current_id]
            if node.state in _TERMINAL_STATES:
                results.append(node)
                continue
            cancelled = node.model_copy(
                update={
                    "state": "cancelled",
                    "fence": node.fence + 1,
                    "cancel_requested": True,
                    "error_category": reason or "cancelled",
                    "completed_at": _iso(now),
                    "updated_at": _iso(now),
                }
            )
            matched = await self._store.compare_and_set(
                "work_items",
                {"task_id": current_id},
                {"state": node.state, "fence": node.fence},
                cancelled,
            )
            if matched:
                results.append(cancelled)
                continue
            refreshed = await self._store.get_one("work_items", {"task_id": current_id})
            results.append(WorkItem.model_validate(refreshed) if refreshed is not None else node)
        return results

    # ---- reconcile_leases ---------------------------------------------------

    async def reconcile_leases(
        self, *, run_id: str | None = None, now: datetime | None = None
    ) -> list[WorkItem]:
        """Return every ``leased`` task whose lease has expired to ``ready``,
        or to ``failed`` once its attempt count has reached ``max_attempts``.
        Either outcome bumps ``fence``, so a worker that missed the deadline
        and only later resumes gets a clean ``fenced`` rejection on its next
        ``heartbeat``/``complete``/``fail`` rather than clobbering whoever
        (or whatever retry) claims the task next."""
        moment = now or _now()
        query: dict[str, Any] = {"state": "leased"}
        if run_id is not None:
            query["run_id"] = run_id
        candidates = [
            WorkItem.model_validate(d) for d in await self._store.find_many("work_items", query)
        ]
        reconciled: list[WorkItem] = []
        for task in candidates:
            if not task.lease_expires_at:
                continue
            try:
                expires_at = datetime.fromisoformat(task.lease_expires_at)
            except ValueError:
                continue
            if expires_at > moment:
                continue
            ceiling = task.max_attempts or MAX_ATTEMPTS_PER_TASK
            if task.attempt >= ceiling:
                updates: dict[str, Any] = {
                    "state": "failed",
                    "error_category": "lease_expired_retry_ceiling",
                    "completed_at": _iso(moment),
                }
            else:
                updates = {
                    "state": "ready",
                    "lease_owner": "",
                    "lease_expires_at": "",
                    "heartbeat_at": "",
                    "next_eligible_at": "",
                }
            replacement = task.model_copy(
                update={**updates, "fence": task.fence + 1, "updated_at": _iso(moment)}
            )
            matched = await self._store.compare_and_set(
                "work_items",
                {"task_id": task.task_id},
                {"state": "leased", "lease_owner": task.lease_owner, "fence": task.fence},
                replacement,
            )
            if matched:
                reconciled.append(replacement)
        return reconciled
