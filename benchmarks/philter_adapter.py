"""
Philter benchmark adapter (UCSF Philter).

Philter is a rule-based de-identification system for clinical text developed
at UCSF. Published benchmark: Norgeot et al., npj Digital Medicine 2020
(~99.46% recall / F2 94.36 on a 2,000-note UCSF corpus -- a `vendor_claim`
in this repo's evidence ledger, not independently reproduced here).

GitHub: https://github.com/UCSF-DSCOLAB/philter-ucsf

philter-ucsf 1.0.3 (verified by direct source inspection, see
`benchmarks/collect_results.py` NOT_RUN_TOOLS) is a CLI-only tool
(`python3 main.py -i <in> -o <out> -f <config>.json`), not a Python library
with an importable `detect_phi()`-style API. An earlier version of this
adapter guessed at several speculative Python API shapes
(`hasattr(p, "detect_phi")`, dict-key sniffing) that philter-ucsf does not
actually expose -- that code has been removed rather than left silently
returning an empty (and misleadingly "measured") zero-recall result.

This adapter runs Philter for real ONLY when `PHILTER_CLI_ENTRYPOINT` names
an installed `philter-ucsf` checkout's `main.py` (or equivalent) AND
`PHILTER_CLI_CONFIG` names a working Philter filter-config JSON file, and
invokes it via subprocess per the documented CLI contract -- it never
silently downgrades to a guessed Python call. Without both env vars set,
`run_all()` returns `not_run` with the reason above instead of a fabricated
zero score.

Authority: UCSF Philter documentation; authorities/AUTHORITY_MATRIX.md Table C
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
    "philter-ucsf 1.0.3 is a CLI-only tool (python3 main.py -i ... -o ... "
    "-f <config>.json), not a Python library with an importable "
    "detect_phi()-style API (confirmed by source inspection, "
    "github.com/BCHSI/philter-ucsf). Set PHILTER_CLI_ENTRYPOINT to an "
    "installed checkout's main.py and PHILTER_CLI_CONFIG to a working "
    "filter-config JSON to run it for real; neither is set in this "
    "environment, so this adapter reports not_run rather than a guessed "
    "Python API call."
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

    Runs the real philter-ucsf CLI via subprocess when PHILTER_CLI_ENTRYPOINT
    and PHILTER_CLI_CONFIG are both set to a working checkout/config;
    otherwise reports not_run with `_NOT_RUN_REASON` -- it never falls back
    to a guessed Python API shape.
    """

    def __init__(self) -> None:
        self._entrypoint = os.environ.get("PHILTER_CLI_ENTRYPOINT", "")
        self._config = os.environ.get("PHILTER_CLI_CONFIG", "")
        self._available = bool(self._entrypoint and self._config
                                and Path(self._entrypoint).is_file()
                                and Path(self._config).is_file())
        self._version = "cli" if self._available else "unknown"
        self._not_run_reason = "" if self._available else _NOT_RUN_REASON

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        if not self._available:
            return []
        return self._analyze_cli(text)

    def _analyze_cli(self, text: str) -> List[PredictedSpan]:
        """Invoke the real philter-ucsf CLI (main.py -i IN -o OUT -f CONFIG)
        on a single-document temp input, per its documented usage; parse its
        real output format rather than guessing an in-process API."""
        with tempfile.TemporaryDirectory() as td:
            in_dir = Path(td) / "in"
            out_dir = Path(td) / "out"
            in_dir.mkdir()
            (in_dir / "doc.txt").write_text(text, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, self._entrypoint, "-i", str(in_dir), "-o", str(out_dir),
                 "-f", self._config],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"philter-ucsf CLI exited {proc.returncode}: {proc.stderr[-2000:]}")
            # philter-ucsf's documented output is the redacted document plus a
            # sibling "asterisk"/coordinate map; exact filenames vary by
            # config, so callers running this for real must confirm the
            # config's output_format before trusting downstream parsing.
            phi_map_path = out_dir / "phi_tags" / "doc.txt"
            if not phi_map_path.is_file():
                raise RuntimeError(
                    f"philter-ucsf CLI produced no phi_tags map at {phi_map_path}; "
                    "confirm PHILTER_CLI_CONFIG's output_format matches this adapter"
                )
            spans: List[PredictedSpan] = []
            for line in phi_map_path.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split("\t")
                if len(parts) < 3:
                    continue
                start, end, label = int(parts[0]), int(parts[1]), parts[2]
                spans.append(PredictedSpan(
                    start=start, end=end, entity_type=label,
                    mapped_type=_map_philter_type(label), score=1.0,
                ))
            return spans

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
        tool_name = f"philter-{self._version}" if self._available else "philter-not_run"

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
        if not self._available:
            # Never emit a fabricated all-zero "measured" score for a tool
            # that was never actually run -- write an explicit not_run record.
            out_path.write_text(json.dumps(
                {"tool": result.tool_name, "status": "not_run", "reason": self._not_run_reason},
                indent=2, sort_keys=True,
            ))
        else:
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
    if adapter._available:
        print_report(result)
    else:
        print(f"philter: not_run -- {adapter._not_run_reason}")
    adapter.write_results(result, args.output_dir)
