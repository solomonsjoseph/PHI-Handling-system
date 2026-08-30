"""Phase 15b regression xfails.

Every genuine defect Phase 15b's adversarial testing found, per the
scheduling rule: a failing regression test here, marked
``xfail(strict=True)`` with a fixed one-line reason, and a matching
``KNOWN_XFAIL`` entry drafted below for Main to add to
``docs/PHASE_STATUS.md``. The fix itself is out of scope for this phase;
Phase 17 must resolve every one of these to either a passing test or a
recorded ``REVIEW_REQUIRED``.

KNOWN_XFAIL entries to add (Main owns docs/PHASE_STATUS.md this batch):

- nodeid: `tests/test_regression_phase15b.py::
  test_learning_case_error_exposes_raw_backstop_only_identifier_via_case_abstract`
  resolving phase: 17
  reason: "awaiting fix: LearningCaseError.case.abstract carries the raw
  identifier in cleartext for any PHI shape the sanitize stage's regex
  set does not recognize (VIN/MBI/DEA/NPI etc.), even though the durable
  store never receives it"
"""
from __future__ import annotations

import pytest
from phi_core.control.learning import LearningCaseError, LearningCaseService
from phi_core.control.store import MemoryControlStore


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "awaiting fix: LearningCaseError.case.abstract carries the raw identifier "
        "in cleartext for any PHI shape the sanitize stage's regex set does not "
        "recognize (VIN/MBI/DEA/NPI etc.), even though the durable store never "
        "receives it"
    ),
)
async def test_learning_case_error_exposes_raw_backstop_only_identifier_via_case_abstract():
    """control/learning.py's LearningCaseService.create_candidate pipeline is:
    _sanitize(abstract) -> _phi_pii_scan(sanitized_abstract) -> reject-on-fail.

    scrub_persisted_text (the sanitize stage) does not recognize every
    HIPAA-relevant identifier shape the later, dedicated PHI/PII scan
    stage does (e.g. a Medicare Beneficiary Identifier, jurisdictions.py's
    MBI GuardPattern) -- that gap between the two stages is exactly what
    makes the PHI scan a genuine backstop rather than a rubber stamp
    (test_control_learning_case_pipeline.py's own VIN-based test proves
    the *store* is protected even so). But the LearningCase object the
    PHI-scan-failure path attaches to the raised LearningCaseError is
    built from that same un-redacted, sanitize-missed abstract *before*
    the scan runs -- so `excinfo.value.case.abstract` (and thus anything
    that logs, serializes, or surfaces that exception object) still
    carries the raw identifier in cleartext, even on the very rejection
    path meant to keep it out of every persisted or reported surface.

    This test intentionally asserts the CURRENT (defective) behavior --
    the literal IS present on the exception's case object -- so it fails
    the moment a fix (e.g. building `case` from the pre-sanitize-miss
    abstract's PHI-scan-safe form, or scrubbing `case.abstract` before
    attaching it to the raised error) closes the gap.
    """
    store = MemoryControlStore()
    service = LearningCaseService(store)
    planted_mbi = "1EG4TE5MK73"

    with pytest.raises(LearningCaseError) as excinfo:
        await service.create_candidate(
            run_id="a" * 32, source="reviewer_correction",
            raw_content=f"Judge dropped a provider identifier {planted_mbi} without redaction.",
        )

    assert planted_mbi not in excinfo.value.case.abstract
