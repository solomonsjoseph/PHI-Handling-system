"""Focused D11 contracts for the canonical decision gate (control/gates.py)."""
from __future__ import annotations

import pytest
from phi_core.control.gates import (
    GATE_NAMES,
    assert_exact_coverage,
    run_decision_gates,
)
from phi_core.control.records import WorkflowRun
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import make_ctx


def _decision(file_id: str, column: str, **overrides: object) -> dict[str, object]:
    base = {
        "file_id": file_id,
        "column": column,
        "action": "keep",
        "subject": "study",
        "phi_category": "NONE",
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


def _file(file_id: str, columns: list[str] | None, **overrides: object) -> dict[str, object]:
    base = {"file_id": file_id, "columns": columns, "stored_path": ""}
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_run_decision_gates_produces_one_gate_result_per_gate_in_the_fixed_order() -> None:
    ctx = make_ctx("Judge")
    decisions = [_decision("f1", "widget_count")]
    files = [_file("f1", ["widget_count"])]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert outcome.ok is True
    assert [g.gate for g in outcome.gate_results] == list(GATE_NAMES)
    assert all(g.gate_version == "gates/1" for g in outcome.gate_results)
    assert all(g.run_id == ctx.run_id and g.task_id == ctx.task_id for g in outcome.gate_results)
    assert all(g.subject == "initial" for g in outcome.gate_results)
    # A clean decision on a column no deterministic rule touches produces
    # zero overrides and a clean coverage pass.
    assert outcome.overrides == []
    assert outcome.rejections == []
    assert len(outcome.decisions) == 1
    assert outcome.decisions[0]["column"] == "widget_count"
    assert outcome.decisions[0]["action"] == "keep"
    coverage = outcome.gate_results[-1]
    assert coverage.gate == "assert_exact_coverage"
    assert coverage.status == "pass"


@pytest.mark.asyncio
async def test_apply_sentinel_escalations_is_not_applicable_without_a_sentinel_report() -> None:
    ctx = make_ctx("Judge")
    decisions = [_decision("f1", "widget_count")]
    files = [_file("f1", ["widget_count"])]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    escalation_gate = next(g for g in outcome.gate_results if g.gate == "apply_sentinel_escalations")
    assert escalation_gate.status == "not_applicable"


@pytest.mark.asyncio
async def test_apply_sentinel_escalations_only_runs_and_overrides_when_sentinel_report_given() -> None:
    ctx = make_ctx("Judge")
    decisions = [_decision("f1", "widget_count")]
    files = [_file("f1", ["widget_count"])]
    sentinel_report = {
        "issues": [
            {"file_id": "f1", "column": "widget_count", "severity": "escalate", "problem": "ambiguous"},
            {"file_id": "f1", "column": "other", "severity": "advisory", "problem": "ignored"},
        ]
    }

    outcome = await run_decision_gates(
        decisions=decisions, files=files, sentinel_report=sentinel_report, stage="initial", ctx=ctx
    )

    escalation_gate = next(g for g in outcome.gate_results if g.gate == "apply_sentinel_escalations")
    assert escalation_gate.status == "pass"
    assert [o["rule"] for o in outcome.overrides] == ["sentinel_escalation"]
    escalated = next(d for d in outcome.decisions if d["column"] == "widget_count")
    assert escalated["action"] == "human_review"


@pytest.mark.asyncio
async def test_aggregated_overrides_include_hard_rule_and_confidence_floor_records_in_order() -> None:
    ctx = make_ctx("Judge")
    decisions = [
        _decision("f1", "ssn", action="keep"),  # hard rule forces drop
        _decision("f1", "widget_count", confidence=0.10),  # below CONFIDENCE_FLOOR
    ]
    files = [_file("f1", ["ssn", "widget_count"])]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert outcome.ok is True
    rules = [o.get("rule") or o.get("citation") for o in outcome.overrides]
    assert any("164.514" in str(r) for r in rules)  # sentinel hard rule
    assert any(o.get("rule") == "confidence_floor" for o in outcome.overrides)
    ssn_decision = next(d for d in outcome.decisions if d["column"] == "ssn")
    assert ssn_decision["action"] == "drop"
    low_conf_decision = next(d for d in outcome.decisions if d["column"] == "widget_count")
    assert low_conf_decision["action"] == "human_review"


@pytest.mark.asyncio
async def test_verify_keep_decisions_demotion_surfaces_in_outcome_demotions() -> None:
    ctx = make_ctx("Judge")
    decisions = [_decision("f1", "widget_count", action="keep")]
    files = [_file("f1", ["widget_count"], stored_path="/no/such/file.csv")]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert len(outcome.demotions) == 1
    assert outcome.demotions[0]["detector"] == "unreadable"
    demoted = outcome.decisions[0]
    assert demoted["action"] == "human_review"
    assert demoted["reason"].startswith("Keep verification (unreadable):")


@pytest.mark.asyncio
async def test_unreadable_schema_file_yields_an_explicit_human_review_record() -> None:
    ctx = make_ctx("Judge")
    decisions: list[dict[str, object]] = []
    files = [_file("f1", None, unreadable_reason="zip entry is corrupt")]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert outcome.ok is True
    synth_gate = next(g for g in outcome.gate_results if g.gate == "synthesize_unreadable_schema")
    assert synth_gate.status == "pass"
    assert len(outcome.decisions) == 1
    record = outcome.decisions[0]
    assert record["file_id"] == "f1"
    assert record["column"] == ""
    assert record["action"] == "human_review"
    assert record["reason"] == "unreadable_schema: zip entry is corrupt"
    coverage = outcome.gate_results[-1]
    assert coverage.status == "pass"
    assert "unreadable_schema recorded for ['f1']" in coverage.detail


@pytest.mark.asyncio
async def test_unreadable_schema_synthesis_is_idempotent_when_record_already_present() -> None:
    ctx = make_ctx("Judge")
    decisions = [
        {
            "file_id": "f1",
            "column": "",
            "action": "human_review",
            "subject": "study",
            "phi_category": None,
            "confidence": 0.0,
            "reason": "unreadable_schema: already recorded upstream",
        }
    ]
    files = [_file("f1", None, unreadable_reason="zip entry is corrupt")]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert outcome.ok is True
    assert len(outcome.decisions) == 1


@pytest.mark.parametrize(
    "decisions,files,expected_problem",
    [
        pytest.param(
            [],
            [{"file_id": "f1", "columns": ["a"]}],
            "missing_decision",
            id="missing_decision",
        ),
        pytest.param(
            [_decision("f1", "a"), _decision("f1", "a")],
            [{"file_id": "f1", "columns": ["a"]}],
            "duplicate_decision",
            id="duplicate_decision",
        ),
        pytest.param(
            [_decision("f1", "a"), _decision("f1", "not_a_real_column")],
            [{"file_id": "f1", "columns": ["a"]}],
            "invented_decision",
            id="invented_decision",
        ),
        pytest.param(
            [_decision("f1", "a", action="not_a_real_action")],
            [{"file_id": "f1", "columns": ["a"]}],
            "invalid_action",
            id="invalid_action",
        ),
    ],
)
def test_assert_exact_coverage_fails_closed_on_missing_duplicate_invented_or_invalid_action(
    decisions: list[dict[str, object]], files: list[dict[str, object]], expected_problem: str
) -> None:
    status, detail = assert_exact_coverage(decisions, files)

    assert status == "fail"
    assert expected_problem in detail


def test_assert_exact_coverage_fails_when_unreadable_schema_record_is_missing() -> None:
    status, detail = assert_exact_coverage([], [{"file_id": "f1", "columns": None}])

    assert status == "fail"
    assert "missing_unreadable_schema_record" in detail


@pytest.mark.asyncio
async def test_malformed_decision_sets_block_the_outcome_end_to_end() -> None:
    ctx = make_ctx("Judge")
    decisions = [_decision("f1", "a"), _decision("f1", "a")]  # duplicate
    files = [_file("f1", ["a"])]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert outcome.ok is False
    assert outcome.gate_results[-1].status == "fail"


@pytest.mark.asyncio
async def test_run_decision_gates_owns_decision_version_via_store_cas_increment() -> None:
    store = MemoryControlStore()
    ctx = make_ctx("Judge", run_id="run-decision-version")
    await store.insert("workflow_runs", WorkflowRun(run_id=ctx.run_id, session_id=ctx.session_id))
    decisions = [_decision("f1", "widget_count")]
    files = [_file("f1", ["widget_count"])]

    first = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx, store=store)
    second = await run_decision_gates(decisions=decisions, files=files, stage="retry", ctx=ctx, store=store)

    assert first.decision_version == 1
    assert second.decision_version == 2
    assert all(f"decision_version={first.decision_version}" in g.detail for g in first.gate_results)
    assert all(f"decision_version={second.decision_version}" in g.detail for g in second.gate_results)
    stored = await store.get_one("workflow_runs", {"run_id": ctx.run_id})
    assert stored["decision_version"] == 2


