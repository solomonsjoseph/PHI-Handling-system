"""Phase 16 evaluation 8/9: Reviewer false-positive rate.

The metric a naive "always flag everything" evaluator would fail: given
synthetic Judge outputs that are genuinely CORRECT, what fraction does
Reviewer Preview incorrectly flag as CORRECTION_REQUIRED/
HUMAN_REVIEW_REQUIRED? Three measurements:

1. The real, unstubbed deterministic checklist alone (``_deterministic_
   checklist``) against 12 genuinely correct decisions, chosen to avoid
   ``_HARD_RULE_TABLE``'s own keep-allowlisted clinical row (see
   measurement 3 below) -- zero false positives.
2. The full ``preview()`` pipeline with a deterministic double standing in
   for the LLM cross-check that is DELIBERATELY imperfect (2 false alarms
   out of 12, the same class of over-eager mistake a real small model
   makes second-guessing a correct decision) -- so the measured
   false-positive rate is genuinely > 0 and demonstrably < 1, proving the
   scoring is sensitive to a real miscalibration rather than passing
   trivially either because the double is perfect or because it flags
   everything.
3. A genuine defect this evaluation surfaced while building measurement 1:
   ``_deterministic_checklist``'s "unsafe KEEP" check fires whenever a
   column matches ANY ``_HARD_RULE_TABLE`` row while proposing 'keep' --
   including the table's OWN last row, whose allow-list is ``["keep"]``
   (roughly 40 legitimate clinical/stratifier columns such as
   "diagnosis_code", "heart_rate_bpm", "bmi", "sex"). A correct 'keep' on
   any of those is incorrectly flagged CORRECTION_REQUIRED -- a real,
   measurable false-positive rate of 1.0 on that column set, recorded as a
   regression xfail in ``test_regression_phase16.py``.
"""
from __future__ import annotations

from typing import Any

import pytest
from phi_core.agents.reviewer import Reviewer
from phi_core.control.testing import make_ctx

FILE_ID = "f1"

# 12 genuinely correct Judge decisions -- no planted error. Deliberately
# includes actions ("drop", "zip3_truncate", "cap_age_90", "year_only",
# "hash", "pseudonymize", "scrub_text") the deterministic "unsafe KEEP"
# check can never fire on, plus two correct 'keep' decisions on genuinely
# non-identifier clinical columns (not in the hard-rule table at all).
CORRECT_DECISIONS: list[dict[str, Any]] = [
    {"file_id": FILE_ID, "column": "ssn", "phi_category": "G", "subject": "participant",
     "action": "drop", "reason": "SSN is a direct identifier", "confidence": 0.95, "citation": "164.514(b)(2)(i)(G)"},
    {"file_id": FILE_ID, "column": "patient_name", "phi_category": "A", "subject": "participant",
     "action": "drop", "reason": "name is a direct identifier", "confidence": 0.95, "citation": "164.514(b)(2)(i)(A)"},
    {"file_id": FILE_ID, "column": "zip_code", "phi_category": "B", "subject": "participant",
     "action": "zip3_truncate", "reason": "ZIP truncated to 3 digits per Safe Harbor", "confidence": 0.9,
     "citation": "164.514(b)(2)(i)(B)"},
    {"file_id": FILE_ID, "column": "age", "phi_category": "C", "subject": "participant",
     "action": "cap_age_90", "reason": "age capped at 90+ per Safe Harbor", "confidence": 0.9,
     "citation": "164.514(b)(2)(i)(C)"},
    {"file_id": FILE_ID, "column": "date_of_birth", "phi_category": "C", "subject": "participant",
     "action": "year_only", "reason": "DOB reduced to year only", "confidence": 0.9,
     "citation": "164.514(b)(2)(i)(C)"},
    {"file_id": FILE_ID, "column": "mrn", "phi_category": "H", "subject": "participant",
     "action": "pseudonymize", "reason": "MRN pseudonymized to preserve linkage", "confidence": 0.9,
     "citation": "164.514(b)(2)(i)(H)"},
    {"file_id": FILE_ID, "column": "study_id", "phi_category": "R", "subject": "participant",
     "action": "hash", "reason": "unique study identifier hashed for linkage", "confidence": 0.85,
     "citation": "164.514(b)(2)(i)(R)"},
    {"file_id": FILE_ID, "column": "clinician_notes", "phi_category": None, "subject": "participant",
     "action": "scrub_text", "reason": "free-text field routed to cell-level scrubbing", "confidence": 0.85,
     "citation": ""},
    {"file_id": FILE_ID, "column": "viral_load", "phi_category": None, "subject": "participant",
     "action": "keep", "reason": "clinical lab value, not an identifier", "confidence": 0.9, "citation": ""},
    {"file_id": FILE_ID, "column": "specimen_type", "phi_category": None, "subject": "participant",
     "action": "keep", "reason": "clinical specimen classification, not an identifier", "confidence": 0.9, "citation": ""},
    {"file_id": FILE_ID, "column": "email", "phi_category": "F", "subject": "participant",
     "action": "drop", "reason": "email is a direct identifier", "confidence": 0.95,
     "citation": "164.514(b)(2)(i)(F)"},
    {"file_id": FILE_ID, "column": "treatment_arm", "phi_category": None, "subject": "participant",
     "action": "keep", "reason": "randomized study-arm assignment, not an identifier", "confidence": 0.9,
     "citation": ""},
]


