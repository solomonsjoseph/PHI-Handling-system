"""
PhysioNet deid benchmark adapter (MIT deid / i2b2 Perl-based de-identifier).

The PhysioNet deid tool (also called MIT deid or safe-harbor scrubber) is the
classic Perl-based PHI de-identifier originally developed at MIT and distributed
through PhysioNet. Achieves approximately 85% recall and variable precision
on clinical discharge summaries.

Published benchmark: Neamatullah et al., BMC Med Inform Decis Mak 2008.
PhysioNet: https://physionet.org/content/deid/1.1/

Install:
    # 1. Obtain a PhysioNet credentialed access account (free but requires DUA)
    # 2. Download deid package from https://physionet.org/content/deid/1.1/
    # 3. Set PHYSIONET_DEID_DIR=/path/to/deid in your environment
    # Perl 5.10+ required.

When deid is not installed this adapter returns an empty BenchmarkResult
with tool_name "physionet-deid-not_installed" rather than raising an error.

Authority: HIPAA 45 CFR 164.514(b); Neamatullah et al. 2008;
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

# PhysioNet deid tag -> our corpus entity types
PHYSIONET_TO_CORPUS = {
    "NAME":        frozenset({"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD"}),
    "DOCTOR":      frozenset({"NAME_PROVIDER"}),
    "PATIENT":     frozenset({"NAME_PATIENT"}),
    "DATE":        frozenset({"DATE_DOB", "DATE_ADMIT", "DATE_DISCHARGE",
                               "DATE_DEATH", "DATE_DEATH_OF_RELATIVE"}),
    "AGE":         frozenset({"AGE_OVER_89"}),
    "LOCATION":    frozenset({"ADDRESS_STREET", "ADDRESS_CITY", "ADDRESS_ZIP", "ADDRESS_STATE"}),
    "PHONE":       frozenset({"PHONE_HOME", "PHONE_WORK", "FAX"}),
    "ID":          frozenset({"SSN", "MRN", "HEALTH_PLAN_ID", "ACCOUNT_NUMBER",
                               "DRIVERS_LICENSE", "NPI"}),
    "HOSPITAL":    frozenset({"HOSPITAL_NAME"}),
    "EMAIL":       frozenset({"EMAIL"}),
    "URL":         frozenset({"URL"}),
    "USERNAME":    frozenset({"EMAIL"}),
    "PROFESSION":  frozenset({"QUASI_PROFESSION"}),
    "ORGANIZATION": frozenset({"PROVIDER_NAME", "HOSPITAL_NAME"}),
}

PHYSIONET_GAP_ENTITY_TYPES = frozenset({
    "BIOMETRIC_FINGERPRINT_TEMPLATE", "BIOMETRIC_VOICE_TEMPLATE",
    "BIOMETRIC_IRIS_TEMPLATE", "BIOMETRIC_DNA_SPECIMEN",
    "DEVICE_UDI_GS1", "DEVICE_SERIAL", "VIN", "LICENSE_PLATE",
    "PHOTO_FULL_FACE", "CREDIT_CARD",
    "IN_AADHAAR", "IN_PAN", "ABHA_NUMBER", "ABHA_ADDRESS",
    "IN_UAN", "IN_ESI", "IN_CGHS", "IN_DRIVING_LICENSE_STATE",
    "DPDPA_CUSTOMER_ID", "DPDPA_ENROLMENT_ID",
})


def _map_physionet_type(label: str) -> str:
    ours = PHYSIONET_TO_CORPUS.get(label.upper())
    return next(iter(ours)) if ours else label


def _perl_available() -> bool:
    try:
        subprocess.run(["perl", "-e", "1"], capture_output=True, check=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _parse_physionet_output(redacted: str, original: str) -> List[PredictedSpan]:
    """Map deid redacted output back to spans in the original text.

    deid replaces PHI with [** TYPE **]. We locate these placeholders and
    map them back to approximate positions in the original using alignment.

    Because deid replaces content, exact offsets must be reconstructed
    by aligning before/after text. This is approximate for long documents.
    """
    spans = []
    placeholder_pattern = re.compile(r'\[\*\*\s*([A-Za-z][A-Za-z0-9\s/]*?)\s*\*\*\]')

    # Walk through original and redacted in lockstep up to the first mismatch
    orig_pos = 0
    red_pos = 0

    while red_pos < len(redacted):
        m = placeholder_pattern.search(redacted, red_pos)
        if not m:
            break
        # Text before placeholder is unchanged -- advance orig_pos by the same amount
        prefix_len = m.start() - red_pos
        orig_pos += prefix_len
        red_pos = m.start()

        label_raw = m.group(1).strip()
        # Determine how many chars in the original were replaced
        # We scan forward in original to find a boundary (heuristic)
        # For precision, the original PHI ends at the next whitespace-delimited token
        # that doesn't appear in the redacted text.
        # Simplification: use placeholder length as proxy for span end
        # This is best-effort; exact alignment requires deid --offset output mode.
        end_est = orig_pos + max(len(label_raw), 4)  # minimum 4 chars replaced
        end_est = min(end_est, len(original))

        label = label_raw.split("/")[0].strip().upper()
        spans.append(PredictedSpan(
            start=orig_pos,
            end=end_est,
            entity_type=label_raw,
            mapped_type=_map_physionet_type(label),
            score=1.0,
        ))
        red_pos = m.end()
        orig_pos = end_est

    return spans


class PhysioNetDeIDAdapter:
    """Benchmark adapter for PhysioNet deid (Perl-based MIT de-identifier).

    Checks PHYSIONET_DEID_DIR env var for the deid installation directory.
    Perl 5.10+ must be available on PATH.
    """

    def __init__(self) -> None:
        self._deid_dir = os.environ.get("PHYSIONET_DEID_DIR", "")
        self._version = "1.1"  # fixed version from PhysioNet download
        deid_script = Path(self._deid_dir) / "deid.pl" if self._deid_dir else None
        self._available = bool(
            deid_script and deid_script.is_file() and _perl_available()
        )
        self._deid_script = str(deid_script) if self._available else ""

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        if not self._available:
            return []
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(text)
            in_file = tf.name

        out_file = in_file + ".res"
        try:
            subprocess.run(
                ["perl", self._deid_script, in_file, out_file],
                cwd=self._deid_dir,
                capture_output=True, timeout=30, check=False,
            )
            if not Path(out_file).exists():
                return []
            redacted = Path(out_file).read_text(encoding="utf-8", errors="replace")
            return _parse_physionet_output(redacted, text)
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
                    gap_entity_types=PHYSIONET_GAP_ENTITY_TYPES,
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
        tool_name = (f"physionet-deid-{self._version}"
                     if self._available else "physionet-deid-not_installed")

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
            gap_entity_types=PHYSIONET_GAP_ENTITY_TYPES,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "physionet_benchmark_result.json"
        out_path.write_text(json.dumps(result.summary_dict(), indent=2, sort_keys=True))
        print(f"Results written to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PhysioNet deid benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument("--output-dir", type=Path,
                        default=_PROJECT_ROOT / "benchmarks" / "results" / "physionet")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    adapter = PhysioNetDeIDAdapter()
    result = adapter.run_all(args.corpus_dir, verbose=args.verbose)
    print_report(result)
    adapter.write_results(result, args.output_dir)
