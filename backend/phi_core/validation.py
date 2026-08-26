"""Classification & method-accuracy validator.

Runs the deterministic Sentinel hard-rule layer over the shipped labelled
corpus and reports:

* per-HIPAA-category precision / recall / F1
* overall category-classification accuracy
* overall method-appropriateness (predicted action == expected action)
* per-column mismatch details for regression debugging

The validator is intentionally LLM-free so tests are fast, deterministic and
reproducible. Coverage of a column by the deterministic layer means the
production pipeline is guaranteed to reach the same conclusion regardless of
LLM temperature or availability. Columns not covered by the deterministic
layer would fall through to the Judge LLM at runtime; those are reported as
`unclassified` here so we know exactly where LLM judgement carries the load.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from phi_core.agents.reasoning import _HARD_RULE_TABLE

CORPUS_PATH = Path(__file__).resolve().parent.parent / "tests" / "corpora" / "hipaa_categories.json"


@dataclass
class Prediction:
    column: str
    dict_hint: str
    expected_letter: str | None
    expected_action: str
    predicted_letter: str | None
    predicted_action: str
    citation: str | None
    category_match: bool
    action_match: bool


@dataclass
class CategoryScore:
    letter: str
    support: int          # gold-count
    predicted: int        # predicted-count (any column predicted this letter)
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float


@dataclass
class AccuracyReport:
    total: int
    category_correct: int
    action_correct: int
    unclassified: int
    category_accuracy: float
    action_accuracy: float
    per_category: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    corpus_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _letter_from_citation(citation: str | None) -> str | None:
    if not citation:
        return None
    m = re.search(r"\(i\)\(([A-R])\)", citation)
    return m.group(1) if m else None


def _match_hard_rule(column: str) -> tuple[str | None, str | None, str | None]:
    """Returns (predicted_action, citation, letter) or (None, None, None) if
    no deterministic rule applies."""
    norm = re.sub(r"\s+", "_", column.strip().lower())
    for pattern, _allow, default_action, citation in _HARD_RULE_TABLE:
        if re.match(pattern, norm):
            return default_action, citation, _letter_from_citation(citation)
    return None, None, None


def _score_category(letter: str, preds: list[Prediction]) -> CategoryScore:
    tp = sum(1 for p in preds if p.expected_letter == letter and p.predicted_letter == letter)
    fp = sum(1 for p in preds if p.expected_letter != letter and p.predicted_letter == letter)
    fn = sum(1 for p in preds if p.expected_letter == letter and p.predicted_letter != letter)
    support = tp + fn
    predicted = tp + fp
    precision = tp / predicted if predicted else (1.0 if support == 0 else 0.0)
    recall = tp / support if support else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return CategoryScore(
        letter=letter, support=support, predicted=predicted,
        tp=tp, fp=fp, fn=fn,
        precision=round(precision, 4), recall=round(recall, 4), f1=round(f1, 4),
    )


def _predict(col: dict[str, Any]) -> Prediction:
    action, citation, letter = _match_hard_rule(col["column"])
    if action is None:
        action = "unclassified"
        letter = None
    category_match = (letter == col["expected_hipaa_letter"])
    action_match = (action == col["expected_action"])
    return Prediction(
        column=col["column"],
        dict_hint=col.get("dict_hint", ""),
        expected_letter=col["expected_hipaa_letter"],
        expected_action=col["expected_action"],
        predicted_letter=letter,
        predicted_action=action,
        citation=citation,
        category_match=category_match,
        action_match=action_match,
    )


def run_validation(corpus: dict[str, Any] | None = None) -> AccuracyReport:
    if corpus is None:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cols = corpus.get("columns", [])
    preds = [_predict(c) for c in cols]

    total = len(preds)
    category_correct = sum(1 for p in preds if p.category_match)
    action_correct = sum(1 for p in preds if p.action_match)
    unclassified = sum(1 for p in preds if p.predicted_action == "unclassified")

    letters = sorted({p.expected_letter for p in preds if p.expected_letter} |
                     {p.predicted_letter for p in preds if p.predicted_letter})
    per_cat = [asdict(_score_category(letter, preds)) for letter in letters]

    return AccuracyReport(
        total=total,
        category_correct=category_correct,
        action_correct=action_correct,
        unclassified=unclassified,
        category_accuracy=round(category_correct / total, 4) if total else 0.0,
        action_accuracy=round(action_correct / total, 4) if total else 0.0,
        per_category=per_cat,
        predictions=[asdict(p) for p in preds],
        corpus_version=corpus.get("version", "unknown"),
    )
