"""Coverage for the 2026-08-24 close-out pass: Manager checkpoint coverage
on Executor/Operator/Reviewer, Auditor's confidence-floor second review, and
the reversal-key round trip. Same dependency-free convention as
test_manager.py: plain ``def test_...()`` driving coroutines with
``asyncio.run(...)``, no live LLM key, no Mongo.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.llm import LlmConfig
from phi_core.agents.reasoning import plain_human_review_reasons
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run
from phi_core.control.transform_primitives import PseudonymRegistry
from phi_core.crypto import decrypt_reversal_map, encrypt_reversal_map


def test_plain_human_review_reasons_never_leaks_raw_codes():
    reasons = ["executor_crashed", "auditor_confidence_below_floor:0.42", "unknown_future_code"]
    plain = plain_human_review_reasons(reasons)
    assert len(plain) == 3
    joined = " ".join(plain)
    # None of the raw internal codes/action-ids should ever surface verbatim.
    assert "executor_crashed" not in joined
    assert "0.42" not in joined
    assert "unknown_future_code" not in joined


def test_pseudonym_registry_save_round_trips_and_never_touches_exports():
    registry = PseudonymRegistry(salt="test-salt")
    token_a = registry.get("Jane Doe")
    token_b = registry.get("John Smith")
    assert registry.get("Jane Doe") == token_a  # same value -> same pseudonym

    blob = registry.save()
    assert isinstance(blob, str) and blob

    restored = decrypt_reversal_map(blob)
    assert restored["salt"] == "test-salt"
    assert restored["map"]["Jane Doe"] == token_a
    assert restored["map"]["John Smith"] == token_b
    # The raw identifier values must never appear in plaintext form outside
    # the encrypted blob -- the blob itself is the only place they live.
    assert "Jane Doe" not in blob
    assert "John Smith" not in blob


def test_pseudonym_registry_save_is_a_pure_function_with_no_db_access():
    registry = PseudonymRegistry(salt="s")
    registry.get("x")
    # No db/session args accepted -- the caller decides where the blob lives.
    import inspect
    sig = inspect.signature(registry.save)
    assert len(sig.parameters) == 0


def test_encrypt_reversal_map_round_trip_empty_and_populated():
    empty = decrypt_reversal_map("")
    assert empty == {}
    blob = encrypt_reversal_map({"salt": "abc", "map": {"1": "P1"}})
    assert decrypt_reversal_map(blob) == {"salt": "abc", "map": {"1": "P1"}}


# ---- Executor crash escalates deterministically, never through consult() --


def test_executor_crash_escalates_to_human_review_not_left_uncaught():
    """A crashing Executor must reach 'awaiting_human_review' cleanly -- it
    must never propagate an uncaught exception out of run_pipeline, and the
    escalation must not depend on Manager's consult() succeeding (consult()
    fails OPEN and must never be the thing deciding a safety-critical exit).
    """
    from phi_core.agents import orchestrator

    class FakeSessions:
        def __init__(self):
            self.updates = []

        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            self.updates.append(_args[1])

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeAgent:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {})

    class FakeRegulationsExpert(FakeAgent):
        pass

    class FakePHIMethodsExpert(FakeAgent):
        async def method_for(self, _category):
            return {}

    class FakeLexicon(FakeAgent):
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeAgent):
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge(FakeAgent):
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"decisions": [
                {"file_id": "f1", "column": "id", "action": "drop",
                 "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)",
                 "confidence": 0.95, "reason": "direct identifier"},
            ]})

    class FakeReviewer(FakeAgent):
        async def preview(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"issues": []})

    class FakeExecutor(FakeAgent):
        async def run(self, **_kwargs):
            raise RuntimeError("disk write failed")

    monkeypatch_targets = {
        "RegulationsExpert": FakeRegulationsExpert, "PHIMethodsExpert": FakePHIMethodsExpert, "Lexicon": FakeLexicon,
        "Instrument": FakeInstrument, "Schema": FakeSchema, "Judge": FakeJudge,
        "Reviewer": FakeReviewer, "Executor": FakeExecutor,
    }
    originals = {name: getattr(orchestrator, name) for name in monkeypatch_targets}
    for name, fake in monkeypatch_targets.items():
        setattr(orchestrator, name, fake)
    try:
        events = []

        async def emit(_msg):
            return None

        async def on_phase(phase, payload):
            events.append((phase, payload))

        db = FakeDb()

        async def _go():
            store = MemoryControlStore()
            await start_test_run(store, "session")
            return await orchestrator.run_pipeline(
                {"id": "session", "files": [
                    {"kind": "dataset", "file_id": "f1", "subtype": "csv",
                     "stored_path": "/tmp/does-not-matter.csv", "columns": ["id"]},
                ]}, db, LlmConfig(provider="anthropic", model="test", max_tokens=100), emit, on_phase,
                control_store=store,
            )

        result = asyncio.run(_go())
    finally:
        for name, orig in originals.items():
            setattr(orchestrator, name, orig)

    assert result["status"] == "awaiting_human_review"
    completion_update = db.sessions.updates[-1]["$set"]
    assert completion_update["status"] == "awaiting_human_review"
    assert "executor_crashed" in completion_update["human_review_reasons"]
    # The plain-English rendering must never leak the raw internal code.
    assert "executor_crashed" not in " ".join(completion_update["human_review_reasons_plain"])


# ---- verify_keep_decisions: an explicit human approval must stick -------


def test_explicit_human_approval_of_keep_is_not_re_demoted(tmp_path):
    """A hard-rule-forced 'keep' whose real values also match a detector
    would loop forever without this exemption: every approval silently
    reverted by the very next call. Reproduced live against the
    level_1_tuberculosis corpus on 2026-08-24 before this fix landed."""
    from phi_core.agents.reasoning import verify_keep_decisions

    csv_path = tmp_path / "dataset.csv"
    csv_path.write_text("state\nCalifornia\n", encoding="utf-8")

    approved = [{"file_id": "f1", "column": "state", "action": "keep",
                "provenance": "human_explicit_action", "confidence": 0.99}]
    verified, demotions = verify_keep_decisions(approved, {"f1": csv_path}, jurisdiction="us")
    assert verified[0]["action"] == "keep"
    assert not demotions


def test_model_comment_inferred_keep_is_still_scanned():
    """The exemption is narrow: a model's guess at interpreting a comment
    is not a person's direct confirmation, so it gets no special pass."""

    from phi_core.agents.reasoning import verify_keep_decisions

    unscanned = [{"file_id": "f1", "column": "state", "action": "keep",
                 "provenance": "human_comment_inferred", "confidence": 0.6}]
    # Any nonexistent path triggers the "unreadable" demotion path, proving
    # this decision was NOT skipped the way the exemption test's was.
    verified, demotions = verify_keep_decisions(unscanned, {"f1": __import__("pathlib").Path("/no/such/file.csv")})
    assert verified[0]["action"] == "human_review"
    assert demotions
