"""Shared helper for building a durable, capability-scoped ``AgentContext``.

Every entry path that starts an agent (the primary pipeline, the human-review
resume tail, corpus research, and settings warmup) opens a real ``WorkItem``
and ``CapabilityGrant`` here rather than constructing an agent from a bare
database handle. Phase 5's ``SuperOrchestrator`` becomes the sole caller of
``TaskService.enqueue``; until then this factory is the one place that does,
kept intentionally narrow so migrating call sites onto it later is a
one-line change per site.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .context import AgentContext, StoreResearchCache, StoreTraceWriter
from .gateway import ProviderGateway, ToolGateway
from .policy import CapabilityPolicy
from .records import CapabilityGrant
from .store import ControlStore, MongoControlStore
from .tasks import TaskService

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

    async def activate(
        self,
        *,
        session_id: str,
        run_id: str,
        agent: str,
        emit: Optional[Callable[[Any], Awaitable[None]]] = None,
        manager: Any = None,
        lease_owner: str | None = None,
    ) -> AgentContext:
        task = await self.task_service.enqueue(
            run_id=run_id,
            session_id=session_id,
            worker=agent,
            task_type=agent.lower().replace(".", "_"),
            correlation_id=run_id,
        )
        claimed = await self.task_service.claim(
            task_id=task.task_id, lease_owner=lease_owner or f"activation:{run_id}"
        )
        if claimed is None:
            raise RuntimeError(f"unable to claim task {task.task_id}")
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
            emit=emit,
            manager=manager,
        )
