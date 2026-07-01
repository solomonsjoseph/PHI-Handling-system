"""
PyDeID benchmark adapter.

PyDeID is a Python-based PHI de-identification library with balanced
precision and recall. Achieves approximately F1 ~87.9% on clinical text.

Published benchmark: PyDeID GitHub / PyPI documentation.
PyPI: https://pypi.org/project/pydeid/
GitHub: https://github.com/NLP4Science/PyDeID

Install:
    pip install pydeid

When PyDeID is not installed this adapter returns an empty BenchmarkResult
with tool_name "pydeid-not_installed" rather than raising an error.

Authority: HIPAA 45 CFR 164.514(b)(2)(i);
           authorities/AUTHORITY_MATRIX.md Table C
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmarks.metrics import (
    BenchmarkResult,
    GoldSpan,
    PredictedSpan,
    aggregate_record_scores,
    print_report,
    score_record,
)

# PyDeID annotation tag -> our corpus entity types
PYDEID_TO_CORPUS = {
    "NAME":        frozenset({"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD"}),
    "DOCTOR":      frozenset({"NAME_PROVIDER"}),
    "PATIENT":     frozenset({"NAME_PATIENT"}),
    "DATE":        frozenset({"DATE_DOB", "DATE_ADMIT", "DATE_DISCHARGE",
                               "DATE_DEATH", "DATE_DEATH_OF_RELATIVE"}),
    "AGE":         frozenset({"AGE_OVER_89"}),
    "LOCATION":    frozenset({"ADDRESS_STREET", "ADDRESS_CITY", "ADDRESS_ZIP",
                               "ADDRESS_STATE", "QUASI_CITY"}),
    "PHONE":       frozenset({"PHONE_HOME", "PHONE_WORK"}),
    "FAX":         frozenset({"FAX"}),
    "EMAIL":       frozenset({"EMAIL"}),
    "SSN":         frozenset({"SSN"}),
    "MEDICALRECORD": frozenset({"MRN"}),
    "ID":          frozenset({"MRN", "HEALTH_PLAN_ID", "ACCOUNT_NUMBER",
                               "DRIVERS_LICENSE", "NPI"}),
    "HOSPITAL":    frozenset({"HOSPITAL_NAME"}),
    "ORGANIZATION": frozenset({"PROVIDER_NAME", "HOSPITAL_NAME"}),
    "URL":         frozenset({"URL"}),
    "DEVICE":      frozenset({"DEVICE_SERIAL", "DEVICE_UDI_GS1"}),
    "BIOMETRIC":   frozenset({"BIOMETRIC_FINGERPRINT_TEMPLATE"}),
}

PYDEID_GAP_ENTITY_TYPES = frozenset({
    "VIN", "LICENSE_PLATE", "PHOTO_FULL_FACE", "CREDIT_CARD",
    "IN_AADHAAR", "IN_PAN", "ABHA_NUMBER", "ABHA_ADDRESS",
    "IN_UAN", "IN_ESI", "IN_CGHS", "IN_DRIVING_LICENSE_STATE",
    "DPDPA_CUSTOMER_ID", "DPDPA_ENROLMENT_ID",
})


def _map_pydeid_type(label: str) -> str:
    ours = PYDEID_TO_CORPUS.get(label.upper())
    return next(iter(ours)) if ours else label


class PyDeIDAdapter:
    """Benchmark adapter for PyDeID Python library.

    Tries 'from pydeid.annotators import Annotator' on init.
    Falls back gracefully if PyDeID is not installed.
    """

    def __init__(self) -> None:
        self._version = "unknown"
        try:
            import pydeid
            self._pydeid = pydeid
            self._available = True
            self._version = getattr(pydeid, "__version__", "unknown")
            # Pre-load the annotator if the API supports it
            if hasattr(pydeid, "annotators"):
                self._annotator = pydeid.annotators.Annotator()
            else:
                self._annotator = None
        except ImportError:
            self._pydeid = None
            self._annotator = None
            self._available = False

    def _run_pydeid(self, text: str) -> List[PredictedSpan]:
        """Call PyDeID on text; handles different API versions."""
        try:
            # Try annotator-based API (pydeid >= 1.0)
            if self._annotator is not None:
                result = self._annotator.annotate(text)
                spans = []
                annotations = (
                    result.get("annotations", []) if isinstance(result, dict) else []
                )
                for ann in annotations:
                    label = str(ann.get("type", ann.get("label", "PHI")))
                    start = int(ann.get("start", ann.get("begin", 0)))
                    end = int(ann.get("end", 0))
                    if end > start:
                        spans.append(PredictedSpan(
                            start=start, end=end,
                            entity_type=label,
                            mapped_type=_map_pydeid_type(label),
                            score=float(ann.get("score", 1.0)),
                        ))
                return spans
            # Try module-level function API
            if hasattr(self._pydeid, "deidentify"):
                anns = self._pydeid.deidentify(text)
                spans = []
                for ann in (anns if isinstance(anns, list) else []):
                    if isinstance(ann, dict):
                        label = str(ann.get("type", "PHI"))
                        start = int(ann.get("start", 0))
                        end = int(ann.get("end", 0))
                        if end > start:
                            spans.append(PredictedSpan(
                                start=start, end=end,
                                entity_type=label,
                                mapped_type=_map_pydeid_type(label),
                                score=1.0,
                            ))
                return spans
        except Exception:
            pass
        return []

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        if not self._available:
            return []
        return self._run_pydeid(text)

    def _run_file_impl(self, jsonl_path, strategy, overlap_threshold, entity_type_agnostic):
        record_scores = []
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                gold = [
                    GoldSpan(
                        start=s["start"], end=s["end"],
                        entity_type=s["entity_type"],
                        hipaa_category=s.get("hipaa_category"),
                        detection_regime=s.get("detection_regime", "contextual_ner_required"),
                        jurisdiction=s.get("jurisdiction", "us"),
                    )
                    for s in obj.get("gold_spans", [])
                ]
                predicted = self.analyze_text(obj["text"])
                rs = score_record(
                    predicted=predicted, gold=gold,
                    gap_entity_types=PYDEID_GAP_ENTITY_TYPES,
                    strategy=strategy, overlap_threshold=overlap_threshold,
                    entity_type_agnostic=entity_type_agnostic,
                )
                rs["record_id"] = obj["record_id"]
                rs["predicted_count"] = len(predicted)
                rs["gold_count"] = len(gold)
                record_scores.append(rs)
        return record_scores

    def run_file(self, jsonl_path, strategy="overlap", overlap_threshold=0.5,
                 entity_type_agnostic=True):
        return self._run_file_impl(jsonl_path, strategy, overlap_threshold, entity_type_agnostic)

    def run_all(
        self,
        corpus_dir: Path,
        pattern: str = "*.jsonl",
        strategy: str = "overlap",
        overlap_threshold: float = 0.5,
        entity_type_agnostic: bool = True,
        verbose: bool = False,
    ) -> BenchmarkResult:
        corpus_dir = Path(corpus_dir)
        tool_name = f"pydeid-{self._version}" if self._available else "pydeid-not_installed"

        if not self._available:
            result = BenchmarkResult(tool_name=tool_name)
            result.corpus_files = []
            return result

        all_scores: List[dict] = []
        total_predicted = 0
        files_processed = []

        for jsonl_path in sorted(corpus_dir.glob(pattern)):
            if verbose:
                print(f"  Processing {jsonl_path.name} ...", end=" ", flush=True)
            file_scores = self._run_file_impl(
                jsonl_path, strategy, overlap_threshold, entity_type_agnostic)
            all_scores.extend(file_scores)
            total_predicted += sum(rs["predicted_count"] for rs in file_scores)
            files_processed.append(str(jsonl_path.name))
            if verbose:
                tp = sum(rs["tp"] for rs in file_scores)
                print(f"{len(file_scores):3d} records  TP={tp}")

        result = aggregate_record_scores(
            all_scores, tool_name=tool_name,
            gap_entity_types=PYDEID_GAP_ENTITY_TYPES,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "pydeid_benchmark_result.json"
        out_path.write_text(json.dumps(result.summary_dict(), indent=2, sort_keys=True))
        print(f"Results written to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PyDeID benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument("--output-dir", type=Path,
                        default=_PROJECT_ROOT / "benchmarks" / "results" / "pydeid")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    adapter = PyDeIDAdapter()
    result = adapter.run_all(args.corpus_dir, verbose=args.verbose)
    print_report(result)
    adapter.write_results(result, args.output_dir)
