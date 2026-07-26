"""Standalone PHI pipeline CLI.

    python -m phi_engine intake   --source PATH [--study S] [--support-confirmed-no-phi] [--workspace W]
    python -m phi_engine organize --study S [--workspace W]
    python -m phi_engine run      --study S --jurisdiction us [--workspace W]
    python -m phi_engine review   --study S list [--workspace W]
    python -m phi_engine review   --study S decide --header H --decision keep|drop|override [--action ACTION] [--workspace W]
    python -m phi_engine status   --study S [--workspace W]

``--study`` is REQUIRED for every subcommand except ``intake``, where it is
the only optional one: without it, intake resolves the study name itself (a
local-only, support-content-only AI boundary, or a random ``study-<hex>``
fallback when no name is inferred) and reports the resolved name in its
receipt. ``--support-confirmed-no-phi`` is intake's positive-consent flag --
it is the only way to permit that local AI naming step when ``--study`` is
omitted; there is no negative "may contain PHI" flag, and the default
(flag absent, ``--study`` absent) performs zero naming-content extraction
and zero model calls.

Every subcommand accepts ``--workspace`` (sets ``PHI_WORKSPACE``). ``--study``,
when supplied, sets ``STUDY_NAME``; both env vars are set BEFORE importing
``phi_engine.config.config``, since that module resolves workspace/study
paths at import time. When ``--study`` is omitted (``intake`` only),
``STUDY_NAME`` is left unset for this invocation -- ``config.INTAKE_DIR`` and
``config.OUTPUT_DIR`` are workspace-relative, not study-relative, so no
import-time study-name fallback can select intake's own directory.
``--jurisdiction`` choices stay ``us``: pinned rule specs currently exist
only for USA (``phi_engine/security/phi_review.py`` ``_PINNED_RULE_SPECS``).
Extending to another jurisdiction needs its own pinned rule-spec entries
grounded in that jurisdiction's authority document set under
``authorities/*.md``.

``intake`` prints ONLY a redacted receipt to stdout --
``{"study": <name>, "status": <status>, "linked": <count>, "review": <count>,
"errors": <count>, "manifest": <protected-manifest-path>}`` -- never entry
paths, review/error detail, or raw exception text; the matching stderr line
is exactly ``intake: study=<study> status=<status> linked=<N> review=<N>
errors=<N>``. Intake status maps directly to the process exit code:
``ready`` -> 0, ``review_required`` -> 8, ``failed`` -> 2. ``organize``
enforces the same boundary for an intake that is not yet ``ready``: it
prints exactly ``intake_review_required`` (exit 8) or ``intake_failed``
(exit 2) and performs no organize work. Any other typed intake/naming/
source/lock failure that reaches the CLI boundary prints only its fixed
public code to stderr -- never a raw exception, path, or traceback -- and
exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Dependency-free (zero phi_engine imports of its own -- see its module
# docstring) -- safe to import eagerly at module scope, unlike every other
# typed exception below, which is imported LAZILY only inside main()'s
# except block because it lives under phi_engine.config/pipeline/utils and
# those packages must not be imported before _set_workspace_env has set
# PHI_WORKSPACE/STUDY_NAME.
from phi_engine.study_name import (
    STUDY_NAME_INVALID_CODE,
    InvalidStudyNameError,
    validate_study_name,
)


def _lexical_workspace_path(raw: str) -> str:
    """Expand ``~`` and, for a relative path, prefix the shell's cwd --
    WITHOUT resolving symlinks, normalizing, or collapsing ``..`` segments.

    Mirrors ``phi_engine.config.config.BASE_DIR``'s lexical-preservation
    contract exactly (same expand-then-cwd-join construction, deliberately
    never ``Path.resolve()``/``os.path.abspath()``): a workspace argument
    with a symlinked component, or a literal ``..`` segment that would
    otherwise silently erase one, must reach the descriptor-relative
    NOFOLLOW ancestry walkers in ``phi_engine.utils.pipeline_lock``/
    ``phi_engine.pipeline.intake`` with that evidence intact, or those
    walks (which reject symlinks AND ``..``/``.`` segments themselves) can
    never see it to reject it with ``intake-tree-unsafe``.
    """
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else Path.cwd() / path)


def _set_workspace_env(args: argparse.Namespace) -> None:
    """Set PHI_WORKSPACE/STUDY_NAME BEFORE any phi_engine.config import.

    ``STUDY_NAME`` is set only when ``--study`` was actually supplied: for
    every subcommand but ``intake`` that is always (``--study`` is
    required), but intake's own study resolution is explicit-argument-driven
    and workspace-relative (never study-relative), so a study-less intake
    invocation must not let config's import-time env fallback claim any
    study identity for this process.

    An explicit ``--study`` is validated through the shared dependency-free
    :func:`validate_study_name` BEFORE it is written to ``STUDY_NAME`` or
    ``phi_engine.config.config`` is imported -- an invalid value must never
    reach config's own (defense-in-depth) STUDY_NAME check, whose failure
    partway through config's module execution is what previously produced
    a raw, chained traceback instead of this module's fixed-code contract.
    """
    if getattr(args, "workspace", None):
        os.environ["PHI_WORKSPACE"] = _lexical_workspace_path(args.workspace)
    study = getattr(args, "study", None)
    if study:
        validate_study_name(study)
        os.environ["STUDY_NAME"] = study
    else:
        # An intake invocation that omits --study must be study-neutral
        # even under an inherited STUDY_NAME (e.g. a stale parent-shell
        # export) -- otherwise config's import-time fallback resolution
        # would still pick it up despite never being supplied on this
        # command line.
        os.environ.pop("STUDY_NAME", None)
    from phi_engine.config import config

    config.get_local_llm_config()


def _add_common_args(parser: argparse.ArgumentParser, *, study_required: bool = True) -> None:
    parser.add_argument(
        "--study", required=study_required, default=None, help="Study name (plain folder name)"
    )
    parser.add_argument("--workspace", default=None, help="Workspace root (sets PHI_WORKSPACE)")


_INTAKE_STATUS_EXIT_CODES = {"ready": 0, "review_required": 8, "failed": 2}


def _cmd_intake(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    from phi_engine.config import config
    from phi_engine.pipeline.intake import intake_add

    manifest = intake_add(
        Path(args.source), args.study, support_confirmed_no_phi=args.support_confirmed_no_phi
    )
    study = manifest["study"]
    status = manifest["status"]
    linked = len(manifest["entries"])
    review = len(manifest["review_items"])
    errors = len(manifest["errors"])
    manifest_path = Path(config.INTAKE_DIR) / study / "intake_manifest.json"

    print(
        json.dumps(
            {
                "study": study,
                "status": status,
                "linked": linked,
                "review": review,
                "errors": errors,
                "manifest": str(manifest_path),
            },
            separators=(",", ":"),
        )
    )
    print(
        f"intake: study={study} status={status} linked={linked} review={review} errors={errors}",
        file=sys.stderr,
    )
    return _INTAKE_STATUS_EXIT_CODES[status]


def _cmd_organize(args: argparse.Namespace) -> int:
    _set_workspace_env(args)
    from phi_engine.pipeline.intake import IntakeNotReadyError
    from phi_engine.pipeline.organize import organize

    try:
        manifest = organize(args.study)
    except IntakeNotReadyError as exc:
        if exc.status == "review_required":
            print("intake_review_required", file=sys.stderr)
            return 8
        print("intake_failed", file=sys.stderr)
        return 2

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
    _add_common_args(p_intake, study_required=False)
    p_intake.add_argument("--source", required=True, help="Source directory to intake (never modified)")
    p_intake.add_argument(
        "--support-confirmed-no-phi",
        action="store_true",
        default=False,
        help=(
            "Positive attestation that support files (forms/dictionary_mapping) "
            "contain no PHI, permitting local-only AI study-name inference when --study "
            "is omitted. Absent by default: no naming-content extraction, no model calls."
        ),
    )
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


# Typed, value-free exceptions that may surface at the CLI boundary. Every
# class is imported LAZILY, only inside main()'s except block below -- never
# at module scope -- because each one lives under phi_engine.config/
# pipeline/utils, and those packages must not be imported before
# _set_workspace_env has had a chance to set PHI_WORKSPACE/STUDY_NAME (by
# the time this except block runs, args.func(args) has already called
# _set_workspace_env, so the import is always safe here). Dispatch is by
# isinstance against the REAL imported class, never by exception class
# NAME -- an unrelated exception that merely happens to share a class name
# (and even a same-named ``.code``/``.reason`` attribute) must re-raise,
# not be swallowed and have its attribute value printed. ``.code``/
# ``.reason`` are each fixed, value-free strings by the raising module's
# own contract, but are still checked against an explicit allowlist here
# before ever reaching stderr, with a safe generic fallback code if a
# future/malformed value isn't recognized -- defense in depth against ever
# printing anything other than a known fixed code. ``PipelineBusyError``
# deliberately carries a real lock path in its own message, so its code
# here is always a literal constant, never ``str(exc)``.
_INTAKE_MANIFEST_ERROR_CODES = frozenset(
    {
        "intake-tree-unsafe",
        "intake_manifest_invalid",
        "intake_manifest_missing",
        "source-unreadable",
        "study-name-collision",
    }
)
_VERIFIED_SOURCE_ERROR_REASONS = frozenset(
    {
        "source-unreadable",
        "source-target-outside-root",
        "source-symlink-not-allowed",
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except InvalidStudyNameError:
        # Value-free AND dependency-free (phi_engine.study_name has zero
        # phi_engine imports of its own, already imported at module scope
        # above) -- classifying this exception NEVER re-imports
        # phi_engine.config.config, even when THAT module's own STUDY_NAME
        # defense-in-depth check is what raised it (reachable only via an
        # inherited environment STUDY_NAME that bypassed
        # _set_workspace_env's own up-front validation, since a --study
        # argument is validated and rejected before config is ever
        # imported). Re-importing a config module whose own execution just
        # failed with this same exception would only raise it again from
        # scratch, producing a second, chained traceback.
        print(STUDY_NAME_INVALID_CODE, file=sys.stderr)
        return 2
    except Exception as exc:
        from phi_engine.config.config import LocalLLMConfigurationError
        from phi_engine.pipeline.intake import IntakeManifestError
        from phi_engine.pipeline.verified_source import VerifiedSourceError
        from phi_engine.utils.pipeline_lock import PipelineBusyError

        if isinstance(exc, LocalLLMConfigurationError):
            print("invalid local LLM configuration", file=sys.stderr)
            return 2
        if isinstance(exc, PipelineBusyError):
            print("pipeline_busy", file=sys.stderr)
            return 2
        if isinstance(exc, IntakeManifestError):
            code = exc.code if exc.code in _INTAKE_MANIFEST_ERROR_CODES else "intake_manifest_error"
            print(code, file=sys.stderr)
            return 2
        if isinstance(exc, VerifiedSourceError):
            reason = exc.reason if exc.reason in _VERIFIED_SOURCE_ERROR_REASONS else "source_verification_error"
            print(reason, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
