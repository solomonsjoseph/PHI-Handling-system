"""The Super Orchestrator (D9): exactly the API below, and nothing more.

Exclusive authority, mutually exclusive with every other collaborator:

- ``SuperOrchestrator`` is the only writer of ``workflow_runs.state`` and
  ``workflow_runs.node``, the only caller of ``TaskService.enqueue``
  (through ``create_child_work``), the only issuer of a human-review
  request, the only consumer of a human-review event, and the only
  acceptor of a material child result.
- ``TaskService`` (``control/tasks.py``) separately owns every
  ``work_items`` state and lease transition -- this class never CAS-writes
  ``work_items`` directly, including for acceptance: a task reaching
  ``succeeded`` is infrastructure-level completion, not the material
  acceptance ``accept_result`` alone decides, and that decision is
  recorded onto the *run's* checkpoint, never onto the task.
- ``run_decision_gates`` (D11, ``control/gates.py``) separately owns
  ``workflow_runs.decision_version``.
- ``ArtifactService`` (D14, ``control/artifacts.py``) separately owns
  every ``artifacts``/``publication_pointers`` transition;
  ``authorize_publication`` calls into it but writes neither collection
  itself.

Caller-supplies-the-typed-result convention: D9's published method
signatures list bare identifiers (``run_id``, ``task_id``, ...) for every
method, but three of them -- ``advance``, ``request_human_review``, and
``authorize_publication`` -- need a value this class cannot safely
re-derive from persisted state alone (which outcome a node reached; the
human-review request's originating task; the artifact/gate-result set a
publish authorizes) without inventing a bespoke, unverifiable rule per
call site. Each of those three accepts one additional keyword argument
here, defaulted so every other call keeps D9's exact arity. This mirrors
every other typed-result boundary already in this codebase
(``TaskService.complete``/``.fail``, ``ArtifactService.certify_publication``):
the caller reports its own typed result; the callee validates, records,
and never re-guesses it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import limits
from .artifacts import ArtifactService
from .policy import MANIFESTS, POLICY_VERSION, CapabilityDenied, _bounded_budget
from .records import CapabilityGrant, HumanReviewEvent, HumanReviewRequest, ResourceBudget, WorkflowRun, WorkItem
from .store import ControlStore
from .tasks import TaskService
from .workflow import (
    CHECKPOINT_VERSION,
    Checkpoint,
    WorkflowError,
    is_terminal,
    next_node,
    resume_node,
)
from .workflow import (
    node as validate_node,
)

WORKFLOW_VERSION = "wf/1"

# Every RunState terminal value is spelled identically to the matching
# TERMINAL_NODES value (both D3 and D9 use "complete", "partially_complete",
# "blocked", "failed", "cancelled" verbatim), so a terminal node's name is
# always a valid RunState too -- advance() relies on this rather than
# maintaining a second node->state translation table.

# WorkItem states cancel_subtree/reconcile_leases already treat as
# terminal (mirrors tasks.py's own _TERMINAL_STATES). A live sibling or
# run-wide task count only counts non-terminal work.
_TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled", "rejected", "superseded"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_for_task_type(task_type: str) -> str:
    """The one manifest whose ``task_types`` grants ``task_type``.

    ``create_child_work`` receives only ``task_type`` (D9's exact
    signature has no ``worker`` parameter), but ``TaskService.enqueue``
    needs the agent name to issue a capability grant. This is the single
    reverse lookup over the same ``MANIFESTS`` mapping the forward
    direction (``CapabilityPolicy._manifest_for``) already validates
    against, so there is exactly one source of truth for the mapping, not
    two that could drift apart.
    """
    for agent, manifest in MANIFESTS.items():
        if task_type in manifest.task_types:
            return agent
    raise CapabilityDenied(f"task type {task_type!r} is not granted to any agent")


def _budget_widens(requested: ResourceBudget, ceiling: ResourceBudget) -> bool:
    """True if ``requested`` asks for more than ``ceiling`` on any field.

    D5: a caller may narrow a child's proposed budget, never widen it
    past what the target agent's own manifest (bounded by the global
    ceilings) already authorizes.
    """
    fields = set(ResourceBudget.model_fields) - {"schema_version"}
    return any(getattr(requested, field) > getattr(ceiling, field) for field in fields)


class SuperOrchestrator:
    def __init__(self, store: ControlStore, tasks: TaskService) -> None:
        self._store = store
        self._tasks = tasks

    # ---- shared loaders ---------------------------------------------------

    async def _load_run(self, run_id: str) -> WorkflowRun:
        document = await self._store.get_one("workflow_runs", {"run_id": run_id})
        if document is None:
            raise WorkflowError(f"unknown run_id: {run_id!r}")
        return WorkflowRun.model_validate(document)

    async def _load_task(self, task_id: str) -> WorkItem:
        document = await self._store.get_one("work_items", {"task_id": task_id})
        if document is None:
            raise WorkflowError(f"unknown task_id: {task_id!r}")
        return WorkItem.model_validate(document)

    # ---- start_run ----------------------------------------------------

    async def start_run(
        self,
        *,
        session_id: str,
        principal: str,
        run_type: str = "study",
        iteration_cap: int = 0,
        correlation_id: str = "",
        run_id: str | None = None,
        root_task_type: str = "pipeline_run",
    ) -> WorkflowRun:
        """Open a new ``WorkflowRun`` at the ``charter`` node and enqueue its
        durable root ``Pipeline`` task.

        Fails closed on an anonymous caller. A route that atomically claims
        a session before starting work may pass that claim's fresh
        ``run_id`` so its session fence, ``WorkflowRun``, and root task
        share one identity. ``root_task_type`` permits the same durable
        Pipeline authority to schedule a fresh run or a human-review resume.
        Other callers omit both and receive a minted id and
        ``"pipeline_run"`` root. This method remains the sole
        `TaskService.enqueue` caller for root work.
        """
        if not principal:
            raise CapabilityDenied("start_run requires an authenticated principal")
        if iteration_cap < 0:
            raise ValueError("iteration_cap must not be negative")
        run_id = run_id or uuid4().hex
        existing = await self._store.get_one("workflow_runs", {"run_id": run_id})
        if existing is None:
            now = _now()
            run = WorkflowRun(
                run_id=run_id,
                session_id=session_id,
                workflow_version=WORKFLOW_VERSION,
                policy_version=POLICY_VERSION,
                run_type=run_type,
                state="running",
                node="charter",
                checkpoint={"node": "charter", "checkpoint_version": CHECKPOINT_VERSION, "payload_refs": []},
                checkpoint_version=CHECKPOINT_VERSION,
                started_at=now,
                updated_at=now,
                correlation_id=correlation_id or run_id,
                budget=ResourceBudget(wall_seconds=limits.MAX_RUN_WALL_S),
            )
            await self._store.insert("workflow_runs", run)
        else:
            run = WorkflowRun.model_validate(existing)
            if run.session_id != session_id:
                raise WorkflowError(f"run_id {run_id!r} does not belong to session {session_id!r}")
        await self._tasks.enqueue(
            run_id=run_id,
            session_id=session_id,
            worker="Pipeline",
            task_type=root_task_type,
            input_ref={"run_type": run_type},
            correlation_id=correlation_id or run_id,
        )
        return run

    # ---- cancel_run -----------------------------------------------------

    async def cancel_run(self, *, session_id: str, run_id: str, principal: str, reason: str) -> WorkflowRun:
        """Idempotently request cancellation.

        Sets ``cancel_requested``/``cancel_requested_at`` under CAS rather
        than forcing an immediate terminal transition: only the running
        handler -- which alone knows whether it is between two phases or
        mid-provider-call -- can safely land on the ``cancelled`` terminal
        node via a later ``advance(outcome="cancelled")``. A run already
        terminal, or already cancel-requested, is returned unchanged.
        """
        if not principal:
            raise CapabilityDenied("cancel_run requires an authenticated principal")
        run = await self._load_run(run_id)
        if run.session_id != session_id:
            raise WorkflowError(f"run_id {run_id!r} does not belong to session {session_id!r}")
        if is_terminal(run.node) or run.cancel_requested:
            return run
        updated = run.model_copy(
            update={
                "cancel_requested": True,
                "cancel_requested_at": _now(),
                "updated_at": _now(),
            }
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "cancel_requested": False}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race requesting cancellation for run_id={run_id!r}")
        for task in await self._store.find_many("work_items", {"run_id": run_id}):
            if task.get("parent_task_id") == "":
                await self._tasks.cancel_subtree(run_id=run_id, task_id=task["task_id"], reason=reason)
        return updated

    # ---- advance --------------------------------------------------------

    async def advance(self, *, run_id: str, outcome: str) -> WorkflowRun:
        """Transition ``run_id`` from its current node on ``outcome``.

        ``outcome`` is supplied by the caller -- the pipeline handler that
        just observed what actually happened (a ``GateOutcome``, a
        ``HumanReviewEvent.kind``, an accepted/rejected child result).
        ``next_node`` is the sole authority translating a reported outcome
        into the next node: it fails closed (``WorkflowError``) on any
        ``(node, outcome)`` pair it does not recognise, so a caller cannot
        invent an outcome that skips, reorders, or disables a gate. Every
        node commits a checkpoint in the same update (D9); a terminal
        target also stamps ``state``/``terminal_outcome``/``completed_at``,
        since every terminal node name is spelled identically to its
        matching ``RunState`` value.
        """
        run = await self._load_run(run_id)
        if is_terminal(run.node):
            raise WorkflowError(f"run_id={run_id!r} is already terminal at node {run.node!r}")
        target = next_node(run.node, outcome)
        now = _now()
        updates: dict[str, Any] = {
            "node": target,
            "checkpoint": {"node": target, "checkpoint_version": CHECKPOINT_VERSION, "payload_refs": []},
            "checkpoint_version": CHECKPOINT_VERSION,
            "updated_at": now,
        }
        if is_terminal(target):
            updates["state"] = target
            updates["terminal_outcome"] = target
            updates["completed_at"] = now
        updated = run.model_copy(update=updates)
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "node": run.node}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race advancing run_id={run_id!r} from node {run.node!r}")
        return updated

    # ---- create_child_work ------------------------------------------------

    async def create_child_work(
        self,
        *,
        run_id: str,
        parent_task_id: str,
        task_type: str,
        input_ref: dict[str, Any],
        budget: ResourceBudget,
    ) -> WorkItem:
        """Validate depth, fanout, run-wide task count, run-wide and
        per-parent parallelism, and budget against the parent's grant,
        then delegate to ``TaskService.enqueue``.

        Fails closed (``CapabilityDenied``) on: an unknown parent task or
        grant; a ``task_type`` the parent's manifest does not list under
        ``allowed_child_task_types``; a depth or live-sibling count past
        the manifest's (or the global D5) ceiling; the run reaching
        ``limits.MAX_TASKS_PER_RUN`` total tasks ever created; the run or
        the immediate parent reaching its non-terminal task ceiling
        (``limits.MAX_PARALLEL_TASKS_PER_RUN``/``MAX_PARALLEL_TASKS_PER_PARENT``);
        or a proposed ``budget`` that asks for more than the target
        agent's own manifest, bounded by the global limits, already
        authorizes.
        """
        parent = await self._load_task(parent_task_id)
        if parent.run_id != run_id:
            raise WorkflowError(f"parent task {parent_task_id!r} does not belong to run {run_id!r}")
        grant_document = await self._store.get_one("capability_grants", {"grant_id": parent.grant_id})
        if grant_document is None:
            raise CapabilityDenied(f"parent task {parent_task_id!r} has no capability grant")
        grant = CapabilityGrant.model_validate(grant_document)
        siblings = await self._store.find_many("work_items", {"parent_task_id": parent_task_id})
        # check_child's fanout ceiling counts total children ever created
        # (D5: "Ledger and Herald each need 2"), excluding only explicitly
        # voided ones -- unchanged from its original definition.
        fanout_siblings = [s for s in siblings if s.get("state") not in ("cancelled", "rejected", "superseded")]
        self._tasks.policy.check_child(grant, task_type, depth=parent.depth + 1, children=len(fanout_siblings))
        live_siblings = [s for s in siblings if s.get("state") not in _TERMINAL_TASK_STATES]
        if len(live_siblings) >= limits.MAX_PARALLEL_TASKS_PER_PARENT:
            raise CapabilityDenied(
                f"parent {parent_task_id!r} already has {len(live_siblings)} live children "
                f"(MAX_PARALLEL_TASKS_PER_PARENT={limits.MAX_PARALLEL_TASKS_PER_PARENT})"
            )
        run_tasks = await self._store.find_many("work_items", {"run_id": run_id})
        if len(run_tasks) >= limits.MAX_TASKS_PER_RUN:
            raise CapabilityDenied(
                f"run_id={run_id!r} already created {len(run_tasks)} tasks "
                f"(MAX_TASKS_PER_RUN={limits.MAX_TASKS_PER_RUN})"
            )
        live_run_tasks = [t for t in run_tasks if t.get("state") not in _TERMINAL_TASK_STATES]
        if len(live_run_tasks) >= limits.MAX_PARALLEL_TASKS_PER_RUN:
            raise CapabilityDenied(
                f"run_id={run_id!r} already has {len(live_run_tasks)} live tasks "
                f"(MAX_PARALLEL_TASKS_PER_RUN={limits.MAX_PARALLEL_TASKS_PER_RUN})"
            )
        worker = _worker_for_task_type(task_type)
        ceiling = _bounded_budget(MANIFESTS[worker].budget)
        if _budget_widens(budget, ceiling):
            raise CapabilityDenied(f"proposed budget for {task_type!r} exceeds {worker!r}'s manifest authority")
        return await self._tasks.enqueue(
            run_id=run_id,
            session_id=parent.session_id,
            worker=worker,
            task_type=task_type,
            parent_task_id=parent_task_id,
            depth=parent.depth + 1,
            input_ref=input_ref,
            correlation_id=parent.correlation_id,
        )


    # ---- request_human_review ---------------------------------------------

    async def request_human_review(
        self,
        *,
        run_id: str,
        node: str,
        reason_codes: list[str],
        decision_version: int,
        audit_version: str = "",
        evidence_version: str = "",
        task_id: str = "",
        required_role: str = "",
    ) -> HumanReviewRequest:
        """Open a ``HumanReviewRequest`` and pause the run at ``node``.

        The only path to ``awaiting_human_review``: D10 repoints every
        current ``Manager.escalate_to_human_review`` caller here.
        ``task_id``/``required_role`` are not part of D9's published
        signature (see module docstring); both default to ``""`` so every
        existing call keeps D9's exact arity.
        """
        run = await self._load_run(run_id)
        validate_node(node)
        request = HumanReviewRequest(
            run_id=run_id,
            session_id=run.session_id,
            workflow_version=WORKFLOW_VERSION,
            task_id=task_id,
            node=node,
            reason_codes=list(reason_codes),
            decision_version=decision_version,
            audit_version=audit_version,
            evidence_version=evidence_version,
            required_role=required_role,
        )
        await self._store.insert("human_review_requests", request)
        now = _now()
        updated = run.model_copy(
            update={"state": "awaiting_human_review", "node": node, "paused_at": now, "updated_at": now}
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race pausing run_id={run_id!r} for human review")
        return request

    # ---- consume_review_event ----------------------------------------------

    async def consume_review_event(self, *, run_id: str, event: HumanReviewEvent) -> WorkflowRun:
        """Record ``event``, resolve its ``HumanReviewRequest``, and resume
        the run to ``running``.

        Does not itself move ``node`` -- that is ``advance``'s exclusive
        job, called separately by the caller once it knows the outcome
        the event produced (D9 draws that line at "the only consumer of a
        human-review event" versus "the only writer of ... node", two
        distinct exclusivities in the same paragraph).
        """
        if event.run_id != run_id:
            raise WorkflowError(f"event.run_id {event.run_id!r} does not match run_id {run_id!r}")
        run = await self._load_run(run_id)
        request_document = await self._store.get_one("human_review_requests", {"request_id": event.request_id})
        if request_document is None:
            raise WorkflowError(f"unknown request_id: {event.request_id!r}")
        request = HumanReviewRequest.model_validate(request_document)
        if request.state == "open":
            resolved_request = request.model_copy(update={"state": "resolved", "resolved_at": _now()})
            matched = await self._store.compare_and_set(
                "human_review_requests", {"request_id": request.request_id}, {"state": "open"}, resolved_request
            )
            if not matched:
                raise WorkflowError(f"lost the race resolving request_id={request.request_id!r}")
        await self._store.insert("human_review_events", event)
        if run.state != "awaiting_human_review":
            return run
        updated = run.model_copy(update={"state": "running", "resumed_at": _now(), "updated_at": _now()})
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race resuming run_id={run_id!r}")
        return updated

    # ---- accept_result ----------------------------------------------------

    async def accept_result(self, *, run_id: str, task_id: str, result: dict[str, Any]) -> bool:
        """The sole acceptance authority for a completed child task.

        A child's ``succeeded`` state (``TaskService.complete``) is
        infrastructure-level completion, not acceptance; a task cannot
        accept its own result. Returns ``False`` -- never raises -- for
        every refusal reason (wrong run, not yet succeeded, or a
        caller-asserted ``result`` that diverges from the task's own
        fenced ``output_ref``): a caller checks the boolean, it does not
        need to distinguish refusal reasons to decide not to proceed.
        Idempotent: accepting an already-accepted task returns ``True``
        without a second write.
        """
        task = await self._load_task(task_id)
        if task.run_id != run_id or task.state != "succeeded" or result != task.output_ref:
            return False
        for _ in range(10):
            run = await self._load_run(run_id)
            accepted_ids = set(run.checkpoint.get("accepted_task_ids") or [])
            if task_id in accepted_ids:
                return True
            accepted_ids.add(task_id)
            updated = run.model_copy(
                update={
                    "checkpoint": {**run.checkpoint, "accepted_task_ids": sorted(accepted_ids)},
                    "updated_at": _now(),
                }
            )
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return True
        raise WorkflowError(f"could not record acceptance for task_id={task_id!r} after retries")

    # ---- recover ------------------------------------------------------

    async def recover(self, *, run_id: str, cause: str) -> WorkflowRun:
        """Resume a run from its last committed checkpoint (D9's fail-closed
        resume default). A terminal run is returned unchanged."""
        run = await self._load_run(run_id)
        if is_terminal(run.node):
            return run
        checkpoint = Checkpoint(
            node=run.node,
            checkpoint_version=run.checkpoint_version or CHECKPOINT_VERSION,
            payload_refs=tuple(run.checkpoint.get("payload_refs") or ()),
        )
        resume_to = resume_node(checkpoint)
        now = _now()
        updated = run.model_copy(
            update={
                "node": resume_to,
                "state": "running" if run.state == "awaiting_human_review" and resume_to != run.node else run.state,
                "resumed_at": now,
                "updated_at": now,
                "checkpoint": {**run.checkpoint, "recovery_cause": cause},
            }
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "node": run.node}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race recovering run_id={run_id!r}")
        return updated

    # ---- authorize_publication ---------------------------------------------

    async def authorize_publication(
        self,
        *,
        run_id: str,
        artifact_ids: list[str] | None = None,
        gate_result_ids: list[str] | None = None,
        certified_by_task_id: str = "",
    ) -> int:
        """Check the publish gates and certify a new publication generation.

        ``artifact_ids``/``gate_result_ids`` are not part of D9's
        published signature (see module docstring): the winning set is
        computed outside ``ControlStore``'s abstraction today (Publish
        Guard's clean-file set lives on the plain ``db.sessions`` document,
        not a control-plane record), so the caller that already holds it
        passes it through rather than this class re-deriving it from a
        collection it has no access to. A ``None``/empty ``artifact_ids``
        authorizes nothing new and returns the current generation
        (``0`` if none has ever been certified) -- a query-only mode.
        Refuses (``WorkflowError``) while a ``HumanReviewRequest`` for
        this run is still ``open``: publication cannot be authorized
        while a human decision is outstanding.
        """
        run = await self._load_run(run_id)
        open_requests = await self._store.find_many(
            "human_review_requests", {"run_id": run_id, "state": "open"}
        )
        if open_requests:
            raise WorkflowError(f"run_id={run_id!r} has an open human review request; publication refused")
        service = ArtifactService(self._store, session_id=run.session_id, run_id=run_id)
        current = await service._current_pointer(run.session_id)
        if not artifact_ids:
            return current.generation if current is not None else 0
        pointer = await service.certify_publication(
            run_id=run_id,
            artifact_ids=list(artifact_ids),
            gate_result_ids=list(gate_result_ids or []),
            fence=(current.fence + 1) if current is not None else 1,
            certified_by_task_id=certified_by_task_id,
        )
        return pointer.generation

    # ---- terminal_outcome ---------------------------------------------

    async def terminal_outcome(self, *, run_id: str) -> dict[str, Any]:
        """A read-only summary of ``run_id``'s terminal status, if any."""
        run = await self._load_run(run_id)
        return {
            "run_id": run.run_id,
            "node": run.node,
            "state": run.state,
            "is_terminal": is_terminal(run.node),
            "terminal_outcome": run.terminal_outcome,
            "completed_at": run.completed_at,
        }
