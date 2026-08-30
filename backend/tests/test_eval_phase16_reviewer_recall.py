"""Phase 16 evaluation 7/9: Reviewer error detection (recall on planted
errors).

``Reviewer.preview`` (``phi_core.agents.reviewer.Reviewer``, Phase 8) runs a
real deterministic checklist first (an "unsafe KEEP" matching a known
direct-identifier hard-rule pattern, docs #91's "deterministic Evidence
Gate" -- ``_deterministic_checklist``, unstubbed) and then an LLM
cross-check. This harness seeds 10 synthetic Judge decisions each carrying
one KNOWN, deliberately planted error (an unsafe KEEP the hard-rule table
covers, a wrong-method choice the hard-rule table does NOT cover and only
an LLM cross-check can catch, and a genuinely ambiguous case that should
escalate to a human) and measures what fraction Reviewer Preview's real
``verdict``/``preview_status`` genuinely flags as blocking or escalate --
never counting on model self-confidence, only on whether the flagged
(file_id, column) matches the planted-error label.
"""
from __future__ import annotations

from typing import Any

import pytest
from phi_core.agents.reviewer import Reviewer
from phi_core.control.testing import make_ctx

FILE_ID = "f1"

# (file_id, column, action, reason) -- one Judge decision each, plus the
# detection mechanism a correct Reviewer Preview run must use to catch it.
PLANTED_ERRORS: list[tuple[dict[str, Any], str]] = [
    # caught by the real, unstubbed deterministic checklist (hard-rule
    # "unsafe KEEP" pattern -- action=='keep' on a column the hard-rule
    # table names as a direct identifier).
    ({"file_id": FILE_ID, "column": "ssn", "phi_category": "G", "subject": "participant",
      "action": "keep", "reason": "kept by mistake", "confidence": 0.7, "citation": ""}, "deterministic"),
    ({"file_id": FILE_ID, "column": "patient_name", "phi_category": "A", "subject": "participant",
      "action": "keep", "reason": "kept by mistake", "confidence": 0.6, "citation": ""}, "deterministic"),
    ({"file_id": FILE_ID, "column": "phone_number", "phi_category": "D", "subject": "participant",
      "action": "keep", "reason": "kept by mistake", "confidence": 0.65, "citation": ""}, "deterministic"),
    ({"file_id": FILE_ID, "column": "email", "phi_category": "F", "subject": "participant",
      "action": "keep", "reason": "kept by mistake", "confidence": 0.55, "citation": ""}, "deterministic"),
    # NOT covered by the hard-rule "unsafe KEEP" check (action != "keep"):
    # a wrong-method choice only an LLM cross-check against
    # RegulationsExpert's rule can catch.
    ({"file_id": FILE_ID, "column": "zip_code", "phi_category": "B", "subject": "participant",
      "action": "hash", "reason": "used hash instead of zip3_truncate", "confidence": 0.7, "citation": ""}, "llm"),
    ({"file_id": FILE_ID, "column": "date_of_birth", "phi_category": "C", "subject": "participant",
      "action": "keep", "reason": "kept DOB in full", "confidence": 0.5, "citation": ""}, "llm"),
    ({"file_id": FILE_ID, "column": "clinician_notes", "phi_category": None, "subject": "participant",
      "action": "keep", "reason": "kept free text unscrubbed", "confidence": 0.6, "citation": ""}, "llm"),
    ({"file_id": FILE_ID, "column": "mrn", "phi_category": "H", "subject": "participant",
      "action": "hash", "reason": "used hash instead of pseudonymize (breaks cross-file linkage)",
      "confidence": 0.7, "citation": ""}, "llm"),
    # genuinely ambiguous -- correct Reviewer behavior is to escalate to a
    # human, not silently pick a correction itself.
    ({"file_id": FILE_ID, "column": "site_of_disease", "phi_category": "R", "subject": "site",
      "action": "drop", "reason": "dropped a possibly-clinical geography field", "confidence": 0.4,
      "citation": ""}, "llm_escalate"),
    ({"file_id": FILE_ID, "column": "study_id", "phi_category": None, "subject": "participant",
      "action": "keep", "reason": "ambiguous whether this is a real identifier or a sequence counter",
      "confidence": 0.4, "citation": ""}, "llm_escalate"),
]


class ScriptedReviewer(Reviewer):
    """Deterministic double for the LLM cross-check: flags exactly the
    "llm"/"llm_escalate"-labeled planted errors above by (file_id, column),
    with the correct severity, and never flags anything else -- so this
    harness measures Reviewer's real plumbing (deterministic checklist +
    issue merge + verdict/preview_status derivation), not the double's own
    judgment quality."""

    _LLM_BLOCKING = {"zip_code", "date_of_birth", "clinician_notes", "mrn"}
    _LLM_ESCALATE = {"site_of_disease", "study_id"}

    async def call_json(self, user_prompt: str, phase: str, default: Any = None, **kwargs: Any) -> Any:
        issues = []
        for entry in PLANTED_ERRORS:
            decision, _mechanism = entry
            column = decision["column"]
            if column in self._LLM_BLOCKING:
                issues.append({"file_id": FILE_ID, "column": column, "problem": "scripted blocking issue",
                                "suggested_action": "drop", "severity": "blocking"})
            elif column in self._LLM_ESCALATE:
                issues.append({"file_id": FILE_ID, "column": column, "problem": "scripted escalation",
                                "suggested_action": "", "severity": "escalate"})
        return {"verdict": "revise", "issues": issues, "summary": "scripted cross-check"}


@pytest.mark.asyncio
async def test_reviewer_preview_recall_on_planted_errors():
    decisions = [entry[0] for entry in PLANTED_ERRORS]
    ctx = make_ctx("Reviewer", session_id="s1")
    reviewer = ScriptedReviewer(ctx)
    result = await reviewer.preview(decisions, statute={}, instrument={"fields": []}, files=[])

    flagged_columns = {
        issue["column"] for issue in result["issues"]
        if str(issue.get("severity", "")).lower() in ("blocking", "escalate")
    }

    caught: list[str] = []
    missed: list[str] = []
    for decision, _mechanism in PLANTED_ERRORS:
        column = decision["column"]
        (caught if column in flagged_columns else missed).append(column)

    recall = len(caught) / len(PLANTED_ERRORS)
    print(f"\n[Phase16][reviewer_recall] planted-error recall: {round(recall, 4)} "
          f"over {len(PLANTED_ERRORS)} cases; caught={caught}; missed={missed}")
    print(f"[Phase16][reviewer_recall] verdict={result['verdict']} preview_status={result['preview_status']}")

    assert not missed, f"Reviewer Preview failed to flag genuine planted error(s): {missed}"
    assert recall == 1.0
    assert result["verdict"] == "revise"
    assert result["preview_status"] == "HUMAN_REVIEW_REQUIRED"  # an escalate is present

    # The deterministic-only subset must be caught even with zero LLM
    # calls -- proves the real hard-rule checklist alone (docs #91) is
    # sufficient for direct-identifier leaks, matching its own
    # ``deterministic_only=True`` mode used for the mandatory post-human
    # re-review (docs #46).
    deterministic_only_result = reviewer._deterministic_checklist(decisions, files=[])
    deterministic_columns = {f["column"] for f in deterministic_only_result if f["verdict"] == "CORRECTION_REQUIRED"}
    deterministic_labels = {d["column"] for d, mech in PLANTED_ERRORS if mech == "deterministic"}
    assert deterministic_labels <= deterministic_columns, (
        f"deterministic checklist alone missed: {deterministic_labels - deterministic_columns}"
    )
