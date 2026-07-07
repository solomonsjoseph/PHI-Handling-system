"""
Corpus generation harness.

Runs all seeded generators and writes JSONL output to corpus/<jurisdiction>/.
Every run with the same seed produces bitwise-identical output.

Usage:
    python -m harness.generate_corpus [--seed SEED] [--jurisdiction us] [--out-dir corpus]
    python -m harness.generate_corpus --mode llm [--seed SEED] [--jurisdiction HIPAA]

Default seed: 42 (IRB-reproducible baseline).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Resolve project root (two levels up from this file)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from generators import (
    AustraliaPrivacyGenerator,
    BrazilLGPDGenerator,
    DICOMHeaderGenerator,
    EMLGenerator,
    EUGDPRGenerator,
    FHIRGenerator,
    HIPAABiometricGenerator,
    HIPAADeviceGenerator,
    HIPAAFaxGenerator,
    HIPAAFundraisingGenerator,
    HIPAALDSGenerator,
    HIPAAQuasiIdentifierGenerator,
    HIPAAReIDCodesGenerator,
    HIPAASafeHarborGenerator,
    HIPAAVehicleGenerator,
    HIPAAVerificationGenerator,
    HL7v2Generator,
    IndiaDPDPAGenerator,
    IndiaIdentifierGenerator,
    UgandaDPPAGenerator,
    XlsxGenerator,
)
from generators.common import Record, write_jsonl
from harness.capability_registry import REGISTRY_PATH, STATUS_ORDER, load_capabilities


@dataclass(frozen=True)
class GeneratorSpec:
    id: str
    jurisdiction: str
    output_relpath: str
    make_records: Callable[[int], list[Record]]


def seeded_generator_specs() -> list[GeneratorSpec]:
    """Return deterministic corpus generator specs in manifest order."""
    return [
        GeneratorSpec(
            "us/hipaa_safe_harbor",
            "us",
            "us/hipaa_safe_harbor",
            lambda seed: HIPAASafeHarborGenerator(seed).generate_batch(10),
        ),
        GeneratorSpec(
            "us/hipaa_quasi_identifiers",
            "us",
            "us/hipaa_quasi_identifiers",
            lambda seed: HIPAAQuasiIdentifierGenerator(seed).generate_batch(50),
        ),
        GeneratorSpec(
            "us/hipaa_lds",
            "us",
            "us/hipaa_lds",
            lambda seed: HIPAALDSGenerator(seed).generate_batch(20),
        ),
        GeneratorSpec(
            "us/hipaa_reid_codes",
            "us",
            "us/hipaa_reid_codes",
            lambda seed: HIPAAReIDCodesGenerator(seed).generate_batch(20),
        ),
        GeneratorSpec(
            "us/hipaa_fundraising",
            "us",
            "us/hipaa_fundraising",
            lambda seed: HIPAAFundraisingGenerator(seed).generate_batch(20),
        ),
        GeneratorSpec(
            "us/hipaa_verification",
            "us",
            "us/hipaa_verification",
            lambda seed: HIPAAVerificationGenerator(seed).generate_batch(20),
        ),
        GeneratorSpec(
            "us/hipaa_biometric",
            "us",
            "us/hipaa_biometric",
            lambda seed: HIPAABiometricGenerator(seed).generate_batch(count_per_mode=4),
        ),
        GeneratorSpec(
            "us/hipaa_device",
            "us",
            "us/hipaa_device",
            lambda seed: HIPAADeviceGenerator(seed).generate_batch(count_per_mode=4),
        ),
        GeneratorSpec(
            "us/hipaa_fax",
            "us",
            "us/hipaa_fax",
            lambda seed: HIPAAFaxGenerator(seed).generate_batch(count_per_mode=4),
        ),
        GeneratorSpec(
            "us/hipaa_vehicle",
            "us",
            "us/hipaa_vehicle",
            lambda seed: HIPAAVehicleGenerator(seed).generate_batch(count_per_mode=4),
        ),
        GeneratorSpec(
            "in/india_dpdpa",
            "in",
            "in/india_dpdpa",
            lambda seed: IndiaDPDPAGenerator(seed).generate_batch(count_per_type=4),
        ),
        GeneratorSpec(
            "in/india_identifiers",
            "in",
            "in/india_identifiers",
            lambda seed: IndiaIdentifierGenerator(seed).generate_batch(count_per_identifier=4),
        ),
        GeneratorSpec(
            "eu/eu_identifiers",
            "eu",
            "eu/eu_identifiers",
            lambda seed: EUGDPRGenerator(seed).generate_batch(count_per_type=4),
        ),
        GeneratorSpec(
            "br/brazil_identifiers",
            "br",
            "br/brazil_identifiers",
            lambda seed: BrazilLGPDGenerator(seed).generate_batch(count_per_type=4),
        ),
        GeneratorSpec(
            "au/australia_identifiers",
            "au",
            "au/australia_identifiers",
            lambda seed: AustraliaPrivacyGenerator(seed).generate_batch(count_per_type=4),
        ),
        GeneratorSpec(
            "ug/uganda_identifiers",
            "ug",
            "ug/uganda_identifiers",
            lambda seed: UgandaDPPAGenerator(seed).generate_batch(count_per_type=4),
        ),
        GeneratorSpec(
            "file_formats/dicom_headers",
            "file_formats",
            "file_formats/dicom_headers",
            lambda seed: DICOMHeaderGenerator(seed).generate_batch(count=20),
        ),
        GeneratorSpec(
            "file_formats/fhir_bundles",
            "file_formats",
            "file_formats/fhir_bundles",
            lambda seed: FHIRGenerator(seed).generate_batch(count=20),
        ),
        GeneratorSpec(
            "file_formats/hl7v2_messages",
            "file_formats",
            "file_formats/hl7v2_messages",
            lambda seed: HL7v2Generator(seed).generate_batch(count=20),
        ),
        GeneratorSpec(
            "file_formats/eml_messages",
            "file_formats",
            "file_formats/eml_messages",
            lambda seed: EMLGenerator(seed).generate_batch(count=20),
        ),
        GeneratorSpec(
            "file_formats/xlsx_phi_corpus",
            "file_formats",
            "file_formats/xlsx_phi_corpus",
            lambda seed: XlsxGenerator(seed).generate(n_per_tier_a=20),
        ),
    ]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _registry_enabled_output_relpaths() -> set[str]:
    """Return generated output relpaths backed by tested/manifested registry entries."""
    enabled: set[str] = set()
    for capability in load_capabilities():
        if capability.kind not in {"jurisdiction", "file_format"}:
            continue
        if STATUS_ORDER[capability.status] < STATUS_ORDER["tested"]:
            continue
        if not capability.generator:
            continue
        if capability.id == "us_hipaa":
            enabled.update(
                spec.output_relpath for spec in seeded_generator_specs() if spec.jurisdiction == "us"
            )
            continue
        output = capability.output
        if output.startswith("corpus/") and output.endswith(".jsonl"):
            enabled.add(output[len("corpus/") : -len(".jsonl")])
    return enabled


def run_generator_spec(spec: GeneratorSpec, seed: int, out_dir: Path) -> dict:
    """Run one deterministic generator spec and write its JSONL output."""
    records = spec.make_records(seed)
    path = out_dir / f"{spec.output_relpath}.jsonl"
    count = write_jsonl(records, path)
    span_count = sum(len(record.gold_spans) for record in records)
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    errors = []
    for record in records:
        record_errors = record.verify_spans()
        if record_errors:
            errors.extend(f"{record.record_id}: {error}" for error in record_errors)

    return {
        "path": _display_path(path),
        "records": count,
        "spans": span_count,
        "sha256": file_hash,
        "span_errors": errors,
    }


def build_seeded_corpus(seed: int, out_dir: Path, jurisdiction: str) -> dict[str, dict]:
    """Build deterministic corpus files for one jurisdiction or all registry-backed specs."""
    supported = {"all", "us", "in", "eu", "br", "au", "ug", "file_formats"}
    if jurisdiction not in supported:
        raise SystemExit(f"Unsupported jurisdiction: {jurisdiction}")

    specs = seeded_generator_specs()
    if jurisdiction == "all":
        enabled_outputs = _registry_enabled_output_relpaths()
        selected_specs = [spec for spec in specs if spec.output_relpath in enabled_outputs]
    else:
        selected_specs = [spec for spec in specs if spec.jurisdiction == jurisdiction]

    summaries: dict[str, dict] = {}
    totals: dict[str, dict[str, int | str]] = {}
    for spec in selected_specs:
        print_prefix = spec.id
        summary = run_generator_spec(spec, seed, out_dir)
        local_name = spec.output_relpath.split("/", 1)[1]
        jurisdiction_summary = summaries.setdefault(spec.jurisdiction, {})
        jurisdiction_summary[local_name] = summary

        total = totals.setdefault(
            spec.jurisdiction,
            {"records": 0, "spans": 0, "jurisdiction": spec.jurisdiction, "seed": seed},
        )
        total["records"] += summary["records"]
        total["spans"] += summary["spans"]

        status = "OK" if not summary["span_errors"] else f"ERRORS({len(summary['span_errors'])})"
        print(
            f"  {print_prefix:35s} {summary['records']:4d} records  "
            f"{summary['spans']:5d} spans  {status}"
        )

    for spec_jurisdiction, total in totals.items():
        summaries[spec_jurisdiction]["__total__"] = total
    return summaries


def build_us_corpus(seed: int, out_dir: Path) -> dict:
    """Build the full USA/HIPAA corpus layer."""
    return build_seeded_corpus(seed, out_dir, "us")["us"]


def build_llm_corpus(
    seed: int,
    out_dir: Path,
    jurisdiction: str = "HIPAA",
    n_records: int = 50,
) -> dict:
    """Generate a synthetic PHI corpus using the LLM.

    The LLM reads AUTHORITY_MATRIX.md, proposes synthetic records with gold spans,
    and writes them to corpus/llm/<jurisdiction>/.jsonl.

    This is the ONLY place in the system where the LLM writes PHI-shaped values.
    All generated values are synthetic by construction -- no real PHI is involved.

    Authority: AUTHORITY_MATRIX.md (all five tables); individual record citations
    trace to specific rows in the matrix.

    Deliberately NOT wrapped with phi_engine.security.llm_tool_guard.guard_llm_output:
    that guard's phi_gate_check blocks on the exact SSN/MRN/phone/etc. patterns this
    function is asked to produce, so wiring it here would block the corpus itself.
    The real-PHI safety net for this path is harness/run_all_validations.py's
    no_real_phi_static_validator, run over the written corpus after generation.
    """
    from phi_engine.config.config import get_llm_client

    llm_dir = out_dir / "llm" / jurisdiction.lower()
    llm_dir.mkdir(parents=True, exist_ok=True)

    matrix_path = PROJECT_ROOT / "authorities" / "AUTHORITY_MATRIX.md"
    if not matrix_path.is_file():
        raise FileNotFoundError(f"Authority matrix not found: {matrix_path}")
    matrix_text = matrix_path.read_text()[:8000]  # token budget

    client = get_llm_client()
    rng = random.Random(seed)

    # Ask LLM to enumerate entity categories for this jurisdiction first
    schema_prompt = f"""\
