"""Corpus generator CLI.

Single-scenario usage (unchanged)::

    python -m phi_corpus.generate \\
        --scenario oncology_v1 \\
        --jurisdiction us \\
        --edge-cases age_over_89,restricted_zip3,notes_carry_name,clinical_hr_90s \\
        --rows 8 \\
        --out /tmp/corpus.zip

Writes the corpus ZIP to ``--out`` and prints the ground-truth summary so a
human operator can spot-check what was planted. The full ground-truth dict
is emitted to ``--ground-truth`` (default: adjacent JSON file). When called
via ``/api/corpus/generate`` the ground truth lives in the session document
only and is never persisted to disk.

Ladder/campaign usage (new)::

    python -m phi_corpus.generate --campaign --tier all --offline --jobs 4 \\
        --out-dir test_reports/corpus/$(date -u +%Y%m%dT%H%M%SZ)

    python -m phi_corpus.generate --campaign --tier all --online \\
        --base-url http://127.0.0.1:8001 --jobs 3 --warmup

``--campaign`` runs the ladder instead of one scenario and writes
``campaign_report.{json,md}`` to ``--out-dir``. Exit code is 0 when
every entry is leak-clean, 1 when any entry recorded an error, and 2 when an
entry in any tier is not leak-clean.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .edge_cases import EDGE_CASES, all_tags
from .planters import plant
from .scenarios import SCENARIOS, list_scenarios


def _cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="phi_corpus.generate")
    p.add_argument("--scenario", choices=list(SCENARIOS.keys()))
    p.add_argument("--jurisdiction", default="us")
    p.add_argument("--edge-cases", default="",
                   help=f"comma-separated tags. Available: {','.join(sorted(all_tags()))}")
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", help="output ZIP path (required outside --campaign)")
    p.add_argument("--ground-truth", default="",
                   help="ground-truth JSON path (default: <out>.groundtruth.json)")
    p.add_argument("--summary-only", action="store_true",
                   help="print scenarios / edge-cases catalog and exit")

    p.add_argument("--tier", choices=["L0", "L1", "L2", "L3", "all"],
                   help="select ladder rungs; implies --campaign")
    p.add_argument("--campaign", action="store_true", help="run the ladder instead of one scenario")
    p.add_argument("--offline", action="store_true",
                   help="deterministic replay mode (default for --campaign)")
    p.add_argument("--online", action="store_true", help="full-pipeline mode")
    p.add_argument("--base-url", default="http://localhost:8001")
    p.add_argument("--token", default=os.environ.get("API_TOKEN", ""))
    p.add_argument("--jobs", type=int, default=0,
                   help="default min(4, cpu_count) offline, 3 online")
    p.add_argument("--warmup", dest="warmup", action="store_true", default=True)
    p.add_argument("--no-warmup", dest="warmup", action="store_false")
    p.add_argument("--unmatched", choices=["human_review", "oracle", "drop"],
                   default="human_review", help="offline only")
    p.add_argument("--out-dir", default="",
                   help="default test_reports/corpus/<UTC timestamp>")
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

    if args.campaign or args.tier:
        return _cli_campaign(args)

    if not args.scenario:
        p.error("--scenario is required outside --campaign mode")
    if not args.out:
        p.error("--out is required outside --campaign mode")

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


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _cli_campaign(args: argparse.Namespace) -> int:
    from . import campaign as _campaign
    from . import report as _report
    from .tiers import ladder_for

    entries = ladder_for(args.tier or "all")
    out_dir = Path(args.out_dir) if args.out_dir else Path("test_reports/corpus") / _utc_stamp()

    online = bool(args.online)
    if online:
        jobs = args.jobs or 3
        result = asyncio.run(_campaign.run_online(
            entries, base_url=args.base_url, token=args.token, jobs=jobs, warmup=args.warmup,
        ))
    else:
        jobs = args.jobs or min(4, os.cpu_count() or 1)
        result = _campaign.run_offline(entries, jobs=jobs, unmatched=args.unmatched)

    paths = _report.write(result, out_dir)
    errors = result.rollup.get("errors", 0)
    print(json.dumps({
        "corpus_version": result.corpus_version,
        "mode": result.mode,
        "elapsed_s": result.elapsed_s,
        "jobs": result.jobs,
        "report_json": paths["json"],
        "report_markdown": paths["markdown"],
        "errors": errors,
    }, indent=2))

    if errors:
        return 1
    for e in result.entries:
        if e.get("error"):
            continue
        leak_status = ((e.get("report") or {}).get("leak") or {}).get("status")
        if leak_status != "clean":
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
