"""Shared helper for building a durable, capability-scoped ``AgentContext``.

Every entry path that starts an agent (the primary pipeline, the human-review
resume tail, corpus research, and settings warmup) opens a real ``WorkItem``
and ``CapabilityGrant`` here rather than constructing an agent from a bare
database handle. Phase 5's ``Manager`` becomes the sole caller of
``TaskService.enqueue``; until then this factory is the one place that does,
kept intentionally narrow so migrating call sites onto it later is a
one-line change per site.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .artifacts import ArtifactService
from .context import (
    AgentContext,
    StoreMethodRegistryReader,
    StoreOpaqueWriter,
    StoreResearchCache,
    StoreTaskCompleter,
    StoreTraceWriter,
)
from .gateway import ProviderGateway, ToolGateway
from .handoff import HandoffGateway
from .policy import CapabilityPolicy
from .records import CapabilityGrant, SandboxRecord
from .sandbox import create_sandbox
from .store import ControlStore, MongoControlStore
from .tasks import TaskService
from .writer import ArtifactWriter

if TYPE_CHECKING:
    from ..agents.llm import LlmConfig


class ActivationFactory:
    """Builds task-scoped ``AgentContext`` instances for one run.

    One factory shares a ``ProviderGateway``/``CapabilityPolicy``/
    ``TaskService`` triple across every agent activated in the run; each
    ``activate`` call still opens its own durable ``WorkItem`` and
    immutable ``CapabilityGrant``.
    """

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        llm_cfg: LlmConfig,
        *,
        store: ControlStore | None = None,
    ) -> None:
        self.store = store or MongoControlStore(db)
        self.policy = CapabilityPolicy(llm_cfg)
        self.gateway = ProviderGateway(self.store, self.policy)
        self.task_service = TaskService(self.store, self.policy)
        # Phase R-c: at most one sandbox workspace per run, shared across
        # every agent activated for it (real filesystem/platform work --
        # never created unless a caller actually asks for one via
        # `needs_sandbox=True`).
        self._sandboxes: dict[str, SandboxRecord] = {}

    async def activate(
        self,
        *,
        session_id: str,
        run_id: str,
        agent: str,
        emit: Optional[Callable[[Any], Awaitable[None]]] = None,
        manager: Any = None,
        lease_owner: str | None = None,
        needs_sandbox: bool = False,
    ) -> AgentContext:
        task = await self.task_service.enqueue(
            run_id=run_id,
            session_id=session_id,
            worker=agent,
            task_type=agent.lower().replace(".", "_"),
            correlation_id=run_id,
        )
        return await self._claim_and_build(
            task_id=task.task_id, session_id=session_id, agent=agent,
            emit=emit, manager=manager, lease_owner=lease_owner or f"activation:{run_id}",
            needs_sandbox=needs_sandbox,
        )

    async def activate_child(
        self,
        *,
        session_id: str,
        run_id: str,
        parent_task_id: str,
        agent: str,
        emit: Optional[Callable[[Any], Awaitable[None]]] = None,
        manager: Any = None,
        lease_owner: str | None = None,
        needs_sandbox: bool = False,
    ) -> AgentContext:
        """Like ``activate``, but the task is created as
        ``Manager``-owned durable child work under
        ``parent_task_id`` (D5: depth, fanout, and budget enforced against
        the parent's own grant), not a bare root enqueue. Reserved for a
        genuine sub-agent delegation chain -- Ledger's Compare/Aggregate
        split and Herald's Abstract/Sections split are the only two today
        -- where a real parent-child relationship exists; every other
        ``activate()`` call is itself a direct child of the root Pipeline
        task and stays on the simpler path."""
        from .manager import Manager
        from .policy import MANIFESTS, _bounded_budget

        task_type = agent.lower().replace(".", "_")
        task = await Manager(self.store, self.task_service).create_child_work(
            run_id=run_id, parent_task_id=parent_task_id, task_type=task_type,
            input_ref={}, budget=_bounded_budget(MANIFESTS[agent].budget),
        )
        return await self._claim_and_build(
            task_id=task.task_id, session_id=session_id, agent=agent,
            emit=emit, manager=manager, lease_owner=lease_owner or f"activation:{run_id}",
            needs_sandbox=needs_sandbox,
        )

    def _sandbox_for(self, run_id: str) -> SandboxRecord:
        """Lazily create (once per run_id, then reuse) the run-scoped
        sandbox workspace. Never called unless a caller opted the run
        into one via ``needs_sandbox=True``."""
        record = self._sandboxes.get(run_id)
        if record is None:
            record = create_sandbox(run_id)
            self._sandboxes[run_id] = record
        return record

    async def _claim_and_build(
        self, *, task_id: str, session_id: str, agent: str,
        emit: Optional[Callable[[Any], Awaitable[None]]], manager: Any, lease_owner: str,
        needs_sandbox: bool = False,
    ) -> AgentContext:
        claimed = await self.task_service.claim(task_id=task_id, lease_owner=lease_owner)
        if claimed is None:
            raise RuntimeError(f"unable to claim task {task_id}")
        grant_doc = await self.store.get_one("capability_grants", {"grant_id": claimed.grant_id})
        if grant_doc is None:
            raise RuntimeError(f"missing capability grant for task {claimed.task_id}")
        grant = CapabilityGrant.model_validate(grant_doc)
        return AgentContext(
            session_id=session_id,
            run_id=claimed.run_id,
            task_id=claimed.task_id,
            agent=agent,
            attempt=claimed.attempt,
            grant=grant,
            gateway=self.gateway,
            tools=ToolGateway(self.gateway),
            trace=StoreTraceWriter(self.store, run_id=claimed.run_id, session_id=session_id),
            cache=StoreResearchCache(self.store),
            artifacts=ArtifactWriter(
                ArtifactService(self.store, session_id=session_id, run_id=claimed.run_id),
                producer_task_id=claimed.task_id,
            ),
            emit=emit,
            manager=manager,
            tasks=StoreTaskCompleter(
                self.task_service, task_id=claimed.task_id,
                lease_owner=claimed.lease_owner, fence=claimed.fence,
            ),
            # Phase R-c: every claimed context gains the control plane.
            # `handoff`/`opaque`/`methods` are cheap store-backed facades,
            # attached unconditionally like `trace`/`cache` above; `sandbox`
            # does real filesystem/platform work and is attached only when
            # `needs_sandbox=True` was requested for this activation.
            handoff=HandoffGateway(self.store, session_id=session_id),
            sandbox=self._sandbox_for(claimed.run_id) if needs_sandbox else None,
            opaque=StoreOpaqueWriter(self.store, self.task_service, run_id=claimed.run_id),
            methods=StoreMethodRegistryReader(self.store),
        )

    async def complete_and_accept(self, ctx: AgentContext, result: dict[str, Any]) -> bool:
        """Have ``Manager.accept_result`` formally accept
        ``ctx``'s already-completed task -- the acceptance authority D5
        step 5 requires for durable child work (a child's ``succeeded``
        state, which ``Agent.__init_subclass__`` already applied when
        ``run()`` returned ``result``, is infrastructure completion, not
        acceptance). Best-effort: returns ``False`` rather than raising
        on a refused acceptance, so a caller never lets this bookkeeping
        step fail the pipeline around an already-delivered result."""
        from .manager import Manager

        return await Manager(self.store, self.task_service).accept_result(
            run_id=ctx.run_id, task_id=ctx.task_id, result=result or {},
        )
