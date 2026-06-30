#!/usr/bin/env python3
"""Skill entrypoint: raw-data-intake (skill 0, setup — NOT an orchestrator phase).

Sorts an unorganized study delivery (flat dump and/or zips) into the canonical
data/raw/<study>/ four-bucket layout and drafts config/<study>/_forms_manifest.yaml.
Classification is filename + extension ONLY (GR-1: no workbook is opened).
Idempotent: a no-op on an already-organized tree unless --force.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import intake  # noqa: E402

from scripts.utils.skill_protocol import (  # noqa: E402
    SkillResult,
    emit_skill_result,
)


def _env_path(name: str) -> Path | None:
    val = os.environ.get(name)
    return Path(val) if val else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sort an unorganized study delivery (skill 0).")
    # --study is OPTIONAL here (unlike other skills): when omitted it is
    # auto-detected with a generic fallback via intake.resolve_study_name, and
    # validated before any filing. --run-id/--run-dir are accepted for interface
    # parity with the other skills but unused (intake is not a DAG phase).
    parser.add_argument(
        "--study",
        default=None,
        help="Study folder under data/raw/; auto-detected (generic fallback) if omitted.",
    )
    parser.add_argument("--run-id", dest="run_id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", dest="run_dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--src", required=True, help="dir or zip of the unorganized delivery")
    parser.add_argument("--force", action="store_true", help="rebuild an already-organized tree")
    parser.add_argument(
        "--add",
        action="store_true",
        help="file NEW files into an already-organized study (never overwrites existing files)",
    )
    parser.add_argument(
        "--prune-source",
        action="store_true",
        help="after filing, delete the loose source files from SRC (never touches data/raw)",
    )
    args = parser.parse_args(argv)

    # Pre-run check: resolve + validate the study folder name BEFORE filing.
    try:
        study, study_source = intake.resolve_study_name(
            args.study, raw_root=_env_path("RPLN_INTAKE_RAW_ROOT")
        )
    except ValueError as exc:
        emit_skill_result(
            SkillResult(
                skill="raw-data-intake",
                ok=False,
                exit_code=2,
                summary=f"invalid study name: {exc}",
                data={"study": args.study},
            )
        )
        return 2

    try:
        result = intake.organize(
            study,
            Path(args.src),
            force=args.force,
            add=args.add,
            prune=args.prune_source,
            raw_root=_env_path("RPLN_INTAKE_RAW_ROOT"),
            config_root=_env_path("RPLN_INTAKE_CONFIG_ROOT"),
            audit_dir=_env_path("RPLN_INTAKE_AUDIT_DIR"),
        )
    except (FileNotFoundError, ValueError, OSError, zipfile.BadZipFile) as exc:
        emit_skill_result(
            SkillResult(
                skill="raw-data-intake",
                ok=False,
                exit_code=2,
                summary=f"intake failed: {exc}",
                data={"study": study},
            )
        )
        return 2

    # Surface the resolved study + how it was resolved so the operator can
    # confirm files went to the right folder (esp. on auto-detect/fallback).
    study_label = f"study={study}" + ("" if study_source == "explicit" else f" ({study_source})")
    if result.skipped:
        summary = f"{study_label}: already organized — skipping"
    else:
        parts = [f"{b}={n}" for b, n in sorted(result.counts.items()) if n]
        if result.already_present:
            parts.append(f"already_present={len(result.already_present)}")
        if result.pruned:
            parts.append(f"pruned={len(result.pruned)}")
        if result.manifest_gaps:
            parts.append(f"manifest_appended={len(result.manifest_gaps)}")
        summary = f"{study_label}: " + ("; ".join(parts) if parts else "no files staged")
    emit_skill_result(
        SkillResult(
            skill="raw-data-intake",
            ok=True,
            exit_code=0,
            summary=summary or "no files staged",
            data={
                "study": study,
                "study_source": study_source,
                "skipped": result.skipped,
                "counts": result.counts,
                "unclassified": result.unclassified,
                "already_present": result.already_present,
                "pruned": result.pruned,
                "manifest_gaps": result.manifest_gaps,
                "manifest_written": result.manifest_written,
                "review_note": result.review_note,
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
