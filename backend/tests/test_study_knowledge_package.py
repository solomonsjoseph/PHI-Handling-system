"""Tests for StudyKnowledgePackage assembly (Phase 5, docs section 28).

Proves the Lexicon, Schema, and Instrument outputs assemble into one versioned
``StudyKnowledgePackage`` record for each of the three required input
combinations, instead of being concatenated into one giant specialist-prose
prompt (the pattern section 28 forbids).

The last test in this file (Phase 5/6 orchestrator follow-up item 3)
proves the assembled package genuinely reaches ``Judge.run``'s actual
call site in the live ``orchestrator.run_pipeline`` dispatch path --
not merely that ``assemble_study_knowledge_package`` exists and works
in isolation, which the tests above this line already established.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.llm import LlmConfig
from phi_core.agents.specialists import assemble_study_knowledge_package
from phi_core.control.records import StudyKnowledgePackage
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run


def _schema_out() -> dict:
    return {
        "columns": [
            {"name": "mrn", "_file_id": "ds1"},
            {"name": "age", "_file_id": "ds1"},
            {"name": "treatment_facility_name", "_file_id": "ds1"},
        ],
    }


def _lexicon_out() -> dict:
    return {
        "columns": [
            {"name": "mrn", "description": "medical record number",
             "phi_flag_hint": True, "clinical_utility": "low", "notes": ""},
            {"name": "age", "description": "age in years",
             "phi_flag_hint": False, "clinical_utility": "medium", "notes": ""},
        ],
        "notes": "",
    }


def _instrument_out() -> dict:
    return {
        "fields": [
            {"label": "Participant name", "collected_variable": None},
            {"label": "Date of birth", "collected_variable": "dob"},
        ],
    }


def test_assemble_dataset_and_dictionary_only():
    pkg = assemble_study_knowledge_package(
        run_id="run-1", datasets=["ds1"],
        schema=_schema_out(), lexicon=_lexicon_out(),
    )
    assert isinstance(pkg, StudyKnowledgePackage)
    assert pkg.run_id == "run-1"
    assert pkg.datasets == ["ds1"]
    assert [f["name"] for f in pkg.schema_findings] == [
        "mrn", "age", "treatment_facility_name"]
    assert [f["name"] for f in pkg.lexicon_findings] == ["mrn", "age"]
    assert pkg.instrument_findings == []
    assert pkg.columns == ["mrn", "age", "treatment_facility_name"]


def test_assemble_dataset_and_forms_only():
    pkg = assemble_study_knowledge_package(
        run_id="run-2", datasets=["ds1"],
        schema=_schema_out(), instrument=_instrument_out(),
    )
    assert pkg.lexicon_findings == []
    assert [f["label"] for f in pkg.instrument_findings] == [
        "Participant name", "Date of birth"]


def test_assemble_dataset_and_dictionary_and_forms():
    pkg = assemble_study_knowledge_package(
        run_id="run-3", datasets=["ds1"],
        schema=_schema_out(), lexicon=_lexicon_out(), instrument=_instrument_out(),
    )
    assert len(pkg.schema_findings) == 3
    assert len(pkg.lexicon_findings) == 2
    assert len(pkg.instrument_findings) == 2
    assert pkg.evidence_refs == []
    assert pkg.conflicts == []
    assert pkg.unresolved_items == []


def test_every_assembly_mints_a_fresh_package_id():
    first = assemble_study_knowledge_package(
        run_id="run-4", datasets=["ds1"], schema=_schema_out())
    second = assemble_study_knowledge_package(
        run_id="run-4", datasets=["ds1"], schema=_schema_out())
    assert first.package_id
    assert first.package_id != second.package_id
    assert first.superseded_by == ""
    assert second.superseded_by == ""


def test_package_supports_supersede_chain_versioning():
    older = assemble_study_knowledge_package(
        run_id="run-5", datasets=["ds1"], schema=_schema_out())
    newer = assemble_study_knowledge_package(
        run_id="run-5", datasets=["ds1"], schema=_schema_out())
    older.superseded_by = newer.package_id
    assert older.superseded_by == newer.package_id
    assert newer.superseded_by == ""


# ---- wiring: the package must reach Judge's actual call -------------------


def test_assembled_package_reaches_judges_actual_call(monkeypatch):
    """Drives the live ``orchestrator.run_pipeline`` dispatch path with a
    spy Judge capturing its own ``run()`` kwargs. If
    ``_dispatch_specialists`` stopped assembling the package, or
    ``_dispatch_decide`` stopped passing it to ``Judge.run``, this test
    would fail: the kwarg would be missing or ``None``, or its fields
    would not match what ``assemble_study_knowledge_package`` actually
    produces from Lexicon/Schema/Instrument's real outputs."""
    from phi_core.agents import orchestrator

    judge_calls: list[dict] = []

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.scrub_count = 0

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": [
                {"name": "mrn", "description": "medical record number"},
            ]})

    class FakeSchema:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": [{"name": "mrn"}]})

    class FakeInstrument:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **kwargs):
            judge_calls.append(kwargs)
            return await complete_fake_task(self.ctx, {"decisions": [{
                "file_id": "f1", "column": "mrn", "action": "drop",
                "phi_category": "A", "confidence": 0.9, "reason": "mrn is a direct identifier",
                "subject": "participant", "citation": "45 CFR 164.514(b)(2)(i)(A)",
            }]})

    class FakeSentinel:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"issues": [{
                "file_id": "f1", "column": "mrn",
                "severity": "blocking", "problem": "policy review needed",
            }]})

    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
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

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {"id": "session", "files": [
                {"kind": "dataset", "file_id": "f1", "columns": ["mrn"]},
                {"kind": "metadata", "file_id": "f2"},
            ]},
            FakeDb(), LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )

    result = asyncio.run(_go())

    assert result.get("status") == "awaiting_human_review", result
    assert judge_calls, "Judge never ran -- the pipeline must have failed before reaching decide"
    package = judge_calls[0].get("study_knowledge_package")
    assert package is not None, (
        "Judge.run was never called with study_knowledge_package -- "
        "assemble_study_knowledge_package's output is not wired into the live dispatch path"
    )
    assert isinstance(package, StudyKnowledgePackage)
    assert package.run_id == "session"
    assert package.datasets == ["f1"]
    assert package.columns == ["mrn"]
    assert package.schema_findings == [{"name": "mrn"}]
    assert package.lexicon_findings == [{"name": "mrn", "description": "medical record number"}]
    assert package.instrument_findings == []