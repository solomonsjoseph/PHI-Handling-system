"""
CliniDeID benchmark adapter (VA Research CliniDeID).

CliniDeID is a HIPAA-compliant de-identification tool covering all 18 Safe Harbor
categories. Achieves approximately F1 0.84 on VA clinical notes.

Published benchmark: Deleger et al., JAMIA 2013; VA release documentation.
Download: https://www.clef-ehealth.org/task-1/ or VA Research release.

Install:
    # CliniDeID is distributed as a Java JAR by VA Research.
    # Set CLINIDEID_JAR=/path/to/clinideid.jar in your environment.
    # Java 11+ required.

When CliniDeID is not installed this adapter returns an empty BenchmarkResult
with tool_name "clinideid-not_installed" rather than raising an error.

Authority: HIPAA 45 CFR 164.514(b)(2)(i) all 18 categories;
           authorities/AUTHORITY_MATRIX.md Table C
"""
from __future__ import annotations

import json
import os
import re
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

# CliniDeID XML output tag -> our corpus entity types
CLINIDEID_TO_CORPUS = {
    "NAME":        frozenset({"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD"}),
    "DATE":        frozenset({"DATE_DOB", "DATE_ADMIT", "DATE_DISCHARGE",
                               "DATE_DEATH", "DATE_DEATH_OF_RELATIVE"}),
    "AGE":         frozenset({"AGE_OVER_89"}),
    "LOCATION":    frozenset({"ADDRESS_STREET", "ADDRESS_CITY", "ADDRESS_ZIP", "ADDRESS_STATE"}),
    "PHONE":       frozenset({"PHONE_HOME", "PHONE_WORK", "FAX"}),
    "FAX":         frozenset({"FAX"}),
    "ID":          frozenset({"SSN", "MRN", "HEALTH_PLAN_ID", "ACCOUNT_NUMBER",
                               "DRIVERS_LICENSE", "PASSPORT_US"}),
    "SSN":         frozenset({"SSN"}),
    "MRN":         frozenset({"MRN"}),
    "EMAIL":       frozenset({"EMAIL"}),
    "URL":         frozenset({"URL"}),
    "IP":          frozenset({"IP_V4", "IP_V6"}),
    "DEVICE":      frozenset({"DEVICE_UDI_GS1", "DEVICE_SERIAL"}),
    "BIOMETRIC":   frozenset({"BIOMETRIC_FINGERPRINT_TEMPLATE", "BIOMETRIC_DNA_SPECIMEN"}),
    "PHOTO":       frozenset({"PHOTO_FULL_FACE"}),
    "VEHICLE":     frozenset({"VIN", "LICENSE_PLATE"}),
    "CERTIFICATE": frozenset({"NPI", "MEDICAL_LICENSE_NUMBER", "DRIVERS_LICENSE"}),
    "HOSPITAL":    frozenset({"HOSPITAL_NAME"}),
}

CLINIDEID_GAP_ENTITY_TYPES = frozenset({
    "IN_AADHAAR", "IN_PAN", "ABHA_NUMBER", "ABHA_ADDRESS",
    "IN_UAN", "IN_ESI", "IN_DRIVING_LICENSE_STATE", "IN_RATION_CARD",
    "DPDPA_CUSTOMER_ID", "DPDPA_ENROLMENT_ID",
})


def _map_clinideid_type(label: str) -> str:
    ours = CLINIDEID_TO_CORPUS.get(label.upper())
    return next(iter(ours)) if ours else label


def _parse_clinideid_xml(xml_text: str, source_text: str) -> List[PredictedSpan]:
    """Parse CliniDeID XML output into PredictedSpan list.

    CliniDeID outputs text with PHI replaced or annotated in XML.
    The typical output format uses <PHI TYPE="NAME">...</PHI> tags.
    """
    spans = []
    pattern = re.compile(r'<PHI\s+TYPE="([^"]+)"[^>]*>(.*?)</PHI>', re.DOTALL)
    offset = 0
    stripped = xml_text

    for match in pattern.finditer(xml_text):
        label = match.group(1)
        phi_text = match.group(2)
        # Find the phi_text in the original source_text
        pos = source_text.find(phi_text, offset)
        if pos == -1:
            continue
        spans.append(PredictedSpan(
            start=pos,
            end=pos + len(phi_text),
            entity_type=label,
            mapped_type=_map_clinideid_type(label),
            score=1.0,
        ))
        offset = pos + len(phi_text)
    return spans


class CliniDeIDAdapter:
    """Benchmark adapter for VA Research CliniDeID (Java JAR).

    Checks for CLINIDEID_JAR environment variable pointing to the JAR file.
    Java 11+ and the JAR must be present for this adapter to be active.
    """

    def __init__(self) -> None:
        self._jar_path = os.environ.get("CLINIDEID_JAR", "")
        self._version = "unknown"
        self._available = bool(
            self._jar_path
            and Path(self._jar_path).is_file()
            and self._java_available()
        )

    def _java_available(self) -> bool:
        try:
            subprocess.run(["java", "-version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        if not self._available:
            return []
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
            tf.write(text)
            in_file = tf.name
        out_file = in_file + ".deid"
        try:
            subprocess.run(
                ["java", "-jar", self._jar_path, in_file, out_file],
                capture_output=True, timeout=30, check=False,
            )
            if not Path(out_file).exists():
                return []
            output = Path(out_file).read_text(encoding="utf-8", errors="replace")
            return _parse_clinideid_xml(output, text)
        except (subprocess.TimeoutExpired, Exception):
            return []
        finally:
            for f in (in_file, out_file):
                try:
                    Path(f).unlink()
                except FileNotFoundError:
                    pass

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
                    gap_entity_types=CLINIDEID_GAP_ENTITY_TYPES,
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
        tool_name = f"clinideid-{self._version}" if self._available else "clinideid-not_installed"

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
            gap_entity_types=CLINIDEID_GAP_ENTITY_TYPES,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "clinideid_benchmark_result.json"
        out_path.write_text(json.dumps(result.summary_dict(), indent=2, sort_keys=True))
        print(f"Results written to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CliniDeID benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument("--output-dir", type=Path,
                        default=_PROJECT_ROOT / "benchmarks" / "results" / "clinideid")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    adapter = CliniDeIDAdapter()
    result = adapter.run_all(args.corpus_dir, verbose=args.verbose)
    print_report(result)
    adapter.write_results(result, args.output_dir)
