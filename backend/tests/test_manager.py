"""Coverage for the Manager (13th agent): supervision, consult, escalation,
and the deliverable contracts declared by Schema and Judge.

Follows the dependency-free convention of test_narrative_export.py and
test_keep_verification_pipeline.py: plain ``def test_...()`` driving
coroutines with ``asyncio.run(...)``, no live LLM key, no Mongo.
"""
from __future__ import annotations

import asyncio
import json

from phi_core.agents.base import _json_validator
from phi_core.agents.manager import Manager, ManagerAdvice, ManagerDecision

# ---- shared fakes, following test_keep_verification_pipeline.py:13-26 -----


class FakeAgentLog:
    async def insert_one(self, *_args, **_kwargs):
        return None


class FakeSessions:
    def __init__(self):
        self.updates: list[tuple] = []

    async def find_one(self, *_args, **_kwargs):
        return None

    async def update_one(self, *args, **_kwargs):
        self.updates.append(args)


class FakeDb:
    def __init__(self):
        self.sessions = FakeSessions()
        self.agent_log = FakeAgentLog()


def _no_sleep(manager: Manager) -> None:
    manager.BACKOFF_S = {2: 0.0, 3: 0.0}


# ---- attempt ceiling / recovery / fail-closed / tool grant -----------------


def test_attempt_ceiling_escalates_after_max_attempts():
    m = Manager(session_id="s", llm=None, db=FakeDb())
    _no_sleep(m)

    async def fake_decide(*, task, legal, default_action, payload):
        return ManagerDecision(action="retry", note=None)
    m._decide = fake_decide

    calls: list[int] = []

    async def always_timeout(system_prompt, extended):
        calls.append(1)
        raise asyncio.TimeoutError()

    reply, ok, err = asyncio.run(m.run_supervised(
        agent_name="Judge", phase="judge.decide", base_system_prompt="SYS",
        primary_attempt=always_timeout))

    assert len(calls) == Manager.MAX_ATTEMPTS
    assert (reply, ok, err) == ("", False, "timeout")


def test_recovery_after_one_retry():
    m = Manager(session_id="s", llm=None, db=FakeDb())
    _no_sleep(m)

    async def fake_decide(*, task, legal, default_action, payload):
        return ManagerDecision(action="retry", note=None)
    m._decide = fake_decide

    calls: list[int] = []

    async def fails_once(system_prompt, extended):
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.TimeoutError()
        return "ok reply"

    reply, ok, err = asyncio.run(m.run_supervised(
        agent_name="Judge", phase="judge.decide", base_system_prompt="SYS",
        primary_attempt=fails_once))

    assert (reply, ok, err) == ("ok reply", True, None)
    assert len(calls) == 2
    report = asyncio.run(m.close_run("complete"))
    assert report["recovered_count"] == 1


def test_fail_closed_on_garbage_manager_reply(monkeypatch):
    import phi_core.agents.base as base
    monkeypatch.setattr(base, "call_llm", lambda system, user, llm: "not json")

    m = Manager(session_id="s", llm=None, db=FakeDb())
    _no_sleep(m)

    async def always_timeout(system_prompt, extended):
        raise asyncio.TimeoutError()

    reply, ok, err = asyncio.run(m.run_supervised(
        agent_name="Judge", phase="judge.decide", base_system_prompt="SYS",
        primary_attempt=always_timeout))

    assert (reply, ok) == ("", False)
    assert m._interventions[0]["action"] == "escalate"


def test_tool_grant_uses_escalated_attempt_on_retry():
    m = Manager(session_id="s", llm=None, db=FakeDb())
    _no_sleep(m)

    async def fake_decide(*, task, legal, default_action, payload):
        assert "grant_web_search" in legal
        return ManagerDecision(action="grant_web_search", note=None)
    m._decide = fake_decide

    primary_calls: list[int] = []
    escalated_calls: list[int] = []

    async def primary(system_prompt, extended):
        primary_calls.append(1)
        raise asyncio.TimeoutError()

    async def escalated(system_prompt, extended):
        escalated_calls.append(1)
        return "web result"

    reply, ok, err = asyncio.run(m.run_supervised(
        agent_name="Scout", phase="scout.run", base_system_prompt="SYS",
        primary_attempt=primary, escalated_attempt=escalated))

    assert (reply, ok, err) == ("web result", True, None)
    assert len(primary_calls) == 1
    assert len(escalated_calls) == 1


# ---- off-task detection -----------------------------------------------


def test_json_validator_off_task_counts_only():
    validate = _json_validator("decisions", 10)
    result = validate('{"decisions": [1, 2, 3]}')
    assert result == {"kind": "off_task", "owed": 10, "delivered": 3}


