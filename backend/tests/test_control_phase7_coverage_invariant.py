"""Phase 7: exact-coverage invariant over duplicate column names.

Judge's classification identity is ``(file_id, column_id)``, not the bare
column name, because column names are not globally unique across files
(docs section 31). This proves the invariant end to end through the real
D11 gate sequence (``run_decision_gates``): a multi-file fixture where two
files each carry a column named ``notes`` must still receive exactly one
decision per *logical* column -- four decisions total, not two collapsed
by name -- with 100 percent coverage.
"""
from __future__ import annotations

import pytest
from phi_core.control.gates import assert_exact_coverage, run_decision_gates
from phi_core.control.records import WorkflowRun
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import make_ctx


def _decision(file_id: str, column: str, **overrides: object) -> dict[str, object]:
    base = {
        "file_id": file_id, "column": column, "action": "keep",
        "subject": "study", "phi_category": "NONE", "confidence": 0.95,
    }
    base.update(overrides)
    return base


def _file(file_id: str, columns: list[str]) -> dict[str, object]:
    return {"file_id": file_id, "columns": columns, "stored_path": ""}


# Two files, each carrying a column literally named "notes" -- the same
# name, two different logical columns, per docs section 31.
_FILES = [
    _file("f1", ["patient_id", "notes"]),
    _file("f2", ["site_id", "notes"]),
]
_DECISIONS = [
    _decision("f1", "patient_id", action="drop", phi_category="A"),
    _decision("f1", "notes", action="scrub_text"),
    _decision("f2", "site_id", action="drop", phi_category="R"),
    _decision("f2", "notes", action="scrub_text"),
]


def test_assert_exact_coverage_treats_same_named_columns_in_different_files_as_distinct() -> None:
    status, detail = assert_exact_coverage(_DECISIONS, _FILES)

    assert status == "pass", detail
    assert "exact coverage over 4 column(s) across 2 file(s)" in detail

    # Prove the fixture genuinely exercises the collision: two distinct
    # logical columns share the bare name "notes", and coverage still
    # requires one decision for each of the two (file_id, column) pairs,
    # not one decision deduplicated by name.
    names = [d["column"] for d in _DECISIONS]
    assert names.count("notes") == 2
    identities = {(d["file_id"], d["column"]) for d in _DECISIONS}
    assert len(identities) == 4


@pytest.mark.asyncio
async def test_run_decision_gates_gives_every_logical_column_exactly_one_decision() -> None:
    store = MemoryControlStore()
    ctx = make_ctx("Judge", run_id="run-coverage-invariant")
    await store.insert("workflow_runs", WorkflowRun(run_id=ctx.run_id, session_id=ctx.session_id))

    outcome = await run_decision_gates(
        decisions=list(_DECISIONS), files=_FILES, stage="initial", ctx=ctx, store=store,
    )

    assert outcome.ok is True
    coverage_gate = next(g for g in outcome.gate_results if g.gate == "assert_exact_coverage")
    assert coverage_gate.status == "pass"
    assert "exact coverage over 4 column(s) across 2 file(s)" in coverage_gate.detail

    identities = [(d["file_id"], d["column"]) for d in outcome.decisions if d.get("column")]
    assert len(identities) == len(set(identities)) == 4
    assert set(identities) == {
        ("f1", "patient_id"), ("f1", "notes"), ("f2", "site_id"), ("f2", "notes"),
    }


@pytest.mark.asyncio
async def test_run_decision_gates_fails_closed_when_one_of_two_same_named_columns_is_missing() -> None:
    """Negative control: dropping only the ``f2``/``notes`` decision (its
    twin ``f1``/``notes`` decision still present) must still fail
    coverage -- proving the check is keyed on the full ``(file_id,
    column)`` identity, not merely "a decision named notes exists"."""
    store = MemoryControlStore()
    ctx = make_ctx("Judge", run_id="run-coverage-invariant-missing")
    await store.insert("workflow_runs", WorkflowRun(run_id=ctx.run_id, session_id=ctx.session_id))

    incomplete = [d for d in _DECISIONS if (d["file_id"], d["column"]) != ("f2", "notes")]
    outcome = await run_decision_gates(
        decisions=incomplete, files=_FILES, stage="initial", ctx=ctx, store=store,
    )

    assert outcome.ok is False
    coverage_gate = next(g for g in outcome.gate_results if g.gate == "assert_exact_coverage")
    assert coverage_gate.status == "fail"
    assert "missing_decision:[('f2', 'notes')]" in coverage_gate.detail
