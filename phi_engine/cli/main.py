"""Standalone PHI pipeline CLI.

    python -m phi_engine intake   --study S --source PATH [--workspace W]
    python -m phi_engine organize --study S [--workspace W]
    python -m phi_engine run      --study S --jurisdiction us [--workspace W]
    python -m phi_engine review   --study S list [--workspace W]
    python -m phi_engine review   --study S decide --header H --decision keep|drop|override [--action ACTION] [--workspace W]
    python -m phi_engine status   --study S [--workspace W]

Every subcommand accepts ``--workspace`` (sets ``PHI_WORKSPACE``) and
``--study`` (sets ``STUDY_NAME``), and sets BOTH env vars BEFORE importing
``phi_engine.config.config`` -- the same import-time-resolution pattern
``harness/run_phi_system.py`` used for ``STUDY_NAME``. ``--jurisdiction``
choices stay ``us``: pinned rule specs currently exist only for USA
(``phi_engine/security/phi_review.py`` ``_PINNED_RULE_SPECS``). Extending to
another jurisdiction needs its own pinned rule-spec entries with authority
remain generator-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _set_workspace_env(args: argparse.Namespace) -> None:
    """Set PHI_WORKSPACE/STUDY_NAME BEFORE any phi_engine.config import."""
    if getattr(args, "workspace", None):
        os.environ["PHI_WORKSPACE"] = str(Path(args.workspace).resolve())
    os.environ["STUDY_NAME"] = args.study
    from phi_engine.config import config

    config.get_local_llm_config()


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--study", required=True, help="Study name (plain folder name)")
    parser.add_argument("--workspace", default=None, help="Workspace root (sets PHI_WORKSPACE)")


def _cmd_intake(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    from phi_engine.pipeline.intake import intake_add

    manifest = intake_add(Path(args.source), args.study)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f"intake: {len(manifest['entries'])} linked, {len(manifest['duplicates'])} duplicates, "
        f"{len(manifest['errors'])} errors",
        file=sys.stderr,
    )
    return 0


def _cmd_organize(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    from phi_engine.pipeline.organize import organize

    manifest = organize(args.study)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f"organize: {len(manifest['datasets'])} datasets, "
        f"{len(manifest['review_bucket'])} in review bucket",
        file=sys.stderr,
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    from phi_engine.pipeline.run import run_pipeline

    result = run_pipeline(args.study, args.jurisdiction)
    print(json.dumps(result.to_json(), indent=2, sort_keys=True))
    print(f"run: exit_code={result.exit_code} -- {result.message}", file=sys.stderr)
    return result.exit_code


def _cmd_review(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    from phi_engine.pipeline.review import decide as review_decide
    from phi_engine.pipeline.review import decide_dependency, list_review_items

    if args.review_action == "list":
        items = list_review_items(args.study)
        print(json.dumps(items, indent=2, sort_keys=True))
        return 0

    if args.review_action == "dependency-decide":
        try:
            decision = decide_dependency(
                args.study,
                dataset=args.dataset,
                recommendation=args.recommendation,
                support=args.support,
                level=args.level,
                sensitivity=args.sensitivity,
                reason_code=args.reason_code,
                detail_file=Path(args.detail_file) if args.detail_file else None,
                decided_by=args.decided_by,
            )
        except (OSError, ValueError):
            print("dependency decision rejected", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "decision_id": decision.decision_id,
                    "recommendation_id": decision.recommendation_id,
                    "dataset_artifact_id": decision.dataset_artifact_id,
                },
                sort_keys=True,
            )
        )
        return 0

    # decide
    if not args.header or not args.decision:
        print("review decide requires --header and --decision", file=sys.stderr)
        return 2
    if args.decision == "override" and not args.action:
        print("review decide --decision override requires --action", file=sys.stderr)
        return 2
    review_decide(
        args.study, header=args.header, decision=args.decision, action=args.action,
        decided_by=args.decided_by or "cli", source="cli",
    )
    print(f"recorded decision: {args.header} -> {args.decision}" + (f" ({args.action})" if args.action else ""))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    import phi_engine.config.config as config

    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    if not runs_dir.is_dir():
        print(json.dumps({"study": args.study, "runs": []}, indent=2))
        return 0
    run_ids = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
    latest: dict[str, Any] | None = None
    if run_ids:
        result_path = runs_dir / run_ids[-1] / "pipeline_result.json"
        if result_path.is_file():
            latest = json.loads(result_path.read_text(encoding="utf-8"))
    print(json.dumps({"study": args.study, "runs": run_ids, "latest_result": latest}, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phi_engine", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_intake = sub.add_parser("intake", help="Symlink-intake a source tree for a study")
    _add_common_args(p_intake)
    p_intake.add_argument("--source", required=True, help="Source directory to intake (never modified)")
    p_intake.set_defaults(func=_cmd_intake)

    p_organize = sub.add_parser("organize", help="Route intake files into normalized dataset JSONL")
    _add_common_args(p_organize)
    p_organize.set_defaults(func=_cmd_organize)

    p_run = sub.add_parser("run", help="Run the full organize->classify->scrub->publish pipeline")
    _add_common_args(p_run)
    p_run.add_argument("--jurisdiction", required=True, choices=["us"])
    p_run.set_defaults(func=_cmd_run)

    p_review = sub.add_parser("review", help="List or decide on held/uncertain PHI review items")
    _add_common_args(p_review)
    review_sub = p_review.add_subparsers(dest="review_action", required=True)
    review_sub.add_parser("list")
    p_decide = review_sub.add_parser("decide")
    p_decide.add_argument("--header", required=True)
    p_decide.add_argument("--decision", required=True, choices=["keep", "drop", "override"])
    p_decide.add_argument("--action", default=None, help="Required when --decision override")
    p_decide.add_argument("--decided-by", default=None)
    p_dependency = review_sub.add_parser(
        "dependency-decide",
        help="Record a decision for a current dependency recommendation",
    )
    p_dependency.add_argument("--dataset", required=True)
    p_dependency.add_argument("--recommendation", required=True)
    p_dependency.add_argument("--support", default=None)
    p_dependency.add_argument(
        "--level",
        required=True,
        choices=["required", "helpful", "ignored"],
    )
    p_dependency.add_argument(
        "--sensitivity",
        required=True,
        choices=["confidential", "non_confidential"],
    )
    p_dependency.add_argument(
        "--reason-code",
        required=True,
        choices=[
            "manifest_declared",
            "same_stem_companion",
            "exact_header_match",
            "only_interpretation",
            "transform_parameters_missing",
        ],
    )
    p_dependency.add_argument("--detail-file", default=None)
    p_dependency.add_argument("--decided-by", required=True)
    p_review.set_defaults(func=_cmd_review)

    p_status = sub.add_parser("status", help="Show the latest run status for a study")
    _add_common_args(p_status)
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        if exc.__class__.__name__ == "LocalLLMConfigurationError":
            print("invalid local LLM configuration", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
