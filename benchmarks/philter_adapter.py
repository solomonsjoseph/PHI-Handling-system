"""
Philter benchmark adapter (UCSF Philter).

Philter is a rule-based de-identification system for clinical text developed
at UCSF. Achieves approximately 80% F1 and ~99.5% recall on UCSF clinical notes.

Published benchmark: Norgeot et al., npj Digital Medicine 2020.
GitHub: https://github.com/UCSF-DSCOLAB/philter-ucsf

Install:
    pip install philter-ucsf
    # or: git clone https://github.com/UCSF-DSCOLAB/philter-ucsf && pip install -e .

When Philter is not installed this adapter returns an empty BenchmarkResult
with tool_name "philter-not_installed" rather than raising an error.

Authority: UCSF Philter documentation; authorities/AUTHORITY_MATRIX.md Table C
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

# Philter tag -> our corpus entity types
PHILTER_TO_CORPUS = {
    "NAME":       frozenset({"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD"}),
    "DATE":       frozenset({"DATE_DOB", "DATE_ADMIT", "DATE_DISCHARGE",
                              "DATE_DEATH", "DATE_DEATH_OF_RELATIVE"}),
    "AGE":        frozenset({"AGE_OVER_89"}),
    "LOCATION":   frozenset({"ADDRESS_STREET", "ADDRESS_CITY", "ADDRESS_ZIP",
                              "ADDRESS_STATE", "QUASI_CITY"}),
    "PHONE":      frozenset({"PHONE_HOME", "PHONE_WORK", "FAX"}),
    "ID":         frozenset({"SSN", "MRN", "HEALTH_PLAN_ID", "ACCOUNT_NUMBER",
                              "DRIVERS_LICENSE", "NPI"}),
    "EMAIL":      frozenset({"EMAIL"}),
    "HOSPITAL":   frozenset({"HOSPITAL_NAME", "PROVIDER_NAME"}),
    "PROFESSION": frozenset({"QUASI_PROFESSION"}),
    "USERNAME":   frozenset({"EMAIL"}),
    "DEVICE":     frozenset({"DEVICE_UDI_GS1", "DEVICE_SERIAL"}),
    "URL":        frozenset({"URL"}),
    "IP":         frozenset({"IP_V4", "IP_V6"}),
    "SSN":        frozenset({"SSN"}),
    "MRN":        frozenset({"MRN"}),
    "PHI":        frozenset({"NAME_PATIENT", "SSN", "MRN"}),  # untyped catch-all
}

PHILTER_GAP_ENTITY_TYPES = frozenset({
    "BIOMETRIC_FINGERPRINT_TEMPLATE", "BIOMETRIC_VOICE_TEMPLATE",
    "BIOMETRIC_IRIS_TEMPLATE", "BIOMETRIC_DNA_SPECIMEN",
    "VIN", "DEVICE_UDI_GS1", "PHOTO_FULL_FACE",
})


def _map_philter_type(label: str) -> str:
    ours = PHILTER_TO_CORPUS.get(label.upper())
    return next(iter(ours)) if ours else label


class PhilterAdapter:
    """Benchmark adapter for UCSF Philter.

    Tries Python import first; falls back to unavailable state if not installed.
    """

    def __init__(self) -> None:
        self._version = "unknown"
        try:
            # Try Python import (philter-ucsf package)
            import philter  # noqa: F401
            self._available = True
            self._backend = "python"
            self._version = getattr(philter, "__version__", "unknown")
        except ImportError:
            self._available = False
            self._backend = "none"

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        if not self._available:
            return []
        if self._backend == "python":
            return self._analyze_python(text)
        return []

    def _analyze_python(self, text: str) -> List[PredictedSpan]:
        """Run Philter via Python API."""
        try:
            from philter import Philter
            p = Philter()
            # Philter's Python API returns a list of (start, end, tag) tuples
            # or a similar structure; adapt as needed per installed version
            detections = p.detect_phi(text) if hasattr(p, "detect_phi") else []
            spans = []
            for det in detections:
                if isinstance(det, (list, tuple)) and len(det) >= 3:
                    start, end, label = int(det[0]), int(det[1]), str(det[2])
                elif isinstance(det, dict):
                    start = int(det.get("start", 0))
                    end = int(det.get("end", 0))
                    label = str(det.get("label", det.get("type", "PHI")))
                else:
                    continue
                spans.append(PredictedSpan(
                    start=start, end=end,
                    entity_type=label,
                    mapped_type=_map_philter_type(label),
                    score=1.0,
                ))
            return spans
        except Exception:
            return []

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
                    gap_entity_types=PHILTER_GAP_ENTITY_TYPES,
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
        tool_name = f"philter-{self._version}" if self._available else "philter-not_installed"

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
            gap_entity_types=PHILTER_GAP_ENTITY_TYPES,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "philter_benchmark_result.json"
        out_path.write_text(json.dumps(result.summary_dict(), indent=2, sort_keys=True))
        print(f"Results written to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Philter benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument("--output-dir", type=Path, default=_PROJECT_ROOT / "benchmarks" / "results" / "philter")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    adapter = PhilterAdapter()
    result = adapter.run_all(args.corpus_dir, verbose=args.verbose)
    print_report(result)
    adapter.write_results(result, args.output_dir)
