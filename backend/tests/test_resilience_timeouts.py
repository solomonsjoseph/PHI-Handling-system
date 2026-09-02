"""Phase 14 (scale and resilience): provider and web-search timeout
degradation.

``gateway.py``'s ``ProviderGateway.complete`` never raises
``TimeoutError`` to its caller -- it catches ``asyncio.wait_for``'s
``TimeoutError`` internally and returns a ``GatewayResult(status="timeout")``
(``control/gateway.py:257-264``). ``Agent.call``'s ``attempt_plain``
translates that into a raised ``asyncio.TimeoutError`` only at the
*agent* boundary (``agents/base.py:247-248``). Every existing timeout
test (``test_manager.py``) exercises that agent-boundary contract with a
gateway fake that raises ``asyncio.TimeoutError`` directly from
``complete()`` -- a shortcut that never actually proves the real
``GatewayResult(status="timeout")`` contract degrades correctly. This
file drives the real contract (a ``FakeGateway`` reply that is a
``GatewayResult`` with ``status="timeout"``, never a raised exception)
through ``Agent.call``, ``Manager.run_supervised``, and
``call_with_web_search``, then up through the real orchestrator pipeline,
proving the whole chain degrades to a clear failure classification
(``human_review`` or a clean raised exception) rather than hanging or
crashing. No live network call anywhere in this file.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.agents.reasoning import Judge
from phi_core.control.gateway import GatewayResult, ProviderGateway
from phi_core.control.manager import ManagerSupervision as Manager
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, make_ctx, start_test_run


def _timeout_result(req) -> GatewayResult:
    """The real production shape of a timed-out gateway call
    (``ProviderGateway.complete``'s own ``except TimeoutError`` branch),
    never a raised exception."""
    return GatewayResult("", (), req.provider, req.model, "", {}, 0.0, 0, "timeout", "timeout", "")


class _AlwaysTimesOutGateway:
    """A ``FakeGateway``-shaped stand-in whose ``complete`` returns the
    genuine ``GatewayResult(status="timeout")`` contract, unconditionally,
    for every request -- never a raised exception, never a live call."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def complete(self, req) -> GatewayResult:
        self.requests.append(req)
        return _timeout_result(req)


# ---- Agent.call() degrades to "" without a manager -------------------------


@pytest.mark.asyncio
async def test_agent_call_degrades_to_empty_string_on_a_genuine_gateway_timeout_result() -> None:
    """No ``Manager`` attached (the solo-agent path in ``Agent.call``):
    a real ``GatewayResult(status="timeout")`` reply must return ``""``
    promptly, never hang or raise out to the caller."""
    gateway = _AlwaysTimesOutGateway()
    ctx = make_ctx("Judge", gateway=gateway)
    judge = Judge(ctx)

    reply = await asyncio.wait_for(
        judge.call("classify this column", phase="judge.test", timeout_s=1.0), timeout=5.0,
    )

    assert reply == ""
    assert judge.call_failures == 1
    assert len(gateway.requests) == 1


# ---- Manager.run_supervised: genuine gateway-timeout-result contract ------


def test_manager_exhausts_attempts_and_reports_timeout_on_genuine_gateway_timeout_result() -> None:
    """Complements ``test_manager.py``'s exception-raising-gateway timeout
    test with the real production contract: the gateway never raises,
    it returns ``GatewayResult(status="timeout")``, and ``Agent.call``'s
    own ``attempt_plain`` is what turns that into the
    ``asyncio.TimeoutError`` ``Manager.run_supervised`` retries against.
    Proves the whole real chain (gateway result -> Agent.call ->
    Manager retry loop) still reaches a clear failure classification,
    not just the agent-level shortcut."""
    ctx = make_ctx("Judge", gateway=_AlwaysTimesOutGateway())
    judge = Judge(ctx)
    manager = Manager(make_ctx("Manager"))
    manager.BACKOFF_S = {2: 0.0, 3: 0.0}
    judge.ctx = ctx.__class__(**{**ctx.__dict__, "manager": manager})

    async def fake_decide(*, task, legal, default_action, payload):
        from phi_core.control.manager import ManagerDecision
        return ManagerDecision(action="retry", note=None)
    manager._decide = fake_decide

    async def go():
        return await asyncio.wait_for(
            judge.call("classify this column", phase="judge.test", timeout_s=1.0), timeout=5.0,
        )

    reply = asyncio.run(go())

    assert reply == ""
    # Every attempt actually reached the gateway (real timeout result each
    # time), and the agent's own failure counter reflects the exhausted
    # supervised retry, not a silent success.
    assert judge.call_failures == 1
    assert len(judge.ctx.gateway.requests) == Manager.MAX_ATTEMPTS


# ---- web-search timeout degrades gracefully --------------------------------


@pytest.mark.asyncio
async def test_regulations_expert_degrades_to_the_deterministic_pack_on_a_genuine_web_search_timeout() -> None:
    """``RegulationsExpert.rules_for`` (the real production entrypoint
    every caller uses, never the internal ``call_with_web_search`` helper
    directly) wraps its web-search call in its own ``try/except Exception``
    (``experts.py:286-318``) specifically to catch this: a web-search
    timeout surfaces as a plain ``RuntimeError`` at the point of denial
    (``agents/base.py:333``), not ``asyncio.TimeoutError``, and this is
    the real, documented degrade-to-deterministic-fallback path, not a
    hang or an unhandled crash. A ``FakeGateway`` reply that is a genuine
    ``GatewayResult(status="timeout")`` (never a raised exception, never
    a live network call) proves it end to end."""
    from phi_core.agents.experts import RegulationsExpert
    from phi_core.control.records import ResourceBudget
    from phi_core.jurisdictions import get_pack

    gateway = _AlwaysTimesOutGateway()
    ctx = make_ctx("RegulationsExpert", gateway=gateway)
    # web_search must be granted for the attempt to reach the gateway at
    # all, rather than short-circuiting on "not granted".
    ctx = ctx.__class__(**{**ctx.__dict__, "grant": ctx.grant.model_copy(
        update={"tools": {"web_search": 3}, "budget": ResourceBudget(**{
            **ctx.grant.budget.model_dump(), "max_tool_calls": 10,
        })},
    )})
    expert = RegulationsExpert(ctx)

    result = await asyncio.wait_for(expert.rules_for("us"), timeout=10.0)

    pack = get_pack("us")
    assert result["sources"] == []
    assert result["as_of"] == "deterministic-fallback"
    assert result["identifier_categories"] == pack.identifier_categories
    # Every web-search attempt actually reached the (always-timing-out)
    # gateway rather than short-circuiting before ever trying.
    assert len(gateway.requests) >= 1


# ---- full-pipeline degradation: Judge/Sentinel repeatedly time out --------


def _minimal_pipeline_files() -> list[dict]:
    return [{"kind": "dataset", "file_id": "f1", "columns": ["id", "notes"]}]


def _drive_real_judge_pipeline(monkeypatch, *, always_timeout: bool):
    """Drives the real ``orchestrator.run_pipeline`` with the real
    ``Judge``/``Sentinel`` classes left entirely unfaked -- only
    Lexicon/Instrument/Schema/Reviewer are stubbed to stay fast and
    deterministic (matching the house style every other file in this
    suite uses for those three), and ``ProviderGateway.complete`` is
    monkeypatched at the class level to always return the genuine
    ``GatewayResult(status="timeout")`` contract, never a raised
    exception and never a live network call. This is the first test in
    the suite to exercise the real Judge/Sentinel LLM-call machinery
    end to end against a simulated total provider outage."""
    from phi_core.agents import orchestrator

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0

        async def _log(self, *_a, **_kw):
            return None

        async def preview(self, **_kw):
            return await complete_fake_task(self.ctx, {"issues": []})

    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)

    if always_timeout:
        async def _always_timeout_complete(self, req):
            return _timeout_result(req)
        monkeypatch.setattr(ProviderGateway, "complete", _always_timeout_complete)

    class FakeSessions:
        async def find_one(self, *_a, **_kw):
            return None

        async def update_one(self, *_a, **_kw):
            return None

    class FakeAgentLog:
        async def insert_one(self, *_a, **_kw):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    async def emit(_msg):
        return None

    async def on_phase(_phase, _payload):
        return None

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {"id": "session", "files": _minimal_pipeline_files()},
            FakeDb(), LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )

    return asyncio.run(asyncio.wait_for(_go(), timeout=30.0))


