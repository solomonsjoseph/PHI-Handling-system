"""
Microsoft Presidio benchmark adapter.

Runs Presidio AnalyzerEngine against the US corpus JSONL files and scores
predictions against gold-standard spans using benchmarks/metrics.py.

Presidio entity type mapping
-----------------------------
Presidio predicts its own entity labels (PERSON, US_SSN, etc.).
We translate those to our extended corpus taxonomy before scoring.
Translation is many-to-many: Presidio's PERSON maps to NAME_PATIENT,
NAME_PROVIDER, and NAME_HOUSEHOLD; our VIN maps to nothing in Presidio.

Gap entity types
----------------
These are our corpus entity types that Presidio has NO recognizer for.
They are treated as structural gaps, not missed detections, in the report.
Gap list is derived from authorities/AUTHORITY_MATRIX.md Table C and
benchmarks/01_presidio_entities.md.

Usage
-----
CLI:
    python -m benchmarks.presidio_adapter \\
        --corpus-dir corpus/us \\
        --output-dir benchmarks/results/presidio \\
        --strategy overlap \\
        --verbose

Python:
    from benchmarks.presidio_adapter import PresidioAdapter
    adapter = PresidioAdapter()
    result = adapter.run_all("corpus/us")
    from benchmarks.metrics import print_report
    print_report(result)

Authority: benchmarks/01_presidio_entities.md
           authorities/AUTHORITY_MATRIX.md Table C
"""
from __future__ import annotations
from dataclasses import asdict
import hashlib

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

# Project root resolution
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
# Presidio entity type → our corpus entity types (many-to-many)
# Derived from benchmarks/01_presidio_entities.md gap analysis.
# ---------------------------------------------------------------------------

# Set of our entity_type values that each Presidio entity type can match.
# Used for entity-type-aware scoring (entity_type_agnostic=False).
PRESIDIO_TO_CORPUS: Dict[str, FrozenSet[str]] = {
    "PERSON":              frozenset({"NAME_PATIENT", "NAME_PROVIDER", "NAME_HOUSEHOLD"}),
    "LOCATION":            frozenset({"ADDRESS_STREET", "ADDRESS_CITY", "ADDRESS_ZIP",
                                       "ADDRESS_STATE", "QUASI_CITY"}),
    "DATE_TIME":           frozenset({"DATE_DOB", "DATE_ADMIT", "DATE_DISCHARGE",
                                       "DATE_DEATH", "DATE_DEATH_OF_RELATIVE"}),
    "PHONE_NUMBER":        frozenset({"PHONE_HOME", "PHONE_WORK",
                                       "FAX", "FAX_SENDER", "FAX_RECEIVER",
                                       "FAX_PHARMACY", "FAX_PAYER", "FAX_PROVIDER",
                                       "FAX_LAB", "FAX_HOSPITAL", "FAX_PCP",
                                       "FAX_RADIOLOGY", "FAX_EFAX_NUMBER",
                                       "FAX_INTERNATIONAL", "FAX_BROADCAST_1",
                                       "FAX_BROADCAST_2", "FAX_BROADCAST_3"}),
    # Note: PHONE_NUMBER is intentionally mapped to FAX types here because
    # Presidio conflates (D) and (E). This produces false positives on FAX
    # records: Presidio finds the number but cannot distinguish fax from phone.
    "EMAIL_ADDRESS":       frozenset({"EMAIL"}),
    "US_SSN":              frozenset({"SSN"}),
    # Custom MBI recognizer added in __init__; maps to HEALTH_PLAN_ID (HIPAA I).
    # CMS MBI spec: C A AN N A AN N A A N N (11 chars). Authority: CMS.gov MBI format.
    "US_MBI":              frozenset({"HEALTH_PLAN_ID"}),
    # Custom VIN recognizer added in __init__; ISO 3779 (excludes I/O/Q per NHTSA).
    # LICENSE_PLATE_VANITY excluded: VIN regex won't reliably match arbitrary vanity plate formats.
    "US_VIN":              frozenset({"VIN", "LICENSE_PLATE"}),
    "US_BANK_NUMBER":      frozenset({"BANK_ACCOUNT", "ACCOUNT_NUMBER"}),
    "US_DRIVER_LICENSE":   frozenset({"DRIVERS_LICENSE"}),
    "MEDICAL_LICENSE":     frozenset({"NPI", "MEDICAL_LICENSE_NUMBER"}),
    "US_PASSPORT":         frozenset({"PASSPORT_US"}),
    "URL":                 frozenset({"URL"}),
    "IP_ADDRESS":          frozenset({"IP_V4", "IP_V6"}),
    "MAC_ADDRESS":         frozenset({"MAC_ADDRESS"}),
    "CREDIT_CARD":         frozenset({"CREDIT_CARD"}),
    "IBAN_CODE":           frozenset({"BANK_ACCOUNT"}),
    "CRYPTO":              frozenset({"CRYPTO_WALLET"}),
    "NRP":                 frozenset({"NATIONALITY_RELIGIOUS_POLITICAL"}),
}

