from phi_core.agents.reasoning import BLOCKING_ISSUE_FLOOR, apply_blocking_floor


def _decide(**kw):
    base = {"file_id": "f1", "column": "col", "action": "keep", "confidence": 0.9,
            "reason": "clinically useful", "phi_category": "NONE"}
    base.update(kw)
    return base


def test_at_floor_forced_to_human_review_with_suggested_fields():
    attempts = {("f1", "col"): BLOCKING_ISSUE_FLOOR}
    out, overrides = apply_blocking_floor([_decide()], attempts)
    assert out[0]["action"] == "human_review"
    assert out[0]["suggested_action"] == "keep"
    assert out[0]["suggested_confidence"] == 0.9
    assert "3" in out[0]["suggested_reason"]
    assert len(overrides) == 1
    override = overrides[0]
    assert override == {
        "file_id": "f1", "column": "col",
        "from": "keep", "to": "human_review",
        "rule": "blocking_issue_floor", "attempts": BLOCKING_ISSUE_FLOOR,
    }


def test_below_floor_not_forced():
    attempts = {("f1", "col"): BLOCKING_ISSUE_FLOOR - 1}
    out, overrides = apply_blocking_floor([_decide()], attempts)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_already_human_review_not_double_touched():
    attempts = {("f1", "col"): BLOCKING_ISSUE_FLOOR}
    out, overrides = apply_blocking_floor(
        [_decide(action="human_review", suggested_action="keep")], attempts)
    assert out[0]["action"] == "human_review"
    assert out[0]["suggested_action"] == "keep"
    assert overrides == []


def test_missing_key_defaults_to_zero_attempts():
    out, overrides = apply_blocking_floor([_decide()], {})
    assert out[0]["action"] == "keep"
    assert overrides == []


import asyncio

import pytest


def _run_blocking_floor_pipeline(tmp_path, monkeypatch, iteration_cap):
    """Drive orchestrator.run_pipeline with a Judge that always proposes a
    fresh action (never repeating the prior one, so the anti-loop rule
    never fires) and a Sentinel that always raises the same blocking issue
    on the same column. Proves the three-try floor overrides iteration_cap."""
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
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis:
        def __init__(self, **_kwargs):
            pass

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"columns": []}

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return {"fields": []}

    class FakeSchema(FakeLexicon):
        pass

    # Cycles through distinct legal actions so the anti-loop rule (which
    # only fires when Judge repeats the exact same rejected action) never
    # short-circuits the loop before the blocking floor gets its three
    # tries.
    judge_actions = ["keep", "pseudonymize", "hash", "keep", "pseudonymize"]

    class FakeJudge:
        def __init__(self, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None
            self.calls = 0

        async def run(self, **_kwargs):
            action = judge_actions[self.calls] if self.calls < len(judge_actions) else "keep"
            self.calls += 1
            return {"decisions": [{
                "file_id": "dataset.csv",
                "column": "col",
                "action": action,
                "confidence": 0.9,
                "reason": "Judge decision",
                "subject": "study",
                "phi_category": "NONE",
            }]}

    class FakeSentinel:
        def __init__(self, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"issues": [{
                "file_id": "dataset.csv",
                "column": "col",
                "severity": "blocking",
                "problem": "always objects",
            }]}

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
        object(),
        emit,
        on_phase,
    ))
    return result, phase_events


@pytest.mark.parametrize("iteration_cap", [1, 2, 3])
def test_blocking_floor_overrides_iteration_cap(tmp_path, monkeypatch, iteration_cap):
    result, phase_events = _run_blocking_floor_pipeline(tmp_path, monkeypatch, iteration_cap)

    judge_iters = [p for p, _ in phase_events if p.startswith("judge_iter_")]
    assert judge_iters == ["judge_iter_1", "judge_iter_2", "judge_iter_3"]

    assert result["status"] == "awaiting_human_review"
    decision = result["decisions"][0]
    assert decision["action"] == "human_review"
    assert decision["suggested_action"] == "hash"

    floor_events = [payload for phase, payload in phase_events if phase == "blocking_floor_iter_3"]
    assert len(floor_events) == 1
    overrides = floor_events[0]["overrides"]
    assert len(overrides) == 1
    assert overrides[0] == {
        "file_id": "dataset.csv", "column": "col",
        "from": "hash", "to": "human_review",
        "rule": "blocking_issue_floor", "attempts": 3,
    }
    # No premature floor override at iterations 1 or 2.
    assert not [p for p, _ in phase_events if p in ("blocking_floor_iter_1", "blocking_floor_iter_2")]
