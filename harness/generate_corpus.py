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
from pathlib import Path

# Resolve project root (two levels up from this file)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from generators import (
    HIPAASafeHarborGenerator,
    HIPAAQuasiIdentifierGenerator,
    HIPAALDSGenerator,
    HIPAAReIDCodesGenerator,
    HIPAAFundraisingGenerator,
    HIPAAVerificationGenerator,
    HIPAABiometricGenerator,
    HIPAADeviceGenerator,
    HIPAAFaxGenerator,
    HIPAAVehicleGenerator,
)
from generators.common import Record, write_jsonl


def build_us_corpus(seed: int, out_dir: Path) -> dict:
    """Build the full USA/HIPAA corpus layer.

    Returns a summary dict with record counts and per-file hashes.
    """
    us_dir = out_dir / "us"
    us_dir.mkdir(parents=True, exist_ok=True)

    summary = {}

    generators = [
        ("hipaa_safe_harbor", HIPAASafeHarborGenerator(seed), lambda g: g.generate_batch(10)),
        ("hipaa_quasi_identifiers", HIPAAQuasiIdentifierGenerator(seed), lambda g: g.generate_batch(50)),
        ("hipaa_lds", HIPAALDSGenerator(seed), lambda g: g.generate_batch(20)),
        ("hipaa_reid_codes", HIPAAReIDCodesGenerator(seed), lambda g: g.generate_batch(20)),
        ("hipaa_fundraising", HIPAAFundraisingGenerator(seed), lambda g: g.generate_batch(20)),
        ("hipaa_verification", HIPAAVerificationGenerator(seed), lambda g: g.generate_batch(20)),
        ("hipaa_biometric", HIPAABiometricGenerator(seed), lambda g: g.generate_batch(count_per_mode=4)),
        ("hipaa_device", HIPAADeviceGenerator(seed), lambda g: g.generate_batch(count_per_mode=4)),
        ("hipaa_fax", HIPAAFaxGenerator(seed), lambda g: g.generate_batch(count_per_mode=4)),
        ("hipaa_vehicle", HIPAAVehicleGenerator(seed), lambda g: g.generate_batch(count_per_mode=4)),
    ]

    total_records = 0
    total_spans = 0

    for name, gen, fn in generators:
        records = fn(gen)
        path = us_dir / f"{name}.jsonl"
        count = write_jsonl(records, path)

        # Span count and file hash
        span_count = sum(len(r.gold_spans) for r in records)
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()

        # Verify all spans
        errors = []
        for r in records:
            errs = r.verify_spans()
            if errs:
                errors.extend(f"{r.record_id}: {e}" for e in errs)

        summary[name] = {
            "path": str(path.relative_to(PROJECT_ROOT)),
            "records": count,
            "spans": span_count,
            "sha256": file_hash,
            "span_errors": errors,
        }
        total_records += count
        total_spans += span_count

        status = "OK" if not errors else f"ERRORS({len(errors)})"
        print(f"  {name:35s} {count:4d} records  {span_count:5d} spans  {status}")

    summary["__total__"] = {
        "records": total_records,
        "spans": total_spans,
        "jurisdiction": "us",
        "seed": seed,
    }
    return summary


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

    for jurisdiction, juris_summary in summaries.items():
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
        "jurisdictions": list(summaries.keys()),
        "files": all_files,
        "totals": all_totals,
        "span_errors": all_errors,
        "validation_status": "PASS" if not all_errors else "FAIL",
    }

    manifest_path = out_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"\nMANIFEST.json written to {manifest_path}")
    return all_errors


def main():
    parser = argparse.ArgumentParser(description="Generate PHI test corpus")
    parser.add_argument("--seed", type=int, default=42, help="PRNG seed (default: 42)")
    parser.add_argument(
        "--jurisdiction",
        default="us",
        help="Jurisdiction layer: 'us', 'all', or jurisdiction name for --mode llm (default: us)",
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
    args = parser.parse_args()

    print(f"Generating corpus: seed={args.seed}, jurisdiction={args.jurisdiction}, mode={args.mode}")
    print(f"Output directory: {args.out_dir}")
    print()

    all_summaries = {}

    if args.mode == "llm":
        jurisdiction = args.jurisdiction.upper() if args.jurisdiction not in ("us", "all") else "HIPAA"
        print(f"LLM corpus generation: jurisdiction={jurisdiction}, n_records={args.n_records}")
        all_summaries[jurisdiction.lower()] = build_llm_corpus(
            args.seed, args.out_dir, jurisdiction=jurisdiction, n_records=args.n_records
        )
    elif args.jurisdiction in ("us", "all"):
        print("USA / HIPAA layer:")
        all_summaries["us"] = build_us_corpus(args.seed, args.out_dir)

    errors = build_manifest(args.seed, all_summaries, args.out_dir)

    # Totals
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
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("All span offsets verified. Corpus PASS.")


if __name__ == "__main__":
    main()
