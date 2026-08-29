"""Tests for StudyKnowledgePackage assembly (Phase 5, docs section 28).

Proves the Lexicon, Schema, and Instrument outputs assemble into one versioned
``StudyKnowledgePackage`` record for each of the three required input
combinations, instead of being concatenated into one giant specialist-prose
prompt (the pattern section 28 forbids).
"""
from __future__ import annotations

from phi_core.agents.specialists import assemble_study_knowledge_package
from phi_core.control.records import StudyKnowledgePackage


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