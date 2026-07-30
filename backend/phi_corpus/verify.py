"""Corpus verifier -- tabular-only scoring.

Given a completed pipeline session and the ground-truth dict produced
by ``phi_corpus.planters.plant()``, this module compares every planted
cell's expected action against the pipeline's Judge decision and
produces a report with two independent scores:

1. **Correctness** -- did the pipeline take the right action per plant?
   Precision / recall / F1 per HIPAA category. This is the "0 % PHI leak
   + 100 % accuracy" claim.
2. **Deferral rate** -- how often did Judge defer a plant that we know is
   decidable (i.e. the ground truth is unambiguous) to human_review?

Scoring path: every plant is a tabular column decision. One Judge
decision governs an entire column, so the verifier credits or debits
each (file, column) pair once. There is no form / PDF plant scoring:
the corpus generator emits only ``datasets/`` and ``dictionary/`` now.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Miss:
    """One row of the false-positive / false-negative table."""
    file: str
    column: str
    expected_action: str
    actual_action: str
    hipaa_category: str
    edge_case_tag: str = ""
    reason: str = ""


@dataclass
class CategoryScore:
    """Per-HIPAA-category precision/recall/F1."""
    category: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return round(self.tp / d, 4) if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return round(self.tp / d, 4) if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


# Actions considered "PHI-touching" for the correctness score. If a cell
# with ground-truth expected_action in this set is actually decided as
# ``keep``, that is a FALSE-NEGATIVE (PHI leak). If a cell with expected
# action ``keep`` is actually decided as anything in this set, that is a
# FALSE-POSITIVE (over-blocking clinical data).
_PHI_ACTIONS: frozenset[str] = frozenset({
    "drop", "year_only", "zip3_truncate", "cap_age_90",
    "pseudonymize", "hash", "scrub_text",
})


def verify(
    ground_truth: dict[str, Any],
    decisions: list[dict[str, Any]],
    file_name_map: dict[str, str] | None = None,
    guard_report: dict[str, Any] | None = None,
    export_paths: dict[str, str] | None = None,   # kept for API back-compat; unused
) -> dict[str, Any]:
    """Compare a corpus ground-truth against the pipeline's actual outputs.

    ``file_name_map`` translates the ORIGINAL corpus file name to the
    pipeline's ``file_id`` so ``(file_id, column)`` lookups resolve.

    ``guard_report`` is surfaced in the report but not used for scoring
    so leaks the guard missed are still caught by the Judge decision.
    """
    planted = ground_truth.get("planted") or []
    _ = export_paths   # retained for backwards-compatible callers

    # ------------------------------------------------------------------
    # Decision index: (file_key, column) -> decision
    # ------------------------------------------------------------------
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        fid = d.get("file_id") or d.get("file_name") or ""
        col = d.get("column") or ""
        by_key[(fid, col)] = d

    if file_name_map:
        name_by_id = {v: k for k, v in file_name_map.items()}
        for d in list(decisions):
            fid = d.get("file_id") or ""
            orig = name_by_id.get(fid)
            if orig:
                by_key[(orig, d.get("column", ""))] = d

    per_cat: dict[str, CategoryScore] = {}
    misses_false_neg: list[Miss] = []
    misses_false_pos: list[Miss] = []
    deferred_decidable: list[Miss] = []
    matched = 0
    # Track scored (file, column) pairs so dataset columns are counted once.
    scored_pairs: set[tuple[str, str]] = set()
    total_planted_cells = 0

    for cell in planted:
        col_name = cell["column"]
        file_name = cell["file_name"]
        cat = cell["hipaa_category"]
        expected = cell["expected_action"]

        pair_key = (file_name, col_name)
        if pair_key in scored_pairs:
            continue
        scored_pairs.add(pair_key)
        total_planted_cells += 1

        dec = by_key.get(pair_key)
        if not dec and file_name_map:
            dec = by_key.get((file_name_map.get(file_name, ""), col_name))
        actual = dec.get("action") if dec else "MISSING"

        # Deferral: decidable case punted to human_review.
        if actual == "human_review":
            deferred_decidable.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=actual,
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="Judge deferred a decidable case",
            ))
            continue

        scr = per_cat.setdefault(cat, CategoryScore(category=cat))
        expected_is_phi = expected in _PHI_ACTIONS
        actual_is_phi = actual in _PHI_ACTIONS

        if expected_is_phi and actual_is_phi:
            scr.tp += 1
            matched += 1
        elif expected_is_phi and not actual_is_phi:
            scr.fn += 1
            misses_false_neg.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=str(actual),
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="PHI leaked: expected transform, got keep-equivalent",
            ))
        elif not expected_is_phi and actual_is_phi:
            scr.fp += 1
            misses_false_pos.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=str(actual),
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="Clinical data over-blocked",
            ))
        else:
            scr.tn += 1
            matched += 1

    # ---- aggregate ---------------------------------------------------
    total_tp = sum(s.tp for s in per_cat.values())
    total_fp = sum(s.fp for s in per_cat.values())
    total_fn = sum(s.fn for s in per_cat.values())
    total_tn = sum(s.tn for s in per_cat.values())
    total = total_tp + total_fp + total_fn + total_tn
    overall_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    overall_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    overall_f1 = 2 * overall_p * overall_r / (overall_p + overall_r) if (overall_p + overall_r) else 0.0
    accuracy = (total_tp + total_tn) / total if total else 1.0

    deferral_rate = (
        len(deferred_decidable) / total_planted_cells
        if total_planted_cells else 0.0
    )

    return {
        "scenario_id": ground_truth.get("scenario_id"),
        "jurisdiction": ground_truth.get("jurisdiction"),
        "edge_case_tags": ground_truth.get("edge_case_tags", []),
        "correctness": {
            "overall_precision": round(overall_p, 4),
            "overall_recall": round(overall_r, 4),
            "overall_f1": round(overall_f1, 4),
            "overall_accuracy": round(accuracy, 4),
            "per_category": [s.to_dict() for s in per_cat.values()],
            "false_positives": [m.__dict__ for m in misses_false_pos],
            "false_negatives": [m.__dict__ for m in misses_false_neg],
        },
        "deferral": {
            "rate": round(deferral_rate, 4),
            "count": len(deferred_decidable),
            "cells": [m.__dict__ for m in deferred_decidable],
        },
        "summary": {
            "planted_columns": total_planted_cells,
            "matched": matched,
            "tp": total_tp, "fp": total_fp, "fn": total_fn, "tn": total_tn,
        },
        "guard_status": (guard_report or {}).get("status"),
    }
