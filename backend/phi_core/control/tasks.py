"""Durable task identity used by the pre-worker control-plane integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .limits import LEASE_SECONDS
from .policy import CapabilityPolicy
from .records import WorkItem
from .store import ControlStore


class TaskService:
    """Creates and claims work records with a persisted, immutable grant."""

    def __init__(self, store: ControlStore, policy: CapabilityPolicy) -> None:
        self._store = store
        self._policy = policy

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

    async def claim(self, *, task_id: str, lease_owner: str) -> WorkItem | None:
        document = await self._store.get_one("work_items", {"task_id": task_id})
        if document is None:
            return None
        task = WorkItem.model_validate(document)
        if task.state != "ready":
            return None
        now = datetime.now(timezone.utc)
        claimed = task.model_copy(
            update={
                "state": "leased",
                "attempt": task.attempt + 1,
                "lease_owner": lease_owner,
                "lease_expires_at": (now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                "heartbeat_at": now.isoformat(),
                "claimed_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "fence": task.fence + 1,
            }
        )
        if not await self._store.compare_and_set(
            "work_items", {"task_id": task_id}, {"state": "ready", "fence": task.fence}, claimed
        ):
            return None
        return claimed
