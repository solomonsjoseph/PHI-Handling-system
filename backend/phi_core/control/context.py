"""Narrow, task-scoped dependencies supplied to agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from .gateway import ProviderGateway, ToolGateway
from .records import CapabilityGrant, DataClass, TraceEvent

if TYPE_CHECKING:
    from phi_core.agents.base import AgentMessage
    from phi_core.agents.manager import Manager


class TraceWriter(Protocol):
    async def emit(self, **fields: Any) -> None: ...

    async def legacy_log(self, message: "AgentMessage") -> None: ...


class ResearchCache(Protocol):
    async def get(self, topic: str, jurisdiction: str = "us") -> dict[str, Any] | None: ...

    async def put(self, topic: str, jurisdiction: str, content: str, source: str, schema_version: int = 1) -> None: ...


class ArtifactWriter(Protocol):
    async def stage(self, type: str, filename: str, data_class: DataClass, retention_class: str) -> tuple[str, Any]: ...

    async def finalize(self, artifact_id: str) -> None: ...


class EvidenceWriter(Protocol):
    async def claim(self, **fields: Any) -> str: ...

    async def source(self, **fields: Any) -> str: ...


class TaskCompleter(Protocol):
    async def complete(self, result: dict[str, Any]) -> None: ...

    async def fail(self, error_category: str) -> None: ...


@dataclass(frozen=True)
class AgentContext:
    session_id: str
    run_id: str
    task_id: str
    agent: str
    attempt: int
    grant: CapabilityGrant
    gateway: ProviderGateway
    tools: ToolGateway
    trace: TraceWriter
    cache: ResearchCache | None = None
    artifacts: ArtifactWriter | None = None
    evidence: EvidenceWriter | None = None
    emit: Callable[["AgentMessage"], Awaitable[None]] | None = None
    manager: "Manager | None" = None
    # Every `Agent` subclass's `run()` is completed against this on return
    # (see `Agent.__init_subclass__`) -- `None` for a context with no
    # backing `WorkItem` to complete (most unit tests build one via
    # `control.testing.make_ctx` without wiring a real `TaskService`).
    tasks: TaskCompleter | None = None


class StoreResearchCache:
    """Cache facade backed by ``ControlStore`` rather than a raw database handle."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def get(self, topic: str, jurisdiction: str = "us") -> dict[str, Any] | None:
        from datetime import datetime, timedelta, timezone

        from phi_core.agents.cache import REFRESH_DAYS

        doc = await self._store.get_one("web_cache", {"topic": topic, "jurisdiction": jurisdiction})
        if not doc:
            return None
        fetched = datetime.fromisoformat(doc["fetched_at"])
        if datetime.now(timezone.utc) - fetched > timedelta(days=REFRESH_DAYS):
            return None
        return doc

    async def put(self, topic: str, jurisdiction: str, content: str, source: str, schema_version: int = 1) -> None:
        from datetime import datetime, timezone

        document = {
            "topic": topic,
            "jurisdiction": jurisdiction,
            "content": content,
            "source": source,
            "schema_version": schema_version,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if not await self._store.replace_one("web_cache", {"topic": topic, "jurisdiction": jurisdiction}, document):
            await self._store.insert("web_cache", document)


class StoreTraceWriter:
    """Phase-2 trace facade while legacy ``agent_log`` remains readable.

    Built on ``ControlStore`` so the same implementation works against
    ``MongoControlStore`` in production and ``MemoryControlStore`` in tests.
    """

    def __init__(self, store: Any, *, run_id: str, session_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._session_id = session_id

    async def legacy_log(self, message: "AgentMessage") -> None:
        await self._store.insert("agent_log", message.model_dump())

    async def emit(self, **fields: Any) -> None:
        for _ in range(8):
            current = await self._store.get_one("workflow_runs", {"run_id": self._run_id})
            if current is None:
                seq = 0
                break
            seq = int(current.get("event_seq", 0)) + 1
            updated = dict(current)
            updated["event_seq"] = seq
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": self._run_id}, {"event_seq": current.get("event_seq", 0)}, updated
            ):
                break
        else:
            seq = int(current.get("event_seq", 0)) + 1 if current else 0
        event = TraceEvent(
            run_id=self._run_id,
            seq=seq,
            session_id=self._session_id,
            task_id=str(fields.pop("task_id", "")),
            parent_task_id=str(fields.pop("parent_task_id", "")),
            depth=int(fields.pop("depth", 0)),
            attempt=int(fields.pop("attempt", 0)),
            agent=str(fields.pop("agent", "")),
            manifest_version=str(fields.pop("manifest_version", "")),
            policy_version=str(fields.pop("policy_version", "")),
            provider=str(fields.pop("provider", "")),
            model=str(fields.pop("model", "")),
            endpoint=str(fields.pop("endpoint", "")),
            provider_request_id=str(fields.pop("provider_request_id", "")),
            usage=dict(fields.pop("usage", {})),
            cost_usd=float(fields.pop("cost_usd", 0.0)),
            latency_ms=int(fields.pop("latency_ms", 0)),
            tool_requested=str(fields.pop("tool_requested", "")),
            tool_policy_decision=str(fields.pop("tool_policy_decision", "")),
            tool_executed=str(fields.pop("tool_executed", "")),
            tool_result_status=str(fields.pop("tool_result_status", "")),
            input_class=fields.pop("input_class", "internal"),
            output_class=fields.pop("output_class", "internal"),
            outcome=str(fields.pop("outcome", "")),
            retry_category=str(fields.pop("retry_category", "")),
            status_text=str(fields.pop("status_text", "")),
            egress_digest=str(fields.pop("egress_digest", "")),
            gateway_decision=str(fields.pop("gateway_decision", "")),
        )
        await self._store.insert("trace_events", event)


class StoreTaskCompleter:
    """Completes (or fails) exactly one ``WorkItem`` -- the one this
    context's owning ``Agent.run()`` was activated to do -- against the
    lease it was claimed under. A stale lease (superseded by a later
    claim, heartbeat, or a concurrent completion) is a silent no-op:
    ``TaskService.complete``/``fail`` return ``outcome="fenced"`` rather
    than raising, and an agent finishing its own work after losing that
    race has nothing left to record."""

    def __init__(self, task_service: Any, *, task_id: str, lease_owner: str, fence: int) -> None:
        self._tasks = task_service
        self._task_id = task_id
        self._lease_owner = lease_owner
        self._fence = fence

    async def complete(self, result: dict[str, Any]) -> None:
        await self._tasks.complete(
            task_id=self._task_id, lease_owner=self._lease_owner, fence=self._fence, output_ref=result,
        )

    async def fail(self, error_category: str) -> None:
        await self._tasks.fail(
            task_id=self._task_id, lease_owner=self._lease_owner, fence=self._fence, error_category=error_category,
        )
