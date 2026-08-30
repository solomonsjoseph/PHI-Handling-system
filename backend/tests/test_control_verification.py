"""Phase 9: migrating Operator's useful deterministic verification into
a typed VerificationResult (docs #54), via control/verification.py."""
from __future__ import annotations

import pytest
from phi_core.control.records import VerificationResult
from phi_core.control.store import MemoryControlStore
from phi_core.control.verification import build_verification_result, record_verification_result


def _kwargs(**overrides):
    base = dict(
        run_id="r1", task_id="execution:m1", attempt_id="a1",
        manifest_id="m1", manifest_version="1",
        input_artifact_version=0, output_artifact_version=0,
    )
    base.update(overrides)
    return base


def test_build_verification_result_passes_on_clean_operator_status() -> None:
    operator_result = {
        "status": "clean",
        "verdicts": [{"file_id": "f1", "column": "ssn", "method": "drop", "verdict": "pass"}],
        "failed_file_ids": [],
    }
    result = build_verification_result(**_kwargs(operator_result=operator_result))
    assert isinstance(result, VerificationResult)
    assert result.passed is True
    assert result.manifest_coverage_percent == 100
    assert result.failed_checks == []


def test_build_verification_result_flags_a_failed_verdict() -> None:
    operator_result = {
        "status": "issues",
        "verdicts": [
            {"file_id": "f1", "column": "ssn", "method": "drop", "verdict": "pass"},
            {"file_id": "f1", "column": "dob", "method": "cap_age_90", "verdict": "fail"},
        ],
        "failed_file_ids": [],
    }
    result = build_verification_result(**_kwargs(operator_result=operator_result))
    assert result.passed is False
    assert result.manifest_coverage_percent == 50
    assert result.failed_checks == ["f1:dob:cap_age_90"]


def test_build_verification_result_flags_an_unreadable_file() -> None:
    operator_result = {"status": "issues", "verdicts": [], "failed_file_ids": ["f2"]}
    result = build_verification_result(**_kwargs(operator_result=operator_result))
    assert result.passed is False
    assert result.failed_checks == ["f2:unreadable"]


def test_build_verification_result_vacuous_coverage_on_no_decisions() -> None:
    operator_result = {"status": "clean", "verdicts": [], "failed_file_ids": []}
    result = build_verification_result(**_kwargs(operator_result=operator_result))
    assert result.manifest_coverage_percent == 100
    assert result.passed is True


def test_build_verification_result_never_passes_on_status_alone_with_real_failures() -> None:
    """A hand-built operator_result claiming status='clean' but still
    listing a failed_file_id must not report passed=True."""
    operator_result = {"status": "clean", "verdicts": [], "failed_file_ids": ["f9"]}
    result = build_verification_result(**_kwargs(operator_result=operator_result))
    assert result.passed is False


@pytest.mark.asyncio
async def test_record_verification_result_persists_into_the_store() -> None:
    store = MemoryControlStore()
    operator_result = {"status": "clean", "verdicts": [], "failed_file_ids": []}
    result = build_verification_result(**_kwargs(operator_result=operator_result))

    await record_verification_result(store, result)

    stored = await store.find_many("verification_results", {"task_id": "execution:m1"})
    assert len(stored) == 1
    assert stored[0]["manifest_id"] == "m1"
    assert stored[0]["passed"] is True
