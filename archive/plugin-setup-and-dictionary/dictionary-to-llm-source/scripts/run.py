#!/usr/bin/env python3
"""Skill entrypoint: dictionary-to-llm-source (extract leg).

Loads the study data dictionary into staging JSONL
(``tmp/{STUDY}/dictionary/``); a later publish step promotes it into
``llm_source/dictionary_mapping/jsonl/``. Invoked by the orchestrator as a
file-path subprocess (D3). Emits a value-free SkillResult marker line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Skills are launched by file path, so the repo root is not on sys.path yet.
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
        description="Extract or publish the study data dictionary leg."
    )
    add_common_skill_args(parser)
    parser.add_argument(
        "--leg",
        choices=["extract", "publish"],
        default="extract",
        help=(
            "extract -> load the dictionary into staging JSONL; "
            "publish -> promote the (propagation-pruned) staging tree into "
            "llm_source/dictionary_mapping/jsonl/. Run --leg publish ONLY after "
            "dataset cleanup-propagation has pruned dropped columns from staging."
        ),
    )
    parser.add_argument(
        "--no-preserve-na",
        action="store_true",
        help="Drop NA tokens instead of preserving them (default: preserve).",
    )
    args = parser.parse_args(argv)

    if args.leg == "publish":
        # Shared publish primitive lives in scripts/ (plugins -> scripts is the
        # only sanctioned dependency direction); the skill consumes it.
        from scripts.pipeline.host_pipeline import publish_dictionary_leg

        published = publish_dictionary_leg()
        emit_skill_result(
            SkillResult(
                skill="dictionary-to-llm-source",
                ok=True,
                exit_code=0,
                summary=(
                    "dictionary published to llm_source"
                    if published
                    else "dictionary publish skipped (empty staging)"
                ),
                data={"study": args.study, "leg": "publish", "published": bool(published)},
            )
        )
        return 0

    from scripts.extraction.load_dictionary import load_study_dictionary

    ok = load_study_dictionary(preserve_na=not args.no_preserve_na)
    emit_skill_result(
        SkillResult(
            skill="dictionary-to-llm-source",
            ok=bool(ok),
            exit_code=0 if ok else 1,
            summary="dictionary extracted to staging" if ok else "dictionary extraction failed",
            data={"study": args.study, "leg": "extract"},
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