@pytest.mark.asyncio
async def test_run_decision_gates_without_store_leaves_decision_version_at_zero() -> None:
    ctx = make_ctx("Judge")
    decisions = [_decision("f1", "widget_count")]
    files = [_file("f1", ["widget_count"])]

    outcome = await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx)

    assert outcome.decision_version == 0


@pytest.mark.asyncio
async def test_run_decision_gates_raises_when_store_given_but_run_never_opened() -> None:
    store = MemoryControlStore()
    ctx = make_ctx("Judge", run_id="run-never-opened")
    decisions = [_decision("f1", "widget_count")]
    files = [_file("f1", ["widget_count"])]

    with pytest.raises(RuntimeError):
        await run_decision_gates(decisions=decisions, files=files, stage="initial", ctx=ctx, store=store)


@pytest.mark.asyncio
async def test_inputs_digest_changes_with_the_decision_set_and_is_stable_for_identical_inputs() -> None:
    ctx = make_ctx("Judge")
    files = [_file("f1", ["widget_count"])]

    outcome_a = await run_decision_gates(
        decisions=[_decision("f1", "widget_count")], files=files, stage="initial", ctx=ctx
    )
    outcome_b = await run_decision_gates(
        decisions=[_decision("f1", "widget_count")], files=files, stage="initial", ctx=ctx
    )
    outcome_c = await run_decision_gates(
        decisions=[_decision("f1", "widget_count", confidence=0.5)], files=files, stage="initial", ctx=ctx
    )

    digest_a = outcome_a.gate_results[0].inputs_digest
    digest_b = outcome_b.gate_results[0].inputs_digest
    digest_c = outcome_c.gate_results[0].inputs_digest
    assert digest_a == digest_b
    assert digest_a != digest_c


def test_high_confidence_auditor_issue_blocks_completion() -> None:
    """Mandatory acceptance test (docs/assurance table, Phase 6 row): a
    verdict='issues' Auditor response with at least one recorded issue
    blocks even at self-reported confidence 0.99 -- confidence is
    telemetry (D12), it can never turn a genuine finding into a pass."""
    from phi_core.agents.reasoning import auditor_escalation_reason

    audit = {
        "verdict": "issues",
        "issues": [{"file": "dataset", "column": "mrn", "problem": "action disagreement"}],
        "confidence": 0.99,
    }
    assert auditor_escalation_reason(audit) == "auditor_issues_verdict"
