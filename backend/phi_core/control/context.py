"""Narrow, task-scoped dependencies supplied to agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from .gateway import ProviderGateway, ToolGateway
from .records import CapabilityGrant, DataClass, MethodRecord, SandboxRecord, TraceEvent
from .store import ControlStore
from .tasks import TaskService

if TYPE_CHECKING:
    from phi_core.agents.base import AgentMessage
    from phi_core.control.manager import ManagerSupervision

    from .handoff import HandoffGateway


class TraceWriter(Protocol):
    async def emit(self, **fields: Any) -> None: ...


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


class OpaqueWriter(Protocol):
    """Async, store-backed counterpart to ``opaque.OpaqueMap`` (Phase R-c):
    an agent never touches the run's opaque map dict directly, only mints
    and resolves tokens through this facade, which persists every mint
    through ``Manager.record_opaque_map`` (the sole
    ``workflow_runs.opaque_map`` writer) before the caller's output can
    reach an LLM-facing structure."""

    async def to_opaque(self, kind: str, canonical: str) -> str: ...

    async def from_opaque(self, token: str) -> str: ...


class MethodRegistryReader(Protocol):
    """Read-only facade onto ``methods.get_approved_methods`` (Phase R-c,
    docs #38): the sole query surface an agent may use to check whether a
    researched method recommendation has actually cleared the
    MethodRegistry lifecycle before treating it as safe to execute."""

    async def get_approved_methods(self, hipaa_category: str | None = None) -> list[MethodRecord]: ...


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
    manager: "ManagerSupervision | None" = None
    # Every `Agent` subclass's `run()` is completed against this on return
    # (see `Agent.__init_subclass__`) -- `None` for a context with no
    # backing `WorkItem` to complete (most unit tests build one via
    # `control.testing.make_ctx` without wiring a real `TaskService`).
    tasks: TaskCompleter | None = None
    # Phase R-c: the control-plane fields every agent needs to reach the
    # previously-inert Phase 2/3 modules. `handoff`/`opaque`/`methods` are
    # store-backed facades (matching `cache`/`artifacts`/`evidence` above);
    # `sandbox` is the run-scoped `SandboxRecord` `control.sandbox
    # .run_isolated` takes directly, not a facade. `sandbox` stays `None`
    # unless the caller opted the run into one (`ActivationFactory
    # .activate(..., needs_sandbox=True)`) -- real filesystem/platform
    # work, never created for every context automatically.
    handoff: "HandoffGateway | None" = None
    sandbox: SandboxRecord | None = None
    opaque: OpaqueWriter | None = None
    methods: MethodRegistryReader | None = None


class StoreResearchCache:
    """Cache facade backed by ``ControlStore`` rather than a raw database
    handle (D16). Every entry is stamped with the ``POLICY_VERSION`` that
    was active when it was written; ``get`` treats a stale-policy or
    unparseable entry as a miss rather than serving content written under
    a policy that may no longer apply. ``evidence_state`` records whether
    the cached content is tool-backed (``UNVERIFIED`` -- this layer does
    not itself re-verify tool citations, so it never claims ``VERIFIED``)
    or untooled (``UNKNOWN``), derived from ``source`` rather than
    trusting a caller-supplied claim."""

    _TOOL_BACKED_SOURCES = frozenset({"web_search"})

    def __init__(self, store: Any) -> None:
        self._store = store

    async def get(self, topic: str, jurisdiction: str = "us") -> dict[str, Any] | None:
        from datetime import datetime, timedelta, timezone

        from .limits import WEB_CACHE_REFRESH_DAYS
        from .policy import POLICY_VERSION

        doc = await self._store.get_one("web_cache", {"topic": topic, "jurisdiction": jurisdiction})
        if not doc:
            return None
        if doc.get("policy_version") != POLICY_VERSION:
            return None
        fetched_raw = doc.get("fetched_at")
        if isinstance(fetched_raw, datetime):
            fetched = fetched_raw if fetched_raw.tzinfo else fetched_raw.replace(tzinfo=timezone.utc)
        elif isinstance(fetched_raw, str):
            try:
                fetched = datetime.fromisoformat(fetched_raw)
            except ValueError:
                return None
        else:
            return None
        if datetime.now(timezone.utc) - fetched > timedelta(days=WEB_CACHE_REFRESH_DAYS):
            return None
        return doc

    async def put(self, topic: str, jurisdiction: str, content: str, source: str, schema_version: int = 1) -> None:
        from datetime import datetime, timezone

        from .policy import POLICY_VERSION

        document = {
            "topic": topic,
            "jurisdiction": jurisdiction,
            "content": content,
            "source": source,
            "evidence_state": "UNVERIFIED" if source in self._TOOL_BACKED_SOURCES else "UNKNOWN",
            "policy_version": POLICY_VERSION,
            "schema_version": schema_version,
            # A native datetime (BSON Date once persisted), not an
            # isoformat string: `server.py::_startup_maintenance`'s
            # `expireAfterSeconds` TTL index only ever fires on a real
            # Date field -- a string field is silently never eligible.
            "fetched_at": datetime.now(timezone.utc),
        }
        if not await self._store.replace_one("web_cache", {"topic": topic, "jurisdiction": jurisdiction}, document):
            await self._store.insert("web_cache", document)


class StoreTraceWriter:
    """The task-facing ``TraceWriter``: every field an agent call passes
    to ``emit`` is forwarded straight to :class:`~.events.TraceEventStore`,
    which owns ``seq`` allocation, hash chaining, and the fence check.

    Built on ``ControlStore`` so the same implementation works against
    ``MongoControlStore`` in production and ``MemoryControlStore`` in
    tests.
    """

    def __init__(self, store: Any, *, run_id: str, session_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._session_id = session_id

    async def emit(self, **fields: Any) -> None:
        from .events import TraceEventStore

        event = TraceEvent(
            event_id=str(fields.pop("event_id", "")) or TraceEvent.model_fields["event_id"].default_factory(),
            run_id=self._run_id,
            seq=0,
            session_id=self._session_id,
            task_id=str(fields.pop("task_id", "")),
            parent_task_id=str(fields.pop("parent_task_id", "")),
            parent_msg_id=str(fields.pop("parent_msg_id", "")),
            phase=str(fields.pop("phase", "")),
            direction=str(fields.pop("direction", "")),
            payload=dict(fields.pop("payload", {})),
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
        fence = fields.pop("fence", None)
        await TraceEventStore(self._store, run_id=self._run_id, session_id=self._session_id).append(
            event, fence=fence,
        )


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


class StoreOpaqueWriter:
    """The task-facing ``OpaqueWriter``: mints a run-scoped opaque token
    via the pure, deterministic :class:`~.opaque.OpaqueMap` (HMAC digest,
    collision check unchanged) and durably persists the enlarged map
    through ``Manager.record_opaque_map`` -- encrypted at rest
    by ``OpaqueMap`` itself (D5) -- before returning the token, so a
    sensitive value projected by any agent this run is recorded before
    that agent's output can reach an LLM-facing structure.

    Retries the load-mutate-persist cycle to absorb a lost race against a
    concurrent writer in the same run (e.g. Schema and Lexicon minting
    different tokens at the same time under section 27's specialist
    concurrency): ``record_opaque_map`` itself already retries its own
    CAS internally, but a *different* concurrent addition landing between
    this class's read and its call to ``record_opaque_map`` would
    otherwise still overwrite that addition outright (last-writer-wins is
    ``record_opaque_map``'s own, pre-existing, unchanged semantics); the
    outer retry here re-reads and re-applies this mint on top of
    whatever the other writer landed, rather than clobbering it.
    """

    def __init__(self, store: ControlStore, tasks: TaskService, *, run_id: str) -> None:
        from .manager import Manager

        self._orchestrator = Manager(store, tasks)
        self._run_id = run_id

    async def to_opaque(self, kind: str, canonical: str) -> str:
        from .opaque import OpaqueMap

        for _ in range(10):
            run = await self._orchestrator._load_run(self._run_id)
            current = dict(run.opaque_map)
            token = OpaqueMap(self._run_id, current).to_opaque(kind, canonical)
            if current == run.opaque_map:
                return token  # this exact (kind, canonical) was already minted
            updated = await self._orchestrator.record_opaque_map(run_id=self._run_id, opaque_map=current)
            if updated.opaque_map == current:
                return token
        raise RuntimeError(f"unable to persist opaque token for run_id={self._run_id!r} after retries")

    async def from_opaque(self, token: str) -> str:
        from .opaque import OpaqueMap

        run = await self._orchestrator._load_run(self._run_id)
        return OpaqueMap(self._run_id, dict(run.opaque_map)).from_opaque(token)


class StoreMethodRegistryReader:
    """The task-facing ``MethodRegistryReader``: delegates to
    ``methods.get_approved_methods``, the sole query surface that never
    returns a record whose lifecycle is anything but ``approved``."""

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    async def get_approved_methods(self, hipaa_category: str | None = None) -> list[MethodRecord]:
        from .methods import get_approved_methods

        return await get_approved_methods(self._store, hipaa_category)