# Flat set of corpus entity types that Presidio can potentially detect
PRESIDIO_COVERABLE: FrozenSet[str] = frozenset(
    et for ets in PRESIDIO_TO_CORPUS.values() for et in ets
)

# Corpus entity types Presidio structurally CANNOT detect.
# Derived from benchmarks/01_presidio_entities.md.
PRESIDIO_GAP_ENTITY_TYPES: FrozenSet[str] = frozenset({
    # HIPAA (H) Medical record number -- no MRN recognizer
    "MRN",
    # HIPAA (E) Fax distinct from phone -- Presidio conflates; scored as FP on phone type
    # (We still include FAX types in coverable because Presidio may fire PHONE_NUMBER on them)
    # Note: HEALTH_PLAN_ID (MBI) and VIN now covered by custom recognizers added in __init__.
    # HIPAA (M) Device identifiers -- no UDI/serial recognizer
    "DEVICE_UDI_GS1",
    "DEVICE_UDI_HIBCC",
    "DEVICE_UDI_ICCBBA",
    "DEVICE_SERIAL",
    "DEVICE_LOT_NUMBER",
    "DEVICE_SAMD_VERSION",
    # HIPAA (P) Biometric identifiers -- no biometric template recognizer
    "BIOMETRIC_FINGERPRINT_TEMPLATE",
    "BIOMETRIC_VOICE_TEMPLATE",
    "BIOMETRIC_IRIS_TEMPLATE",
    "BIOMETRIC_RETINAL_TEMPLATE",
    "BIOMETRIC_DNA_SPECIMEN",
    "BIOMETRIC_FACE_TEMPLATE",
    "BIOMETRIC_ENROLLMENT_ID",
    # HIPAA (Q) Photo -- image-redactor only, not text
    "PHOTO_FULL_FACE",
    "PHOTO_REFERENCE",
    # HIPAA (C) Age > 89 -- DATE_TIME does not implement age-over-89 rule
    "AGE_OVER_89",
    # HIPAA (K) partial gaps
    "LICENSE_PLATE_VANITY",
    # Re-identification code types -- no re-ID logic
    "REID_CODE_PERMITTED",
    "REID_CODE_FORBIDDEN",
    # Quasi-identifiers -- no k-anonymity / combination detection
    "QUASI_PROFESSION",
    "QUASI_RARE_DISEASE",
    # LDS / audit context types
    "LDS_DISCLOSURE_CODE",
    "AUDIT_REQUESTER",
    "SUBPOENA_REFERENCE",
})