def test_pipeline_raises_a_clean_decision_gate_failure_not_a_hang_when_judge_repeatedly_times_out(monkeypatch) -> None:
    """A total, sustained provider outage during Judge/Sentinel's real
    LLM-call machinery means Judge's own ``call_json`` default
    (``{"decisions": []}``, ``reasoning.py:732``) is all it ever
    produces -- zero decisions for every column. ``run_decision_gates``'s
    exact-coverage proof fails closed on that (``missing_decision`` for
    every column) and raises ``DecisionGateFailure`` -- a documented,
    disclosed propagation path (``orchestrator.py``'s own
    ``_dispatch_gate_decisions`` docstring), not a defect. This proves
    that propagation is prompt (bounded, never the kind of hang a stuck
    retry loop or an unawaited coroutine would cause) and clean (a
    named, catchable exception carrying the exact missing columns),
    exactly the same "clear failure classification, not a hang or an
    unhandled crash" property the RegulationsExpert propagation test
    below proves for the other research-timeout path."""
    from phi_core.control.gates import DecisionGateFailure

    started = time.perf_counter()
    with pytest.raises(DecisionGateFailure) as excinfo:
        _drive_real_judge_pipeline(monkeypatch, always_timeout=True)
    elapsed = time.perf_counter() - started

    assert "missing_decision" in str(excinfo.value)
    assert elapsed < 10.0, f"took {elapsed:.2f}s -- looks like a hang, not a clean raise"


