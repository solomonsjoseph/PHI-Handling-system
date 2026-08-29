"""Coverage for Manager's deterministic guardian query broker:
attach_schema/ask_schema, attach_instrument/ask_instrument,
attach_lexicon/ask_lexicon.

Follows test_manager.py's dependency-free convention: plain fakes, no live
LLM key, no Mongo.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.manager import ExecutionHealthSupervisor, Manager
from phi_core.control.records import HandoffResult
from phi_core.control.testing import make_ctx


class FakeSchema:
    def verify(self, column: str, file_id: str | None = None) -> dict:
        if column.lower() == "mrn":
            return {"present": True, "file_id": file_id or "f1"}
        return {"present": False, "explanation": "not present"}


class FakeInstrument:
    def verify(self, field_or_variable: str, file_id: str | None = None) -> dict:
        if field_or_variable.lower() == "dob":
            return {"present": True, "file_id": file_id or "f1", "field": {"label": "DOB"}}
        return {"present": False}


class FakeLexicon:
    def __init__(self):
        self.calls = 0

    async def answer(self, column: str, assumption: str, reasoning: str) -> dict:
        self.calls += 1
        return {"verdict": "confirmed", "explanation": "matches dictionary row", "citation": "row 4"}


def _manager() -> Manager:
    return Manager(make_ctx("Manager"))


def test_ask_schema_without_attach_is_unavailable():
    m = _manager()
    result = asyncio.run(m.ask_schema("Judge", "mrn"))
    assert result == {"available": False, "reason": "schema_not_attached"}


def test_ask_schema_forwards_to_verify():
    m = _manager()
    m.attach_schema(FakeSchema())
    result = asyncio.run(m.ask_schema("Judge", "mrn"))
    assert result == {"available": True, "present": True, "file_id": "f1"}

    absent = asyncio.run(m.ask_schema("Judge", "nope"))
    assert absent["available"] is True
    assert absent["present"] is False


def test_ask_instrument_without_attach_is_unavailable():
    m = _manager()
    result = asyncio.run(m.ask_instrument("Sentinel", "dob"))
    assert result == {"available": False, "reason": "instrument_not_attached"}


def test_ask_instrument_forwards_to_verify():
    m = _manager()
    m.attach_instrument(FakeInstrument())
    result = asyncio.run(m.ask_instrument("Sentinel", "dob"))
    assert result["available"] is True
    assert result["present"] is True
    assert result["field"] == {"label": "DOB"}


def test_ask_lexicon_without_attach_is_unavailable():
    m = _manager()
    result = asyncio.run(m.ask_lexicon("Judge", "mrn", "direct identifier", "looks like an MRN"))
    assert result == {"available": False, "reason": "lexicon_not_attached"}


def test_ask_lexicon_forwards_to_answer():
    m = _manager()
    fake = FakeLexicon()
    m.attach_lexicon(fake)
    result = asyncio.run(m.ask_lexicon("Judge", "mrn", "direct identifier", "looks like an MRN"))
    assert result["available"] is True
    assert result["verdict"] == "confirmed"
    assert fake.calls == 1


def test_ask_lexicon_budget_exhausted():
    m = _manager()
    m.attach_lexicon(FakeLexicon())
    for _ in range(Manager.LEXICON_QUERY_BUDGET):
        result = asyncio.run(m.ask_lexicon("Judge", "mrn", "a", "r"))
        assert result["available"] is True
    exhausted = asyncio.run(m.ask_lexicon("Judge", "mrn", "a", "r"))
    assert exhausted == {"available": False, "reason": "budget_exhausted"}


# ---- Wave 4b: handoff-observation action responder -------------------------


def test_manager_is_the_execution_health_supervisor_compat_name():
    """`Manager` is a compatibility re-export of the real, renamed class --
    proves the alias resolves to the genuine demoted supervisor, not a
    stale duplicate definition."""
    assert Manager is ExecutionHealthSupervisor


def _result(*, allowed: bool, reason_code: str = "", sender: str = "Judge",
            recipient: str = "Schema") -> HandoffResult:
    return HandoffResult(handoff_id="h1", run_id="r1", sender=sender,
                         recipient=recipient, allowed=allowed, reason_code=reason_code)


def test_respond_to_handoff_allows_an_allowed_result():
    m = _manager()
    action = asyncio.run(m.respond_to_handoff(result=_result(allowed=True)))
    assert action == "ALLOW"


def test_respond_to_handoff_blocks_a_plain_denial():
    m = _manager()
    action = asyncio.run(m.respond_to_handoff(result=_result(allowed=False, reason_code="topology_blocked")))
    assert action == "BLOCK"


def test_respond_to_handoff_cancels_on_residual_phi_detected():
    m = _manager()
    action = asyncio.run(m.respond_to_handoff(result=_result(allowed=False, reason_code="residual_phi_detected")))
    assert action == "CANCEL"


def test_respond_to_handoff_cancels_on_secret_detected():
    m = _manager()
    action = asyncio.run(m.respond_to_handoff(result=_result(allowed=False, reason_code="secret_detected")))
    assert action == "CANCEL"


def test_respond_to_handoff_escalates_after_repeated_denial_on_same_edge():
    m = _manager()
    denial = _result(allowed=False, reason_code="topology_blocked", sender="Judge", recipient="Schema")
    actions = [asyncio.run(m.respond_to_handoff(result=denial))
              for _ in range(ExecutionHealthSupervisor.HANDOFF_DENIAL_ESCALATION_THRESHOLD)]
    assert actions[:-1] == ["BLOCK"] * (len(actions) - 1)
    assert actions[-1] == "ESCALATE"


def test_respond_to_handoff_denial_count_is_scoped_per_edge():
    m = _manager()
    same_edge = _result(allowed=False, reason_code="topology_blocked", sender="Judge", recipient="Schema")
    other_edge = _result(allowed=False, reason_code="topology_blocked", sender="Judge", recipient="Lexicon")
    for _ in range(ExecutionHealthSupervisor.HANDOFF_DENIAL_ESCALATION_THRESHOLD - 1):
        assert asyncio.run(m.respond_to_handoff(result=same_edge)) == "BLOCK"
    # A denial on a *different* edge never contributes to the first edge's count.
    assert asyncio.run(m.respond_to_handoff(result=other_edge)) == "BLOCK"
    assert asyncio.run(m.respond_to_handoff(result=same_edge)) == "ESCALATE"


def test_respond_to_handoff_budget_returns_limit():
    m = _manager()
    action = asyncio.run(m.respond_to_handoff_budget(category="judge_specialist_query"))
    assert action == "LIMIT"