def _map_presidio_type(presidio_entity_type: str) -> FrozenSet[str]:
    """All corpus entity types a Presidio entity can legitimately match.

    Used to set PredictedSpan.mapped_types for entity-type-aware scoring:
    a match against ANY member of this set counts, since Presidio's coarse
    categories (e.g. "PERSON") map to several of our finer-grained gold
    types (e.g. NAME_PATIENT, NAME_PROVIDER, NAME_HOUSEHOLD).
    """
    ours = PRESIDIO_TO_CORPUS.get(presidio_entity_type)
    return ours if ours else frozenset({presidio_entity_type})


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PresidioAdapter:
    """Benchmark adapter for Microsoft Presidio AnalyzerEngine.

    Requires presidio-analyzer >= 2.2.355 (in requirements.txt).
    Install: pip install presidio-analyzer && python -m spacy download en_core_web_lg

    The adapter runs Presidio against our JSONL corpus files and scores
    predictions with benchmarks.metrics using two strategies:
    - exact: (start, end) must match exactly (conservative)
    - overlap: overlap fraction >= 0.5 (lenient, for partial detections)
    """

    def __init__(self, language: str = "en", profile: str = "stock") -> None:
        if profile not in {"stock", "tuned"}:
            raise ValueError("profile must be 'stock' or 'tuned'")

        self.language = language
        self.profile = profile
        try:
            from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
            self.analyzer = AnalyzerEngine()
            if profile == "tuned":
                # CMS MBI: C A AN N A AN N A A N N (11 chars)
                # Authority: CMS Medicare Beneficiary Identifier specification
                mbi_recognizer = PatternRecognizer(
                    supported_entity="US_MBI",
                    patterns=[Pattern(
                        name="US_MBI",
                        regex=r"\b[1-9][AC-HJ-NP-RT-Y][AC-HJ-NP-RT-Y0-9][0-9][AC-HJ-NP-RT-Y][AC-HJ-NP-RT-Y0-9][0-9][AC-HJ-NP-RT-Y][AC-HJ-NP-RT-Y][0-9][0-9]\b",
                        score=0.85,
                    )],
                )
                # ISO 3779 VIN: 17 chars excluding I, O, Q
                vin_recognizer = PatternRecognizer(
                    supported_entity="US_VIN",
                    patterns=[Pattern(
                        name="US_VIN",
                        regex=r"\b[A-HJ-NPR-Z0-9]{17}\b",
                        score=0.7,
                    )],
                )
                self.analyzer.registry.add_recognizer(mbi_recognizer)
                self.analyzer.registry.add_recognizer(vin_recognizer)
            self._available = True
        except ImportError:
            self._available = False
            self.analyzer = None

        self._version = self._detect_version()
    def _detect_version(self) -> str:
        try:
            from importlib.metadata import PackageNotFoundError, version
            try:
                return version("presidio-analyzer")
            except PackageNotFoundError:
                pass
            import presidio_analyzer
            return getattr(presidio_analyzer, "__version__", "unknown")
        except ImportError:
            return "not_installed"

    def _require_presidio(self) -> None:
        if not self._available:
            raise ImportError(
                "presidio-analyzer is not installed. "
                "Run: pip install presidio-analyzer && "
                "python -m spacy download en_core_web_lg"
            )

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        """Run Presidio on a single text string; return PredictedSpan list."""
        self._require_presidio()
        results = self.analyzer.analyze(text=text, language=self.language)
        spans = []
        for r in results:
            mapped_types = _map_presidio_type(r.entity_type)
            spans.append(PredictedSpan(
                start=r.start,
                end=r.end,
                entity_type=r.entity_type,
                mapped_type=next(iter(mapped_types)),
                mapped_types=mapped_types,
                score=r.score,
            ))
        return spans

    def run_file(
        self,
        jsonl_path: Path,
        strategy: str = "overlap",
        overlap_threshold: float = 0.5,
        entity_type_agnostic: bool = True,
    ) -> List[dict]:
        """Score Presidio against one JSONL corpus file.

        Returns a list of per-record score dicts (from score_record()).
        Also attaches predicted_count, gold_count, strict score, and raw
        prediction artifact metadata to each dict. Raw record text is never
        retained.
        """
        self._require_presidio()
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
                    gap_entity_types=PRESIDIO_GAP_ENTITY_TYPES,
                    strategy=strategy,
                    overlap_threshold=overlap_threshold,
                    entity_type_agnostic=entity_type_agnostic,
                )
                strict_score = score_record_strict_all_span(
                    predicted=predicted,
                    gold=gold,
                )
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
        """Score Presidio against all JSONL files in corpus_dir.

        Returns a BenchmarkResult aggregated across all files.
        """
        if scoring_profile not in {"legacy_overlap_coverable", "strict_all_span"}:
            raise ValueError("scoring_profile must be 'legacy_overlap_coverable' or 'strict_all_span'")
        self._require_presidio()
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
            file_predicted = sum(rs["predicted_count"] for rs in file_scores)
            total_predicted += file_predicted
            files_processed.append(str(jsonl_path.name))
            if verbose:
                primary_scores = [
                    rs["strict_all_span_score"] if scoring_profile == "strict_all_span" else rs
                    for rs in file_scores
                ]
                file_tp = sum(rs["tp"] for rs in primary_scores)
                file_fn = sum(rs["fn"] for rs in primary_scores)
                file_fp = sum(rs["fp"] for rs in primary_scores)
                file_gaps = sum(len(rs.get("gap_spans", [])) for rs in file_scores)
                print(f"{len(file_scores):3d} records  "
                      f"TP={file_tp} FP={file_fp} FN={file_fn} gaps={file_gaps}")

        strict_scores = [rs["strict_all_span_score"] for rs in all_scores]
        primary_scores = strict_scores if scoring_profile == "strict_all_span" else all_scores
        result = aggregate_record_scores(
            primary_scores,
            tool_name=f"presidio-{self.profile}-{self._version}",
            gap_entity_types=PRESIDIO_GAP_ENTITY_TYPES,
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
        """Write benchmark summary JSON and raw prediction JSONL to output_dir."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        profile = self.profile
        raw_name = f"presidio_{profile}_raw_predictions.jsonl"
        result.raw_prediction_artifact = raw_name
        summary_path = output_dir / f"presidio_{profile}_benchmark_result.json"
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

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Presidio benchmark against US PHI corpus"
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=_PROJECT_ROOT / "corpus" / "us",
        help="Directory containing corpus JSONL files (default: corpus/us)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_PROJECT_ROOT / "benchmarks" / "results" / "presidio",
        help="Directory for result output (default: benchmarks/results/presidio)",
    )
    parser.add_argument(
        "--profile",
        choices=["stock", "tuned"],
        default="stock",
        help="Presidio profile: stock AnalyzerEngine or tuned with custom recognizers (default: stock)",
    )
    parser.add_argument(
        "--scoring-profile",
        choices=["legacy_overlap_coverable", "strict_all_span"],
        default="legacy_overlap_coverable",
        help="Primary scoring profile for aggregate fields (default: legacy_overlap_coverable)",
    )
    parser.add_argument(
        "--strategy",
        choices=["exact", "overlap"],
        default="overlap",
        help="Span matching strategy (default: overlap)",
    )
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.5,
        help="Overlap fraction threshold when strategy=overlap (default: 0.5)",
    )
    parser.add_argument(
        "--entity-type-aware",
        action="store_true",
        default=False,
        help="Enable entity-type-aware scoring (default: agnostic)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Print per-file progress and per-HIPAA-category breakdown",
    )
    args = parser.parse_args()

    adapter = PresidioAdapter(profile=args.profile)

    if not adapter._available:
        print("ERROR: presidio-analyzer not installed.")
        print("Run: pip install presidio-analyzer && python -m spacy download en_core_web_lg")
        sys.exit(1)

    print(f"Presidio version : {adapter._version}")
    print(f"Profile          : {adapter.profile}")
    print(f"Corpus directory : {args.corpus_dir}")
    print(f"Strategy         : {args.strategy} (threshold={args.overlap_threshold})")
    print(f"Scoring profile  : {args.scoring_profile}")
    print(f"Entity-type mode : {'aware' if args.entity_type_aware else 'agnostic'}")
    print()

    result = adapter.run_all(
        corpus_dir=args.corpus_dir,
        strategy=args.strategy,
        overlap_threshold=args.overlap_threshold,
        entity_type_agnostic=not args.entity_type_aware,
        scoring_profile=args.scoring_profile,
        verbose=args.verbose,
    )

    print_report(result, verbose=args.verbose)
    adapter.write_results(result, args.output_dir)


if __name__ == "__main__":
    main()
