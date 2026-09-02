"""Deterministic-guardian contract for Schema (Task 6, inverted by Task 10):
headers and cardinality still gate through the exact same deterministic
header-safety machinery, but Schema itself is now a code-writing agent
(step 10) -- it never reads a dataset file in-process, only through
generated code run inside ``agents/codegen.py``'s sandboxed/containerized
boundary.

These tests never touch Docker or a live LLM: ``StubExtractionSchema``
overrides ``Schema._extract_via_codegen`` -- the one seam that does --
with a canned ``ExtractedSchema`` built from a real on-disk file via the
same plain csv/xlsx readers the pre-step-10 deterministic Schema used to
call directly. Everything downstream of that call (``Schema.run``'s
header-safety gate, opaque projection, uncertain-ceiling raise,
cardinality suppression, ``verify()``) is the real, unstubbed
production code -- exactly what these tests pin.

Follows the dependency-free convention of test_manager.py and
test_keep_verification_pipeline.py: plain ``def test_...()`` driving
coroutines with ``asyncio.run(...)``, no live LLM key, no Mongo.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from phi_core.agents.codegen import CodeGenerationExhausted
from phi_core.agents.extract_model import ExtractedColumn, ExtractedSchema
from phi_core.agents.specialists import Schema
from phi_core.control.testing import make_ctx
from phi_core.file_readers import read_csv_columns, read_xlsx_columns


class StubExtractionSchema(Schema):
    """Overrides only ``_extract_via_codegen``, so ``call``/``call_json``
    are never even reachable through the normal ``run()`` path -- proven
    below by making them raise, matching this test file's original
    "Schema must never call an LLM through this seam" intent, just
    scoped to the correct seam now that Schema legitimately calls one
    through ``generate_with_retry`` elsewhere."""

    async def call(self, *a, **kw):
        raise AssertionError("StubExtractionSchema must never reach an LLM directly -- override _extract_via_codegen instead")

    async def call_json(self, *a, **kw):
        raise AssertionError("StubExtractionSchema must never reach an LLM directly -- override _extract_via_codegen instead")

    async def _extract_via_codegen(self, f: dict[str, Any], sandbox: Any) -> ExtractedSchema | None:
        headers = f.get("columns")
        if headers is None:
            path = Path(f["stored_path"])
            ext = self._dataset_ext(f)
            if ext == "xlsx":
                headers, _rows = read_xlsx_columns(path)
            else:
                headers, _rows = read_csv_columns(path)
        if not headers:
            return None
        columns = [
            ExtractedColumn(name=h, position=i, distinct_count=2, null_count=0, inferred_type="string")
            for i, h in enumerate(headers)
        ]
        return ExtractedSchema(columns=columns, row_count=10)


def _schema() -> StubExtractionSchema:
    return StubExtractionSchema(make_ctx("Schema"))


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
    assert result["header_notice"] == ""


def test_schema_reads_a_real_file_via_the_extraction_seam_when_columns_absent(tmp_path):
    """Schema never uses a pre-populated ``columns`` field in production
    (step 10 deletes that fallback entirely -- the code-writing agent is
    the only extraction path); this test's stub simulates a successful
    generated-code round reading the real file directly, proving the
    rest of ``run()`` does not itself depend on ``columns`` being
    present."""
    path = tmp_path / "dataset.csv"
    headers = ["mrn", "visit_date"]
    _write_csv(path, headers, [["1", "2024-01-01"], ["2", "2024-01-02"]])

    schema = _schema()
    dataset_files = [{
        "file_id": "f1",
        "original_name": path.name,
        "stored_path": str(path),
        "subtype": "csv",
        # No "columns" key -- forces the stub's file_readers fallback.
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
    error_rows = [r for r in schema.ctx.trace.legacy_messages if r.phase == "schema.error:f1"]
    assert len(error_rows) == 1
    assert error_rows[0].payload["error"] == "no headers provided"


def test_schema_logs_no_headers_when_code_generation_is_exhausted():
    """A real (unstubbed-at-this-seam) codegen exhaustion must degrade
    exactly like an empty extraction -- same "no headers provided"
    message the pre-step-10 fail-loud path always used -- with the
    exhaustion reason recorded alongside it for diagnosability."""

    class ExhaustingSchema(Schema):
        async def _extract_via_codegen(self, f, sandbox):
            raise CodeGenerationExhausted("exhausted", diagnostics=["empty_reply: the model returned no source"])

    schema = ExhaustingSchema(make_ctx("Schema"))
    dataset_files = [{"file_id": "f1", "stored_path": "/nonexistent.csv", "subtype": "csv"}]

    result = asyncio.run(schema.run(dataset_files=dataset_files))

    assert result["columns"] == []
    error_rows = [r for r in schema.ctx.trace.legacy_messages if r.phase == "schema.error:f1"]
    assert len(error_rows) == 1
    assert error_rows[0].payload["error"] == "no headers provided"
    assert error_rows[0].payload["reason"] == "code_generation_exhausted"


def test_schema_logs_no_headers_when_the_extraction_artifact_is_invalid():
    """An artifact that runs but fails ExtractedSchema's strict contract
    (a duplicate name, here) must fail this file closed, not raise past
    run() -- ``_extract_via_codegen`` propagates the ValueError/pydantic
    ValidationError, and ``run()`` converts it to the same structured
    error row a codegen exhaustion produces."""

    class InvalidArtifactSchema(Schema):
        async def _extract_via_codegen(self, f, sandbox):
            from phi_core.agents.extract_model import ExtractedSchema

            return ExtractedSchema.model_validate({
                "columns": [
                    {"name": "dup", "position": 0, "distinct_count": 1, "null_count": 0, "inferred_type": "string"},
                    {"name": "dup", "position": 1, "distinct_count": 1, "null_count": 0, "inferred_type": "string"},
                ],
                "row_count": 5,
            })

    schema = InvalidArtifactSchema(make_ctx("Schema"))
    dataset_files = [{"file_id": "f1", "stored_path": "/nonexistent.csv", "subtype": "csv"}]

    result = asyncio.run(schema.run(dataset_files=dataset_files))

    assert result["columns"] == []
    error_rows = [r for r in schema.ctx.trace.legacy_messages if r.phase == "schema.error:f1"]
    assert len(error_rows) == 1
    assert error_rows[0].payload["reason"] == "invalid_artifact"


def test_schema_reports_a_header_notice_when_headers_were_tokenised(tmp_path):
    """Step 10's ``header_notice`` field: a plain-English sentence naming
    how many headers were tokenised and why, empty when none were."""
    path = tmp_path / "dataset.csv"
    headers = ["patient_id", "123-45-6789", "age"]
    _write_csv(path, headers, [["1", "x", "40"]])

    schema = _schema()
    dataset_files = [{"file_id": "f1", "stored_path": str(path), "subtype": "csv"}]

    result = asyncio.run(schema.run(dataset_files=dataset_files))

    assert result["header_notice"] != ""
    assert "1 of 3" in result["header_notice"]


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

    j = StubJudge(make_ctx("Judge"))
    schema = {"columns": [{"name": "a", "_file_id": "f1"},
                          {"name": "b", "_file_id": "f1"}]}
    asyncio.run(j.run(schema=schema, instrument={}, lexicon={}, statute={}))

    assert captured["expect_key"] == "decisions"
    assert captured["min_items"] == 2


def test_specialists_never_import_the_raw_row_reader():
    """Section 24/25/26 'no raw row access': the specialists module must
    never reach for ``file_readers.iter_dataset_rows``, the only reader
    that yields raw dataset cell values. Schema works from headers plus
    integer cardinality stats, Lexicon from dictionary rows, Instrument
    from form text -- none exposes a dataset cell to an LLM."""
    import ast

    import phi_core.agents.specialists as specialists

    src = Path(specialists.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    assert "iter_dataset_rows" not in names
