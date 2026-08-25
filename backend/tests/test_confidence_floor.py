import asyncio

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.agents.reasoning import CONFIDENCE_FLOOR, apply_confidence_floor
from phi_core.control.store import MemoryControlStore


def _decide(**kw):
    base = {"file_id": "f1", "column": "col", "action": "keep", "confidence": 0.9,
            "reason": "clinically useful", "phi_category": "NONE"}
    base.update(kw)
    return base


def test_below_floor_forced_to_human_review_with_suggested_fields():
    out, overrides = apply_confidence_floor([_decide(confidence=0.55)])
    assert out[0]["action"] == "human_review"
    assert out[0]["suggested_action"] == "keep"
    assert out[0]["suggested_confidence"] == 0.55
    assert "0.55" in out[0]["suggested_reason"]
    assert len(overrides) == 1
    assert overrides[0]["rule"] == "confidence_floor"


def test_at_floor_not_forced():
    out, overrides = apply_confidence_floor([_decide(confidence=CONFIDENCE_FLOOR)])
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_already_human_review_not_double_touched():
    out, overrides = apply_confidence_floor([_decide(action="human_review", confidence=0.2)])
    assert out[0]["action"] == "human_review"
    assert overrides == []


def _run_confidence_floor_pipeline(tmp_path, monkeypatch, iteration_cap):
    """Drive orchestrator.run_pipeline with a Judge that always proposes
    'keep' at a fixed below-floor confidence and a Sentinel that never
    raises an issue, so the only thing that can route the decision to
    human_review is the confidence floor itself. Sentinel doc: the floor
    is 'fixed at 0.80 regardless of iteration_cap/rigor selector' -- this
    proves the loop terminates identically on iteration 1 no matter how
    many iterations `iteration_cap` would otherwise allow."""
    from phi_core.agents import orchestrator

    source = tmp_path / "dataset.csv"
    source.write_text("col\nsome value\n", encoding="utf-8")
    phase_events = []

    class FakeSessions:
        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            return None

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeStatute:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis:
        def __init__(self, *_a, **_kwargs):
            pass

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"columns": []}

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return {"fields": []}

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, *_a, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            return {"decisions": [{
                "file_id": "dataset.csv",
                "column": "col",
                "action": "keep",
                "confidence": 0.55,
                "reason": "Judge decision",
                "phi_category": "NONE",
            }]}

    class FakeSentinel:
        def __init__(self, *_a, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"issues": []}

    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()
    result = asyncio.run(orchestrator.run_pipeline(
        {
            "id": "session",
            "iteration_cap": iteration_cap,
            "files": [{
                "kind": "dataset",
                "file_id": "dataset.csv",
                "stored_path": str(source),
            }],
        },
        db,
        LlmConfig(provider="anthropic", model="test", max_tokens=100),
        emit,
        on_phase,
        control_store=MemoryControlStore(),
    ))
    return result, phase_events


@pytest.mark.parametrize("iteration_cap", [1, 2, 3])
def test_floor_fires_identically_regardless_of_iteration_cap(tmp_path, monkeypatch, iteration_cap):
    result, phase_events = _run_confidence_floor_pipeline(tmp_path, monkeypatch, iteration_cap)

    # The floor fires on the very first iteration and Sentinel never raises
    # a blocking issue, so the loop short-circuits at iteration 1 no matter
    # how many iterations `iteration_cap` (or the BLOCKING_ISSUE_FLOOR-driven
    # `max_iterations`) would otherwise permit.
    judge_iters = [p for p, _ in phase_events if p.startswith("judge_iter_")]
    assert judge_iters == ["judge_iter_1"]
    floor_events = [payload for phase, payload in phase_events if phase == "confidence_floor_iter_1"]
    assert len(floor_events) == 1
    overrides = floor_events[0]["overrides"]
    assert overrides == [{
        "file_id": "dataset.csv", "column": "col",
        "from": "keep", "to": "human_review",
        "rule": "confidence_floor", "confidence": 0.55,
    }]

    assert result["status"] == "awaiting_human_review"
    decision = result["decisions"][0]
    assert decision["action"] == "human_review"
    assert decision["suggested_action"] == "keep"
    assert decision["suggested_confidence"] == 0.55