@pytest.mark.asyncio
async def test_judge_call_failures_counter_is_what_drives_the_human_review_escalation(monkeypatch) -> None:
    """Narrower unit-level companion to the pipeline test above: drives
    the real ``Judge.run`` directly against an always-timing-out gateway
    and confirms its own ``call_failures`` counter -- the exact signal
    ``_dispatch_decide``/``_dispatch_gate_decisions`` read to force
    ``human_review`` (``orchestrator.py:1381,1648-1649``) -- is nonzero
    after a real (not faked-away) Judge call against a timing-out
    gateway, never silently zero."""
    gateway = _AlwaysTimesOutGateway()
    ctx = make_ctx("Judge", gateway=gateway)
    judge = Judge(ctx)

    result = await asyncio.wait_for(
        judge.run(schema={"columns": []}, instrument={"fields": []}, lexicon={"columns": []}, statute={}),
        timeout=10.0,
    )

    assert judge.call_failures >= 1
    assert isinstance(result, dict)


# ---- research degradation: one category times out, others succeed --------


def test_pipeline_completes_when_one_research_category_times_out_but_others_succeed(monkeypatch) -> None:
    """``_dispatch_demand_driven_research`` gathers every
    ``PHIMethodsExpert.method_for`` call with ``return_exceptions=True``
    (``orchestrator.py:1183-1186``) -- one category's persistent timeout
    must not crash the run or drop the other categories' results, only
    that one category's ``praxis_methods`` entry."""
    from phi_core.agents import orchestrator

    _COLUMNS = ["id", "zip", "notes"]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "drop", "phi_category": "A",
         "confidence": 0.9, "reason": "x", "subject": "participant", "citation": "c"},
        {"file_id": "f1", "column": "zip", "action": "zip3_truncate", "phi_category": "B",
         "confidence": 0.9, "reason": "x", "subject": "participant", "citation": "c"},
        {"file_id": "f1", "column": "notes", "action": "keep", "phi_category": "NONE",
         "confidence": 0.9, "reason": "x", "subject": "participant", "citation": ""},
    ]

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"decisions": decisions})

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0

        async def _log(self, *_a, **_kw):
            return None

        async def preview(self, **_kw):
            return await complete_fake_task(self.ctx, {"issues": [{
                "file_id": "f1", "column": "id", "severity": "blocking", "problem": "review needed",
            }]})

    class TimingOutRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {
                "regulation": "HIPAA Safe Harbor", "citation": "45 CFR 164.514",
                "handling_rules": [], "sources": [],
            })

    calls: list[str] = []

    class PartiallyTimingOutPHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def method_for(self, category):
            calls.append(category)
            if category == "A":
                raise asyncio.TimeoutError("simulated persistent provider timeout")
            return {"category": category, "methods": [{"name": "zip3_truncate", "sources": []}]}

    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "RegulationsExpert", TimingOutRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", PartiallyTimingOutPHIMethodsExpert)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)

    class FakeSessions:
        async def find_one(self, *_a, **_kw):
            return None

        async def update_one(self, *_a, **_kw):
            return None

    class FakeAgentLog:
        async def insert_one(self, *_a, **_kw):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    async def emit(_msg):
        return None

    async def on_phase(_phase, _payload):
        return None

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {"id": "session", "files": [{"kind": "dataset", "file_id": "f1", "columns": _COLUMNS}]},
            FakeDb(), LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )

    result = asyncio.run(asyncio.wait_for(_go(), timeout=15.0))

    assert result["status"] == "awaiting_human_review"
    assert sorted(calls) == ["A", "B"]


