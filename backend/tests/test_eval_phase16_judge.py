"""Phase 16 evaluation 4/9: Judge two-stage classification (triage +
FINAL CLASSIFICATION, docs sections 31-34/40/41, Phase 7).

Reuses the Phase A labeled corpus (``backend/tests/corpora/
hipaa_categories.json``, already ground-truthing every HIPAA Safe Harbor
letter A-R plus non-PHI keeps and free-text scrubs -- see
``test_classification_accuracy.py``) as this evaluation's ground truth,
rather than inventing a second, redundant one. That existing harness only
measures the *deterministic hard-rule layer*
(``phi_core.validation.run_validation``); this one measures Judge's real
two-stage pipeline end to end: the real, unstubbed
``triage_columns`` (TRIAGE, stage 1, deterministic) and the real
``Judge._as_proposal``/``_validate_decision_entry``/``_column_decision``
(FINAL CLASSIFICATION, stage 2) that turns a model reply into typed
``ColumnDecision`` records -- only the model reply itself (``call_json``)
is a deterministic double.

The double answers correctly for every column except a fixed, deliberately
wrong 10% (every 10th corpus row), so the per-category precision/recall/F1
below is a genuine, non-trivial measurement of whether Judge's real
plumbing propagates a wrong model answer faithfully (it must: Judge is not
supposed to self-correct a wrong classification -- that is Reviewer's job,
evaluated separately) rather than a vacuous 100% that would be true by
construction.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from phi_core.agents.reasoning import Judge
from phi_core.control.testing import make_ctx
from phi_core.evaluations.scoring import macro_f1, per_label_scores

CORPUS_PATH = Path(__file__).parent / "corpora" / "hipaa_categories.json"
FILE_ID = "f1"

# A plausible-but-wrong Safe Harbor letter/action to substitute on the
# deliberately-wrong subset -- never the correct answer for that row, and
# always a *real* action/letter from the system's own vocabulary (the kind
# of near-miss a real model makes, not a malformed reply).
_WRONG_LETTER = {
    "A": "H", "B": "N", "C": "D", "D": "G", "E": "F", "F": "E", "G": "D",
    "H": "A", "I": "J", "J": "I", "K": "M", "L": "K", "M": "L", "N": "B",
    "O": "N", "P": "Q", "Q": "P", "R": "H", "": "A",
}
_WRONG_ACTION = {
    "drop": "keep", "keep": "drop", "cap_age_90": "keep", "year_only": "keep",
    "zip3_truncate": "keep", "hash": "keep", "pseudonymize": "drop", "scrub_text": "keep",
}


def _load_corpus() -> list[dict[str, Any]]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["columns"]


class ScriptedJudge(Judge):
    """Deterministic double for Judge's one model call (``judge.decide``):
    answers each column from a precomputed, deliberately-imperfect script
    rather than free-forming a reply, so triage + FINAL CLASSIFICATION
    (Judge's own real code) are exercised against a controlled, repeatable
    input."""

    def __init__(self, ctx: Any, decisions: list[dict[str, Any]]) -> None:
        super().__init__(ctx)
        self._decisions = decisions
        self.call_count = 0

    async def call_json(self, user_prompt: str, phase: str, default: Any = None, **kwargs: Any) -> Any:
        self.call_count += 1
        assert phase == "judge.decide"
        return {"decisions": self._decisions}


def _scripted_decisions(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions = []
    for i, col in enumerate(corpus):
        label_letter = col["expected_hipaa_letter"] or ""
        label_action = col["expected_action"]
        wrong = (i % 10 == 9)  # every 10th row: a deliberate, plausible miss
        letter = _WRONG_LETTER[label_letter] if wrong else (label_letter or None)
        action = _WRONG_ACTION.get(label_action, "keep") if wrong else label_action
        decisions.append({
            "file_id": FILE_ID, "column": col["column"],
            "phi_category": letter if letter not in ("", None) else None,
            "subject": "participant", "action": action,
            "reason": col.get("dict_hint", ""), "confidence": 0.4 if wrong else 0.9,
            "citation": "45 CFR 164.514(b)(2)(i)" if letter else "",
        })
    return decisions


@pytest.mark.asyncio
async def test_judge_two_stage_per_category_precision_recall_f1():
    corpus = _load_corpus()
    scripted = _scripted_decisions(corpus)
    ctx = make_ctx("Judge", session_id="s1")
    judge = ScriptedJudge(ctx, scripted)

    schema = {"columns": [{"name": col["column"], "_file_id": FILE_ID} for col in corpus]}
    lexicon = {"columns": [
        {"name": col["column"], "description": col.get("dict_hint", "")} for col in corpus
    ]}
    instrument = {"fields": []}
    statute = {"jurisdiction": "us", "regulation": "HIPAA Safe Harbor"}

    result = await judge.run(schema=schema, instrument=instrument, lexicon=lexicon, statute=statute)
    assert judge.call_count == 1
    assert len(result["decisions"]) == len(corpus)
    assert len(result["column_decisions"]) == len(corpus)

    # ground truth categories, using "NONE" for a non-PHI keep the same
    # way test_classification_accuracy.py's corpus itself represents it.
    label_by_column = {
        col["column"]: (col["expected_hipaa_letter"] or "NONE") for col in corpus
    }
    action_label_by_column = {col["column"]: col["expected_action"] for col in corpus}

    category_pairs: list[tuple[str, str]] = []
    action_pairs: list[tuple[str, str]] = []
    for cd, d in zip(result["column_decisions"], result["decisions"], strict=True):
        column = cd["column_id"]
        predicted_category = cd["sensitivity_classification"] or "NONE"
        category_pairs.append((predicted_category, label_by_column[column]))
        action_pairs.append((d["action"], action_label_by_column[column]))

    labels = tuple(sorted({"NONE"} | {c["expected_hipaa_letter"] for c in corpus if c["expected_hipaa_letter"]}))
    per_category = per_label_scores(category_pairs, labels)
    macro = macro_f1(per_category)
    action_accuracy = sum(1 for p, label in action_pairs if p == label) / len(action_pairs)

    print(f"\n[Phase16][judge] macro-F1 across {len(labels)} HIPAA categories: {macro}")
    for label in labels:
        s = per_category[label]
        print(f"[Phase16][judge] category={label}: support={s.support} tp={s.tp} fp={s.fp} "
              f"fn={s.fn} precision={s.precision} recall={s.recall} f1={s.f1}")
    print(f"[Phase16][judge] action accuracy: {round(action_accuracy, 4)} over {len(action_pairs)} columns")

    # Not just aggregate accuracy: every exercised category must be
    # individually reported (support > 0 for every corpus-covered letter).
    exercised = [label for label in labels if per_category[label].support > 0]
    assert set(exercised) == set(labels), "a labeled category from the corpus was never scored"

    # The scripted double is right on ~90% of columns (10 deliberately
    # wrong out of 98) -- macro-F1 must land measurably below a perfect
    # 1.0 (proving Judge's real pipeline faithfully propagates the wrong
    # answers rather than silently self-correcting them) but still high
    # (proving the pipeline does not corrupt the ~90% it got right).
    assert 0.75 <= macro < 1.0, f"expected a genuine, non-trivial macro-F1; got {macro}"
    assert 0.75 <= action_accuracy < 1.0, f"expected a genuine, non-trivial action accuracy; got {action_accuracy}"


@pytest.mark.asyncio
async def test_judge_triage_state_reflects_specialist_coverage():
    """TRIAGE (stage 1) is deterministic evidence bookkeeping, not a model
    judgment -- exercised directly against a small labeled set. A column
    documented by BOTH Lexicon and Instrument (whose real field shape is
    ``{"label": ..., "collected_variable": ...}``) must triage KNOWN: two
    independent sources agree it is understood.

    A column documented by neither Lexicon nor Instrument still correctly
    triages UNKNOWN, the fail-closed default; a Lexicon-only column triages
    UNVERIFIED."""
    corpus = _load_corpus()[:9]
    known = corpus[:3]        # documented by both Lexicon and Instrument
    lexicon_only = corpus[3:6]
    unknown = corpus[6:9]     # documented by neither

    decisions = []
    for col in corpus:
        decisions.append({
            "file_id": FILE_ID, "column": col["column"],
            "phi_category": col["expected_hipaa_letter"] or None,
            "subject": "participant", "action": col["expected_action"],
            "reason": "scripted", "confidence": 0.9, "citation": "",
        })
    ctx = make_ctx("Judge", session_id="s2")
    judge = ScriptedJudge(ctx, decisions)
    schema = {"columns": [{"name": col["column"], "_file_id": FILE_ID} for col in corpus]}
    lexicon = {"columns": [
        {"name": col["column"], "description": col.get("dict_hint", "")}
        for col in (known + lexicon_only)
    ]}
    # Real Instrument.run() output shape -- "label"/"collected_variable",
    # never "name"/"field"/"column".
    instrument = {"fields": [{"label": col["column"], "collected_variable": col["column"]} for col in known]}
    result = await judge.run(schema=schema, instrument=instrument, lexicon=lexicon, statute={})

    provenance_by_column = {cd["column_id"]: cd["technical_rationale"] for cd in result["column_decisions"]}
    for col in known:
        assert "triage=KNOWN" in provenance_by_column[col["column"]], (
            "Instrument coverage no longer elevates both-covered columns past "
            "UNVERIFIED to KNOWN; see test_regression_phase16.py"
        )
    for col in lexicon_only:
        assert "triage=UNVERIFIED" in provenance_by_column[col["column"]]
    for col in unknown:
        assert "triage=UNKNOWN" in provenance_by_column[col["column"]]
