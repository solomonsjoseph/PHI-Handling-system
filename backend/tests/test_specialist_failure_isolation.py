"""Phase 5 follow-up item 2 (section 27): specialist failure isolation in
``_dispatch_specialists``.

Phase 5 found ``_dispatch_specialists``'s ``asyncio.gather(state.lex_task,
state.schema_task, state.inst_task)`` had no ``return_exceptions=True``, so
one specialist's exception lost the other two specialists' already-
successful results (violates section 27: "preserving successful artifacts
when a sibling fails"). This test drives the full ``orchestrator.run_pipeline``
(same house style as ``test_demand_driven_research.py``) with Lexicon
crashing and Schema/Instrument succeeding, and proves Judge's actual
received schema/instrument kwargs are the real successful outputs (not
degraded defaults) while Lexicon's crash degrades to its own "did not run"
empty shape instead of losing its siblings' results or crashing the whole
pipeline.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.llm import LlmConfig
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run

_COLUMNS = ["col1"]


def _pipeline_files() -> list[dict]:
    return [
        {"kind": "dataset", "file_id": "f1", "columns": _COLUMNS},
        {"kind": "narrative", "file_id": "f2"},
        {"kind": "metadata", "file_id": "f3"},
    ]


def _decisions() -> list[dict]:
    return [{
        "file_id": "f1", "column": "col1", "action": "keep", "phi_category": "NONE",
        "confidence": 0.9, "reason": "col1 decision", "subject": "participant", "citation": "",
    }]


def _drive_pipeline_with_crashing_lexicon(monkeypatch):
    """Drive ``orchestrator.run_pipeline`` with Lexicon crashing and
    Schema/Instrument succeeding; captures every Judge invocation's
    kwargs so the test can assert on what Judge actually received."""
    from phi_core.agents import orchestrator

    judge_calls: list[dict] = []

    class CrashingLexicon:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            raise RuntimeError("Lexicon exploded")

    class SucceedingSchema:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": [{"name": "col1"}]})

    class SucceedingInstrument:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.scrub_count = 0

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"fields": [{"label": "f1"}]})

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **kwargs):
            judge_calls.append(kwargs)
            return await complete_fake_task(self.ctx, {"decisions": _decisions()})

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0

        async def _log(self, *_a, **_kw):
            return None

        async def preview(self, **_kw):
            return await complete_fake_task(self.ctx, {"issues": [{
                "file_id": "f1", "column": "col1",
                "severity": "blocking", "problem": "policy review needed",
            }]})

    monkeypatch.setattr(orchestrator, "Lexicon", CrashingLexicon)
    monkeypatch.setattr(orchestrator, "Schema", SucceedingSchema)
    monkeypatch.setattr(orchestrator, "Instrument", SucceedingInstrument)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
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
        result = await orchestrator.run_pipeline(
            {"id": "session", "files": _pipeline_files()},
            FakeDb(), LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )
        return result

    result = asyncio.run(_go())
    return result, judge_calls


def test_one_specialist_crash_does_not_lose_siblings_results(monkeypatch):
    """Lexicon crashes; Schema and Instrument succeed. ``_dispatch_specialists``
    must not raise, and Judge must receive Schema/Instrument's real
    successful outputs alongside Lexicon's degraded 'did not run' shape,
    never a crash that discards all three specialists' work."""
    result, judge_calls = _drive_pipeline_with_crashing_lexicon(monkeypatch)

    # The pipeline must not itself crash: it reaches the same
    # 'awaiting_human_review' short-circuit test_demand_driven_research.py's
    # FakeSentinel-always-blocks pattern always produces, never an
    # unhandled exception surfacing from _dispatch_specialists.
    assert result.get("status") == "awaiting_human_review", result
    assert judge_calls, "Judge never ran -- _dispatch_specialists must have raised"
    call = judge_calls[0]
    # Schema/Instrument's real successful outputs reached Judge unchanged.
    assert call["schema"] == {"columns": [{"name": "col1"}]}
    assert call["instrument"] == {"fields": [{"label": "f1"}]}
    # Lexicon's crash degraded to the same "did not run" empty shape
    # _dispatch_research already uses when no dictionary files exist --
    # never a raised exception, never Schema/Instrument's results lost
    # alongside it.
    assert call["lexicon"] == {"columns": [], "notes": ""}
