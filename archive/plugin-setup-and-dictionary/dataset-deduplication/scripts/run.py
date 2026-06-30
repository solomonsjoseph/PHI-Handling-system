#!/usr/bin/env python3
"""Skill entrypoint: dataset-deduplication (Phase 2).

Deduplicates **raw** dataset files under ``data/raw/{STUDY}/datasets/`` using
filename normalization and header/row-count-only tiers (Note 4). Never reads
cell values. Ambiguous groups route to the form-first review queue
``audit/human_review/{group}/`` (Note 22); auto-resolved merges leave a value-free
record under ``audit/dataset_dedup/``. Invoked by the orchestrator before SoT
generation and extraction.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.utils.skill_protocol import (  # noqa: E402
    SkillResult,
    add_common_skill_args,
    emit_skill_result,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicate raw dataset files (headers + row counts only)."
    )
    add_common_skill_args(parser)
    args = parser.parse_args(argv)

    import config
    from scripts.extraction.raw_file_dedup import dedup_raw_datasets

    datasets_dir = Path(config.RAW_DATA_DIR) / args.study / "datasets"
    audit_dir = Path(config.STUDY_AUDIT_DIR)
    archive_dir = Path(config.TMP_DIR) / args.study / "dedup_archive"

    try:
        report = dedup_raw_datasets(
            args.study,
            datasets_dir=datasets_dir,
            audit_dir=audit_dir,
            archive_dir=archive_dir,
            run_dir=Path(args.run_dir) if args.run_dir else None,
        )
    except Exception as exc:
        emit_skill_result(
            SkillResult(
                skill="dataset-deduplication",
                ok=False,
                exit_code=1,
                summary=f"dedup failed: {type(exc).__name__}",
                data={"study": args.study},
            )
        )
        print(f"dataset-deduplication failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    held = len(report.held_for_review)
    emit_skill_result(
        SkillResult(
            skill="dataset-deduplication",
            ok=True,
            exit_code=0,
            summary=(
                f"{len(report.auto_resolved)} auto-resolved, "
                f"{held} held for review, {len(report.removed_paths)} archived"
            ),
            data={
                "study": args.study,
                "groups_scanned": report.groups_scanned,
                "auto_resolved": len(report.auto_resolved),
                "held_for_review": held,
                "archived": len(report.removed_paths),
                "errors": len(report.errors),
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
