"""Generate and verify runtime SoT outputs for PDF-backed forms.

This is the repo-level orchestration wrapper around the sot-lean-generator
helper scripts. It keeps the Source Truth runtime build reproducible without
moving row-2+ dataset values into the SoT path:

1. Resolve each PDF-backed form.
2. Build a source pack from the PDF plus dataset row-1 headers only.
3. Generate a lean YAML candidate into ``/tmp``.
4. Verify the candidate against the source pack.
5. After the candidate verifies, write the policy YAML + per-form schema (the
   construction inputs) into the AUDIT zone
   (``output/<study>/audit/SoT_construction/<pair>/``, fenced from the LLM) and
   promote ONLY the joined query view into
   ``output/<study>/llm_source/SoT/<pair>/joined/`` (N2/N3/N17: the joined view
   is the sole LLM-facing SoT file).
"""

# ruff: noqa: S108

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from scripts.ai_assistant.sot_joined_view import (
    build_joined_query_view,
    write_joined_query_view_yaml,
)
from scripts.audit.review_paths import is_sot_review_report_path, sot_review_report_path
from scripts.source_truth.study_intake import (
    _find_dataset,
    _find_pdf,
    _form_code,
    _write_sot_review_report,
)

SUPPORTED_DATASET_SUFFIXES = (".xlsx", ".xlsm", ".csv")

# Indo-VAP has a few raw datasets sharing the same leading form code. The SoT
# runtime policy is generated for the dataset that corresponds to the printed
# annotated CRF. The other datasets remain published under dataset_schema/files.
SOT_PUBLISH_STEM_ALIASES: dict[str, dict[str, str]] = {
    "Indo-VAP": {"14_CaseControl": "14_Case_Control"},
}

PDF_FORM_DATASET_OVERRIDES: dict[str, dict[str, str]] = {
    "Indo-VAP": {
        "2A": "2A_ICBaseline",
        "14": "14_CaseControl",
        "18": "18_NonConsent",
        "95": "95_SAE",
    }
}


def _sot_pair_name(form: str) -> str:
    """Return the output pair directory name for a resolved form id."""

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", form.strip()).strip("_")
    return cleaned or form


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _source_pack_headers(source_pack: Path) -> list[str]:
    payload = json.loads(source_pack.read_text(encoding="utf-8"))
    headers = payload.get("headers")
    if not isinstance(headers, list) or not all(isinstance(item, str) for item in headers):
        raise ValueError(f"source pack is missing a string headers array: {source_pack}")
    return headers


def _policy_phi_actions(policy_path: Path) -> dict[str, str]:
    payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    variables = payload.get("variables")
    if not isinstance(variables, dict):
        return {}
    actions: dict[str, str] = {}
    for variable_id, meta in variables.items():
        if not isinstance(variable_id, str) or not isinstance(meta, dict):
            continue
        action = meta.get("phi")
        if isinstance(action, str) and action:
            actions[variable_id] = action
    return actions


_DUPLICATE_BINDING_CONFLICT_KIND = "dataset_duplicate_header_binding_conflict"
_HARD_PDF_MISSING_KIND = "printed_widget_without_dataset_header"
_PDF_FIELD_COUNT_MISMATCH_KIND = "pdf_field_count_column_count_mismatch"
_ALIAS_ANNOTATION_KIND = "pdf_annotation_alias_to_dataset_header"

_HOLD_DISCREPANCY_KINDS = frozenset(
    {
        _DUPLICATE_BINDING_CONFLICT_KIND,
        _HARD_PDF_MISSING_KIND,
        _PDF_FIELD_COUNT_MISMATCH_KIND,
    }
)

# Strict N3 handling: any unresolved discrepancy that routes to human review also
# blocks joined-view publication. The LLM-facing SoT remains clean, resolved-only
# information; no discrepancy-bearing candidate is promoted.
_PUBLISH_BLOCKING_HOLD_KINDS = frozenset(
    _HOLD_DISCREPANCY_KINDS | {_ALIAS_ANNOTATION_KIND}
)


