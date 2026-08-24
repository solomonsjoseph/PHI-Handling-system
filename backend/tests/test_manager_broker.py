"""Coverage for Manager's deterministic guardian query broker:
attach_schema/ask_schema, attach_instrument/ask_instrument,
attach_lexicon/ask_lexicon.

Follows test_manager.py's dependency-free convention: plain fakes, no live
LLM key, no Mongo.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.manager import Manager


class FakeAgentLog:
    async def insert_one(self, *_args, **_kwargs):
        return None


class FakeSessions:
    async def find_one(self, *_args, **_kwargs):
        return None

    async def update_one(self, *_args, **_kwargs):
        return None


class FakeDb:
    def __init__(self):
        self.sessions = FakeSessions()
        self.agent_log = FakeAgentLog()


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
    return Manager(session_id="s", llm=None, db=FakeDb())


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
