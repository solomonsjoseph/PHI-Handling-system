"""
Tests for benchmarks/metrics.py and benchmarks/presidio_adapter.py.

Validates:
  1. Exact-match scoring produces correct TP/FP/FN
  2. Overlap scoring matches partial spans
  3. Gap spans are excluded from FN count
  4. Per-entity-type, per-HIPAA-category, per-detection-regime breakdowns
  5. BenchmarkResult summary_dict keys are present and within range
  6. Presidio entity type mapping is self-consistent (no empty mapped sets)
  7. Presidio gap list does not overlap with coverable set
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.metrics import (
    GoldSpan,
    PredictedSpan,
    SpanScore,
    aggregate_record_scores,
    score_record,
)
from benchmarks.presidio_adapter import (
    PRESIDIO_COVERABLE,
    PRESIDIO_GAP_ENTITY_TYPES,
    PRESIDIO_TO_CORPUS,
)


# ---------------------------------------------------------------------------
# SpanScore
# ---------------------------------------------------------------------------

class TestSpanScore:

    def test_precision_zero_denom(self):
        s = SpanScore(tp=0, fp=0, fn=0)
        assert s.precision == 0.0

    def test_recall_zero_denom(self):
        s = SpanScore(tp=0, fp=0, fn=0)
        assert s.recall == 0.0

    def test_f1_zero_denom(self):
        s = SpanScore(tp=0, fp=0, fn=0)
        assert s.f1 == 0.0

    def test_perfect_score(self):
        s = SpanScore(tp=10, fp=0, fn=0)
        assert s.precision == 1.0
        assert s.recall == 1.0
        assert s.f1 == 1.0

    def test_no_recall(self):
        s = SpanScore(tp=0, fp=0, fn=10)
        assert s.precision == 0.0
        assert s.recall == 0.0
        assert s.f1 == 0.0

    def test_add(self):
        a = SpanScore(tp=3, fp=1, fn=2)
        b = SpanScore(tp=2, fp=0, fn=1)
        c = a.add(b)
        assert c.tp == 5
        assert c.fp == 1
        assert c.fn == 3


# ---------------------------------------------------------------------------
# score_record -- exact match
# ---------------------------------------------------------------------------

class TestScoreRecordExact:

    def _make_gold(self, start, end, et, hc=None, regime="contextual_ner_required"):
        return GoldSpan(start=start, end=end, entity_type=et,
                        hipaa_category=hc, detection_regime=regime)

    def _make_pred(self, start, end, et="PRED"):
        return PredictedSpan(start=start, end=end, entity_type=et, mapped_type=et)

    def test_perfect_match(self):
        gold = [self._make_gold(0, 10, "SSN", "G")]
        pred = [self._make_pred(0, 10, "SSN")]
        rs = score_record(pred, gold, strategy="exact")
        assert rs["tp"] == 1
        assert rs["fp"] == 0
        assert rs["fn"] == 0

    def test_false_negative(self):
        gold = [self._make_gold(0, 10, "SSN", "G")]
        pred = []
        rs = score_record(pred, gold, strategy="exact")
        assert rs["tp"] == 0
        assert rs["fn"] == 1
        assert rs["fp"] == 0

    def test_false_positive(self):
        gold = []
        pred = [self._make_pred(0, 10)]
        rs = score_record(pred, gold, strategy="exact")
        assert rs["fp"] == 1
        assert rs["tp"] == 0
        assert rs["fn"] == 0

    def test_off_by_one_is_miss(self):
        gold = [self._make_gold(0, 10, "SSN", "G")]
        pred = [self._make_pred(0, 9)]    # one char short
        rs = score_record(pred, gold, strategy="exact")
        assert rs["tp"] == 0
        assert rs["fn"] == 1
        assert rs["fp"] == 1

    def test_gap_span_excluded_from_fn(self):
        gold = [
            self._make_gold(0, 10, "VIN", "L"),           # gap
            self._make_gold(20, 30, "SSN", "G"),           # coverable
        ]
        pred = [self._make_pred(20, 30, "SSN")]            # matches SSN only
        rs = score_record(pred, gold, gap_entity_types=frozenset({"VIN"}), strategy="exact")
        assert rs["tp"] == 1
        assert rs["fn"] == 0   # VIN not counted as FN
        assert rs["fp"] == 0
        assert len(rs["gap_spans"]) == 1
        assert rs["gap_spans"][0].entity_type == "VIN"

    def test_per_hipaa_category_populated(self):
        gold = [self._make_gold(0, 10, "SSN", "G", "rule_applicable")]
        pred = [self._make_pred(0, 10, "SSN")]
        rs = score_record(pred, gold, strategy="exact")
        assert "G" in rs["per_hipaa_category"]
        assert rs["per_hipaa_category"]["G"].tp == 1

    def test_per_detection_regime_populated(self):
        gold = [self._make_gold(0, 10, "SSN", "G", "rule_applicable")]
        pred = [self._make_pred(0, 10)]
        rs = score_record(pred, gold, strategy="exact")
        assert "rule_applicable" in rs["per_detection_regime"]

    def test_multiple_spans_multiple_matches(self):
        gold = [
            self._make_gold(0, 5, "SSN", "G"),
            self._make_gold(10, 20, "EMAIL", "F"),
        ]
        pred = [
            self._make_pred(0, 5),
            self._make_pred(10, 20),
        ]
        rs = score_record(pred, gold, strategy="exact")
        assert rs["tp"] == 2
        assert rs["fp"] == 0
        assert rs["fn"] == 0

    def test_no_double_count(self):
        """One prediction cannot match two gold spans."""
        gold = [
            self._make_gold(0, 10, "SSN", "G"),
            self._make_gold(0, 10, "MRN", "H"),
        ]
        pred = [self._make_pred(0, 10)]
        rs = score_record(pred, gold, strategy="exact")
        # Only one match possible; one gold span is FN
        assert rs["tp"] == 1
        assert rs["fn"] == 1


# ---------------------------------------------------------------------------
# score_record -- overlap match
# ---------------------------------------------------------------------------

class TestScoreRecordOverlap:

    def _make_gold(self, start, end, et="SSN", hc="G"):
        return GoldSpan(start=start, end=end, entity_type=et, hipaa_category=hc)

    def _make_pred(self, start, end, et="SSN"):
        return PredictedSpan(start=start, end=end, entity_type=et, mapped_type=et)

    def test_partial_overlap_above_threshold(self):
        # pred covers 8/10 chars of gold → 80% overlap → TP
        gold = [self._make_gold(0, 10)]
        pred = [self._make_pred(0, 8)]
        rs = score_record(pred, gold, strategy="overlap", overlap_threshold=0.5)
        assert rs["tp"] == 1

    def test_partial_overlap_below_threshold(self):
        # pred covers 4/10 chars → 40% → miss
        gold = [self._make_gold(0, 10)]
        pred = [self._make_pred(0, 4)]
        rs = score_record(pred, gold, strategy="overlap", overlap_threshold=0.5)
        assert rs["tp"] == 0
        assert rs["fn"] == 1

    def test_no_overlap(self):
        gold = [self._make_gold(0, 10)]
        pred = [self._make_pred(20, 30)]
        rs = score_record(pred, gold, strategy="overlap", overlap_threshold=0.5)
        assert rs["tp"] == 0
        assert rs["fn"] == 1
        assert rs["fp"] == 1


# ---------------------------------------------------------------------------
# aggregate_record_scores
# ---------------------------------------------------------------------------

class TestAggregateRecordScores:

    def test_aggregation(self):
        scores = [
            {"tp": 3, "fp": 1, "fn": 0,
             "per_entity_type": {"SSN": SpanScore(tp=3, fp=1, fn=0)},
             "per_hipaa_category": {"G": SpanScore(tp=3)},
             "per_detection_regime": {"rule_applicable": SpanScore(tp=3)},
             "gap_spans": []},
            {"tp": 2, "fp": 0, "fn": 1,
             "per_entity_type": {"EMAIL": SpanScore(tp=2, fn=1)},
             "per_hipaa_category": {"F": SpanScore(tp=2, fn=1)},
             "per_detection_regime": {"rule_applicable": SpanScore(tp=2, fn=1)},
             "gap_spans": []},
        ]
        result = aggregate_record_scores(scores, tool_name="test")
        assert result.total_records == 2
        assert result.aggregate.tp == 5
        assert result.aggregate.fp == 1
        assert result.aggregate.fn == 1
        assert "SSN" in result.per_entity_type
        assert "EMAIL" in result.per_entity_type

    def test_summary_dict_keys(self):
        scores = [
            {"tp": 1, "fp": 0, "fn": 0,
             "per_entity_type": {"SSN": SpanScore(tp=1)},
             "per_hipaa_category": {"G": SpanScore(tp=1)},
             "per_detection_regime": {"rule_applicable": SpanScore(tp=1)},
             "gap_spans": []},
        ]
        result = aggregate_record_scores(scores, tool_name="test")
        d = result.summary_dict()
        required_keys = {
            "tool", "total_records", "total_gold_spans", "total_predicted_spans",
            "aggregate_precision", "aggregate_recall", "aggregate_f1", "macro_f1",
            "gap_span_count", "gap_detection_rate", "coverable_span_count",
            "per_entity_type", "per_hipaa_category", "per_detection_regime",
            "gap_entity_types",
        }
        assert required_keys.issubset(d.keys()), (
            f"Missing keys: {required_keys - d.keys()}"
        )

    def test_gap_detection_rate(self):
        scores = [
            {"tp": 0, "fp": 0, "fn": 0,
             "per_entity_type": {},
             "per_hipaa_category": {},
             "per_detection_regime": {},
             "gap_spans": [GoldSpan(0, 10, "VIN"), GoldSpan(20, 30, "BIOMETRIC_FINGERPRINT_TEMPLATE")]},
        ]
        result = aggregate_record_scores(scores, tool_name="test",
                                          gap_entity_types=frozenset({"VIN", "BIOMETRIC_FINGERPRINT_TEMPLATE"}))
        assert result.gap_span_count == 2
        assert result.total_gold_spans == 2
        assert result.gap_detection_rate == 1.0


# ---------------------------------------------------------------------------
# Presidio entity type mapping self-consistency
# ---------------------------------------------------------------------------

class TestPresidioMapping:

    def test_all_mapped_sets_nonempty(self):
        for presidio_type, corpus_types in PRESIDIO_TO_CORPUS.items():
            assert len(corpus_types) > 0, (
                f"PRESIDIO_TO_CORPUS['{presidio_type}'] is empty"
            )

    def test_gap_and_coverable_disjoint(self):
        """No entity type should be both coverable and in the gap set."""
        overlap = PRESIDIO_COVERABLE & PRESIDIO_GAP_ENTITY_TYPES
        # FAX types are intentionally in COVERABLE (Presidio may detect them as PHONE)
        # We allow that overlap by design -- Presidio detects the number but mislabels it.
        # All other types must be disjoint.
        non_fax_overlap = {t for t in overlap if not t.startswith("FAX")}
        assert not non_fax_overlap, (
            f"Non-FAX entity types in both coverable and gap sets: {non_fax_overlap}"
        )

    def test_vin_coverable(self):
        # VIN now covered by custom US_VIN PatternRecognizer added in PresidioAdapter.__init__.
        # No longer a structural gap; moved to PRESIDIO_TO_CORPUS["US_VIN"].
        assert "VIN" in PRESIDIO_COVERABLE

    def test_biometric_types_in_gap(self):
        for bt in ["BIOMETRIC_FINGERPRINT_TEMPLATE", "BIOMETRIC_VOICE_TEMPLATE",
                   "BIOMETRIC_IRIS_TEMPLATE", "BIOMETRIC_DNA_SPECIMEN"]:
            assert bt in PRESIDIO_GAP_ENTITY_TYPES, f"{bt} should be in gap set"

    def test_device_types_in_gap(self):
        for dt in ["DEVICE_UDI_GS1", "DEVICE_UDI_HIBCC", "DEVICE_UDI_ICCBBA", "DEVICE_SERIAL"]:
            assert dt in PRESIDIO_GAP_ENTITY_TYPES, f"{dt} should be in gap set"

    def test_mrn_in_gap(self):
        assert "MRN" in PRESIDIO_GAP_ENTITY_TYPES

    def test_ssn_is_coverable(self):
        assert "SSN" in PRESIDIO_COVERABLE

    def test_email_is_coverable(self):
        assert "EMAIL" in PRESIDIO_COVERABLE

    def test_url_is_coverable(self):
        assert "URL" in PRESIDIO_COVERABLE
