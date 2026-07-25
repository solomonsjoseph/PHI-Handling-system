"""Benchmark: score detectors against a gold-standard synthetic corpus.

Metrics: precision, recall, F1 (overall), per-HIPAA-category, per-detector.
Match rule: category equivalence AND span overlap >= 50 percent.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from .detectors import detect_text
from .models import BenchmarkResult, CorpusRecord, DetectedSpan, GoldSpan


_EQUIVALENCES: dict[str, set[str]] = {
    "NAME": {"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD", "PERSON"},
    "ADDRESS": {"ADDRESS_STREET", "ADDRESS_CITY", "ADDRESS_ZIP", "ZIP", "LOCATION"},
    "DATE": {"DATE_DOB", "DATE_ADMIT", "DATE_TIME"},
    "PHONE": {"PHONE_HOME", "PHONE_WORK", "PHONE_NUMBER"},
    "FAX": {"FAX"},
    "EMAIL": {"EMAIL", "EMAIL_ADDRESS"},
    "SSN": {"SSN", "US_SSN"},
    "MRN": {"MRN"},
    "HEALTH_PLAN_ID": {"MBI"},
    "ACCOUNT": {"ACCOUNT_NUMBER", "BANK_ACCOUNT", "US_BANK_NUMBER", "CREDIT_CARD", "IBAN_CODE"},
    "LICENSE": {"DRIVERS_LICENSE", "NPI", "US_DRIVER_LICENSE", "MEDICAL_LICENSE", "US_PASSPORT"},
    "VEHICLE": {"VIN", "LICENSE_PLATE"},
    "DEVICE": {"DEVICE_UDI", "DEVICE_SERIAL"},
    "URL": {"URL"},
    "IP_ADDRESS": {"IP_V4", "IP_V6", "IP_ADDRESS"},
    "BIOMETRIC": {"BIOMETRIC"},
    "PHOTO": {"PHOTO_FULL_FACE"},
    "OTHER_UNIQUE": {"CLINICAL_TRIAL_ID", "INTERNAL_CODE"},
    "QUASI": {"QUASI_PROFESSION", "QUASI_CITY", "QUASI_RARE_DISEASE"},
    "AGE": {"AGE_OVER_89"},
}


def _category_matches(gold: GoldSpan, det: DetectedSpan) -> bool:
    if gold.hipaa_category and det.hipaa_category and gold.hipaa_category == det.hipaa_category:
        return True
    peers = _EQUIVALENCES.get(gold.category, set()) | {gold.category, gold.entity_type}
    return det.entity_type in peers or det.entity_type == gold.entity_type


def _overlap_ratio(g: GoldSpan, d: DetectedSpan) -> float:
    o_start = max(g.start, d.start)
    o_end = min(g.end, d.end)
    inter = max(0, o_end - o_start)
    span_len = max(1, g.end - g.start)
    return inter / span_len


def score_record(record: CorpusRecord, detected: list[DetectedSpan]) -> tuple[int, int, int, Counter, Counter]:
    tp = 0
    matched_det_idx: set[int] = set()
    per_cat_tp: Counter = Counter()
    per_cat_fn: Counter = Counter()
    for gold in record.gold_spans:
        best = -1
        best_ratio = 0.0
        for i, det in enumerate(detected):
            if i in matched_det_idx:
                continue
            if not _category_matches(gold, det):
                continue
            r = _overlap_ratio(gold, det)
            if r >= 0.5 and r > best_ratio:
                best = i
                best_ratio = r
        if best >= 0:
            matched_det_idx.add(best)
            tp += 1
            per_cat_tp[gold.hipaa_category or gold.category] += 1
        else:
            per_cat_fn[gold.hipaa_category or gold.category] += 1
    fp = len(detected) - len(matched_det_idx)
    fn = len(record.gold_spans) - tp
    return tp, fp, fn, per_cat_tp, per_cat_fn


def run_benchmark(records: Iterable[CorpusRecord], corpus_id: str, detectors: list[str]) -> BenchmarkResult:
    total_tp = total_fp = total_fn = 0
    total_gold = 0
    per_cat_tp: Counter = Counter()
    per_cat_fp: Counter = Counter()
    per_cat_fn: Counter = Counter()
    per_det: dict[str, Counter] = defaultdict(Counter)
    n = 0
    for record in records:
        n += 1
        total_gold += len(record.gold_spans)
        det_spans = detect_text(record.text, detectors=detectors)
        tp, fp, fn, ptp, pfn = score_record(record, det_spans)
        total_tp += tp
        total_fp += fp
        total_fn += fn
        per_cat_tp.update(ptp)
        per_cat_fn.update(pfn)
        for d in det_spans:
            per_det[d.detector]["total"] += 1

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    per_category: dict[str, dict[str, float]] = {}
    all_cats = set(per_cat_tp) | set(per_cat_fn)
    for c in sorted(all_cats):
        ctp = per_cat_tp[c]
        cfn = per_cat_fn[c]
        crec = ctp / (ctp + cfn) if (ctp + cfn) else 0.0
        per_category[c or "unlabeled"] = {"tp": ctp, "fn": cfn, "recall": round(crec, 4)}

    return BenchmarkResult(
        corpus_id=corpus_id,
        detectors=detectors,
        total_records=n,
        total_gold_spans=total_gold,
        tp=total_tp,
        fp=total_fp,
        fn=total_fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        per_category=per_category,
        per_detector={k: dict(v) for k, v in per_det.items()},
    )
