"""
phi_engine benchmark adapter -- our own detection surface as an evaluated tool.

Scores phi_engine's structured/regex PHI-detection surface
(``phi_engine.security.presidio_gate.analyze_text`` -- the BLOCKING_PATTERNS
catalog wrapped as Presidio PatternRecognizers, with Verhoeff/Indian-phone
checksum validation -- OR-combined with a direct scan of
``phi_engine.security.phi_patterns.WARN_PATTERNS``, the lower-confidence
heuristics ``presidio_gate`` does not wire in) against the same JSONL corpus
files and ``benchmarks.metrics`` scoring pipeline as every other adapter in
this package, so it can be compared on identical terms.

Per repo Claim Discipline (CLAUDE.md Truth Protocol): this is a REGEX/pattern
surface, not a free-text NER competitor -- see docs/SOTA_COMPARISON.md for the
positioning argument. This benchmark measures the structured-identifier
detection surface honestly, including its gaps, not a claim of SOTA free-text
performance.

Pattern-name -> corpus entity-type mapping (verified empirically against the
actual seed-42 corpus's gold_spans entity_type strings, NOT assumed from
Presidio's own predefined-recognizer naming convention, which this repo's
corpus does not follow for several India-specific types -- see
benchmarks/presidio_adapter.py's PRESIDIO_TO_CORPUS for a mapping that
predates this verification and is left as-is, out of scope here).
Unmapped pattern names (WARN_PATTERNS' NUMERIC_ID_SHORT is too generic to map
confidently) fall through to "UNKNOWN" and count as false positives under
strict scoring -- the honest default per the evidence plan.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, FrozenSet, List

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
    score_record_strict_all_span,
)

# ---------------------------------------------------------------------------
# phi_patterns pattern name -> our corpus entity types (many-to-many)
# ---------------------------------------------------------------------------

_DATE_TYPES = frozenset({
    "DATE", "DATE_ADMIT", "DATE_DOB", "DATE_DOB_YEAR_ONLY", "DATE_OF_BIRTH",
    "DATE_SERVICE",
})
_NAME_TYPES = frozenset({
    "NAME", "NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD", "NAME_REQUESTOR",
    "NAME_AMBIGUOUS", "NAME_PATIENT_FIRST", "NAME_PATIENT_LAST", "PATIENT_NAME",
    "PERSON_NAME",
})
_PHONE_TYPES = frozenset({"PHONE", "PHONE_HOME", "PHONE_WORK", "PHONE_REQUESTOR"})

PHI_ENGINE_TO_CORPUS: Dict[str, FrozenSet[str]] = {
    # ── BLOCKING_PATTERNS (via presidio_gate.analyze_text; Verhoeff/phone
    #    checksum-validated so these are structured, not just shape matches) ──
    "AADHAAR": frozenset({"AADHAAR"}),
    "PAN": frozenset({"PAN"}),
    "INDIAN_VOTER_ID": frozenset({"VOTER_ID_EPIC"}),
    "INDIAN_DL": frozenset({"DRIVING_LICENSE_IN"}),
    "INDIAN_PASSPORT": frozenset({"IN_PASSPORT"}),
    "INDIAN_PHONE": frozenset({"MOBILE_IN"}),
    "EMAIL": frozenset({"EMAIL"}),
    "URL": frozenset({"URL"}),
    "SSN": frozenset({"SSN"}),
    "SSN_UNHYPHENATED": frozenset({"SSN"}),
    "MRN": frozenset({"MRN"}),
    "MRN_LABELED": frozenset({"MRN"}),
    "IP": frozenset({"IP_ADDRESS", "IP_V4"}),
    "US_PHONE": _PHONE_TYPES,
    "AGE_OVER_89": frozenset({"AGE_OVER_89"}),
    "ADDRESS": frozenset({"ADDRESS_STREET"}),
    "DATE_ISO": _DATE_TYPES,
    "DATE_TEXT": _DATE_TYPES,
    "PERSON_NAME_PREFIX": _NAME_TYPES,
    # ── WARN_PATTERNS (direct regex scan; presidio_gate does not wire these
    #    in -- see phi_engine/security/presidio_gate.py::_build_analyzer) ──
    # NUMERIC_ID_SHORT deliberately unmapped: a bare 6-7 digit run could be
    # MRN, ACCESSION_NUMBER, ACCOUNT_NUMBER, CASE_NUMBER, INTERNAL_CODE,
    # BADGE_NUMBER, ... -- too ambiguous to credit against any one gold type.
    "DATE_MDY": _DATE_TYPES,
    "PERSON_NAME_GENERIC": _NAME_TYPES,
    # "INDIAN_PIN" has no corpus gold-span equivalent observed empirically;
    # left unmapped (falls through to UNKNOWN) rather than guessed.
}

PHI_ENGINE_COVERABLE: FrozenSet[str] = frozenset(
    et for ets in PHI_ENGINE_TO_CORPUS.values() for et in ets
)


def _map_phi_engine_type(pattern_name: str) -> FrozenSet[str]:
    """All corpus entity types a phi_patterns pattern name can legitimately match.

    Unmapped pattern names (and NUMERIC_ID_SHORT, mapped nowhere by design)
    return ``frozenset({"UNKNOWN"})`` -- never matches a real gold span, so
    it always scores as a false positive under strict/entity-aware scoring,
    per the evidence plan's explicit fallback design.
    """
    ours = PHI_ENGINE_TO_CORPUS.get(pattern_name)
    return ours if ours else frozenset({"UNKNOWN"})


# Corpus entity types phi_engine's regex/pattern surface structurally cannot
# detect (no BLOCKING_PATTERNS/WARN_PATTERNS entry covers them at all -- this
# is a REGEX surface, not free-text NER; see module docstring). Enumerated by
# hand against the full seed-42 corpus's observed gold entity_type set
# (verified 2026-07-07), mirroring benchmarks/presidio_adapter.py's
# PRESIDIO_GAP_ENTITY_TYPES convention.
PHI_ENGINE_GAP_ENTITY_TYPES: FrozenSet[str] = frozenset({
    "ABHA_ADDRESS", "ABHA_NUMBER", "ACCESSION_NUMBER", "ACCOUNT_NUMBER",
    "ADDRESS_CITY", "ADDRESS_ZIP", "AGE", "AU_PASSPORT", "BADGE_NUMBER",
    "BANK_ACCOUNT", "BIOMETRIC", "BIOMETRIC_DNA_SPECIMEN",
    "BIOMETRIC_ENROLLMENT_ID", "BIOMETRIC_FACE_TEMPLATE",
    "BIOMETRIC_FINGERPRINT_TEMPLATE", "BIOMETRIC_IRIS_TEMPLATE",
    "BIOMETRIC_RETINAL_TEMPLATE", "BIOMETRIC_VOICE_TEMPLATE", "BR_PASSPORT",
    "BSN_NL", "CASE_NUMBER", "CLINICAL_TRIAL_ID", "CNH_BR", "CNPJ_BR", "CNS_BR",
    "CODICE_FISCALE_IT", "CPF", "CPR_DK", "CTRI_ID", "DEPARTMENT",
    "DEVICE_LOT_NUMBER", "DEVICE_SAMD_VERSION", "DEVICE_SERIAL", "DEVICE_UDI",
    "DEVICE_UDI_GS1", "DEVICE_UDI_HIBCC", "DEVICE_UDI_ICCBBA", "DNI_ES",
    "DPDPA_ACQUISITION_FORM", "DPDPA_APP_REF", "DPDPA_CUSTOMER_ID",
    "DPDPA_ENROLMENT_ID", "DRIVERS_LICENSE", "DRIVERS_LICENSE_AU", "DVA_FILE",
    "FAX", "FAX_BROADCAST_1", "FAX_BROADCAST_2", "FAX_BROADCAST_3",
    "FAX_EFAX_NUMBER", "FAX_HOSPITAL", "FAX_INTERNATIONAL", "FAX_LAB",
    "FAX_PAYER", "FAX_PCP", "FAX_PHARMACY", "FAX_PROVIDER", "FAX_RADIOLOGY",
    "FAX_RECEIVER", "FAX_SENDER", "GEOGRAPHIC_SUBDIVISION", "HEALTH_ID_UG",
    "HEALTH_INSURANCE_UG", "HEALTH_PLAN_ID", "IHI", "INSTITUTION_NAME",
    "INTERNAL_CODE", "IP_V6", "LICENSE_PLATE", "LICENSE_PLATE_VANITY",
    "MEDICARE_NUMBER", "NATIONAL_ID_UG", "NIR_FR", "NPI", "NSSF_NUMBER",
    "PASSPORT_UG", "PERSONNUMMER_SE", "PESEL_PL", "PHONE_AU", "PHONE_BR",
    "PHONE_UG", "PHOTO_FULL_FACE", "PIS_PASEP_BR", "POSTAL_CODE_EU",
    "QUASI_CITY", "QUASI_PROFESSION", "QUASI_RARE_DISEASE",
    "REID_CODE_FORBIDDEN", "REID_CODE_PERMITTED", "RG_BR", "SSN_TRUNCATED",
    "STEUER_ID_DE", "TFN", "TIN_UG", "TITULO_ELEITOR_BR", "UAN", "VIN",
    "ZIP",
})


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class PhiEngineAdapter:
    """Benchmark adapter for phi_engine's own structured PHI-detection surface.

    Combines ``presidio_gate.analyze_text`` (BLOCKING_PATTERNS, checksum-
    validated) with a direct ``WARN_PATTERNS`` regex scan (lower-confidence
    heuristics ``presidio_gate`` does not wire in -- see
    ``phi_engine/security/presidio_gate.py::_build_analyzer``). Both surfaces
    are value-free at the source: findings carry offsets and pattern names,
    never the matched substring.
    """

    def __init__(self) -> None:
        try:
            from phi_engine.security.phi_patterns import WARN_PATTERNS
            from phi_engine.security.presidio_gate import analyze_text

            self._analyze_text = analyze_text
            self._warn_patterns = WARN_PATTERNS
            self._available = True
        except ImportError:
            self._analyze_text = None
            self._warn_patterns = []
            self._available = False
        self._version = "phi_engine-adapter-v1"

    def _require_available(self) -> None:
        if not self._available:
            raise ImportError(
                "phi_engine.security.presidio_gate / phi_patterns are not importable "
                "(presidio-analyzer + a blank spaCy tokenizer are required -- "
                "pip install presidio-analyzer; no NER model download needed)."
            )

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        self._require_available()
        spans: List[PredictedSpan] = []
        for finding in self._analyze_text(text):
            mapped = _map_phi_engine_type(finding.pattern_name)
            spans.append(
                PredictedSpan(
                    start=finding.start,
                    end=finding.end,
                    entity_type=finding.pattern_name,
                    mapped_type=next(iter(mapped)),
                    mapped_types=mapped,
                    score=finding.score,
                )
            )
        for label, pattern in self._warn_patterns:
            mapped = _map_phi_engine_type(label)
            for match in pattern.finditer(text):
                spans.append(
                    PredictedSpan(
                        start=match.start(),
                        end=match.end(),
                        entity_type=label,
                        mapped_type=next(iter(mapped)),
                        mapped_types=mapped,
                        score=0.5,  # WARN tier: lower confidence than BLOCKING
                    )
                )
        return spans

    def run_file(
        self,
        jsonl_path: Path,
        strategy: str = "overlap",
        overlap_threshold: float = 0.5,
        entity_type_agnostic: bool = True,
    ) -> List[dict]:
        self._require_available()
        record_scores = []
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                text = obj["text"]
                record_id = obj["record_id"]
                gold = [
                    GoldSpan(
                        start=s["start"],
                        end=s["end"],
                        entity_type=s["entity_type"],
                        hipaa_category=s.get("hipaa_category"),
                        detection_regime=s.get("detection_regime", "contextual_ner_required"),
                        jurisdiction=s.get("jurisdiction", "us"),
                    )
                    for s in obj.get("gold_spans", [])
                ]
                predicted = self.analyze_text(text)
                rs = score_record(
                    predicted=predicted,
                    gold=gold,
                    gap_entity_types=PHI_ENGINE_GAP_ENTITY_TYPES,
                    strategy=strategy,
                    overlap_threshold=overlap_threshold,
                    entity_type_agnostic=entity_type_agnostic,
                )
                strict_score = score_record_strict_all_span(predicted=predicted, gold=gold)
                strict_score["gap_spans"] = rs["gap_spans"]
                rs["strict_all_span_score"] = strict_score
                rs["record_id"] = record_id
                rs["corpus_file"] = str(jsonl_path)
                rs["predicted_count"] = len(predicted)
                rs["gold_count"] = len(gold)
                rs["predictions"] = [
                    {**asdict(span), "mapped_types": sorted(span.mapped_types)}
                    for span in predicted
                ]
                rs["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                record_scores.append(rs)
        return record_scores

    def run_all(
        self,
        corpus_dir: Path,
        pattern: str = "*.jsonl",
        strategy: str = "overlap",
        overlap_threshold: float = 0.5,
        entity_type_agnostic: bool = True,
        scoring_profile: str = "legacy_overlap_coverable",
        verbose: bool = False,
    ) -> BenchmarkResult:
        if scoring_profile not in {"legacy_overlap_coverable", "strict_all_span"}:
            raise ValueError(
                "scoring_profile must be 'legacy_overlap_coverable' or 'strict_all_span'"
            )
        tool_name = self._version
        if not self._available:
            result = BenchmarkResult(tool_name=f"{tool_name}-not_installed")
            result.corpus_files = []
            return result

        corpus_dir = Path(corpus_dir)
        all_scores: List[dict] = []
        total_predicted = 0
        files_processed = []

        for jsonl_path in sorted(corpus_dir.glob(pattern)):
            if verbose:
                print(f"  Processing {jsonl_path.name} ...", end=" ", flush=True)
            file_scores = self.run_file(
                jsonl_path,
                strategy=strategy,
                overlap_threshold=overlap_threshold,
                entity_type_agnostic=entity_type_agnostic,
            )
            all_scores.extend(file_scores)
            total_predicted += sum(rs["predicted_count"] for rs in file_scores)
            files_processed.append(str(jsonl_path.name))
            if verbose:
                primary = [
                    rs["strict_all_span_score"] if scoring_profile == "strict_all_span" else rs
                    for rs in file_scores
                ]
                tp = sum(rs["tp"] for rs in primary)
                fp = sum(rs["fp"] for rs in primary)
                fn = sum(rs["fn"] for rs in primary)
                gaps = sum(len(rs.get("gap_spans", [])) for rs in file_scores)
                print(
                    f"{len(file_scores):3d} records  TP={tp} FP={fp} FN={fn} gaps={gaps}"
                )

        strict_scores = [rs["strict_all_span_score"] for rs in all_scores]
        primary_scores = strict_scores if scoring_profile == "strict_all_span" else all_scores
        result = aggregate_record_scores(
            primary_scores,
            tool_name=tool_name,
            gap_entity_types=PHI_ENGINE_GAP_ENTITY_TYPES,
            scoring_profile=scoring_profile,
            strict_record_scores=strict_scores,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        result._raw_prediction_rows = [
            {
                "record_id": rs["record_id"],
                "corpus_file": rs["corpus_file"],
                "text_sha256": rs["text_sha256"],
                "gold_count": rs["gold_count"],
                "predictions": rs["predictions"],
            }
            for rs in all_scores
        ]
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_name = "phi_engine_raw_predictions.jsonl"
        result.raw_prediction_artifact = raw_name
        summary_path = output_dir / "phi_engine_benchmark_result.json"
        raw_path = output_dir / raw_name

        raw_rows = getattr(result, "_raw_prediction_rows", [])
        with raw_path.open("w", encoding="utf-8") as fh:
            for row in raw_rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

        summary_path.write_text(json.dumps(result.summary_dict(), indent=2, sort_keys=True))
        print(f"Results written to {summary_path}")
        print(f"Raw predictions written to {raw_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="phi_engine detection-surface benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument(
        "--output-dir", type=Path, default=_PROJECT_ROOT / "benchmarks" / "results" / "phi-engine"
    )
    parser.add_argument("--strategy", choices=["exact", "overlap"], default="overlap")
    parser.add_argument("--overlap-threshold", type=float, default=0.5)
    parser.add_argument(
        "--scoring-profile",
        choices=["legacy_overlap_coverable", "strict_all_span"],
        default="legacy_overlap_coverable",
    )
    parser.add_argument("--entity-type-aware", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    adapter = PhiEngineAdapter()
    result = adapter.run_all(
        args.corpus_dir,
        strategy=args.strategy,
        overlap_threshold=args.overlap_threshold,
        entity_type_agnostic=not args.entity_type_aware,
        scoring_profile=args.scoring_profile,
        verbose=args.verbose,
    )
    print_report(result)
    adapter.write_results(result, args.output_dir)
