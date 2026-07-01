"""
spaCy benchmark adapter -- general NER lower bound.

Uses spaCy en_core_web_sm as a general-purpose NER baseline for PHI detection.
This is NOT a PHI-specific tool; it serves as the lower bound against which
clinical PHI tools are measured.

spaCy NER labels -> our corpus taxonomy mapping:
  PERSON  -> NAME_PATIENT, NAME_PROVIDER (both)
  DATE    -> DATE_DOB, DATE_ADMIT, DATE_DISCHARGE (all date types)
  GPE     -> ADDRESS_CITY, ADDRESS_STATE
  LOC     -> ADDRESS_STREET
  ORG     -> PROVIDER_NAME, HOSPITAL_NAME
  FAC     -> HOSPITAL_NAME (facility)

Published F1: general NER, not PHI-specific. Typically < 70% F1 on clinical text.
(No published clinical PHI benchmark for en_core_web_sm; serves as lower bound.)

Authority: spaCy en_core_web_sm NER documentation (spacy.io)
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

# spaCy label -> frozenset of our corpus entity types (many-to-many)
SPACY_TO_CORPUS = {
    "PERSON":   frozenset({"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD"}),
    "DATE":     frozenset({"DATE_DOB", "DATE_ADMIT", "DATE_DISCHARGE",
                            "DATE_DEATH", "DATE_DEATH_OF_RELATIVE"}),
    "TIME":     frozenset({"DATE_ADMIT", "DATE_DISCHARGE"}),
    "GPE":      frozenset({"ADDRESS_CITY", "ADDRESS_STATE", "QUASI_CITY"}),
    "LOC":      frozenset({"ADDRESS_STREET", "ADDRESS_CITY"}),
    "ORG":      frozenset({"PROVIDER_NAME", "HOSPITAL_NAME"}),
    "FAC":      frozenset({"HOSPITAL_NAME"}),
    "CARDINAL": frozenset({"SSN", "MRN", "PHONE_HOME", "PHONE_WORK",
                            "HEALTH_PLAN_ID", "ACCOUNT_NUMBER"}),
    "NORP":     frozenset({"NATIONALITY_RELIGIOUS_POLITICAL"}),
}

SPACY_COVERABLE = frozenset(et for ets in SPACY_TO_CORPUS.values() for et in ets)

SPACY_GAP_ENTITY_TYPES = frozenset({
    "EMAIL", "SSN", "FAX", "IP_V4", "IP_V6", "URL", "BIOMETRIC_FINGERPRINT_TEMPLATE",
    "BIOMETRIC_VOICE_TEMPLATE", "BIOMETRIC_IRIS_TEMPLATE", "BIOMETRIC_DNA_SPECIMEN",
    "DEVICE_UDI_GS1", "VIN", "LICENSE_PLATE", "IN_AADHAAR", "IN_PAN",
    "ABHA_NUMBER", "ABHA_ADDRESS", "IN_UAN", "IN_ESI", "IN_DRIVING_LICENSE_STATE",
})


def _map_spacy_type(label: str) -> str:
    ours = SPACY_TO_CORPUS.get(label)
    if not ours:
        return label
    return next(iter(ours))


class SpaCyAdapter:
    """Benchmark adapter for spaCy en_core_web_sm general NER.

    Requires spacy >= 3.8.0 (in requirements.txt) and the en_core_web_sm model.
    Install model: python -m spacy download en_core_web_sm
    """

    def __init__(self, model: str = "en_core_web_sm") -> None:
        self._model_name = model
        try:
            import spacy
            self._nlp = spacy.load(model)
            self._available = True
            self._version = spacy.__version__
        except (ImportError, OSError):
            self._nlp = None
            self._available = False
            self._version = "not_installed"

    def _require_spacy(self) -> None:
        if not self._available:
            raise ImportError(
                f"spaCy model {self._model_name!r} is not available. "
                f"Run: python -m spacy download {self._model_name}"
            )

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        self._require_spacy()
        doc = self._nlp(text)
        spans = []
        for ent in doc.ents:
            mapped = _map_spacy_type(ent.label_)
            spans.append(PredictedSpan(
                start=ent.start_char,
                end=ent.end_char,
                entity_type=ent.label_,
                mapped_type=mapped,
                score=1.0,
            ))
        return spans

    def run_file(
        self,
        jsonl_path: Path,
        strategy: str = "overlap",
        overlap_threshold: float = 0.5,
        entity_type_agnostic: bool = True,
    ) -> List[dict]:
        self._require_spacy()
        record_scores = []
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                text = obj["text"]
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
                predicted = self.analyze_text(text)
                rs = score_record(
                    predicted=predicted, gold=gold,
                    gap_entity_types=SPACY_GAP_ENTITY_TYPES,
                    strategy=strategy, overlap_threshold=overlap_threshold,
                    entity_type_agnostic=entity_type_agnostic,
                )
                rs["record_id"] = obj["record_id"]
                rs["predicted_count"] = len(predicted)
                rs["gold_count"] = len(gold)
                record_scores.append(rs)
        return record_scores

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
        tool_name = f"spacy-{self._model_name}-{self._version}"

        if not self._available:
            result = BenchmarkResult(tool_name=f"{tool_name}-not_installed")
            result.corpus_files = []
            return result

        all_scores: List[dict] = []
        total_predicted = 0
        files_processed = []

        for jsonl_path in sorted(corpus_dir.glob(pattern)):
            if verbose:
                print(f"  Processing {jsonl_path.name} ...", end=" ", flush=True)
            file_scores = self.run_file(
                jsonl_path, strategy=strategy,
                overlap_threshold=overlap_threshold,
                entity_type_agnostic=entity_type_agnostic,
            )
            all_scores.extend(file_scores)
            total_predicted += sum(rs["predicted_count"] for rs in file_scores)
            files_processed.append(str(jsonl_path.name))
            if verbose:
                tp = sum(rs["tp"] for rs in file_scores)
                fp = sum(rs["fp"] for rs in file_scores)
                fn = sum(rs["fn"] for rs in file_scores)
                print(f"{len(file_scores):3d} records  TP={tp} FP={fp} FN={fn}")

        result = aggregate_record_scores(
            all_scores, tool_name=tool_name,
            gap_entity_types=SPACY_GAP_ENTITY_TYPES,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "spacy_benchmark_result.json"
        out_path.write_text(json.dumps(result.summary_dict(), indent=2, sort_keys=True))
        print(f"Results written to {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="spaCy NER benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument("--output-dir", type=Path, default=_PROJECT_ROOT / "benchmarks" / "results" / "spacy")
    parser.add_argument("--strategy", choices=["exact", "overlap"], default="overlap")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    adapter = SpaCyAdapter()
    result = adapter.run_all(args.corpus_dir, strategy=args.strategy, verbose=args.verbose)
    print_report(result)
    adapter.write_results(result, args.output_dir)
