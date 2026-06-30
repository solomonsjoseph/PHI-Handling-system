#!/usr/bin/env python3
"""Skill entrypoint: phi-classification (header-only PHI handling gate).

Runs the header-only PHI handling review for the study's manifest-approved forms
*before any row value is opened*: classifies each header by jurisdiction, cross-
verifies direct identifiers against the SoT, and writes the authoritative
``phi_handling_approval.json`` into the run dir (consumed downstream by the
scrub). Invoked by the orchestrator as a file-path subprocess (D3). Emits a
value-free SkillResult marker line — form NAMES + counts only.

The authoritative gate logic lives in the dataset-to-llm-source skill
(``extract_to_llm_source._run_form_approval_gate``); this entrypoint reaches it
through the canonical ``scripts.skills.*`` name (resolved by the consolidation
finder), which is the allowed plugins→scripts direction.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from phi_engine.utils.skill_protocol import (  # noqa: E402
    SkillResult,
    add_common_skill_args,
    emit_skill_result,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Header-only PHI handling classification gate.")
    add_common_skill_args(parser)
    parser.add_argument(
        "--form",
        action="append",
        default=[],
        dest="forms",
        help="Restrict to a specific form (repeatable); omit for all manifest forms.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        dest="max_workers",
        help="Cap the header-review thread pool (default: auto).",
    )
    args = parser.parse_args(argv)

    if not args.run_dir:
        print("phi-classification requires --run-dir", file=sys.stderr)
        return 2

    import config
    from scripts.skills.extract_to_llm_source import _run_form_approval_gate

    study_raw_dir = Path(config.RAW_DATA_DIR) / args.study
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    gate = _run_form_approval_gate(
        study=args.study,
        study_raw_dir=study_raw_dir,
        run_dir=run_dir,
        max_workers=args.max_workers,
        selected_forms=tuple(args.forms),
    )

    emit_skill_result(
        SkillResult(
            skill="phi-classification",
            ok=True,  # holding forms is a normal, non-error outcome (partial publish)
            summary=(
                f"{len(gate.approved_forms)} approved, {len(gate.held_forms)} held"
                + (" (partial)" if gate.partial else "")
            ),
            data={
                "study": args.study,
                "approved_forms": sorted(gate.approved_forms),
                "held_forms": sorted(gate.held_forms),
                "partial": bool(gate.partial),
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
