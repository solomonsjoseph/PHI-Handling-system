"""Tests for Reviewer Final (Phase 10, docs #55): the real completeness/
authorization/privacy/utility gate over an already-executed,
already-verified run. Distinct from ``test_reviewer.py``, which covers
the coverage-audit mode (``Reviewer.run``); every test here calls
``Reviewer.finalize`` directly.
"""
from __future__ import annotations

import asyncio

from phi_core.agents.reviewer import Reviewer
from phi_core.control.records import ExecutionResult, HumanDecision, VerificationResult, VerifiedClassificationManifest
from phi_core.control.testing import make_ctx


def _manifest(**overrides) -> VerifiedClassificationManifest:
    base = dict(run_id="r1", preview_review_id="pr1", decision_refs=["f1:id", "f1:ssn"])
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def _execution_result(**overrides) -> ExecutionResult:
    base = dict(task_id="t1", run_id="r1", manifest_id="m1", success=True)
    base.update(overrides)
    return ExecutionResult(**base)


def _verification_result(**overrides) -> VerificationResult:
    base = dict(run_id="r1", passed=True, failed_checks=[])
    base.update(overrides)
    return VerificationResult(**base)


def _decisions() -> list[dict]:
    return [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]


def _safe_output_metadata() -> dict:
    return {
        "column_counts": {"f1": {"decisions": 2, "verdicts": 2}},
        "schema_valid": {"f1": True},
    }


def _finalize(reviewer: Reviewer, **kwargs) -> dict:
    defaults = dict(
        manifest=_manifest(), execution_result=_execution_result(),
        verification_result=_verification_result(), decisions=_decisions(),
        human_decisions=[], safe_output_metadata=_safe_output_metadata(),
    )
    defaults.update(kwargs)
    return asyncio.run(reviewer.finalize(**defaults))


def test_clean_run_passes_every_check():
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(reviewer)

    assert result["verdict"] == "PASS"
    assert result["signal"] is None
    assert result["findings"] == []
    assert all(c["pass"] for c in result["checks"])
    assert {c["name"] for c in result["checks"]} == {
        "every_approved_action_executed", "nothing_omitted", "nothing_unauthorized",
        "human_decisions_followed", "deterministic_verification_passed",
        "privacy_intent_preserved", "utility_requirement_respected", "no_unresolved_issue",
    }


def test_missing_execution_fails_and_signals_execution_error():
    """A manifest-authorized column that never reached execution (and was
    not deferred to human review) is an execution-side failure."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(
        reviewer,
        manifest=_manifest(decision_refs=["f1:id", "f1:ssn", "f1:notes"]),
    )

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["every_approved_action_executed"]["pass"] is False
    assert result["signal"] == {"failure_class": "EXECUTION_ERROR"}


def test_executor_failure_fails_and_signals_execution_error():
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(reviewer, execution_result=_execution_result(success=False, failure_class="EXECUTOR_CODE_ERROR"))

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["utility_requirement_respected"]["pass"] is False
    assert result["signal"] == {"failure_class": "EXECUTION_ERROR"}


def test_unreadable_export_fails_utility_check():
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(reviewer, safe_output_metadata={
        "column_counts": {"f1": {"decisions": 2, "verdicts": 2}},
        "schema_valid": {"f1": False},
    })

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["utility_requirement_respected"]["pass"] is False
    assert "unreadable_files=['f1']" in by_name["utility_requirement_respected"]["detail"]


def test_omitted_verification_fails_nothing_omitted_check():
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(reviewer, safe_output_metadata={
        "column_counts": {"f1": {"decisions": 2, "verdicts": 1}},
        "schema_valid": {"f1": True},
    })

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["nothing_omitted"]["pass"] is False
    assert result["signal"] == {"failure_class": "EXECUTION_ERROR"}


def test_unauthorized_execution_fails_and_signals_method_error():
    """A decision executed that the frozen manifest never authorized."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(
        reviewer,
        manifest=_manifest(decision_refs=["f1:id"]),  # ssn never authorized
    )

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["nothing_unauthorized"]["pass"] is False
    assert result["signal"] == {"failure_class": "METHOD_ERROR"}


def test_failed_deterministic_verification_fails_and_signals_method_error():
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(
        reviewer,
        verification_result=_verification_result(passed=False, failed_checks=["f1:ssn:drop"]),
    )

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["deterministic_verification_passed"]["pass"] is False
    assert "f1:ssn:drop" in by_name["deterministic_verification_passed"]["detail"]
    assert result["signal"] == {"failure_class": "METHOD_ERROR"}


def test_direct_identifier_kept_fails_privacy_check_and_signals_regulation_error():
    reviewer = Reviewer(make_ctx("Reviewer"))
    decisions = [
        {"file_id": "f1", "column": "ssn", "action": "keep", "phi_category": "G", "citation": ""},
    ]
    result = _finalize(
        reviewer,
        decisions=decisions,
        manifest=_manifest(decision_refs=["f1:ssn"]),
        safe_output_metadata={
            "column_counts": {"f1": {"decisions": 1, "verdicts": 1}},
            "schema_valid": {"f1": True},
        },
    )

    assert result["verdict"] == "FAIL"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["privacy_intent_preserved"]["pass"] is False
    assert result["signal"] == {"failure_class": "REGULATION_ERROR"}


def test_unresolved_human_review_ref_routes_to_human_review_required():
    """A manifest human_review_ref with no matching HumanDecision on
    record is genuinely unresolved -- never a hard FAIL, since a human
    still needs to act."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(
        reviewer,
        manifest=_manifest(human_review_refs=["hd-pending-1"]),
    )

    assert result["verdict"] == "HUMAN_REVIEW_REQUIRED"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["human_decisions_followed"]["pass"] is False
    assert by_name["no_unresolved_issue"]["pass"] is False
    assert result["signal"] == {"failure_class": "HUMAN_REVIEW_REQUIRED"}


def test_resolved_human_review_ref_passes():
    reviewer = Reviewer(make_ctx("Reviewer"))
    hd = HumanDecision(decision_id="hd-1", action="APPROVE", principal="alice@lab.edu")
    result = _finalize(
        reviewer,
        manifest=_manifest(human_review_refs=["hd-1"]),
        human_decisions=[hd],
    )

    assert result["verdict"] == "PASS"


def test_deferred_human_decision_routes_to_human_review_required():
    """A HumanDecision that explicitly DEFERs is an unresolved issue even
    though it is 'on record' (not simply missing)."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    hd = HumanDecision(decision_id="hd-1", action="DEFER", principal="alice@lab.edu")
    result = _finalize(
        reviewer,
        manifest=_manifest(human_review_refs=["hd-1"]),
        human_decisions=[hd],
    )

    assert result["verdict"] == "HUMAN_REVIEW_REQUIRED"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["no_unresolved_issue"]["pass"] is False
    assert result["signal"] == {"failure_class": "HUMAN_REVIEW_REQUIRED"}


def test_manifest_unresolved_items_alone_routes_to_human_review_required():
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(
        reviewer,
        manifest=_manifest(unresolved_items=1, status="invalidated"),
    )

    assert result["verdict"] == "HUMAN_REVIEW_REQUIRED"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["no_unresolved_issue"]["pass"] is False


def test_findings_never_carry_a_raw_value():
    """Every ReviewFinding-shaped finding carries only identifiers/counts
    in its detail text -- never a decision's phi_category, citation, or
    any dataset value."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    result = _finalize(
        reviewer,
        manifest=_manifest(decision_refs=["f1:id"]),
    )

    for finding in result["findings"]:
        assert "phi_category" not in finding["detail"]
        assert "citation" not in finding["detail"]