class OverEagerScriptedReviewer(Reviewer):
    """Deterministic double for the LLM cross-check that is deliberately
    over-eager: raises a false blocking alarm on 2 of the 12 genuinely
    correct decisions above (the class of mistake a real, imperfectly
    calibrated small model makes second-guessing a correct call) and
    correctly stays silent on the other 10."""

    _FALSE_ALARM_COLUMNS = {"study_id", "treatment_arm"}

    async def call_json(self, user_prompt: str, phase: str, default: Any = None, **kwargs: Any) -> Any:
        issues = [
            {"file_id": FILE_ID, "column": column, "problem": "scripted false alarm",
             "suggested_action": "drop", "severity": "blocking"}
            for column in self._FALSE_ALARM_COLUMNS
        ]
        return {"verdict": "revise" if issues else "approved", "issues": issues, "summary": "scripted cross-check"}


def test_deterministic_checklist_alone_has_zero_false_positives_on_correct_decisions():
    findings = Reviewer._deterministic_checklist(CORRECT_DECISIONS, files=[])
    blocking = [f for f in findings if f["verdict"] == "CORRECTION_REQUIRED"]
    print(f"\n[Phase16][reviewer_precision] deterministic-checklist-only false positives: "
          f"{len(blocking)}/{len(CORRECT_DECISIONS)}")
    assert not blocking, f"deterministic checklist incorrectly flagged: {blocking}"


def test_deterministic_checklist_false_positive_rate_on_hard_rule_table_keep_listed_columns():
    """Genuine defect this evaluation surfaced (see ``test_regression_
    phase16.py::test_deterministic_checklist_ignores_hard_rule_allow_list_
    for_unsafe_keep``): ``_deterministic_checklist``'s "unsafe KEEP" check
    used to fire whenever a column matched ANY ``_HARD_RULE_TABLE`` row --
    including the table's own last row, whose allow-list is ``["keep"]``
    (roughly 40 legitimate clinical/stratifier column names such as
    "diagnosis_code", "heart_rate_bpm", "bmi", "sex"). That defect is now
    fixed: the check skips any row whose allow-list includes 'keep', so a
    correct 'keep' on those columns is no longer flagged. Measured
    directly here against 4 of those columns as a zero false-positive
    rate."""
    keep_listed_correct_decisions = [
        {"file_id": FILE_ID, "column": column, "phi_category": None, "subject": "participant",
         "action": "keep", "reason": f"clinical value, not an identifier ({column})",
         "confidence": 0.9, "citation": ""}
        for column in ("diagnosis_code", "heart_rate_bpm", "bmi", "sex")
    ]
    findings = Reviewer._deterministic_checklist(keep_listed_correct_decisions, files=[])
    blocking = [f for f in findings if f["verdict"] == "CORRECTION_REQUIRED"]
    fpr = round(len(blocking) / len(keep_listed_correct_decisions), 4)
    print(f"\n[Phase16][reviewer_precision] deterministic-checklist false-positive rate on "
          f"hard-rule-table keep-listed columns: {fpr} ({len(blocking)}/{len(keep_listed_correct_decisions)})")
    assert fpr == 0.0, (
        "a correct 'keep' on a keep-listed clinical column was incorrectly flagged "
        f"as CORRECTION_REQUIRED; got fpr={fpr}"
    )


@pytest.mark.asyncio
async def test_reviewer_preview_false_positive_rate_with_an_imperfect_llm_cross_check():
    ctx = make_ctx("Reviewer", session_id="s1")
    reviewer = OverEagerScriptedReviewer(ctx)
    result = await reviewer.preview(CORRECT_DECISIONS, statute={}, instrument={"fields": []}, files=[])

    flagged_columns = {
        issue["column"] for issue in result["issues"]
        if str(issue.get("severity", "")).lower() in ("blocking", "escalate")
    }
    false_positives = [d["column"] for d in CORRECT_DECISIONS if d["column"] in flagged_columns]
    fpr = round(len(false_positives) / len(CORRECT_DECISIONS), 4)
    print(f"\n[Phase16][reviewer_precision] false-positive rate: {fpr} over "
          f"{len(CORRECT_DECISIONS)} correct decisions; false_positives={false_positives}")
    print(f"[Phase16][reviewer_precision] verdict={result['verdict']} preview_status={result['preview_status']}")

    # A "flag everything" evaluator would score 1.0 here and fail this
    # bound; a "flag nothing" evaluator would score 0.0 and never catch a
    # real planted error (see the recall harness) -- this scripted double
    # is deliberately imperfect, landing strictly between the two.
    assert 0.0 < fpr < 1.0, f"expected a genuine, non-trivial false-positive rate; got {fpr}"
    assert false_positives == ["study_id", "treatment_arm"]
    assert result["verdict"] == "revise"
    assert result["preview_status"] == "CORRECTION_REQUIRED"
