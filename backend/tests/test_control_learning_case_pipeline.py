"""Phase 12 item 4: the docs #73 learning candidate pipeline
(control/learning.py's LearningCaseService, feeding but not replacing the
existing LearningService D16 machinery)."""
from __future__ import annotations

import pytest
from phi_core.control.learning import (
    LEARNING_CANDIDATES_COLLECTION,
    LEARNING_CASES_COLLECTION,
    LearningCaseError,
    LearningCaseService,
)
from phi_core.control.store import MemoryControlStore

RUN_ID = "a" * 32


def _service() -> tuple[LearningCaseService, MemoryControlStore]:
    store = MemoryControlStore()
    return LearningCaseService(store), store


# ---- the safe path: a clean candidate reaches the safe store -------------


@pytest.mark.asyncio
async def test_a_clean_candidate_reaches_the_safe_learning_store():
    service, store = _service()

    case = await service.create_candidate(
        run_id=RUN_ID, source="judge_mistake", raw_content=(
            "Judge classified an ambiguous free-text column as a date type "
            "because the header contained the word 'date', when the actual "
            "values were narrative comments."
        ),
    )

    assert case.sanitized is True
    assert case.phi_pii_scan_passed is True
    assert case.reconstruction_check_passed is True
    assert case.policy_validation_passed is True
    stored = await store.get_one(LEARNING_CASES_COLLECTION, {"case_id": case.case_id})
    assert stored is not None
    assert stored["abstract"]
    # never left a row behind in staging once promoted
    assert await store.get_one(LEARNING_CANDIDATES_COLLECTION, {"case_id": case.case_id}) is None


@pytest.mark.asyncio
async def test_every_declared_source_is_accepted():
    service, _store = _service()
    for source in (
        "judge_mistake", "reviewer_correction", "human_decision", "research_failure",
        "method_failure", "executor_failure", "verification_failure", "successful_recovery",
    ):
        case = await service.create_candidate(
            run_id=RUN_ID, source=source, raw_content=f"a validated {source} signal, generalized",
        )
        assert case.source == source


# ---- unsafe candidates are caught and DELETEd, never reaching the store --


@pytest.mark.asyncio
async def test_a_planted_ssn_is_redacted_by_sanitize_before_it_ever_reaches_the_store():
    """The sanitize stage (scrub_persisted_text) already redacts SSNs, so
    a candidate built from SSN-bearing raw content is promoted (the PHI
    scan finds nothing left to catch) -- but the raw digits themselves
    must never appear anywhere in the safe store, proving sanitize
    genuinely ran rather than being a no-op label."""
    service, store = _service()

    case = await service.create_candidate(
        run_id=RUN_ID, source="human_decision",
        raw_content="Reviewer overrode Judge because SSN 123-45-6789 was visible in the preview.",
    )

    assert case.sanitized is True
    assert "123-45-6789" not in case.abstract
    for documents in store._collections.values():
        for doc in documents:
            assert "123-45-6789" not in repr(doc)


@pytest.mark.asyncio
async def test_a_planted_person_name_is_redacted_by_sanitize_before_it_ever_reaches_the_store():
    service, store = _service()

    case = await service.create_candidate(
        run_id=RUN_ID, source="reviewer_correction",
        raw_content="Reviewer corrected Judge's decision after noticing Jennifer Alvarez in the preview cell.",
    )

    assert "Jennifer Alvarez" not in case.abstract
    for documents in store._collections.values():
        for doc in documents:
            assert "Jennifer Alvarez" not in repr(doc)


