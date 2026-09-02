"""Run-level (D5) resource-budget enforcement: the aggregate ceilings that
cannot be checked per-task because they accumulate across every task and
every gateway call in one ``WorkflowRun`` -- total tokens, total cost,
total tool calls, total artifact bytes, and total wall-clock time since
the run started. Per-task/per-child bounds (depth, fanout, parallelism,
attempts, and the ``CapabilityGrant``-scoped token/cost/tool/wall
ceilings) are enforced by ``TaskService``/``Manager.create_child_work``
and ``ProviderGateway`` directly; this module is the run-wide accumulator
both call into.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from .policy import BudgetExceeded
from .records import CapabilityGrant, WorkflowRun
from .store import ControlStore
from .workflow import WorkflowError


def _elapsed_seconds(started_at: str) -> float:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - started).total_seconds()


async def check_run_budget(
    store: ControlStore, run_id: str, *,
    tokens: int = 0, cost_usd: float = 0.0, tool_calls: int = 0, artifact_bytes: int = 0,
) -> WorkflowRun | None:
    """Atomically reserve the given prospective consumption against
    ``run_id``'s run-level ``ResourceBudget``: a check-and-reserve CAS loop,
    not a bare read-check. Refuses (``BudgetExceeded``) without writing
    anything when adding the prospective amount to recorded ``usage`` would
    exceed the budget, or when the run's wall-clock age already exceeds
    ``budget.wall_seconds``. A ceiling of ``0`` (unset) is never enforced --
    only ``Manager.start_run`` mints a real one.

    On success, ``run_id``'s ``usage`` is incremented by the prospective
    amount *before* this returns, so two concurrent callers can never both
    observe headroom for more than the budget allows: the second caller's
    CAS retry re-reads the first caller's reservation and is checked against
    it. Callers must reconcile the reservation with actual post-consumption
    usage via ``record_run_usage`` (a signed delta: actual minus reserved),
    including on every failure/early-return path after a successful
    reservation, so a call that reserves the worst case but consumes less
    (or nothing, on error) gives the difference back.

    Returns the loaded (post-reservation) run so callers needn't re-fetch
    it, or ``None`` for a run_id with no durable ``WorkflowRun`` (a
    pre-migration session): nothing to accumulate into, so nothing to
    refuse or reserve here either.
    """
    for _ in range(8):
        document = await store.get_one("workflow_runs", {"run_id": run_id})
        if document is None:
            return None
        run = WorkflowRun.model_validate(document)
        for amount, field, bound_name in (
            (tokens, "tokens", "MAX_TOKENS_PER_RUN"),
            (cost_usd, "cost_usd", "MAX_COST_PER_RUN_USD"),
            (tool_calls, "tool_calls", "MAX_TOOL_CALLS_PER_RUN"),
            (artifact_bytes, "artifact_bytes", "MAX_ARTIFACT_BYTES_PER_RUN"),
        ):
            ceiling = getattr(run.budget, f"max_{field}")
            if ceiling and getattr(run.usage, field) + amount > ceiling:
                raise BudgetExceeded(f"{bound_name} would be exceeded for run_id={run_id!r}")
        if run.budget.wall_seconds and run.started_at and _elapsed_seconds(run.started_at) > run.budget.wall_seconds:
            raise BudgetExceeded(f"MAX_RUN_WALL_S would be exceeded for run_id={run_id!r}")
        updated_usage = run.usage.model_copy(update={
            "tokens": run.usage.tokens + tokens,
            "cost_usd": run.usage.cost_usd + cost_usd,
            "tool_calls": run.usage.tool_calls + tool_calls,
            "artifact_bytes": run.usage.artifact_bytes + artifact_bytes,
        })
        updated = run.model_copy(update={"usage": updated_usage, "updated_at": datetime.now(timezone.utc).isoformat()})
        if await store.compare_and_set("workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated):
            return updated
    raise WorkflowError(f"could not reserve run budget for run_id={run_id!r} after retries")


async def record_run_usage(
    store: ControlStore, run_id: str, *,
    tokens: int = 0, cost_usd: float = 0.0, tool_calls: int = 0, artifact_bytes: int = 0,
) -> None:
    """CAS-retry reconciliation of ``run_id``'s recorded usage after real
    consumption: ``check_run_budget`` already reserved a prospective amount
    up front, so each argument here is a signed delta (actual minus
    reserved, negative when actual consumption came in under the reserved
    worst case) rather than the raw actual amount. A run with no durable
    ``WorkflowRun`` has nothing to accumulate into and this is a silent
    no-op, matching ``check_run_budget``'s ``None``."""
    for _ in range(8):
        document = await store.get_one("workflow_runs", {"run_id": run_id})
        if document is None:
            return
        run = WorkflowRun.model_validate(document)
        updated_usage = run.usage.model_copy(update={
            "tokens": run.usage.tokens + tokens,
            "cost_usd": run.usage.cost_usd + cost_usd,
            "tool_calls": run.usage.tool_calls + tool_calls,
            "artifact_bytes": run.usage.artifact_bytes + artifact_bytes,
        })
        updated = run.model_copy(update={"usage": updated_usage, "updated_at": datetime.now(timezone.utc).isoformat()})
        if await store.compare_and_set("workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated):
            return
    raise WorkflowError(f"could not record run usage for run_id={run_id!r} after retries")


async def record_grant_tool_usage(store: ControlStore, grant_id: str, tool_uses: Mapping[str, int]) -> None:
    """CAS-retry increment of ``grant_id``'s ``tools_used`` after a gateway
    call actually consumed the requested tool budget. Without this, every
    ``CapabilityGrant`` is checked against its own always-zero starting
    ``tools_used`` forever, so a task's per-grant tool ceiling is never
    actually enforced across repeated calls on the same grant."""
    if not tool_uses:
        return
    for _ in range(8):
        document = await store.get_one("capability_grants", {"grant_id": grant_id})
        if document is None:
            return
        grant = CapabilityGrant.model_validate(document)
        updated_tools_used = dict(grant.tools_used)
        for tool, uses in tool_uses.items():
            updated_tools_used[tool] = updated_tools_used.get(tool, 0) + uses
        updated = grant.model_copy(update={"tools_used": updated_tools_used})
        if await store.compare_and_set(
            "capability_grants", {"grant_id": grant_id}, {"tools_used": grant.tools_used}, updated
        ):
            return
    raise WorkflowError(f"could not record grant tool usage for grant_id={grant_id!r} after retries")
