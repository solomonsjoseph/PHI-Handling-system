"""Corpus verifier — dual-scoring per Sir's Q2(iii).

Given a completed pipeline session and the ground-truth dict produced
by ``phi_corpus.planters.plant()``, this module compares every planted
cell's expected action / category against the pipeline's actual decision
and produces a report with two independent scores:

1. **Correctness** — did the pipeline take the right action per plant?
   Precision / recall / F1 per HIPAA category. This is the "0 % PHI leak
   + 100 % accuracy" claim.
2. **Deferral rate** — how often did Judge defer a plant that we know is
   decidable (i.e. the ground truth is unambiguous) to human_review?
   Lowering this over iterations is the "reduce human review" goal.

Both scores are returned so the paper can plot a downward-trending
deferral-rate curve while the correctness curve stays flat at 100 %.

The verifier only inspects the pipeline's ``agent_decisions`` list (the
Judge output). It does not need direct file access, so it works on any
session that has completed classification even if the export path was
cleared for security.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    "pseudonymize", "scrub_text",
})


def verify(
    ground_truth: dict[str, Any],
    decisions: list[dict[str, Any]],
    file_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare a corpus ground-truth against the pipeline's actual decisions.

    ``file_name_map`` translates the ORIGINAL corpus file name (as it
    appears in the ground-truth) to the pipeline's ``file_id`` so we can
    look up decisions by ``(file_id, column)``. Passing ``None`` matches
    on the file name directly (Judge sometimes carries the original name
    in the decision record; in that case a direct match works).
    """
    planted = ground_truth.get("planted") or []

    # Index decisions by (file_id_or_name, column) -> decision
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        fid = d.get("file_id") or d.get("file_name") or ""
        col = d.get("column") or ""
        by_key[(fid, col)] = d

    # If a file_name_map was given, also index by original name so a
    # lookup on either name resolves.
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
    deferred_decidable: list[Miss] = []  # Q2(iii): decidable cases sent to human_review
    matched = 0
    total_planted_cells = 0

    for cell in planted:
        col_name = cell["column"]
        file_name = cell["file_name"]
        cat = cell["hipaa_category"]
        expected = cell["expected_action"]
        actual = None

        # Prefer file-name key first because that's what the ground truth
        # carries. If the map is present the (file_id, col) lookup above
        # already installed both.
        dec = by_key.get((file_name, col_name))
        if not dec and file_name_map:
            dec = by_key.get((file_name_map.get(file_name, ""), col_name))
        if dec:
            actual = dec.get("action")
        else:
            # Column absent from the decision list — could mean the
            # Executor dropped the whole column at Sentinel refusal; count
            # once per PHI cell so recall reflects the miss.
            actual = "MISSING"

        # A dataset column produces N rows; we only score the (file, col)
        # pair ONCE, not once per row, because Judge emits ONE decision
        # per column. Track that we've seen this pair.
        pair_key = (file_name, col_name)
        seen_key = (pair_key, "_scored")
        if seen_key in by_key:
            continue
        by_key[seen_key] = {"_scored": True}
        total_planted_cells += 1

        # Deferral to human_review of a decidable case (Q2(iii))
        if actual == "human_review":
            deferred_decidable.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=actual,
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="Judge deferred a decidable case",
            ))
            # Do not add to TP/FP/FN — deferral is a separate score.
            continue

        # Correctness scoring
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

    # Aggregate
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
    }
