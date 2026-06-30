#!/usr/bin/env python3
"""Skill entrypoint: sot-lean-generator (Stage-0 intake passthrough).

Thin passthrough to the per-form Source-Truth intake CLI
(``study_intake.main``), which resolves the annotated PDF + dataset for one form
and produces the deterministic source pack + page renders (or a PHI-metadata-only
human-review note when sources are missing/ambiguous). Invoked by the
orchestrator as a file-path subprocess (D3), once per form. Emits a value-free
SkillResult marker line.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.utils.skill_protocol import SkillResult, emit_skill_result  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from scripts.source_truth.study_intake import main as intake_main

    rc = intake_main(argv)

    study = argv[argv.index("--study") + 1] if "--study" in argv else ""
    form = argv[argv.index("--form") + 1] if "--form" in argv else ""
    emit_skill_result(
        SkillResult(
            skill="sot-lean-generator",
            ok=(rc == 0),
            exit_code=rc,
            summary=f"intake {form or '?'} -> exit {rc}",
            data={"study": study, "form": form},
        )
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