You are building a synthetic PHI test corpus for {jurisdiction} compliance testing.
The corpus will be used to benchmark PHI de-identification tools.

From the authority matrix excerpt below, list the entity categories relevant to {jurisdiction}.
For each category, give:
- entity_type: short identifier (e.g. "NAME", "SSN", "MRN", "DATE_OF_BIRTH")
- hipaa_category: HIPAA Safe Harbor letter A-R, or empty string for non-HIPAA
- authority_citation: exact legal citation
- description: one-line description

AUTHORITY MATRIX (excerpt):
---
{matrix_text}
---

Respond with a JSON array of entity category objects. JSON only, no prose."""

    schema_raw = client.complete(schema_prompt)
    # Strip markdown fences
    import re
    schema_raw = re.sub(r"```(?:json)?\s*|\s*```", "", schema_raw.strip())
    try:
        categories = json.loads(schema_raw)
    except json.JSONDecodeError:
        # Fallback: hard-coded HIPAA Safe Harbor minimal set
        categories = [
            {"entity_type": "NAME", "hipaa_category": "A", "authority_citation": "45 CFR 164.514(b)(2)(i)(A)", "description": "Names"},
            {"entity_type": "DATE", "hipaa_category": "C", "authority_citation": "45 CFR 164.514(b)(2)(i)(C)", "description": "Dates except year"},
            {"entity_type": "PHONE", "hipaa_category": "D", "authority_citation": "45 CFR 164.514(b)(2)(i)(D)", "description": "Phone numbers"},
            {"entity_type": "EMAIL", "hipaa_category": "F", "authority_citation": "45 CFR 164.514(b)(2)(i)(F)", "description": "Email addresses"},
            {"entity_type": "SSN", "hipaa_category": "G", "authority_citation": "45 CFR 164.514(b)(2)(i)(G)", "description": "Social security numbers"},
            {"entity_type": "MRN", "hipaa_category": "H", "authority_citation": "45 CFR 164.514(b)(2)(i)(H)", "description": "Medical record numbers"},
            {"entity_type": "ADDRESS", "hipaa_category": "B", "authority_citation": "45 CFR 164.514(b)(2)(i)(B)", "description": "Geographic subdivisions"},
        ]

    # Generate records in batches of 10 to avoid hitting token limits
    BATCH = 10
    all_records: list[dict] = []
    batch_num = 0

    while len(all_records) < n_records:
        remaining = n_records - len(all_records)
        batch_size = min(BATCH, remaining)

        # Shuffle categories so each batch covers different entity types
        shuffled = categories[:]
        rng.shuffle(shuffled)
        focus_cats = shuffled[:min(5, len(shuffled))]
        focus_json = json.dumps(focus_cats, indent=2)

        record_prompt = f"""\
