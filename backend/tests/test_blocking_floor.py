import asyncio

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.agents.reasoning import BLOCKING_ISSUE_FLOOR, apply_blocking_floor
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run


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


def _run_blocking_floor_pipeline(tmp_path, monkeypatch, iteration_cap, sentinel_issue_column="col"):
    """Drive orchestrator.run_pipeline with a Judge that always proposes a
    fresh action (never repeating the prior one, so the anti-loop rule
    never fires) and a Sentinel that always raises the same blocking issue.
    `sentinel_issue_column=None` produces a malformed blocking issue with no
    `column` key, so nothing is ever trackable toward the floor -- proves
    the three-try floor overrides iteration_cap, and that a columnless
    blocking issue can never satisfy it vacuously."""
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
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {})

    class FakePraxis:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    # Cycles through distinct legal actions so the anti-loop rule (which
    # only fires when Judge repeats the exact same rejected action) never
    # short-circuits the loop before the blocking floor gets its three
    # tries.
    judge_actions = ["keep", "pseudonymize", "hash", "keep", "pseudonymize"]
    # A fresh FakeJudge is instantiated every Judge<->Sentinel loop iteration
    # (orchestrator.run_pipeline does `judge = Judge(judge_ctx)` per-iteration,
    # matching the real agent), so a per-instance counter always reads back
    # index 0. Track the call count outside the class so it advances across
    # iterations the way judge_actions is meant to be cycled.
    judge_calls = [0]

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            calls = judge_calls[0]
            action = judge_actions[calls] if calls < len(judge_actions) else "keep"
            judge_calls[0] += 1
            return await complete_fake_task(self.ctx, {"decisions": [{
                "file_id": "dataset.csv",
                "column": "col",
                "action": action,
                "confidence": 0.9,
                "reason": "Judge decision",
                "subject": "study",
                "phi_category": "NONE",
            }]})

    class FakeSentinel:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0

        async def run(self, **_kwargs):
            issue = {
                "file_id": "dataset.csv",
                "severity": "blocking",
                "problem": "always objects",
            }
            if sentinel_issue_column is not None:
                issue["column"] = sentinel_issue_column
            return await complete_fake_task(self.ctx, {"issues": [issue]})

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

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
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
            control_store=store,
        )

    result = asyncio.run(_go())
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


def test_columnless_blocking_never_vacuously_satisfies_the_floor(tmp_path, monkeypatch):
    """A malformed Sentinel reply -- a blocking issue with no `column` key --
    can never be tracked toward the per-column floor. `iteration >= iteration_cap
    and all(... for key in blocking_by_column)` must not treat an empty
    `blocking_by_column` as satisfied: that would let iteration_cap=1 break
    the loop after a single iteration, without any column ever earning even
    one attempt, let alone three."""
    result, phase_events = _run_blocking_floor_pipeline(
        tmp_path, monkeypatch, iteration_cap=1, sentinel_issue_column=None)

    # The loop must run all the way to max_iterations (3): the malformed
    # blocking issue can never satisfy the floor-termination branch, so only
    # the range exhausting naturally ends it.
    judge_iters = [p for p, _ in phase_events if p.startswith("judge_iter_")]
    assert judge_iters == ["judge_iter_1", "judge_iter_2", "judge_iter_3"]

    # No column was ever tracked, so the floor never overrides anything.
    assert not [p for p, _ in phase_events if p.startswith("blocking_floor_iter_")]

    # Still ends in human review (Sentinel has unresolved blocking issues
    # after the cap), but via the downstream 'sentinel_blocking_after_cap'
    # gate rather than a floor override, and the decision itself is
    # whatever Judge last proposed -- never forced by apply_blocking_floor.
    assert result["status"] == "awaiting_human_review"
    decision = result["decisions"][0]
    assert decision["action"] == "hash"
    assert decision.get("suggested_action") is None