def _maintainer_reviewed_printed_widgets(entry: dict, *, form: str) -> bool:
    """True when every PDF-only widget label is pre-approved in code for *form*."""
    says = entry.get("pdf_annotation_says")
    if not isinstance(says, list) or not says:
        return False
    labels = [label for label in says if isinstance(label, str)]
    if not labels:
        return False
    import importlib.util

    gpc_path = Path(__file__).resolve().parent / "generate_pdf_aware_candidate.py"
    spec = importlib.util.spec_from_file_location("generate_pdf_aware_candidate", gpc_path)
    if spec is None or spec.loader is None:
        return False
    gpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gpc)
    approved = getattr(gpc, "TRUE_PDF_VARIABLES_WITHOUT_DATASET_HEADER", {}).get(form, set())
    return bool(approved) and all(label in approved for label in labels)


def _discrepancy_review_reason(policy_path: Path, *, form: str = "") -> str | None:
    """Return a held-for-review reason when policy documents an un-reviewed discrepancy.

    Reads SoT policy metadata only (discrepancy kinds) — never dataset row values.
    Holds on binding conflicts, un-reviewed printed widgets without dataset headers,
    PDF-field-count vs column-count mismatches, and annotation aliases whose label
    differs from the dataset column (case-insensitive). Combined bindings,
    maintainer-reviewed printed-widget discrepancies, and clean policies return None.
    """
    try:
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(policy, dict):
        return None
    discrepancies = policy.get("discrepancies")
    if not isinstance(discrepancies, list):
        return None
    for entry in discrepancies:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if entry.get("reviewed") is True:
            continue
        if kind in _HOLD_DISCREPANCY_KINDS:
            if (
                kind == _HARD_PDF_MISSING_KIND
                and form
                and _maintainer_reviewed_printed_widgets(entry, form=form)
            ):
                continue
            return str(kind)
        if kind == _ALIAS_ANNOTATION_KIND:
            says = entry.get("pdf_annotation_says")
            if not isinstance(says, list):
                continue
            for item in says:
                if not isinstance(item, dict):
                    continue
                if item.get("curated") is True:
                    continue
                label = item.get("label")
                col = item.get("dataset_column")
                if isinstance(label, str) and isinstance(col, str) and label.lower() != col.lower():
                    return str(kind)
    return None


def _write_dataset_schema(
    schema_path: Path,
    *,
    repo_root: Path,
    study: str,
    form: str,
    dataset: Path,
    source_pack: Path,
    policy_path: Path,
) -> None:
    headers = _source_pack_headers(source_pack)
    phi_actions = _policy_phi_actions(policy_path)
    columns: list[dict[str, object]] = []
    for idx, header in enumerate(headers, start=1):
        column: dict[str, object] = {
            "name": header,
            "source_order": idx,
        }
        if header in phi_actions:
            column["phi_action"] = phi_actions[header]
        columns.append(column)

    payload = {
        "study": study,
        "form": form,
        "source_dataset": _relative_to_repo(dataset, repo_root),
        "columns": columns,
    }
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish_verified_sot_outputs(
    *,
    repo_root: Path,
    study: str,
    form: str,
    dataset: Path,
    source_pack: Path,
    verified_policy: Path,
    out_root: Path,
) -> Path:
    pair = _sot_pair_name(form)
    # N2/N3/N17: the joined query view is the SOLE LLM-facing SoT file. The
    # construction material (policy YAML + dataset schema) is written into the
    # AUDIT zone (output/<study>/audit/SoT_construction/<pair>/), which is fenced
    # from the LLM by deny_if_audit_zone — never into llm_source. Only joined/ is
    # promoted into the LLM read zone. (out_root = output/<study>/llm_source/SoT,
    # so out_root.parents[1] = output/<study>.)
    construction_dir = out_root.parents[1] / "audit" / "SoT_construction" / pair
    policy_path = construction_dir / "pdf" / f"{form}_policy.yaml"
    schema_path = construction_dir / "dataset" / f"{form}_schema.json"
    joined_path = out_root / pair / "joined" / f"{form}_joined_query_view.yaml"

    policy_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(verified_policy, policy_path)
    _write_dataset_schema(
        schema_path,
        repo_root=repo_root,
        study=study,
        form=form,
        dataset=dataset,
        source_pack=source_pack,
        policy_path=policy_path,
    )
    joined_path.parent.mkdir(parents=True, exist_ok=True)
    write_joined_query_view_yaml(joined_path, build_joined_query_view(policy_path, schema_path))
    review_report = sot_review_report_path(out_root.parents[1] / "audit", form)
    if review_report.is_file():
        review_report.unlink()
    return joined_path


