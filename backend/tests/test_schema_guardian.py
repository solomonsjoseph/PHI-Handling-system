"""Deterministic-guardian contract for Schema (Task 6): headers and
cardinality only, no LLM call, no classification.

Follows the dependency-free convention of test_manager.py and
test_keep_verification_pipeline.py: plain ``def test_...()`` driving
coroutines with ``asyncio.run(...)``, no live LLM key, no Mongo.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import openpyxl
import pytest

from phi_core.agents.specialists import Schema


class FakeAgentLog:
    def __init__(self):
        self.rows: list[dict] = []

    async def insert_one(self, doc):
        self.rows.append(doc)


class FakeDb:
    def __init__(self):
        self.agent_log = FakeAgentLog()


class NoLlmSchema(Schema):
    """Raises if Schema ever reaches for an LLM -- proves the deterministic
    rewrite never calls out, not just that it happens not to in this test."""

    async def call(self, *a, **kw):
        raise AssertionError("Schema must never call an LLM")

    async def call_json(self, *a, **kw):
        raise AssertionError("Schema must never call an LLM")


def _schema() -> NoLlmSchema:
    return NoLlmSchema(session_id="s", llm=None, db=FakeDb())


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.write_text(
        ",".join(headers) + "\n" + "\n".join(",".join(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_xlsx(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    wb.save(path)
    wb.close()


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_schema_reports_exactly_the_headers_with_no_judgment_fields(tmp_path, ext):
    path = tmp_path / f"dataset.{ext}"
    headers = ["patient_id", "site", "age"]
    rows = [["1", "north", "40"], ["2", "south", "55"], ["3", "north", "40"]]
    if ext == "csv":
        _write_csv(path, headers, rows)
    else:
        _write_xlsx(path, headers, rows)

    schema = _schema()
    dataset_files = [{
        "file_id": "f1",
        "original_name": path.name,
        "stored_path": str(path),
        "subtype": ext,
        "columns": headers,
    }]

    result = asyncio.run(schema.run(dataset_files=dataset_files))

    assert [c["name"] for c in result["columns"]] == headers
    assert all(c["_file_id"] == "f1" for c in result["columns"])
    for c in result["columns"]:
        assert set(c) == {"name", "_file_id"}
        assert "candidate_phi_category" not in c
        assert "inferred_meaning" not in c
        assert "confidence" not in c


def test_schema_falls_back_to_file_readers_when_columns_unpopulated(tmp_path):
    """Intake normally pre-populates `columns`; when it hasn't (a resumed
    or otherwise incompletely-hydrated session), Schema still reads the
    real header row deterministically rather than failing loud."""
    path = tmp_path / "dataset.csv"
    headers = ["mrn", "visit_date"]
    _write_csv(path, headers, [["1", "2024-01-01"], ["2", "2024-01-02"]])

    schema = _schema()
    dataset_files = [{
        "file_id": "f1",
        "original_name": path.name,
        "stored_path": str(path),
        "subtype": "csv",
        # No "columns" key.
    }]

    result = asyncio.run(schema.run(dataset_files=dataset_files))

    assert [c["name"] for c in result["columns"]] == headers
    assert schema.verify("mrn") == {"present": True, "file_id": "f1"}


def test_schema_logs_and_skips_a_file_with_no_headers(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    schema = _schema()
    dataset_files = [{
        "file_id": "f1",
        "original_name": "empty.csv",
        "stored_path": str(path),
        "subtype": "csv",
    }]

    result = asyncio.run(schema.run(dataset_files=dataset_files))

    assert result["columns"] == []
    error_rows = [r for r in schema.db.agent_log.rows if r["phase"] == "schema.error:f1"]
    assert len(error_rows) == 1
    assert error_rows[0]["payload"]["error"] == "no headers provided"


def test_verify_present_and_absent_case_insensitive():
    schema = _schema()
    schema._headers = {"f1": ["patient_id", "site"]}

    assert schema.verify("Patient_ID") == {"present": True, "file_id": "f1"}
    assert schema.verify("Patient_ID", file_id="f1") == {"present": True, "file_id": "f1"}
    assert schema.verify("Patient_ID", file_id="f2") == {
        "present": False,
        "explanation": "not present in the dataset headers -- this is the final list, nothing else exists",
    }
    assert schema.verify("nonexistent") == {
        "present": False,
        "explanation": "not present in the dataset headers -- this is the final list, nothing else exists",
    }


def test_cardinality_returns_integers_only(tmp_path):
    path = tmp_path / "dataset.csv"
    headers = ["site", "outcome"]
    rows = [["north", "yes"], ["south", "no"], ["north", "no"], ["north", "yes"]]
    _write_csv(path, headers, rows)

    schema = _schema()
    dataset_files = [{
        "file_id": "f1",
        "original_name": "dataset.csv",
        "stored_path": str(path),
        "subtype": "csv",
        "columns": headers,
    }]
    asyncio.run(schema.run(dataset_files=dataset_files))

    stats = schema.cardinality("site", file_id="f1")
    assert stats == {"distinct": 2, "rows": 4}
    assert all(isinstance(v, int) for v in stats.values())
    assert schema.cardinality("SITE") == stats
    assert schema.cardinality("missing_column") == {}


def test_judge_never_structurally_required_schema_classification_fields():
    """A schema dict with no candidate_phi_category still satisfies Judge's
    deliverable contract -- proving Judge never depended on the removed
    LLM-classification fields."""
    import phi_core.agents.reasoning as reasoning

    captured: dict = {}

    class StubJudge(reasoning.Judge):
        async def call_json(self, *a, **kw):
            captured.update(kw)
            return {"decisions": []}

    j = StubJudge(session_id="s", llm=None, db=FakeDb())
    schema = {"columns": [{"name": "a", "_file_id": "f1"},
                          {"name": "b", "_file_id": "f1"}]}
    asyncio.run(j.run(schema=schema, instrument={}, lexicon={}, statute={}))

    assert captured["expect_key"] == "decisions"
    assert captured["min_items"] == 2
