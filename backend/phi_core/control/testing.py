"""Control-plane test factories with no raw database dependency in agents."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from .artifacts import ArtifactService
from .context import AgentContext, ResearchCache
from .gateway import GatewayRequest, GatewayResult, ToolGateway
from .policy import CapabilityPolicy
from .records import WorkflowRun
from .store import ControlStore, MemoryControlStore
from .tasks import TaskService
from .writer import ArtifactWriter


@dataclass(frozen=True)
class _TestLlmConfig:
    provider: str = "anthropic"
    model: str = "test-model"
    base_url: str = ""


@dataclass
class FakeGateway:
    replies: deque[GatewayResult | str] = field(default_factory=deque)
    requests: list[GatewayRequest] = field(default_factory=list)

    async def complete(self, req: GatewayRequest) -> GatewayResult:
        self.requests.append(req)
        reply = self.replies.popleft() if self.replies else ""
        if isinstance(reply, GatewayResult):
            return reply
        return GatewayResult(reply, (), req.provider, req.model, "fake", {}, 0.0, 0, "ok", "", "")


@dataclass
class MemoryTrace:
    """``TraceWriter`` test fake. ``legacy_messages`` is a compatibility
    convenience for unit tests written before ``emit`` carried every
    ``AgentMessage`` field (``phase``/``payload``/``direction``/etc.)
    itself: each entry exposes those fields as attributes via
    ``SimpleNamespace`` rather than requiring dict-key access, matching
    the ``AgentMessage`` shape those tests already assert against."""

    events: list[dict[str, Any]] = field(default_factory=list)
    legacy_messages: list[Any] = field(default_factory=list)

    async def emit(self, **fields: Any) -> None:
        self.events.append(dict(fields))
        self.legacy_messages.append(SimpleNamespace(**fields))


@dataclass
class MemoryResearchCache:
    values: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    async def get(self, topic: str, jurisdiction: str = "us") -> dict[str, Any] | None:
        return self.values.get((topic, jurisdiction))

    async def put(self, topic: str, jurisdiction: str, content: str, source: str, schema_version: int = 1) -> None:
        self.values[(topic, jurisdiction)] = {
            "topic": topic,
            "jurisdiction": jurisdiction,
            "content": content,
            "source": source,
            "schema_version": schema_version,
        }


async def complete_fake_task(ctx: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Complete ``ctx``'s task with ``result`` and return ``result`` unchanged.

    Real agents get this for free from ``Agent.__init_subclass__``'s wrapper
    (``phi_core/agents/base.py``), which only wraps actual ``Agent``
    subclasses. An orchestrator-level unit-test fake standing in for a real
    agent is a bare class, not an ``Agent`` subclass, so it never completes
    its task -- leaving it stuck ``leased`` forever and ``accept_result``
    refusing every acceptance with "not accepted". Call this at the end of
    a fake's ``run()`` (or equivalent) with the same ``ctx`` orchestrator
    passed into its constructor.
    """
    if ctx is not None and getattr(ctx, "tasks", None) is not None:
        await ctx.tasks.complete(result if isinstance(result, dict) else {})
    return result


async def start_test_run(
    store: ControlStore, session_id: str, *, run_id: str | None = None, principal: str = "test-operator",
) -> WorkflowRun:
    """Open a real ``WorkflowRun`` through ``Manager.start_run``
    for orchestrator-level unit tests that call ``run_pipeline`` directly
    with a bare ``MemoryControlStore()``. Every production entry path opens
    a run this way before running the pipeline (see ``server.py``); a test
    that skips this step leaves ``accept_result``/``_load_run`` with no
    ``WorkflowRun`` document to find, raising ``unknown run_id`` on the
    pipeline's first agent acceptance. Defaults ``run_id`` to ``session_id``
    so it matches ``run_pipeline``'s own default ``effective_run_id`` when
    the caller passes no explicit ``run_id``.
    """
    from .manager import Manager

    orch = Manager(store, TaskService(store, CapabilityPolicy(None)))
    return await orch.start_run(session_id=session_id, principal=principal, run_id=run_id or session_id)