Generate {batch_size} synthetic PHI test records for {jurisdiction} compliance benchmarking.
Each record must contain realistic-looking (but entirely FICTIONAL) clinical text.
Focus on these entity types this batch:
{focus_json}

Rules:
- ALL values are SYNTHETIC / FICTIONAL. No real people, no real PHI.
- Each record has a unique record_id starting with "llm_{jurisdiction.lower()}_{batch_num:03d}_"
- Include at least 2 different entity types per record
- gold_spans must have correct character offsets into the text string
- detection_regime: "rule_applicable" for structured, "contextual_ner_required" for free-text

Respond with a JSON array of records matching exactly this schema:
[
  {{
    "record_id": "llm_hipaa_000_001",
    "text": "Patient Jane Doe, DOB 1985-03-12, MRN 4829301, SSN 123-45-6789...",
    "jurisdiction": "{jurisdiction}",
    "gold_spans": [
      {{
        "start": 8,
        "end": 16,
        "entity_type": "NAME",
        "hipaa_category": "A",
        "authority_citation": "45 CFR 164.514(b)(2)(i)(A)",
        "detection_regime": "contextual_ner_required"
      }}
    ]
  }}
]

JSON array only. No prose."""

        raw = client.complete(record_prompt)
        raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw.strip())
        try:
            batch_records = json.loads(raw)
            if isinstance(batch_records, list):
                all_records.extend(batch_records)
        except json.JSONDecodeError as exc:
            print(f"  Warning: batch {batch_num} JSON parse error ({exc}); skipping")

        batch_num += 1
        if batch_num > n_records // BATCH + 5:
            # Prevent runaway loop if LLM keeps failing
            break

    # Validate spans and write
    from generators.common import GoldSpan, Record, write_jsonl

    validated: list[Record] = []
    errors: list[str] = []
    for raw_rec in all_records[:n_records]:
        spans = []
        for s in raw_rec.get("gold_spans", []):
            spans.append(GoldSpan(
                start=int(s["start"]),
                end=int(s["end"]),
                entity_type=s.get("entity_type", "UNKNOWN"),
                hipaa_category=s.get("hipaa_category", ""),
                jurisdiction=raw_rec.get("jurisdiction", jurisdiction),
                authority_citation=s.get("authority_citation", ""),
                detection_regime=s.get("detection_regime", "TEXT"),
            ))
        rec = Record(
            record_id=raw_rec["record_id"],
            text=raw_rec.get("text", raw_rec.get("query", "")),
            gold_spans=spans,
            jurisdiction=raw_rec.get("jurisdiction", jurisdiction),
        )
        span_errs = rec.verify_spans()
        if span_errs:
            errors.extend(f"{rec.record_id}: {e}" for e in span_errs)
        validated.append(rec)

    out_file = llm_dir / f"{jurisdiction.lower()}_llm.jsonl"
    count = write_jsonl(validated, out_file)
    span_count = sum(len(r.gold_spans) for r in validated)
    file_hash = hashlib.sha256(out_file.read_bytes()).hexdigest() if out_file.exists() else ""

    status = "OK" if not errors else f"ERRORS({len(errors)})"
    print(f"  llm_{jurisdiction.lower():28s} {count:4d} records  {span_count:5d} spans  {status}")

    summary = {
        f"llm_{jurisdiction.lower()}": {
            "path": str(out_file.relative_to(PROJECT_ROOT)),
            "records": count,
            "spans": span_count,
            "sha256": file_hash,
            "span_errors": errors,
        },
        "__total__": {
            "records": count,
            "spans": span_count,
            "jurisdiction": jurisdiction.lower(),
            "seed": seed,
        },
    }
    return summary


def build_manifest(seed: int, summaries: dict, out_dir: Path) -> list:
    """Write MANIFEST.json to out_dir.

    summaries has structure: {jurisdiction: {generator_name: {...}, "__total__": {...}}}
    """
    all_errors = []
    all_files = {}
    all_totals = {}

    for jurisdiction in sorted(summaries):
        juris_summary = summaries[jurisdiction]
        for name, info in juris_summary.items():
            if name == "__total__":
                all_totals[jurisdiction] = info
                continue
            all_errors.extend(info.get("span_errors", []))
            all_files[f"{jurisdiction}/{name}"] = {
                "path": info["path"],
                "records": info["records"],
                "spans": info["spans"],
                "sha256": info["sha256"],
            }

    manifest = {
        "version": "2.0.0-dev",
        "seed": seed,
        "jurisdictions": sorted(summaries),
        "files": all_files,
        "totals": all_totals,
        "span_errors": all_errors,
        "validation_status": "PASS" if not all_errors else "FAIL",
        "claim_level": "L2-partial",
        "capability_registry_sha256": hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    manifest_path = out_dir / "MANIFEST.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"\nMANIFEST.json written to {manifest_path}")
    return all_errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate PHI test corpus")
    parser.add_argument("--seed", type=int, default=42, help="PRNG seed (default: 42)")
    parser.add_argument(
        "--jurisdiction",
        default="us",
        help="Jurisdiction layer: 'us', 'all', 'in', 'eu', 'br', 'au', 'ug', or 'file_formats' (default: us)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "corpus",
        help="Output directory (default: corpus/)",
    )
    parser.add_argument(
        "--mode",
        choices=["seeded", "llm"],
        default="seeded",
        help="Generation mode: 'seeded' (deterministic Python generators) or 'llm' (LLM-generated records)",
    )
    parser.add_argument(
        "--n-records",
        type=int,
        default=50,
        help="Number of records for --mode llm (default: 50)",
    )
    args = parser.parse_args(argv)

    print(f"Generating corpus: seed={args.seed}, jurisdiction={args.jurisdiction}, mode={args.mode}")
    print(f"Output directory: {args.out_dir}")
    print()

    if args.mode == "llm":
        jurisdiction = args.jurisdiction.upper() if args.jurisdiction not in ("us", "all") else "HIPAA"
        print(f"LLM corpus generation: jurisdiction={jurisdiction}, n_records={args.n_records}")
        all_summaries = {
            jurisdiction.lower(): build_llm_corpus(
                args.seed, args.out_dir, jurisdiction=jurisdiction, n_records=args.n_records
            )
        }
    else:
        all_summaries = build_seeded_corpus(args.seed, args.out_dir, args.jurisdiction)

    errors = build_manifest(args.seed, all_summaries, args.out_dir)

    total_records = sum(
        s["__total__"]["records"]
        for s in all_summaries.values()
        if "__total__" in s
    )
    total_spans = sum(
        s["__total__"]["spans"]
        for s in all_summaries.values()
        if "__total__" in s
    )

    print(f"\nTotal: {total_records} records, {total_spans} spans")

    if errors:
        print(f"\nSPAN ERRORS ({len(errors)}):")
        for error in errors:
            print(f"  {error}")
        return 1

    print("All span offsets verified. Corpus PASS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