def test_off_task_reply_is_retried_and_record_carries_only_counts():
    m = Manager(session_id="s", llm=None, db=FakeDb())
    _no_sleep(m)

    async def fake_decide(*, task, legal, default_action, payload):
        return ManagerDecision(action="retry", note=None)
    m._decide = fake_decide

    validate = _json_validator("decisions", 10)

    async def under_delivers(system_prompt, extended):
        # A short, well-formed reply naming a real column -- the validator
        # must reduce this to counts only, never pass the column through.
        return '{"decisions": [{"column": "patient_name"}, {"column": "mrn"}, {"column": "dob"}]}'

    reply, ok, err = asyncio.run(m.run_supervised(
        agent_name="Judge", phase="judge.decide", base_system_prompt="SYS",
        primary_attempt=under_delivers, validate=validate))

    assert ok is False
    rec = m._interventions[0]
    assert rec["owed"] == 10 and rec["delivered"] == 3
    assert rec["error_kind"] == "off_task"
    assert set(rec) <= {"agent", "phase", "attempt", "error_kind", "attempt_s",
                        "over_budget", "owed", "delivered", "action", "note", "reason"}
    dumped = json.dumps(m._interventions)
    assert "patient_name" not in dumped
    assert "mrn" not in dumped
    assert "column" not in dumped


# ---- Judge / Schema deliverable contracts -------------------------------


def test_judge_declares_deliverable_contract():
    import phi_core.agents.reasoning as reasoning

    captured: dict = {}

    class StubJudge(reasoning.Judge):
        async def call_json(self, *a, **kw):
            captured.update(kw)
            return {"decisions": []}

    j = StubJudge(session_id="s", llm=None, db=FakeDb())
    asyncio.run(j.run(schema={"columns": [{}, {}, {}, {}]}, instrument={},
                      lexicon={}, statute={}))

    assert captured["expect_key"] == "decisions"
    assert captured["min_items"] == 4


def test_judge_min_items_zero_on_empty_study():
    import phi_core.agents.reasoning as reasoning

    captured: dict = {}

    class StubJudge(reasoning.Judge):
        async def call_json(self, *a, **kw):
            captured.update(kw)
            return {"decisions": []}

    j = StubJudge(session_id="s", llm=None, db=FakeDb())
    asyncio.run(j.run(schema={"columns": []}, instrument={}, lexicon={}, statute={}))
    assert captured["min_items"] == 0


def test_schema_declares_deliverable_contract():
    """Schema is deterministic (Task 6): it never calls call_json, and its
    deliverable is exactly the dataset headers it parsed, one entry per
    header, tagged with the file that produced them."""
    import phi_core.agents.specialists as specialists

    called = {"call_json": False}

    class StubSchema(specialists.Schema):
        async def call_json(self, *a, **kw):
            called["call_json"] = True
            return {"columns": []}

    s = StubSchema(session_id="s", llm=None, db=FakeDb())
    dataset_files = [{"file_id": "f1", "original_name": "d.csv",
                      "columns": ["a", "b", "c"]}]
    result = asyncio.run(s.run(dataset_files=dataset_files))

    assert called["call_json"] is False
    assert [c["name"] for c in result["columns"]] == ["a", "b", "c"]
    assert all(c["_file_id"] == "f1" for c in result["columns"])


# ---- lateness ------------------------------------------------------------


def test_lateness_recorded_without_retry(monkeypatch):
    m = Manager(session_id="s", llm=None, db=FakeDb())
    monkeypatch.setattr(m, "BUDGET_S", {"Judge": -1.0})

    async def instant_success(system_prompt, extended):
        return "ok"

    reply, ok, err = asyncio.run(m.run_supervised(
        agent_name="Judge", phase="judge.decide", base_system_prompt="SYS",
        primary_attempt=instant_success))

    assert (reply, ok, err) == ("ok", True, None)
    report = asyncio.run(m.close_run("complete"))
    assert report["late_call_count"] == 1


# ---- adaptation within a run ---------------------------------------------