def _natural_code_key(code: str) -> tuple[int, str]:
    digits = "".join(ch for ch in code if ch.isdigit())
    suffix = code[len(digits) :]
    return (int(digits or 0), suffix)


def discover_pdf_backed_forms_with_reviews(
    repo_root: Path,
    study_dir: Path,
    study: str,
) -> tuple[list[str], list[Path]]:
    """Return discoverable forms and route incomplete/ambiguous pairs to review."""

    pdf_dir = study_dir / "annotated_pdfs"
    dataset_dir = study_dir / "datasets"
    review_paths: list[Path] = []
    if not pdf_dir.is_dir():
        review_paths.append(
            _write_sot_review_report(
                repo_root=repo_root,
                study=study,
                form="annotated_pdfs",
                reason="missing_pdf_directory",
                issues=[
                    {
                        "classification": "missing_pdf_directory",
                        "detail": f"Annotated PDF directory was not found: {pdf_dir}",
                    }
                ],
            )
        )
        return [], review_paths
    if not dataset_dir.is_dir():
        review_paths.append(
            _write_sot_review_report(
                repo_root=repo_root,
                study=study,
                form="datasets",
                reason="missing_dataset_directory",
                issues=[
                    {
                        "classification": "missing_dataset_directory",
                        "detail": f"Dataset directory was not found: {dataset_dir}",
                    }
                ],
            )
        )
        return [], review_paths

    pdf_codes = {_form_code(path.stem) for path in pdf_dir.glob("*.pdf")}
    datasets_by_code: dict[str, list[Path]] = defaultdict(list)
    for suffix in SUPPORTED_DATASET_SUFFIXES:
        for dataset in dataset_dir.glob(f"*{suffix}"):
            datasets_by_code[_form_code(dataset.stem)].append(dataset)

    overrides = PDF_FORM_DATASET_OVERRIDES.get(study, {})
    forms: list[str] = []
    for code in sorted(pdf_codes, key=_natural_code_key):
        override = overrides.get(code)
        if override:
            if any(
                (dataset_dir / f"{override}{suffix}").exists()
                for suffix in SUPPORTED_DATASET_SUFFIXES
            ):
                forms.append(override)
            else:
                review_paths.append(
                    _write_sot_review_report(
                        repo_root=repo_root,
                        study=study,
                        form=override,
                        reason="missing_dataset",
                        issues=[
                            {
                                "classification": "missing_dataset",
                                "detail": (
                                    f"Override for annotated PDF form code {code} points to "
                                    f"missing dataset {override}"
                                ),
                            }
                        ],
                    )
                )
            continue

        candidate_paths = sorted(datasets_by_code.get(code, []), key=lambda path: path.name)
        candidates = sorted({path.stem for path in candidate_paths})
        if len(candidates) == 1:
            forms.append(candidates[0])
        elif not candidates:
            review_paths.append(
                _write_sot_review_report(
                    repo_root=repo_root,
                    study=study,
                    form=code,
                    reason="missing_dataset",
                    issues=[
                        {
                            "classification": "missing_dataset",
                            "detail": f"No dataset found for annotated PDF form code {code}",
                        }
                    ],
                )
            )
        else:
            review_paths.append(
                _write_sot_review_report(
                    repo_root=repo_root,
                    study=study,
                    form=code,
                    reason="ambiguous_dataset",
                    issues=[
                        {
                            "classification": "ambiguous_dataset",
                            "detail": (
                                f"Ambiguous datasets for annotated PDF form code {code}: "
                                f"{', '.join(path.name for path in candidate_paths)}"
                            ),
                        }
                    ],
                )
            )
    return forms, review_paths


def pdf_backed_dataset_stems(study: str, repo_root: Path) -> frozenset[str]:
    """Return dataset stems that require a published SoT joined query view."""

    study_dir = repo_root / "data" / "raw" / study
    forms, _ = discover_pdf_backed_forms_with_reviews(repo_root, study_dir, study)
    aliases = SOT_PUBLISH_STEM_ALIASES.get(study, {})
    return frozenset(aliases.get(form, form) for form in forms)


def _run_result(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)  # noqa: S603


