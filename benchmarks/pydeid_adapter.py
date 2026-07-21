"""
PyDeID benchmark adapter.

PyDeID is referenced in de-identification literature as a Python-based PHI
de-identification library. This repo's own prior investigation
(`benchmarks/collect_results.py` NOT_RUN_TOOLS) established: the PyPI
package literally named `pydeid` (0.0.1) is an empty placeholder with no
importable submodules -- not the real academic tool. The real pyDeid
(GEMINI-Medicine/pyDeid on GitHub, a refactor of the PhysioNet Perl
de-identifier) has no PyPI release, and a git-installable build did not
produce an importable `pydeid` module in this environment.

GitHub: https://github.com/GEMINI-Medicine/pyDeid

An earlier version of this adapter guessed at several speculative Python
API shapes (`pydeid.annotators.Annotator().annotate()`,
`pydeid.deidentify()`, multiple dict-key fallbacks) that were never
confirmed against a real installation -- that code has been removed rather
than left silently returning an empty (and misleadingly "measured")
zero-recall result. This adapter always reports `not_run` with the reason
above until a future session confirms pyDeid's actual installed API
surface from a real, working install and can implement a non-speculative
call against it (or an official CLI, if one exists).

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

_NOT_RUN_REASON = (
    "The PyPI package literally named 'pydeid' (0.0.1) is an empty "
    "placeholder with no importable submodules -- not the real academic "
    "tool. The real pyDeid (GEMINI-Medicine/pyDeid on GitHub) has no PyPI "
    "release and a git-installable build did not produce an importable "
    "'pydeid' module in this environment, and this repo has no confirmed "
    "API/CLI contract for it to call against without guessing. Excluded "
    "rather than reporting a misleading 0%-recall row for a tool that was "
    "never actually exercised."
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
})


def _map_pydeid_type(label: str) -> str:
    ours = PYDEID_TO_CORPUS.get(label.upper())
    return next(iter(ours)) if ours else label


class PyDeIDAdapter:
    """Benchmark adapter for PyDeID.

    Always reports not_run (`_NOT_RUN_REASON`) in this repository -- there is
    no confirmed working install or documented API/CLI contract to call
    against without guessing (see module docstring). Never falls back to a
    speculative Python API shape.
    """

    def __init__(self) -> None:
        self._available = False
        self._version = "unknown"
        self._not_run_reason = _NOT_RUN_REASON

    def analyze_text(self, text: str) -> List[PredictedSpan]:
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
        result = BenchmarkResult(tool_name="pydeid-not_run")
        result.corpus_files = []
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "pydeid_benchmark_result.json"
        # Never emit a fabricated all-zero "measured" score for a tool that
        # was never actually run -- write an explicit not_run record.
        out_path.write_text(json.dumps(
            {"tool": result.tool_name, "status": "not_run", "reason": self._not_run_reason},
            indent=2, sort_keys=True,
        ))
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
    print(f"pydeid: not_run -- {adapter._not_run_reason}")
    adapter.write_results(result, args.output_dir)
