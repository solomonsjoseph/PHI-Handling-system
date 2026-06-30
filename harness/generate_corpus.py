"""
Corpus generation harness.

Runs all seeded generators and writes JSONL output to corpus/<jurisdiction>/.
Every run with the same seed produces bitwise-identical output.

Usage:
    python -m harness.generate_corpus [--seed SEED] [--jurisdiction us] [--out-dir corpus]

Default seed: 42 (IRB-reproducible baseline).
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
        choices=["us", "all"],
        default="us",
        help="Which jurisdiction layer to generate (default: us)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "corpus",
        help="Output directory (default: corpus/)",
    )
    args = parser.parse_args()

    print(f"Generating corpus: seed={args.seed}, jurisdiction={args.jurisdiction}")
    print(f"Output directory: {args.out_dir}")
    print()

    all_summaries = {}

    if args.jurisdiction in ("us", "all"):
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
