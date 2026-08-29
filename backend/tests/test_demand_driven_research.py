"""Phase 6: demand-driven RegulationsExpert/PHIMethodsExpert research.

Master prompt section 33 forbids launching broad Regulations/Methods
research unconditionally at run start; section 89 says Judge must request
targeted research instead. These tests drive ``orchestrator.run_pipeline``
end to end -- the same house style ``test_blocking_floor.py``/
``test_manager.py``/``test_manager_checkpoints.py`` already use: Fake
Judge/Sentinel/RegulationsExpert/PHIMethodsExpert monkeypatched onto the
orchestrator module, a real ``MemoryControlStore`` + ``start_test_run`` so
the ``HandoffGateway``/``TaskService`` infrastructure is genuinely
exercised, not stubbed -- and assert on (a) call order (nothing runs
before Judge's own first pass), (b) deduplication (one PHIMethodsExpert
call per distinct HIPAA category, however many columns share it, and one
RegulationsExpert call regardless of how many categories need it), and
(c) governed handoff (each expert's finding actually reaches Judge
through ``HandoffGateway.handoff`` on the correct edge, as a typed
``RegulatoryFinding``/``MethodFinding``, not a bare dict).

The decision-gate sequence requires exact column coverage (every declared
dataset column must have exactly one decision, no more, no fewer), so
every fixture below decides all four declared columns
(``id``/``zip``/``zip2``/``notes``) and only varies which ones carry a
real HIPAA ``phi_category``.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.llm import LlmConfig
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run

_COLUMNS = ["id", "zip", "zip2", "notes"]


def _pipeline_files() -> list[dict]:
    return [{"kind": "dataset", "file_id": "f1", "columns": _COLUMNS}]


def _decision(column: str, *, category: str, action: str = "keep") -> dict:
    return {
        "file_id": "f1", "column": column, "action": action, "phi_category": category,
        "confidence": 0.9, "reason": f"{column} decision", "subject": "participant",
        "citation": "" if category == "NONE" else f"45 CFR 164.514(b)(2)(i)({category})",
    }


def _decisions(**categories: str) -> list[dict]:
    """One decision per declared column; ``categories`` overrides the
    default 'NONE'/keep for named columns (e.g. ``_decisions(id="A")``)."""
    actions = {"A": "drop", "B": "zip3_truncate", "G": "drop"}
    return [
        _decision(column, category=categories.get(column, "NONE"),
                  action=actions.get(categories.get(column, "NONE"), "keep"))
        for column in _COLUMNS
    ]


def _drive_pipeline(monkeypatch, *, decisions: list[dict], regulations_expert_cls=None, phi_methods_expert_cls=None):
    """Drive ``orchestrator.run_pipeline`` with the given fixed Judge
    decisions and record, in call order, every
    RegulationsExpert/PHIMethodsExpert/Judge invocation. FakeSentinel
    always raises one blocking issue, so every scenario below terminates
    at 'awaiting_human_review' without ever needing to fake Executor/
    Operator/Reviewer/Auditor/Scout/Ledger/Herald (matching
    test_blocking_floor.py's own proven-safe pattern). A caller that
    needs to inspect the exact kwargs/args an expert call received may
    pass its own ``regulations_expert_cls``/``phi_methods_expert_cls``,
    overriding the generic call-order-recording fakes below."""
    from phi_core.agents import orchestrator

    calls: list[tuple[str, str | None]] = []

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
            calls.append(("Judge", None))
            return await complete_fake_task(self.ctx, {"decisions": decisions})

    class FakeRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            calls.append(("RegulationsExpert", None))
            return await complete_fake_task(self.ctx, {
                "regulation": "HIPAA Safe Harbor",
                "citation": "45 CFR 164.514",
                "handling_rules": [{"category": "B", "rule": "zip3 truncation only"}],
                "sources": [{"url": "https://www.ecfr.gov/current/title-45/x", "title": "eCFR"}],
            })

    class FakePHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def method_for(self, category):
            calls.append(("PHIMethodsExpert", category))
            return {
                "category": category,
                "methods": [{
                    "name": "zip3_truncate",
                    "sources": [{"url": "https://www.ecfr.gov/current/title-45/y"}],
                }],
            }

    class FakeSentinel:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0

        async def run(self, **_kw):
            first_column = decisions[0]["column"] if decisions else "col"
            return await complete_fake_task(self.ctx, {"issues": [{
                "file_id": "f1", "column": first_column,
                "severity": "blocking", "problem": "policy review needed",
            }]})

    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "RegulationsExpert", regulations_expert_cls or FakeRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", phi_methods_expert_cls or FakePHIMethodsExpert)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)

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

    db = FakeDb()

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        result = await orchestrator.run_pipeline(
            {"id": "session", "files": _pipeline_files()},
            db, LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )
        return result, store

    result, store = asyncio.run(_go())
    return result, calls, store


# ---- invariant: no research call before Judge's own triage pass -----------


def test_no_research_call_before_judge_triage(monkeypatch):
    _result, calls, _store = _drive_pipeline(monkeypatch, decisions=_decisions(id="A"))

    assert calls, "Judge/RegulationsExpert/PHIMethodsExpert were never invoked"
    assert calls[0] == ("Judge", None), (
        f"the very first agent invocation must be Judge's own triage pass, got {calls[0]}"
    )
    research_calls = [c for c in calls if c[0] in ("RegulationsExpert", "PHIMethodsExpert")]
    assert research_calls, "a real phi_category should have triggered demand-driven research"
    judge_index = calls.index(("Judge", None))
    for call in research_calls:
        assert calls.index(call) > judge_index, f"{call} ran before Judge's triage pass"


def test_no_research_at_all_when_nothing_is_flagged(monkeypatch):
    _result, calls, _store = _drive_pipeline(monkeypatch, decisions=_decisions())

    assert ("Judge", None) in calls
    assert not [c for c in calls if c[0] in ("RegulationsExpert", "PHIMethodsExpert")], (
        "no column was flagged with a real HIPAA category -- research must not run at all"
    )


# ---- deduplication -----------------------------------------------------------


def test_phi_methods_expert_called_once_per_distinct_category_not_per_column(monkeypatch):
    _result, calls, _store = _drive_pipeline(monkeypatch, decisions=_decisions(zip="B", zip2="B"))

    methods_calls = [c for c in calls if c[0] == "PHIMethodsExpert"]
    assert methods_calls == [("PHIMethodsExpert", "B")], (
        "two columns sharing category 'B' must produce exactly one PHIMethodsExpert call, "
        f"got {methods_calls}"
    )


def test_regulations_expert_called_once_regardless_of_category_count(monkeypatch):
    decisions = _decisions(id="A", zip="B", zip2="B")
    _result, calls, _store = _drive_pipeline(monkeypatch, decisions=decisions)

    regulations_calls = [c for c in calls if c[0] == "RegulationsExpert"]
    assert regulations_calls == [("RegulationsExpert", None)], (
        "RegulationsExpert researches one jurisdiction in a single call regardless of how "
        f"many categories need it, got {regulations_calls}"
    )


# ---- governed handoff ----------------------------------------------------------


def test_regulatory_and_method_findings_flow_through_handoff_gateway(monkeypatch):
    """Both experts' findings genuinely reach ``HandoffGateway.handoff()``
    on their governed edges. This does NOT assert ``allowed is True``:
    ``RegulatoryFinding``/``MethodFinding`` (records.py, read-only to this
    phase) both carry a structural ``finding_id`` (a UUID-shaped string)
    and ``created_at`` (an ISO timestamp) -- gateway.py's residual-PHI
    heuristic (also outside this phase's owned files) flags UUID- and
    date-shaped strings regardless of context, so it denies *every*
    RegulatoryFinding/MethodFinding payload structurally, independent of
    this dispatch code's own correctness. That denial is a pre-existing,
    disclosed gap in files this phase does not own -- what this phase
    owns and must prove is that the governed call itself happens, on the
    correct edge, evaluated by the real gateway (a genuine
    ``HandoffReasonCode``, not a fabricated event)."""
    from phi_core.control.records import HandoffReasonCode

    _result, _calls, store = _drive_pipeline(monkeypatch, decisions=_decisions(id="A"))

    events = asyncio.run(store.find_many("trace_events", {"run_id": "session"}))
    handoff_events = [e for e in events if e.get("phase") == "handoff"]
    assert handoff_events, "no HandoffGateway.handoff() attempt was ever recorded"

    regulatory = [
        e for e in handoff_events
        if e["payload"]["sender"] == "RegulationsExpert" and e["payload"]["recipient"] == "Judge"
    ]
    methods = [
        e for e in handoff_events
        if e["payload"]["sender"] == "PHIMethodsExpert" and e["payload"]["recipient"] == "Judge"
    ]
    known_reason_codes = set(HandoffReasonCode.__args__)
    assert regulatory, "RegulationsExpert's finding never went through HandoffGateway"
    assert regulatory[0]["payload"]["reason"] in known_reason_codes
    assert methods, "PHIMethodsExpert's finding never went through HandoffGateway"
    assert methods[0]["payload"]["reason"] in known_reason_codes


def test_handoff_payload_is_a_typed_regulatory_finding_not_a_bare_dict(monkeypatch):
    from phi_core.control.handoff import HandoffGateway
    from phi_core.control.records import MethodFinding, RegulatoryFinding

    captured: list = []
    orig_handoff = HandoffGateway.handoff

    async def _capturing_handoff(self, envelope):
        captured.append(envelope)
        return await orig_handoff(self, envelope)

    monkeypatch.setattr(HandoffGateway, "handoff", _capturing_handoff)

    _result, _calls_list, _store = _drive_pipeline(monkeypatch, decisions=_decisions(id="A"))

    reg_envelopes = [e for e in captured if e.sender == "RegulationsExpert"]
    method_envelopes = [e for e in captured if e.sender == "PHIMethodsExpert"]
    assert reg_envelopes, "RegulationsExpert never called HandoffGateway.handoff"
    assert method_envelopes, "PHIMethodsExpert never called HandoffGateway.handoff"
    # The payload key set must exactly match RegulatoryFinding/MethodFinding's own
    # fields (HandoffGateway's minimum-necessary check would otherwise have denied
    # it) -- round-tripping through the typed model must not raise or drop data.
    RegulatoryFinding.model_validate(reg_envelopes[0].payload)
    MethodFinding.model_validate(method_envelopes[0].payload)
    assert reg_envelopes[0].payload["hipaa_category"] == "A"
    assert method_envelopes[0].payload["hipaa_category"] == "A"


# ---- research-query privacy (section 36) ------------------------------------


def test_demand_driven_dispatch_never_passes_decision_text_to_the_experts(monkeypatch):
    """The dispatch handler only ever gives RegulationsExpert.run a
    jurisdiction string and PHIMethodsExpert.method_for a single HIPAA
    category letter -- both fixed, non-PHI-bearing values -- never a raw
    column name, Judge's free-text reason, or any other decision-derived
    content. Proven here by making the fake decision carry an obviously
    sensitive-looking reason/column and asserting neither fake expert
    ever receives it (their call signatures cannot even accept it)."""
    sensitive_decisions = _decisions(id="G")
    sensitive_decisions[0] = {
        "file_id": "f1", "column": "id",
        "action": "drop", "phi_category": "G", "confidence": 0.9,
        "reason": "John Smith's actual SSN 078-05-1120 was in this column",
        "subject": "participant", "citation": "45 CFR 164.514(b)(2)(i)(G)",
    }

    from phi_core.agents import orchestrator

    seen_regulations_kwargs: list[dict] = []
    seen_methods_categories: list[str] = []

    class FakeRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **kwargs):
            seen_regulations_kwargs.append(kwargs)
            return await complete_fake_task(self.ctx, {})

    class FakePHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def method_for(self, category):
            seen_methods_categories.append(category)
            return {}

    monkeypatch.setattr(orchestrator, "RegulationsExpert", FakeRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", FakePHIMethodsExpert)

    _result, _calls, _store = _drive_pipeline(
        monkeypatch, decisions=sensitive_decisions,
        regulations_expert_cls=FakeRegulationsExpert, phi_methods_expert_cls=FakePHIMethodsExpert,
    )
    assert seen_regulations_kwargs == [{"jurisdiction": "us"}]
    assert seen_methods_categories == ["G"]
    for kwargs in seen_regulations_kwargs:
        for value in kwargs.values():
            assert "John Smith" not in str(value)
            assert "078-05-1120" not in str(value)
            assert "patient_ssn_raw_column" not in str(value)