async def make_execute_state(
    *,
    db: Any,
    sid: str,
    session: dict[str, Any],
    session_filter: dict[str, Any],
    files: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    make_ctx: Any,
    complete_and_accept: Any,
    manager: Any,
    on_phase: Any,
    close_last_phase: Any,
    phase_timings: dict[str, Any] | None = None,
    run_started: float | None = None,
    omit_by_file: dict[str, set[str]] | None = None,
    sentinel_report: dict[str, Any] | None = None,
    run_id: str = "",
    store: ControlStore | None = None,
) -> Any:
    """Build an ``agents.orchestrator._PipelineDriverState`` populated
    exactly like ``run_pipeline``'s own driver loop would by the time it
    reaches the ``"execute"`` node, for a test calling
    ``_dispatch_execute``/``_dispatch_verify_output``/``_dispatch_package``
    (or the ``_dispatch_execute_tail`` combinator) directly (step 6: the
    retired ``execute_decisions`` monolith's former parameter list, now a
    state-construction helper rather than a plain function call, matching
    every other ``_dispatch_*`` handler's own test pattern). Lazy-imports
    ``agents.orchestrator`` to avoid a module-load-order dependency
    (this module is imported by orchestrator's own docstrings, never the
    reverse, but the import stays function-local to keep that direction
    explicit).
    """
    import time as _time

    from phi_core.agents.orchestrator import ResultAcceptanceError, _PipelineDriverState

    state = _PipelineDriverState(
        session=session, db=db, llm_cfg=_TestLlmConfig(), emit=complete_fake_task,
        on_phase=on_phase, run_id=run_id or None, control_store=store, root_task_id=None,
        sid=sid, effective_run_id=run_id,
    )
    state.session_filter = session_filter
    state.files = files
    state.approved_decisions = decisions
    state.make_ctx = make_ctx
    state.complete_and_accept = complete_and_accept

    async def require_accepted(ctx: Any, result: dict[str, Any], agent: str) -> None:
        if not await complete_and_accept(ctx, result):
            raise ResultAcceptanceError(f"{agent} result was not accepted")

    state.require_accepted = require_accepted
    state.manager = manager
    state.close_last_phase = close_last_phase
    state.phase_timings = phase_timings if phase_timings is not None else {}
    state.run_started = run_started if run_started is not None else _time.perf_counter()
    state.omit_by_file = omit_by_file or {}
    state.sentinel_report = sentinel_report or {}
    return state


def make_ctx(agent: str, **overrides: Any) -> AgentContext:
    """Build a task-scoped context for an agent unit test.

    ``gateway``, ``trace``, ``cache``, ``artifacts``, ``evidence``, and
    identifiers may be overridden. The factory never exposes a database
    handle. ``session_id``/``run_id`` default to a fresh hex token (not a
    literal placeholder) because ``run_scoped_dir`` -- and therefore the
    default ``artifacts`` facade below -- refuses anything else.
    """
    run_id = overrides.pop("run_id", uuid4().hex)
    task_id = overrides.pop("task_id", uuid4().hex)
    session_id = overrides.pop("session_id", uuid4().hex)
    policy = overrides.pop("policy", CapabilityPolicy(_TestLlmConfig()))
    task_type = overrides.pop("task_type", agent.lower().replace(".", "_"))
    grant = overrides.pop("grant", policy.issue_grant(run_id=run_id, task_id=task_id, agent=agent, task_type=task_type))
    gateway = overrides.pop("gateway", FakeGateway())
    trace = overrides.pop("trace", MemoryTrace())
    cache: ResearchCache | None = overrides.pop("cache", MemoryResearchCache())
    store = overrides.pop("store", None)
    artifacts = overrides.pop("artifacts", None)
    if artifacts is None:
        artifacts = ArtifactWriter(
            ArtifactService(store or MemoryControlStore(), session_id=session_id, run_id=run_id),
            producer_task_id=task_id,
        )
    evidence = overrides.pop("evidence", None)
    if overrides:
        unknown = ", ".join(sorted(overrides))
        raise TypeError(f"unsupported context override(s): {unknown}")
    return AgentContext(
        session_id=session_id,
        run_id=run_id,
        task_id=task_id,
        agent=agent,
        attempt=1,
        grant=grant,
        gateway=gateway,  # type: ignore[arg-type]
        tools=ToolGateway(gateway),  # type: ignore[arg-type]
        trace=trace,
        cache=cache,
        artifacts=artifacts,
        evidence=evidence,
    )
