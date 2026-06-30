#!/usr/bin/env python3
"""Skill entrypoint: audit-verification (Phase 9).

Runs the 17-assertion post-publish verifier for a completed run (manifest
reconciliation, ledger hashes + no-LLM sentinel, quarantine-empty, PHI absence
scan, decided-vs-applied protection lattice, ledger coverage completeness, …) by
delegating to the trusted ``extract_to_llm_source verify`` path, which owns the
assertion suite and its exit-code routing. Invoked by the orchestrator as a
file-path subprocess (D3). Emits a value-free SkillResult carrying the verifier
exit code.
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
    parser = argparse.ArgumentParser(description="Run the 17-assertion publish verifier.")
    add_common_skill_args(parser)
    args = parser.parse_args(argv)

    from scripts.skills.extract_to_llm_source import EXIT_OK
    from scripts.skills.extract_to_llm_source import main as ext_main

    forwarded = ["verify", "--study", args.study]
    if args.run_id:
        forwarded += ["--run", args.run_id]
    rc = ext_main(forwarded)

    emit_skill_result(
        SkillResult(
            skill="audit-verification",
            ok=(rc == EXIT_OK),
            exit_code=rc,
            summary=f"verifier -> exit {rc}",
            data={"study": args.study, "run_id": args.run_id},
        )
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
