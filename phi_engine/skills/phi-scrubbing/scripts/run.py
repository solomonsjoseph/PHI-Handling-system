#!/usr/bin/env python3
"""Skill entrypoint: phi-scrubbing.

Runs the fail-closed PHI scrub over the staged dataset JSONL (rewrites in place,
emits the per-dataset audit ledgers, quarantines un-scrubbable rows). Defaults
to partial-publish-on-review mode (one bad form never blocks the study); pass
``--strict`` to restore strict-abort. Invoked by the orchestrator as a file-path
subprocess (D3). Emits a value-free SkillResult marker line.
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
    parser = argparse.ArgumentParser(description="Run the fail-closed PHI scrub over staging.")
    add_common_skill_args(parser)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict-abort on the first un-scrubbable row (default: partial-publish-on-review).",
    )
    args = parser.parse_args(argv)

    import config
    from phi_engine.security.phi_scrub import PHIScrubError, run_scrub

    runs_dir = Path(config.OUTPUT_DIR) / args.study / "runs" if args.run_id else None
    try:
        run_scrub(
            args.study,
            run_id=args.run_id,
            runs_dir=runs_dir,
            partial_on_review=not args.strict,
        )
    except PHIScrubError as exc:
        emit_skill_result(
            SkillResult(
                skill="phi-scrubbing",
                ok=False,
                exit_code=1,
                summary=f"scrub fail-closed: {type(exc).__name__}",
                data={"study": args.study, "strict": args.strict},
            )
        )
        print(f"PHI scrub failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    emit_skill_result(
        SkillResult(
            skill="phi-scrubbing",
            ok=True,
            summary="scrub complete",
            data={"study": args.study, "strict": args.strict, "run_id": args.run_id},
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
