"""Generate the USA HIPAA Safe Harbor A-R tabular corpus package."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from generators.hipaa_18_tabular import USHIPAA18TabularCorpusGenerator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the deterministic USA HIPAA A-R tabular corpus"
    )
    parser.add_argument("--seed", type=int, default=42, help="PRNG seed (default: 42)")
    parser.add_argument(
        "--n-subjects",
        type=int,
        default=18,
        help="Synthetic subject rows (default: 18)",
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="Output directory")
    parser.add_argument(
        "--no-edge-cases",
        action="store_true",
        help="Write only the canonical A-R baseline",
    )
    args = parser.parse_args(argv)

    try:
        report = USHIPAA18TabularCorpusGenerator(seed=args.seed).write(
            args.out_dir,
            n_subjects=args.n_subjects,
            include_edge_cases=not args.no_edge_cases,
        )
    except (OSError, ValueError) as exc:
        print(f"Corpus generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"HIPAA categories: {report['categories']}/18")
    print(f"Subjects: {report['subjects']}")
    print(f"Baseline validation: {report['validation_status']}")
    print(f"Edge cases: {report['edge_cases']}")
    print(f"Output: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