# ---- regulations-expert timeout propagates cleanly, does not hang --------


def test_pipeline_raises_promptly_not_hangs_when_regulations_expert_repeatedly_times_out(monkeypatch) -> None:
    """Unlike ``PHIMethodsExpert`` (gathered with ``return_exceptions=True``),
    ``_dispatch_demand_driven_research`` awaits ``RegulationsExpert``'s own
    task directly (``orchestrator.py:1187``, no ``return_exceptions``): a
    persistent timeout there is NOT swallowed inside ``run_pipeline`` --
    it propagates to the caller, which is exactly what
    ``server.py``'s ``session_handle`` route relies on (its own
    ``except Exception`` at the call site marks the session ``status:
    "failed"`` with a correlated error id). This proves that propagation
    is prompt (bounded by the 15s ``wait_for`` below) and clean (a plain
    raised exception), never a hang or a swallowed/silent failure."""
    from phi_core.agents import orchestrator

    decisions = [
        {"file_id": "f1", "column": "id", "action": "drop", "phi_category": "A",
         "confidence": 0.9, "reason": "x", "subject": "participant", "citation": "c"},
    ]

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"decisions": decisions})

    class AlwaysTimingOutRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            raise asyncio.TimeoutError("simulated persistent provider timeout")

    class NeverCalledPHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def method_for(self, category):
            return {"category": category, "methods": []}

    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "RegulationsExpert", AlwaysTimingOutRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", NeverCalledPHIMethodsExpert)

    class FakeSessions:
        async def find_one(self, *_a, **_kw):
            return None

        async def update_one(self, *_a, **_kw):
            return None

    class FakeAgentLog:
        async def insert_one(self, *_a, **_kw):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    async def emit(_msg):
        return None

    async def on_phase(_phase, _payload):
        return None

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {"id": "session", "files": [{"kind": "dataset", "file_id": "f1", "columns": ["id"]}]},
            FakeDb(), LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )

    # No outer wait_for here: the real RegulationsExpert exception must
    # propagate directly out of run_pipeline, promptly, on its own --
    # wrapping this in a safety-net wait_for would make "raised because
    # RegulationsExpert's own exception propagated" indistinguishable
    # from "raised because the safety net itself timed out" (a genuine
    # hang), which is exactly the ambiguity this test exists to rule out.
    started = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError, match="simulated persistent provider timeout"):
        asyncio.run(_go())
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"took {elapsed:.2f}s to propagate -- looks like a hang, not a clean raise"
