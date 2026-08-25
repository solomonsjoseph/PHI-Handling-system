"""Durable workflow-run creation before the workflow state machine lands."""
from __future__ import annotations

from datetime import datetime, timezone

from .limits import MAX_RUN_WALL_S
from .policy import POLICY_VERSION
from .records import ResourceBudget, WorkflowRun
from .store import ControlStore

WORKFLOW_VERSION = "wf/1"


class RunStore:
    """Owns durable creation of a workflow run.

    Phase 5 assigns transition authority to ``SuperOrchestrator``.  Until
    then this class only opens the final-form record used by gateway calls.
    """

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    async def open_run(
        self,
        *,
        session_id: str,
        run_id: str,
        run_type: str = "study",
        correlation_id: str = "",
    ) -> WorkflowRun:
        now = datetime.now(timezone.utc).isoformat()
        run = WorkflowRun(
            run_id=run_id,
            session_id=session_id,
            workflow_version=WORKFLOW_VERSION,
            policy_version=POLICY_VERSION,
            run_type=run_type,
            state="running",
            node="charter",
            started_at=now,
            updated_at=now,
            correlation_id=correlation_id or run_id,
            budget=ResourceBudget(wall_seconds=MAX_RUN_WALL_S),
        )
        await self._store.insert("workflow_runs", run)
        return run