@pytest.mark.asyncio
async def test_the_phi_scan_stage_is_a_genuine_backstop_not_a_rubber_stamp():
    """A VIN is a real HIPAA-relevant identifier shape (jurisdictions.py's
    ``VIN`` pattern) that ``scrub_persisted_text``'s own regex set does
    not redact -- confirmed empirically (unlike SSN/name/MRN, which
    sanitize already handles). This is the pipeline's PHI/PII scan stage
    genuinely catching something sanitize missed, not merely re-detecting
    what sanitize already removed."""
    service, store = _service()

    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id=RUN_ID, source="method_failure",
            raw_content="Method failure surfaced vehicle VIN 1HGCM82633A123456 in the diagnostic text.",
        )

    assert excinfo.value.reason == "phi_pii_scan_failed"
    rejected = excinfo.value.case
    assert rejected is not None
    assert rejected.phi_pii_scan_passed is False
    assert await store.get_one(LEARNING_CANDIDATES_COLLECTION, {"case_id": rejected.case_id}) is None
    assert await store.find_many(LEARNING_CASES_COLLECTION, {}) == []
    for documents in store._collections.values():
        for doc in documents:
            assert "1HGCM82633A123456" not in repr(doc)


@pytest.mark.asyncio
async def test_a_long_digit_run_fails_the_reconstruction_check_even_when_not_ssn_shaped():
    service, store = _service()
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id=RUN_ID, source="method_failure",
            raw_content="The method failed for record 9988776655443322, unrelated to any HIPAA pattern shape.",
        )
    assert excinfo.value.reason == "reconstruction_check_failed"
    assert "long_digit_run" in str(excinfo.value)
    assert await store.find_many(LEARNING_CASES_COLLECTION, {}) == []


@pytest.mark.asyncio
async def test_a_long_quoted_excerpt_fails_the_reconstruction_check():
    service, _store = _service()
    excerpt = "x" * 60
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id=RUN_ID, source="verification_failure",
            raw_content=f'Verifier flagged the literal cell value "{excerpt}" as unexpectedly retained.',
        )
    assert excinfo.value.reason == "reconstruction_check_failed"


@pytest.mark.asyncio
async def test_an_unvalidated_signal_fails_policy_validation():
    service, store = _service()
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id=RUN_ID, source="research_failure",
            raw_content="A research step failed for reasons that have not been independently confirmed yet.",
            validated=False,
        )
    assert excinfo.value.reason == "policy_validation_failed"
    assert "source_not_validated" in str(excinfo.value)
    assert await store.find_many(LEARNING_CASES_COLLECTION, {}) == []


@pytest.mark.asyncio
async def test_an_empty_abstract_after_sanitize_fails_policy_validation():
    service, _store = _service()
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(run_id=RUN_ID, source="executor_failure", raw_content="   ")
    assert excinfo.value.reason == "policy_validation_failed"
    assert "empty_abstract" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_invalid_source_is_refused_before_anything_is_staged():
    service, store = _service()
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id=RUN_ID, source="not_a_real_source",  # type: ignore[arg-type]
            raw_content="anything",
        )
    assert excinfo.value.reason == "invalid_source"
    assert await store.find_many(LEARNING_CANDIDATES_COLLECTION, {}) == []


@pytest.mark.asyncio
async def test_an_unscoped_run_id_fails_policy_validation():
    service, _store = _service()
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id="../etc/passwd", source="successful_recovery",
            raw_content="A recovery pattern worth generalizing.",
        )
    assert excinfo.value.reason == "policy_validation_failed"
    assert "run_id_not_scoped" in str(excinfo.value)


# ---- get_case reads only from the safe store ------------------------------


@pytest.mark.asyncio
async def test_get_case_returns_none_for_a_rejected_candidate():
    service, _store = _service()
    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id=RUN_ID, source="judge_mistake",
            raw_content="Diagnostic dump included VIN 1HGCM82633A123456 verbatim.",
        )
    assert await service.get_case(excinfo.value.case.case_id) is None


@pytest.mark.asyncio
async def test_get_case_returns_the_stored_case_after_success():
    service, _store = _service()
    case = await service.create_candidate(
        run_id=RUN_ID, source="judge_mistake", raw_content="A generalized, validated judge mistake.",
    )
    fetched = await service.get_case(case.case_id)
    assert fetched is not None
    assert fetched.case_id == case.case_id