def _run(cmd: list[str], *, cwd: Path) -> None:
    result = _run_result(cmd, cwd=cwd)
    if result.returncode == 0:
        if result.stdout.strip():
            print(result.stdout.strip())
        return
    message = [
        f"command failed with exit {result.returncode}: {' '.join(cmd)}",
        result.stdout.strip(),
        result.stderr.strip(),
    ]
    raise RuntimeError("\n".join(part for part in message if part))


def _print_result_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def _cleanup_sot_temps(form: str) -> None:
    """Destroy the per-form /tmp SoT intermediates after publish (Note 2).

    Mirrors the temp paths generate_form uses (source pack, 600-DPI render dir,
    candidate). Best-effort: the joined view is already promoted, so a cleanup
    hiccup must not fail the run.
    """
    Path(f"/tmp/sot_source_pack_{form}.json").unlink(missing_ok=True)
    Path(f"/tmp/{form}_lean.yaml").unlink(missing_ok=True)
    shutil.rmtree(Path(f"/tmp/sot_render_{form}"), ignore_errors=True)


def generate_form(
    repo_root: Path, study: str, form: str, out_dir: Path, *, run_dir: Path | None = None
) -> Path:
    """Generate + verify one form's SoT; policy/schema go to the audit zone, only the joined view is promoted to llm_source."""

    study_dir = repo_root / "data" / "raw" / study
    pdf = _find_pdf(study_dir, form)
    dataset_error: str | None = None
    try:
        dataset = _find_dataset(study_dir, form)
    except ValueError as exc:
        dataset = None
        dataset_error = str(exc)

    issues: list[dict[str, str]] = []
    if pdf is None:
        issues.append(
            {
                "classification": "missing_pdf",
                "detail": f"No annotated PDF found for {study}/{form}",
            }
        )
    if dataset is None:
        if dataset_error is None:
            issues.append(
                {
                    "classification": "missing_dataset",
                    "detail": f"No dataset found for {study}/{form}",
                }
            )
        else:
            issues.append(
                {
                    "classification": "ambiguous_dataset",
                    "detail": dataset_error,
                }
            )

    if issues:
        return _write_sot_review_report(
            repo_root=repo_root,
            study=study,
            form=form,
            reason="; ".join(issue["classification"] for issue in issues),
            issues=issues,
            resolved_pdf=pdf,
            resolved_dataset=dataset,
        )
    if dataset is None or pdf is None:
        raise ValueError("Dataset and PDF must both be resolved when no issues are found.")

    extract_script = (
        repo_root
        / "plugins"
        / "report-ai-study-pipeline"
        / "skills"
        / "sot-lean-generator"
        / "scripts"
        / "extract_sources.py"
    )
    generator_script = (
        repo_root
        / "plugins"
        / "report-ai-study-pipeline"
        / "skills"
        / "sot-lean-generator"
        / "scripts"
        / "generate_pdf_aware_candidate.py"
    )
    checker_script = (
        repo_root
        / "plugins"
        / "report-ai-study-pipeline"
        / "skills"
        / "sot-lean-generator"
        / "scripts"
        / "check_lean_policy.py"
    )
    diff_script = repo_root / "scripts" / "source_truth" / "diff_against_gold.py"

    source_pack = Path(f"/tmp/sot_source_pack_{form}.json")
    render_dir = Path(f"/tmp/sot_render_{form}")
    candidate = Path(f"/tmp/{form}_lean.yaml")
    gold = repo_root / "data" / "SoT" / study / f"{form}_policy.lean.yaml"

    _run(
        [
            sys.executable,
            str(extract_script),
            "--repo-root",
            str(repo_root),
            "--pdf",
            str(pdf),
            "--dataset",
            str(dataset),
            "--out",
            str(source_pack),
            "--render-dir",
            str(render_dir),
        ]
        + (["--run-dir", str(run_dir)] if run_dir is not None else []),
        cwd=repo_root,
    )
    _run(
        [
            sys.executable,
            str(generator_script),
            "--repo-root",
            str(repo_root),
            "--form",
            form,
            "--source-pack",
            str(source_pack),
            "--out",
            str(candidate),
        ],
        cwd=repo_root,
    )
    _run(
        [
            sys.executable,
            str(checker_script),
            "--lean",
            str(candidate),
            "--source-pack",
            str(source_pack),
            "--repo-root",
            str(repo_root),
        ],
        cwd=repo_root,
    )
    if gold.exists():
        diff_cmd = [
            sys.executable,
            str(diff_script),
            "--study",
            study,
            "--form",
            form,
            "--candidate",
            str(candidate),
            "--repo-root",
            str(repo_root),
        ]
        diff_result = _run_result(diff_cmd, cwd=repo_root)
        _print_result_output(diff_result)
        if diff_result.returncode == 1:
            print(
                f"  anchored candidate rejected for {study}/{form}; "
                "verifying and promoting anchored gold instead",
                flush=True,
            )
            _run(
                [
                    sys.executable,
                    str(checker_script),
                    "--lean",
                    str(gold),
                    "--source-pack",
                    str(source_pack),
                    "--repo-root",
                    str(repo_root),
                ],
                cwd=repo_root,
            )
            return _publish_verified_sot_outputs(
                repo_root=repo_root,
                study=study,
                form=form,
                dataset=dataset,
                source_pack=source_pack,
                verified_policy=gold,
                out_root=out_dir,
            )
        if diff_result.returncode != 0:
            message = [
                f"command failed with exit {diff_result.returncode}: {' '.join(diff_cmd)}",
                diff_result.stdout.strip(),
                diff_result.stderr.strip(),
            ]
            raise RuntimeError("\n".join(part for part in message if part))
    else:
        print(f"  gold diff skipped for {study}/{form}: no anchored gold at {gold}", flush=True)

    held_reason = _discrepancy_review_reason(candidate, form=form)
    if held_reason:
        review_path = _write_sot_review_report(
            repo_root=repo_root,
            study=study,
            form=form,
            reason=held_reason,
            issues=[
                {
                    "classification": held_reason,
                    "detail": (
                        "The candidate policy passed structural verification but documents an "
                        "un-reviewed Source Truth discrepancy; see the review report for the "
                        "recorded discrepancy kind."
                    ),
                }
            ],
            resolved_pdf=pdf,
            resolved_dataset=dataset,
            action_taken=(
                "candidate policy passed structural verification but was flagged for human review"
            ),
            required_next_step=(
                "confirm the documented discrepancy, update the policy metadata if needed, "
                "then rerun Stage 0"
            ),
        )
        return review_path
    return _publish_verified_sot_outputs(
        repo_root=repo_root,
        study=study,
        form=form,
        dataset=dataset,
        source_pack=source_pack,
        verified_policy=candidate,
        out_root=out_dir,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True, help="Study folder name, e.g. Indo-VAP")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument(
        "--form",
        action="append",
        dest="forms",
        help="Form id to generate. Repeat for multiple forms. Defaults to all PDF-backed forms.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Output SoT root. Defaults to output/<study>/llm_source/SoT.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Orchestrator run dir (header_extraction.json for Note 6 shared store).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    study_dir = repo_root / "data" / "raw" / args.study
    out_dir = args.out_dir or repo_root / "output" / args.study / "llm_source" / "SoT"
    review_paths: list[Path] = []
    if args.forms:
        forms = args.forms
    else:
        forms, review_paths = discover_pdf_backed_forms_with_reviews(
            repo_root, study_dir, args.study
        )

    failures: list[tuple[str, str]] = []
    generated: list[Path] = []
    reviewed: list[Path] = [*review_paths]
    for form in forms:
        print(f"FORM {form}", flush=True)
        try:
            result = generate_form(repo_root, args.study, form, out_dir, run_dir=args.run_dir)
        except Exception as exc:
            failures.append((form, str(exc)))
            print(f"  FAIL {exc}", flush=True)
        else:
            if is_sot_review_report_path(result):
                reviewed.append(result)
                print(f"  REVIEW {result}", flush=True)
            else:
                generated.append(result)
                print("  OK", flush=True)
        finally:
            _cleanup_sot_temps(form)  # N2: destroy intermediates after the form

    print(
        f"SUMMARY generated={len(generated)} review={len(reviewed)} "
        f"failed={len(failures)} out={out_dir}"
    )
    for path in reviewed:
        print(f"review_report={path}")
    if failures:
        for form, message in failures:
            print(f"\n[{form}]\n{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