def test_adaptation_reuses_coaching_note_within_run():
    m = Manager(session_id="s", llm=None, db=FakeDb())
    _no_sleep(m)

    payloads: list[dict] = []

    async def fake_decide(*, task, legal, default_action, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return ManagerDecision(action="retry", note="be strict")
        return ManagerDecision(action="retry", note=None)
    m._decide = fake_decide

    calls1: list[str] = []

    async def agent1(system_prompt, extended):
        calls1.append(system_prompt)
        if len(calls1) == 1:
            raise asyncio.TimeoutError()
        return "ok1"

    asyncio.run(m.run_supervised(agent_name="Judge", phase="p1",
                                 base_system_prompt="SYS", primary_attempt=agent1))

    calls2: list[str] = []

    async def agent2(system_prompt, extended):
        calls2.append(system_prompt)
        if len(calls2) == 1:
            raise asyncio.TimeoutError()
        return "ok2"

    asyncio.run(m.run_supervised(agent_name="Sentinel", phase="p2",
                                 base_system_prompt="SYS", primary_attempt=agent2))

    assert payloads[1]["note_that_worked_earlier"] == "be strict"
    assert "[Manager operational note] be strict" in calls2[1]


# ---- consult ---------------------------------------------------------------


def test_consult_fails_open_on_manager_error():
    m = Manager(session_id="s", llm=None, db=FakeDb())

    async def raising_call_json(*a, **kw):
        raise RuntimeError("boom")
    m.call_json = raising_call_json

    advice = asyncio.run(m.consult(agent_name="Judge", phase="p",
                                   signal={"iteration": 1}))
    assert advice.action == "continue"


# ---- escalate_to_human_review ----------------------------------------------


def test_escalate_to_human_review_persists_and_returns_documented_keys():
    db = FakeDb()
    m = Manager(session_id="s", llm=None, db=db)
    closed: list[bool] = []

    async def close_last_phase():
        closed.append(True)

    result = asyncio.run(m.escalate_to_human_review(
        session_filter={"id": "s"}, reasons=["judge_call_failure"],
        close_last_phase=close_last_phase, phase_timings={"a": 1},
        run_elapsed_s=12.3, approved_decisions=[{"action": "keep"}],
        sentinel_report={"verdict": "approved"}))

    assert closed == [True]
    assert db.sessions.updates
    filt, update_doc = db.sessions.updates[-1]
    assert filt == {"id": "s"}
    assert update_doc["$set"]["status"] == "awaiting_human_review"
    assert update_doc["$set"]["human_review_reasons"] == ["judge_call_failure"]
    assert "manager_report" in update_doc["$set"]
    assert set(result) == {"status", "decisions", "sentinel", "phase_timings", "manager_report"}
    assert result["status"] == "awaiting_human_review"
    assert result["decisions"] == [{"action": "keep"}]


# ---- unsupervised parity ----------------------------------------------------


def test_unsupervised_call_matches_prior_behavior(monkeypatch):
    import phi_core.agents.base as base

    class MiniAgent(base.Agent):
        NAME = "Mini"
        PROMPT = "sys"

    calls: list[int] = []

    def raising_call_llm(system, user, llm):
        calls.append(1)
        raise asyncio.TimeoutError()
    monkeypatch.setattr(base, "call_llm", raising_call_llm)

    a = MiniAgent(session_id="s", llm=None, db=FakeDb())
    reply = asyncio.run(a.call("hi", "phase1"))

    assert reply == ""
    assert a.call_failures == 1
    assert len(calls) == 1


def test_call_failures_counted_once_not_per_retry(monkeypatch):
    import phi_core.agents.base as base

    class MiniAgent(base.Agent):
        NAME = "Mini"
        PROMPT = "sys"

    def raising_call_llm(system, user, llm):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(base, "call_llm", raising_call_llm)

    db = FakeDb()
    m = Manager(session_id="s", llm=None, db=db)
    _no_sleep(m)

    async def fake_decide(*, task, legal, default_action, payload):
        return ManagerDecision(action="retry", note=None)
    m._decide = fake_decide

    a = MiniAgent(session_id="s", llm=None, db=db, manager=m)
    reply = asyncio.run(a.call("hi", "phase1"))

    assert reply == ""
    assert a.call_failures == 1


# ---- orchestrator delegation -----------------------------------------------


def test_orchestrator_delegates_escalation_to_manager(monkeypatch):
    from phi_core.agents import orchestrator

    SENTINEL_RESULT = {"marker": "fake-manager-escalation-result"}
    escalate_calls: list[dict] = []

    class FakeManager:
        def __init__(self, **_kwargs):
            pass

        async def run(self, *, roster, phase_plan):
            return {}

        async def note_phase(self, phase, elapsed_s):
            return None

        async def consult(self, *, agent_name, phase, signal):
            return ManagerAdvice(action="continue", note=None)

        async def escalate_to_human_review(self, **kwargs):
            escalate_calls.append(kwargs)
            return SENTINEL_RESULT

        async def close_run(self, outcome):
            return {}

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

    class FakeJudge:
        def __init__(self, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None
        async def run(self, **_kwargs):
            return {"decisions": [{"file_id": "f", "column": "c",
                                   "action": "keep", "reason": "r",
                                   "subject": "participant"}]}

    class FakeSentinel:
        def __init__(self, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"issues": [{"column": "c", "severity": "blocking",
                                "detail": "unresolved leak"}]}

    monkeypatch.setattr(orchestrator, "Manager", FakeManager)
    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)

    db = FakeDb()

    async def emit(_message):
        return None

    phase_events: list[tuple] = []

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    result = asyncio.run(orchestrator.run_pipeline(
        {"id": "s", "files": []}, db, object(), emit, on_phase))

    assert result is SENTINEL_RESULT
    assert len(escalate_calls) == 1
    assert "sentinel_blocking_after_cap" in escalate_calls[0]["reasons"]


# ---- constructor parity: Ledger / Herald accept `manager` ------------------


def test_ledger_and_herald_accept_manager_kwarg():
    from phi_core.agents.outward import Herald, Ledger

    ledger = Ledger(session_id="s", llm=None, db=None, emit=None, manager=None)
    herald = Herald(session_id="s", llm=None, db=None, emit=None, manager=None)

    assert ledger._common["manager"] is None
    assert herald._common["manager"] is None
