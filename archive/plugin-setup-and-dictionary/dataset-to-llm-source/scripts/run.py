#!/usr/bin/env python3
"""Skill entrypoint: dataset-to-llm-source.

Thin passthrough to the trusted host publish CLI
(``extract_to_llm_source.main``) which drives PHI review → scrub → promote →
PHI guard gate → staging destruction, with ``run`` / ``verify`` / ``status``
subcommands and the canonical exit-code contract. This entrypoint forwards argv
verbatim and adds the value-free SkillResult marker the orchestrator reads.

Invoked by the orchestrator as a file-path subprocess (D3), e.g.::

    run.py run --study Indo-VAP
    run.py verify --study Indo-VAP --run run_<id>
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.utils.skill_protocol import SkillResult, emit_skill_result  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from scripts.skills.extract_to_llm_source import (
        EXIT_OK,
        EXIT_PARTIAL_REVIEW,
    )
    from scripts.skills.extract_to_llm_source import (
        main as ext_main,
    )

    rc = ext_main(argv)

    subcommand = next((a for a in argv if not a.startswith("-")), "run")
    study = ""
    if "--study" in argv:
        with contextlib.suppress(IndexError):
            study = argv[argv.index("--study") + 1]

    emit_skill_result(
        SkillResult(
            skill="dataset-to-llm-source",
            ok=rc in {EXIT_OK, EXIT_PARTIAL_REVIEW},
            exit_code=rc,
            summary=f"{subcommand} -> exit {rc}",
            data={"study": study, "subcommand": subcommand},
        )
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
