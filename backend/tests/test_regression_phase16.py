"""Phase 16 regression-test class (per the phase's scheduling rule): a
failing regression test for every genuine defect this phase's evaluation
harnesses surfaced. Each is ``xfail(strict=True)``; the fix itself is out
of scope for this phase and is recorded separately in
``docs/PHASE_STATUS.md``'s ``KNOWN_XFAIL`` section for Phase 17 to resolve.
"""
from __future__ import annotations

import pytest
from phi_core.agents.reasoning import triage_columns
from phi_core.agents.reviewer import Reviewer


def test_triage_known_state_is_unreachable_from_real_instrument_fields():
    """Discovered building the Phase 16 Judge two-stage classification
    evaluation harness (``test_eval_phase16_judge.py::
    test_judge_triage_state_reflects_specialist_coverage``).

    ``triage_columns``'s own docstring (docs section 32) defines KNOWN as
    "documented in both the dictionary (Lexicon) and a study form
    (Instrument): two independent sources agree it is understood." It
    identifies instrument coverage via
    ``_entry_identity(f, ("name", "field", "column"))`` -- but
    ``Instrument.run()``'s real, unedited output (``agents/specialists.py``,
    ``Instrument.PROMPT``'s own declared schema) is
    ``{"fields": [{"label": str, "collected_variable": str|null}]}``. No
    real Instrument field ever carries a ``name``/``field``/``column`` key,
    so ``instrument_names`` in ``triage_columns`` is always empty for real
    data, and a Schema column can never satisfy
    ``identity[1] in instrument_names`` -- KNOWN is dead code from real
    Instrument data. Every column a form genuinely documents currently
    triages no better than UNVERIFIED (the same state as Lexicon-only
    coverage), silently losing the "two independent sources" provenance
    signal FINAL CLASSIFICATION's ``_TRIAGE_PROVENANCE`` mapping is meant
    to carry through to each ``ColumnDecision.technical_rationale``.

    This test states the INTENDED behavior (real Instrument coverage,
    combined with real Lexicon coverage, reaches KNOWN) and is expected to
    fail until the identity-key mismatch is fixed -- e.g. by adding
    ``"label"``/``"collected_variable"`` to the instrument name_keys, or by
    having callers pass instrument fields already normalized to carry a
    ``"column"`` key before they reach ``triage_columns``.
    """
    schema = [{"name": "mrn", "_file_id": "f1"}]
    lexicon = [{"name": "mrn", "description": "medical record number"}]
    instrument = [{"label": "mrn", "collected_variable": "mrn"}]
    triage = triage_columns(schema, lexicon, instrument)
    assert triage[("f1", "mrn")] == "KNOWN"


@pytest.mark.xfail(
    strict=True,
    reason="awaiting fix: Reviewer._deterministic_checklist's unsafe-KEEP check matches ANY "
           "_HARD_RULE_TABLE row including the table's own keep-allowlisted clinical row, so a "
           "correct 'keep' on ~40 legitimate clinical columns (diagnosis_code, heart_rate_bpm, "
           "bmi, sex, ...) is incorrectly flagged CORRECTION_REQUIRED",
)
def test_reviewer_deterministic_checklist_does_not_flag_a_correct_keep_on_a_keep_allowlisted_column():
    """Discovered building the Phase 16 Reviewer false-positive-rate
    evaluation harness (``test_eval_phase16_reviewer_precision.py::
    test_deterministic_checklist_false_positive_rate_on_hard_rule_table_
    keep_listed_columns``, which measures the real, current 1.0
    false-positive rate this defect produces on that column set).

    ``Reviewer._deterministic_checklist``'s "unsafe KEEP" rule
    (``agents/reviewer.py``) is:

        if action == "keep" and any(
            re.match(pattern, col_norm) for pattern, *_ in _HARD_RULE_TABLE
        ):
            ... CORRECTION_REQUIRED, kind="unsafe_keep" ...

    This checks only whether the column matches ANY hard-rule-table
    pattern -- never whether that particular row's own allow-list actually
    excludes 'keep'. ``_HARD_RULE_TABLE``'s last row (agents/
    deterministic_rules.py) is itself an allow-list of ~40 legitimate
    clinical/stratifier column names whose ONLY allowed action is 'keep'
    (hemoglobin, bmi, systolic_bp, heart_rate_bpm, dose_mg, sex, state,
    diagnosis_code, site_of_disease, treatment_outcome, ...). A genuinely
    correct Judge decision of action='keep' on any of those columns
    matches that row and is misclassified as an "unsafe KEEP", the same
    severity used for a real leak (a name or SSN column proposed as
    'keep'). This degrades Reviewer Preview's real precision on one of
    the most common correct decisions the system makes.

    This test states the INTENDED behavior (a keep-allowlisted column's
    correct 'keep' produces no CORRECTION_REQUIRED finding) and is
    expected to fail until the check also consults the matched row's own
    allow-list -- e.g. only flag "unsafe KEEP" when 'keep' is NOT in the
    matched row's allowed actions.
    """
    decisions = [{"file_id": "f1", "column": "diagnosis_code", "action": "keep",
                  "reason": "clinical diagnosis code, not an identifier"}]
    findings = Reviewer._deterministic_checklist(decisions, files=[])
    blocking = [f for f in findings if f["verdict"] == "CORRECTION_REQUIRED"]
    assert not blocking, f"a correct keep-allowlisted 'keep' was incorrectly flagged: {blocking}"
