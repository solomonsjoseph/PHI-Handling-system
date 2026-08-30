"""Shared, non-agent scoring primitives for the Phase 16 evaluation harnesses.

Every function here takes only predictions and external labels -- never a
model's self-reported confidence -- and returns a plain, JSON-serializable
result. Mirrors the precision/recall/F1 formulas already established by
``phi_core.validation._score_category`` (Phase A's classification-accuracy
harness) rather than inventing a second convention: precision/recall default
to 1.0 when their denominator is zero and the class has no support, and to
0.0 when there is support but zero true positives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Hashable, Sequence


@dataclass(frozen=True)
class ClassScore:
    label: Hashable
    support: int
    predicted: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "support": self.support, "predicted": self.predicted,
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
        }


def accuracy(pairs: Sequence[tuple[Any, Any]]) -> float:
    """Fraction of ``(predicted, label)`` pairs where ``predicted == label``.
    ``pairs`` must be non-empty."""
    if not pairs:
        raise ValueError("accuracy() requires at least one (predicted, label) pair")
    matches = sum(1 for predicted, label in pairs if predicted == label)
    return round(matches / len(pairs), 4)


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Precision/recall/F1 from raw true/false positive/negative counts."""
    support = tp + fn
    predicted = tp + fp
    precision = tp / predicted if predicted else (1.0 if support == 0 else 0.0)
    recall = tp / support if support else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return round(precision, 4), round(recall, 4), round(f1, 4)


def per_label_scores(pairs: Sequence[tuple[Any, Any]], labels: Sequence[Hashable]) -> dict[Hashable, ClassScore]:
    """One-vs-rest precision/recall/F1 for every label in ``labels``, over
    ``(predicted, true_label)`` pairs. A label with zero support in the
    labeled set (no case whose true label is it) still gets an entry so a
    caller can see it was never exercised, distinct from a label that was
    exercised and scored perfectly."""
    out: dict[Hashable, ClassScore] = {}
    for label in labels:
        tp = sum(1 for predicted, true in pairs if true == label and predicted == label)
        fp = sum(1 for predicted, true in pairs if true != label and predicted == label)
        fn = sum(1 for predicted, true in pairs if true == label and predicted != label)
        precision, recall, f1 = precision_recall_f1(tp, fp, fn)
        support = tp + fn
        predicted_count = tp + fp
        out[label] = ClassScore(
            label=label, support=support, predicted=predicted_count,
            tp=tp, fp=fp, fn=fn, precision=precision, recall=recall, f1=f1,
        )
    return out


def macro_f1(scores: dict[Hashable, ClassScore], *, only_supported: bool = True) -> float:
    """Mean F1 across labels. ``only_supported`` (default) excludes labels
    with zero support from the average, so an unexercised label never
    inflates the macro score with a vacuous 1.0."""
    values = [s.f1 for s in scores.values() if not only_supported or s.support > 0]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
