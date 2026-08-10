"""Corpus generator CLI.

Usage::

    python -m phi_corpus.generate \
        --scenario oncology_v1 \
        --jurisdiction us \
        --edge-cases age_over_89,restricted_zip3,notes_carry_name,clinical_hr_90s \
        --rows 8 \
        --out /tmp/corpus.zip

Writes the corpus ZIP to ``--out`` and prints the ground-truth summary
so a human operator can spot-check what was planted. The full ground-
truth dict is emitted to ``--ground-truth`` (default: adjacent JSON
file). When called via ``/api/corpus/generate`` the ground truth lives
in the session document only and is never persisted to disk.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .edge_cases import EDGE_CASES, all_tags
from .planters import plant
from .scenarios import SCENARIOS, list_scenarios


def _cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="phi_corpus.generate")
    p.add_argument("--scenario", required=True,
                   choices=list(SCENARIOS.keys()))
    p.add_argument("--jurisdiction", default="us")
    p.add_argument("--edge-cases", default="",
                   help=f"comma-separated tags. Available: {','.join(sorted(all_tags()))}")
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", required=True, help="output ZIP path")
    p.add_argument("--ground-truth", default="",
                   help="ground-truth JSON path (default: <out>.groundtruth.json)")
    p.add_argument("--summary-only", action="store_true",
                   help="print scenarios / edge-cases catalog and exit")
    args = p.parse_args(argv)

    if args.summary_only:
        print(json.dumps({
            "scenarios": list_scenarios(),
            "edge_cases": [
                {"tag": e.tag, "label": e.label, "column": e.applies_to_column}
                for e in EDGE_CASES.values()
            ],
        }, indent=2))
        return 0

    tags = [t.strip() for t in args.edge_cases.split(",") if t.strip()]
    art = plant(
        scenario_id=args.scenario,
        jurisdiction=args.jurisdiction,
        edge_case_tags=tags,
        row_count=args.rows,
        seed=args.seed,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(art.zip_bytes)

    gt_path = Path(args.ground_truth) if args.ground_truth else out.with_suffix(".groundtruth.json")
    gt_path.write_text(json.dumps(art.ground_truth, indent=2))

    # Summary to stdout for the operator
    print(json.dumps({
        "output_zip": str(out),
        "ground_truth": str(gt_path),
        "scenario": args.scenario,
        "jurisdiction": args.jurisdiction,
        "edge_cases": tags,
        "summary": art.ground_truth_summary,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
