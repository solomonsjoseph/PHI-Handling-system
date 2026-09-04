import asyncio

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.agents.reasoning import CONFIDENCE_FLOOR, apply_confidence_floor
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run


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


def _run_confidence_floor_pipeline(tmp_path, monkeypatch, iteration_cap, judge_decisions=None):
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

    class FakeRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {})

    class FakePHIMethodsExpert:
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

    class FakeJudge:
        calls = 0

        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            if judge_decisions is not None:
                FakeJudge.calls += 1
                return await complete_fake_task(
                    self.ctx, {"decisions": judge_decisions(FakeJudge.calls)},
                )
            return await complete_fake_task(self.ctx, {"decisions": [{
                "file_id": "dataset.csv",
                "column": "col",
                "action": "keep",
                "confidence": 0.55,
                "reason": "Judge decision",
                "phi_category": "NONE",
            }]})

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0

        async def preview(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"issues": []})

    monkeypatch.setattr(orchestrator, "RegulationsExpert", FakeRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", FakePHIMethodsExpert)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)

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


def test_invalid_judge_action_is_re_asked_instead_of_sent_to_a_human(tmp_path, monkeypatch):
    """Judge is told it has no 'human_review' option. When it answers with one
    anyway, `validate_decisions` fails closed and discards everything it
    reasoned, which costs a reviewer a column over a formatting slip. The
    decide loop now spends one more Judge call instead."""
    def decisions(call):
        action = "human_review" if call == 1 else "pseudonymize"
        return [{
            "file_id": "dataset.csv", "column": "col", "action": action,
            "confidence": 0.95, "reason": "Judge decision", "phi_category": "R",
            "citation": "45 CFR 164.514(b)(2)(i)(R)",
        }]

    result, phase_events = _run_confidence_floor_pipeline(
        tmp_path, monkeypatch, iteration_cap=2, judge_decisions=decisions,
    )

    phases = [p for p, _ in phase_events]
    assert "judge_rejection_reask_iter_1" in phases
    assert "judge_iter_2" in phases
    assert result["decisions"][0]["action"] == "pseudonymize"


def test_a_judge_that_keeps_answering_invalidly_still_reaches_a_human(tmp_path, monkeypatch):
    """The re-ask is bounded by the operator's rigor selector, so a model that
    never complies still fails closed rather than looping."""
    def decisions(_call):
        return [{
            "file_id": "dataset.csv", "column": "col", "action": "human_review",
            "confidence": 0.95, "reason": "Judge decision", "phi_category": "R",
        }]

    result, phase_events = _run_confidence_floor_pipeline(
        tmp_path, monkeypatch, iteration_cap=2, judge_decisions=decisions,
    )

    judge_iters = [p for p, _ in phase_events if p.startswith("judge_iter_")]
    assert judge_iters == ["judge_iter_1", "judge_iter_2"]
    assert result["decisions"][0]["action"] == "human_review"
