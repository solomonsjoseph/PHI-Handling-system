"""
Benchmark scoring engine for PHI detection evaluation.

Implements span-level precision, recall, F1 and gap detection rate.
Designed to be tool-agnostic: works with Presidio, AWS Comprehend Medical,
Modified Deidentify, or any detector whose output is mapped to
(start, end, entity_type) tuples.

Scoring strategies
------------------
EXACT   -- (start, end) must match gold span character offsets exactly.
           Most conservative. Standard for NER evaluation.
OVERLAP -- A prediction is TP if its character-offset overlap with any
           gold span >= overlap_threshold (default 0.5). More lenient;
           matches partial detections.

Both strategies support optional entity-type alignment. Without alignment
(entity_type_agnostic=True), only span position matters -- measures "any PHI
detected." With alignment, the mapped entity type must also match -- measures
"right category detected."

Gap detection rate
------------------
Gold spans whose entity_type is in an "uncoverable" set for the detector
(e.g., BIOMETRIC_* for Presidio) are counted as structural gaps, not missed
detections. gap_detection_rate = structural_gap_spans / total_gold_spans.

Authority: See authorities/AUTHORITY_MATRIX.md Table C (benchmark tool gaps)
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PredictedSpan:
    """A single PHI detection output from a benchmark tool."""
    start: int
    end: int
    entity_type: str        # tool's own label (e.g., Presidio's "PERSON")
    mapped_type: str = ""   # translated to our corpus taxonomy; set by adapter
    mapped_types: FrozenSet[str] = field(default_factory=frozenset)  # full candidate set, for type-aware matching
    score: float = 1.0      # confidence score if available


@dataclass
class GoldSpan:
    """A single gold-standard PHI span from our corpus."""
    start: int
    end: int
    entity_type: str        # our corpus taxonomy label
    hipaa_category: Optional[str] = None  # A-R
    detection_regime: str = "contextual_ner_required"
    jurisdiction: str = "us"


@dataclass
class SpanScore:
    """TP/FP/FN counts for a single (record, entity_type) combination."""
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom > 0 else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom > 0 else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        denom = p + r
        return 2 * p * r / denom if denom > 0 else 0.0

    def add(self, other: "SpanScore") -> "SpanScore":
        return SpanScore(
            tp=self.tp + other.tp,
            fp=self.fp + other.fp,
            fn=self.fn + other.fn,
        )


@dataclass
class BenchmarkResult:
    """Aggregate benchmark result for a set of records."""
    tool_name: str
    corpus_files: List[str] = field(default_factory=list)
    total_records: int = 0
    total_gold_spans: int = 0
    total_predicted_spans: int = 0

    # Per-entity-type scores (mapped taxonomy)
    per_entity_type: Dict[str, SpanScore] = field(default_factory=dict)

    # Per-HIPAA-category scores (A-R)
    per_hipaa_category: Dict[str, SpanScore] = field(default_factory=dict)

    # Per-detection-regime scores
    per_detection_regime: Dict[str, SpanScore] = field(default_factory=dict)

    # Gap analysis
    gap_entity_types: Set[str] = field(default_factory=set)  # types tool cannot detect
    gap_span_count: int = 0   # gold spans in gap categories
    coverable_span_count: int = 0  # gold spans Presidio could potentially detect

    # Aggregate (all entity types combined)
    aggregate: SpanScore = field(default_factory=SpanScore)

    # Strict benchmark protocol (exact span + exact entity, all gold spans)
    strict_all_span: SpanScore = field(default_factory=SpanScore)
    scoring_profile: str = "legacy_overlap_coverable"
    raw_prediction_artifact: str = ""

    @property
    def macro_f1(self) -> float:
        if not self.per_entity_type:
            return 0.0
        f1_values = [s.f1 for s in self.per_entity_type.values()]
        return sum(f1_values) / len(f1_values)

    @property
    def gap_detection_rate(self) -> float:
        """Fraction of gold spans that the tool structurally cannot detect."""
        if self.total_gold_spans == 0:
            return 0.0
        return self.gap_span_count / self.total_gold_spans

    def summary_dict(self) -> dict:
        return {
            "tool": self.tool_name,
            "scoring_profile": self.scoring_profile,
            "total_records": self.total_records,
            "total_gold_spans": self.total_gold_spans,
            "total_predicted_spans": self.total_predicted_spans,
            "aggregate_precision": round(self.aggregate.precision, 4),
            "aggregate_recall": round(self.aggregate.recall, 4),
            "aggregate_f1": round(self.aggregate.f1, 4),
            "macro_f1": round(self.macro_f1, 4),
            "strict_all_span_precision": round(self.strict_all_span.precision, 4),
            "strict_all_span_recall": round(self.strict_all_span.recall, 4),
            "strict_all_span_f1": round(self.strict_all_span.f1, 4),
            "raw_prediction_artifact": self.raw_prediction_artifact,
            "gap_span_count": self.gap_span_count,
            "gap_detection_rate": round(self.gap_detection_rate, 4),
            "coverable_span_count": self.coverable_span_count,
            "per_entity_type": {
                et: {
                    "precision": round(s.precision, 4),
                    "recall": round(s.recall, 4),
                    "f1": round(s.f1, 4),
                    "tp": s.tp, "fp": s.fp, "fn": s.fn,
                }
                for et, s in sorted(self.per_entity_type.items())
            },
            "per_hipaa_category": {
                cat: {
                    "precision": round(s.precision, 4),
                    "recall": round(s.recall, 4),
                    "f1": round(s.f1, 4),
                    "tp": s.tp, "fp": s.fp, "fn": s.fn,
                }
                for cat, s in sorted(self.per_hipaa_category.items())
            },
            "per_detection_regime": {
                regime: {
                    "precision": round(s.precision, 4),
                    "recall": round(s.recall, 4),
                    "f1": round(s.f1, 4),
                    "tp": s.tp, "fp": s.fp, "fn": s.fn,
                }
                for regime, s in sorted(self.per_detection_regime.items())
            },
            "gap_entity_types": sorted(self.gap_entity_types),
        }


# ---------------------------------------------------------------------------
# Span matching helpers
# ---------------------------------------------------------------------------

def _overlap_fraction(pred: PredictedSpan, gold: GoldSpan) -> float:
    """Intersection-over-Union (IoU) of predicted and gold spans.

    Standard IoU avoids inflating scores when a short prediction is fully
    contained within a long gold span (which naive shorter-span division does).
    """
    inter_start = max(pred.start, gold.start)
    inter_end = min(pred.end, gold.end)
    if inter_end <= inter_start:
        return 0.0
    inter_len = inter_end - inter_start
    union_len = (pred.end - pred.start) + (gold.end - gold.start) - inter_len
    return inter_len / union_len if union_len > 0 else 0.0


def _exact_match(pred: PredictedSpan, gold: GoldSpan) -> bool:
    return pred.start == gold.start and pred.end == gold.end


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def score_record(
    predicted: List[PredictedSpan],
    gold: List[GoldSpan],
    gap_entity_types: FrozenSet[str] = frozenset(),
    strategy: str = "exact",
    overlap_threshold: float = 0.5,
    entity_type_agnostic: bool = True,
) -> dict:
    """Score predictions against gold spans for a single record.

    Parameters
    ----------
    predicted : list of PredictedSpan
    gold : list of GoldSpan
    gap_entity_types : frozenset
        Gold entity types the tool structurally cannot detect.
        These are counted as gaps, not FNs.
    strategy : "exact" | "overlap"
    overlap_threshold : float (only used when strategy="overlap")
    entity_type_agnostic : bool
        If True, only span position is checked (not entity type).

    Returns
    -------
    dict with keys:
        tp, fp, fn               -- aggregate counts
        per_entity_type          -- dict: entity_type -> SpanScore
        per_hipaa_category       -- dict: hipaa_cat -> SpanScore
        per_detection_regime     -- dict: regime -> SpanScore
        gap_spans                -- list of GoldSpan in gap_entity_types
        matched_gold             -- set of indices into gold that were matched
    """
    # Separate out gap spans
    gap_spans = [g for g in gold if g.entity_type in gap_entity_types]
    coverable_gold = [g for g in gold if g.entity_type not in gap_entity_types]

    matched_gold: Set[int] = set()    # indices into coverable_gold
    matched_pred: Set[int] = set()    # indices into predicted

    per_entity: Dict[str, SpanScore] = defaultdict(SpanScore)
    per_hipaa: Dict[str, SpanScore] = defaultdict(SpanScore)
    per_regime: Dict[str, SpanScore] = defaultdict(SpanScore)

    def _is_match(p: PredictedSpan, g: GoldSpan) -> bool:
        if strategy == "exact":
            pos_match = _exact_match(p, g)
        else:
            pos_match = _overlap_fraction(p, g) >= overlap_threshold
        if not pos_match:
            return False
        if entity_type_agnostic:
            return True
        candidates = p.mapped_types or ({p.mapped_type} if p.mapped_type else set())
        return g.entity_type in candidates

    # Greedy left-to-right matching (sufficient for non-overlapping spans)
    for gi, g in enumerate(coverable_gold):
        for pi, p in enumerate(predicted):
            if pi in matched_pred:
                continue
            if _is_match(p, g):
                matched_gold.add(gi)
                matched_pred.add(pi)
                # TP
                et = g.entity_type
                per_entity[et] = per_entity[et].add(SpanScore(tp=1))
                if g.hipaa_category:
                    hc = g.hipaa_category
                    per_hipaa[hc] = per_hipaa[hc].add(SpanScore(tp=1))
                per_regime[g.detection_regime] = per_regime[g.detection_regime].add(SpanScore(tp=1))
                break

    # FN: coverable gold not matched
    for gi, g in enumerate(coverable_gold):
        if gi not in matched_gold:
            et = g.entity_type
            per_entity[et] = per_entity[et].add(SpanScore(fn=1))
            if g.hipaa_category:
                hc = g.hipaa_category
                per_hipaa[hc] = per_hipaa[hc].add(SpanScore(fn=1))
            per_regime[g.detection_regime] = per_regime[g.detection_regime].add(SpanScore(fn=1))

    # FP: predictions not matched to any coverable gold
    for pi, p in enumerate(predicted):
        if pi not in matched_pred:
            et = p.mapped_type or p.entity_type
            per_entity[et] = per_entity[et].add(SpanScore(fp=1))

    tp = sum(s.tp for s in per_entity.values())
    fp = sum(s.fp for s in per_entity.values())
    fn = sum(s.fn for s in per_entity.values())

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "per_entity_type": dict(per_entity),
        "per_hipaa_category": dict(per_hipaa),
        "per_detection_regime": dict(per_regime),
        "gap_spans": gap_spans,
        "matched_gold": matched_gold,
    }



def score_record_strict_all_span(
    predicted: List[PredictedSpan],
    gold: List[GoldSpan],
) -> dict:
    """Score one record with exact span and exact mapped entity matching.

    Unlike the legacy coverable-span profile, this profile counts every gold
    span as claim-bearing. Structural gaps are therefore false negatives.
    """
    matched_gold: Set[int] = set()
    matched_pred: Set[int] = set()

    per_entity: Dict[str, SpanScore] = defaultdict(SpanScore)
    per_hipaa: Dict[str, SpanScore] = defaultdict(SpanScore)
    per_regime: Dict[str, SpanScore] = defaultdict(SpanScore)

    for gi, g in enumerate(gold):
        for pi, p in enumerate(predicted):
            if pi in matched_pred:
                continue
            candidates = p.mapped_types or ({p.mapped_type} if p.mapped_type else set())
            if _exact_match(p, g) and g.entity_type in candidates:
                matched_gold.add(gi)
                matched_pred.add(pi)
                per_entity[g.entity_type] = per_entity[g.entity_type].add(SpanScore(tp=1))
                if g.hipaa_category:
                    per_hipaa[g.hipaa_category] = per_hipaa[g.hipaa_category].add(SpanScore(tp=1))
                per_regime[g.detection_regime] = per_regime[g.detection_regime].add(SpanScore(tp=1))
                break

    for gi, g in enumerate(gold):
        if gi not in matched_gold:
            per_entity[g.entity_type] = per_entity[g.entity_type].add(SpanScore(fn=1))
            if g.hipaa_category:
                per_hipaa[g.hipaa_category] = per_hipaa[g.hipaa_category].add(SpanScore(fn=1))
            per_regime[g.detection_regime] = per_regime[g.detection_regime].add(SpanScore(fn=1))

    for pi, p in enumerate(predicted):
        if pi not in matched_pred:
            et = p.mapped_type or p.entity_type
            per_entity[et] = per_entity[et].add(SpanScore(fp=1))

    tp = sum(s.tp for s in per_entity.values())
    fp = sum(s.fp for s in per_entity.values())
    fn = sum(s.fn for s in per_entity.values())

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "per_entity_type": dict(per_entity),
        "per_hipaa_category": dict(per_hipaa),
        "per_detection_regime": dict(per_regime),
        "gap_spans": [],
        "matched_gold": matched_gold,
    }


def aggregate_strict_scores(record_scores: List[dict], tool_name: str) -> SpanScore:
    """Aggregate strict all-span TP/FP/FN counts."""
    score = SpanScore()
    for rs in record_scores:
        score = score.add(SpanScore(tp=rs["tp"], fp=rs["fp"], fn=rs["fn"]))
    return score


def aggregate_record_scores(
    record_scores: List[dict],
    tool_name: str,
    gap_entity_types: FrozenSet[str] = frozenset(),
    *,
    scoring_profile: str = "legacy_overlap_coverable",
    strict_record_scores: List[dict] | None = None,
) -> BenchmarkResult:
    """Aggregate per-record scores into a BenchmarkResult."""
    result = BenchmarkResult(
        tool_name=tool_name,
        gap_entity_types=set(gap_entity_types),
        scoring_profile=scoring_profile,
    )

    per_et: Dict[str, SpanScore] = defaultdict(SpanScore)
    per_hc: Dict[str, SpanScore] = defaultdict(SpanScore)
    per_dr: Dict[str, SpanScore] = defaultdict(SpanScore)
    agg = SpanScore()

    for rs in record_scores:
        result.total_records += 1
        agg = agg.add(SpanScore(tp=rs["tp"], fp=rs["fp"], fn=rs["fn"]))
        result.gap_span_count += len(rs.get("gap_spans", []))

        for et, s in rs["per_entity_type"].items():
            per_et[et] = per_et[et].add(s)
        for hc, s in rs["per_hipaa_category"].items():
            per_hc[hc] = per_hc[hc].add(s)
        for dr, s in rs["per_detection_regime"].items():
            per_dr[dr] = per_dr[dr].add(s)

    result.aggregate = agg
    result.per_entity_type = dict(per_et)
    result.per_hipaa_category = dict(per_hc)
    result.per_detection_regime = dict(per_dr)

    if scoring_profile == "strict_all_span":
        result.total_gold_spans = agg.tp + agg.fn
        result.coverable_span_count = max(result.total_gold_spans - result.gap_span_count, 0)
    else:
        result.coverable_span_count = agg.tp + agg.fn
        result.total_gold_spans = result.coverable_span_count + result.gap_span_count

    if strict_record_scores is not None:
        result.strict_all_span = aggregate_strict_scores(strict_record_scores, tool_name)

    return result


# ---------------------------------------------------------------------------
# Printing helpers
# ---------------------------------------------------------------------------

def print_report(result: BenchmarkResult, verbose: bool = False) -> None:
    """Print a human-readable benchmark report to stdout."""
    d = result.summary_dict()

    print(f"\n{'='*60}")
    print(f"BENCHMARK REPORT: {result.tool_name}")
    print(f"{'='*60}")
    print(f"Scoring profile   : {d['scoring_profile']}")
    print(f"Records evaluated : {d['total_records']}")
    print(f"Total gold spans  : {d['total_gold_spans']}")
    print(f"  Coverable       : {d['coverable_span_count']}")
    print(f"  Structural gaps : {d['gap_span_count']} ({d['gap_detection_rate']:.1%} of gold)")
    print(f"Predicted spans   : {d['total_predicted_spans']}")
    print()
    score_scope = "all spans" if d["scoring_profile"] == "strict_all_span" else "coverable spans only"
    print(f"AGGREGATE SCORES ({score_scope}):")
    print(f"  Precision : {d['aggregate_precision']:.4f}")
    print(f"  Recall    : {d['aggregate_recall']:.4f}")
    print(f"  F1        : {d['aggregate_f1']:.4f}")
    print(f"  Macro-F1  : {d['macro_f1']:.4f}")
    print(f"  Strict all-span F1 : {d['strict_all_span_f1']:.4f}")

    if verbose and d["per_hipaa_category"]:
        print(f"\nPER HIPAA CATEGORY (A-R):")
        from generators.common import HIPAA_CATEGORIES
        for cat in sorted(d["per_hipaa_category"]):
            s = d["per_hipaa_category"][cat]
            desc = HIPAA_CATEGORIES.get(cat, "")
            print(f"  ({cat}) {desc[:35]:<35} P={s['precision']:.3f} R={s['recall']:.3f} F1={s['f1']:.3f} "
                  f"TP={s['tp']} FP={s['fp']} FN={s['fn']}")

    if verbose and d["per_detection_regime"]:
        print(f"\nPER DETECTION REGIME:")
        for regime, s in sorted(d["per_detection_regime"].items()):
            print(f"  {regime:<35} P={s['precision']:.3f} R={s['recall']:.3f} F1={s['f1']:.3f}")

    if d["gap_entity_types"]:
        print(f"\nGAP ENTITY TYPES (tool cannot detect):")
        for et in sorted(d["gap_entity_types"]):
            print(f"  {et}")

    print(f"{'='*60}\n")
