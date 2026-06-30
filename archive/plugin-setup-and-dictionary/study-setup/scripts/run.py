#!/usr/bin/env python3
"""Skill entrypoint: study-setup (interactive/setup — NOT an orchestrator phase).

Prepares a study for a publish run *outside* the 10-phase DAG (Gap 4): it
pre-creates the run directory tree and reports readiness of the required inputs
(``config/<study>/_forms_manifest.yaml``, ``config/<study>/_study_privacy.yaml``,
the raw datasets dir, and the PHI HMAC key). With ``--bootstrap-key`` it creates
a fresh 0600 HMAC key when none exists (refusing to overwrite an existing key,
which would invalidate every prior pseudonym). Emits a value-free SkillResult.

With ``--interactive`` it runs the guided config-authoring wizard (Note 11), and
``--write-config`` is the equivalent non-interactive flag form; both accept the
study's jurisdictions, compliance posture, ``data_as_of``, and per-file
Required/Optional/Reject classification and WRITE the two config YAMLs (the
pipeline still re-validates at phase 0, so the wizard is a guardrail, not a
gatekeeper). A separate Streamlit UI wizard lives at
``scripts/ai_assistant/ui/wizard.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from scripts.utils.skill_protocol import (  # noqa: E402
    SkillResult,
    add_common_skill_args,
    emit_skill_result,
)


def _run_config_wizard(args: argparse.Namespace) -> int:
    """Author config/<study>/ via the interactive Q&A or the flag form (Note 11)."""
    import wizard

    try:
        if args.interactive:
            privacy_path, manifest_path = wizard.run_interactive(args.study, force=args.force)
        else:
            jurisdictions = [j.strip() for j in (args.jurisdictions or "").split(",") if j.strip()]
            posture = args.compliance_posture or wizard.available_postures()[0]
            errors = wizard.validate_privacy_inputs(
                jurisdictions=jurisdictions, posture=posture, data_as_of=args.data_as_of
            ) + wizard.validate_manifest_inputs(
                args.study, required=args.required, optional=args.optional, reject=args.reject
            )
            if errors:
                emit_skill_result(
                    SkillResult(
                        skill="study-setup",
                        ok=False,
                        exit_code=2,
                        summary="config invalid: " + "; ".join(errors),
                        data={"study": args.study, "errors": errors},
                    )
                )
                return 2
            privacy = wizard.build_privacy_config(
                jurisdictions=jurisdictions, data_as_of=args.data_as_of
            )
            manifest = wizard.build_forms_manifest(
                required=args.required, optional=args.optional, reject=args.reject
            )
            privacy_path, manifest_path = wizard.write_configs(
                args.study, privacy, manifest, force=args.force
            )
            # N11 posture fix: write the posture where the scrub engine reads it.
            wizard.write_scrub_override(args.study, posture, force=args.force)
    except (ValueError, FileExistsError) as exc:
        emit_skill_result(
            SkillResult(
                skill="study-setup",
                ok=False,
                exit_code=2,
                summary=f"config authoring failed: {exc}",
                data={"study": args.study},
            )
        )
        return 2
    emit_skill_result(
        SkillResult(
            skill="study-setup",
            ok=True,
            exit_code=0,
            summary="config written",
            data={
                "study": args.study,
                "privacy": str(privacy_path),
                "manifest": str(manifest_path),
            },
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a study for a publish run.")
    add_common_skill_args(parser)
    parser.add_argument(
        "--bootstrap-key",
        action="store_true",
        help="Create a fresh 0600 PHI HMAC key when none exists (never overwrites).",
    )
    # Note 11 — guided config authoring (optional; default behavior is the
    # readiness scaffold below).
    parser.add_argument(
        "--interactive", action="store_true", help="Run the guided Q&A config wizard."
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="Non-interactive flag form: write config from the flags below.",
    )
    parser.add_argument("--jurisdictions", help="comma list, e.g. USA,INDIA (with --write-config)")
    parser.add_argument("--compliance-posture", help="safe_harbor | limited_dataset")
    parser.add_argument("--data-as-of", help="ISO YYYY-MM-DD source-data recency date")
    parser.add_argument(
        "--required", action="append", default=[], help="required dataset file (repeatable)"
    )
    parser.add_argument(
        "--optional", action="append", default=[], help="optional dataset file (repeatable)"
    )
    parser.add_argument(
        "--reject", action="append", default=[], help="rejected dataset file (repeatable)"
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing config files")
    parser.add_argument(
        "--scaffold",
        action="store_true",
        help="Write manifest + date_locales *.scaffold.yaml drafts (N11; never overwrites live config).",
    )
    args = parser.parse_args(argv)

    import config

    if args.scaffold:
        import wizard

        try:
            paths = wizard.write_scaffold_sidecars(args.study, force=args.force)
        except FileExistsError as exc:
            emit_skill_result(
                SkillResult(
                    skill="study-setup",
                    ok=False,
                    exit_code=2,
                    summary=str(exc),
                    data={"study": args.study},
                )
            )
            return 2
        emit_skill_result(
            SkillResult(
                skill="study-setup",
                ok=True,
                exit_code=0,
                summary="scaffold sidecars written",
                data={
                    "study": args.study,
                    "manifest_scaffold": str(paths[0]),
                    "date_locales_scaffold": str(paths[1]),
                },
            )
        )
        return 0

    if args.interactive or args.write_config:
        return _run_config_wizard(args)

    config.ensure_directories()
    config.ensure_run_directories(study=args.study, run_id=args.run_id)

    forms_manifest = Path(config.study_config_path("_forms_manifest.yaml", study=args.study))
    study_privacy = Path(config.study_config_path("_study_privacy.yaml", study=args.study))
    datasets_dir = Path(config.RAW_DATA_DIR) / args.study / "datasets"
    key_path = Path(config.PHI_KEY_PATH) if config.PHI_KEY_PATH else None

    key_created = False
    if args.bootstrap_key and key_path is not None and not key_path.is_file():
        from scripts.security.phi_scrub import bootstrap_key

        bootstrap_key(key_path)
        key_created = True

    readiness = {
        "forms_manifest": forms_manifest.is_file(),
        "study_privacy": study_privacy.is_file(),
        "datasets_dir": datasets_dir.is_dir(),
        "phi_key": bool(key_path and key_path.is_file()),
    }
    missing = sorted(name for name, present in readiness.items() if not present)

    emit_skill_result(
        SkillResult(
            skill="study-setup",
            ok=not missing,
            exit_code=0 if not missing else 1,
            summary=("ready" if not missing else f"missing: {', '.join(missing)}")
            + (" (key created)" if key_created else ""),
            data={"study": args.study, "readiness": readiness, "key_created": key_created},
        )
    )
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
