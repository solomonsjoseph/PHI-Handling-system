"""SoT Stage-0 intake CLI — resolve paths and produce a source pack for a single form.

Cross-LLM portability note
--------------------------
This module is intentionally a thin CLI wrapper, not a ``.claude/skills/`` file.
The canonical shape is a callable CLI so that any agentic tool — ChatGPT, Gemini,
Cursor, Aider, or a plain shell script — can reach Stage 0 without Claude-specific
infrastructure.

What this wrapper does (Stage 0 only)
--------------------------------------
1. Resolves the annotated PDF path:  ``data/raw/<study>/annotated_pdfs/<form>.pdf``
   Because PDF filenames in the repo use human-readable names with version suffixes
   (e.g. ``6 HIV v1.0.pdf``) while the form argument uses underscore-separated
   identifiers (e.g. ``6_HIV``), the wrapper performs a glob search for any ``.pdf``
   in the ``annotated_pdfs/`` directory whose leading form-code matches the
   ``--form`` argument.  Exact filename match (``<form>.pdf``) is tried first as
   a fast path.

2. Resolves the dataset path: ``data/raw/<study>/datasets/<form>.xlsx`` (or
   ``<form>.csv``).

3. Shells out to ``plugins/report-ai-study-pipeline/skills/sot-lean-generator/scripts/extract_sources.py`` via
   the current Python interpreter so the invocation stays inside the active
   virtual environment.

4. Prints the resolved output paths to stdout in key=value lines so they are
   machine-parseable:
       source_pack=/tmp/sot_source_pack_<form>.json
       render=/tmp/sot_render_<form>/<pdf_name>.page-001.png
       render=/tmp/sot_render_<form>/<pdf_name>.page-002.png

Stages 1-3 (LLM-driven lean-YAML authoring) are NOT performed here.  They
require LLM reasoning and live in the skill pipeline described in
``plugins/report-ai-study-pipeline/skills/sot-lean-generator/SKILL.md``.

Stage 4 (lean policy verification) is a separate CLI:
    ``uv run --all-groups python plugins/report-ai-study-pipeline/skills/sot-lean-generator/scripts/check_lean_policy.py``
    See ``make sot-verify``.

Usage
-----
    python -m scripts.source_truth.study_intake \\
        --study Indo-VAP --form 6_HIV [--repo-root .]

    make sot-source-pack STUDY=Indo-VAP FORM=6_HIV
"""

# ruff: noqa: S108

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.audit.review_paths import sot_review_report_path

# ---------------------------------------------------------------------------
# Helper functions also importable by extract_sources.py
# ---------------------------------------------------------------------------


def _form_code(name: str) -> str:
    """Extract the leading form-code prefix from a filename stem.

    Examples::

        "6 HIV v1.0"  → "6"
        "12A Follow-up A v1.0" → "12A"
        "6_HIV" → "6"
    """
    m = re.match(r"^([0-9]+[A-Za-z]?)", name.replace("_", " "))
    return m.group(1) if m else name


def _find_pdf(study_dir: Path, form: str) -> Path | None:
    """Locate the annotated PDF for *form* under *study_dir/annotated_pdfs/*.

    Strategy:
    1. Exact match: ``<form>.pdf``
    2. Glob match: any PDF whose leading form-code matches *form*'s code.
    """
    pdf_dir = study_dir / "annotated_pdfs"
    if not pdf_dir.is_dir():
        return None

    # Fast path: exact filename
    exact = pdf_dir / f"{form}.pdf"
    if exact.exists():
        return exact

    # Glob: find by form-code prefix
    target_code = _form_code(form)
    for candidate in sorted(pdf_dir.glob("*.pdf")):
        if _form_code(candidate.stem) == target_code:
            return candidate
    return None


def _rejected_dataset_names(study_dir: Path) -> frozenset[str]:
    """Return the set of rejected filenames from the study's ``_forms_manifest.yaml``.

    Degrades gracefully to an empty set if the manifest is absent, unreadable,
    or raises any error (including :class:`ManifestMismatchError` from unrelated
    drift).  This mirrors the extraction leg's manifest handling but is
    intentionally fail-open so that a manifest problem never blocks SoT intake.
    """
    try:
        # Shared forms_manifest module (stays in scripts/); importing
        # dataset_pipeline here would be a skill→skill edge (sot-lean-generator →
        # dataset-to-llm-source), forbidden by Note 19.
        from scripts.extraction.forms_manifest import check_forms_manifest

        return check_forms_manifest(study_dir / "datasets").rejected_files
    except Exception:
        return frozenset()


def _find_dataset(study_dir: Path, form: str) -> Path | None:
    """Locate the dataset file for *form* under *study_dir/datasets/*.

    Tries ``<form>.xlsx`` and ``<form>.csv`` in that order, then falls back to
    a glob search by form-code prefix.  In both stages, files listed under
    ``reject:`` in the study's ``_forms_manifest.yaml`` are excluded so that a
    rejected duplicate stub (e.g. ``14_CaseControl.xlsx``) is never returned
    when the kept variant (e.g. ``14_Case_Control.xlsx``) exists.
    """
    ds_dir = study_dir / "datasets"
    if not ds_dir.is_dir():
        return None

    rejected = _rejected_dataset_names(study_dir)

    for ext in (".xlsx", ".xlsm", ".csv"):
        exact = ds_dir / f"{form}{ext}"
        if exact.exists() and exact.name not in rejected:
            return exact

    target_code = _form_code(form)
    matches: list[Path] = []
    for ext in (".xlsx", ".xlsm", ".csv"):
        matches.extend(
            candidate
            for candidate in sorted(ds_dir.glob(f"*{ext}"))
            if _form_code(candidate.stem) == target_code and candidate.name not in rejected
        )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(candidate.name for candidate in matches)
        raise ValueError(
            f"ambiguous dataset fallback for form code {target_code!r}; "
            f"use an exact form id. Candidates: {names}"
        )
    return None


def _sot_review_report_path(repo_root: Path, study: str, form: str) -> Path:
    audit_dir = repo_root / "output" / study / "audit"
    return sot_review_report_path(audit_dir, form)


def _rel_or_str(path: Path | None, root: Path) -> str:
    if path is None:
        return "not_resolved"
    return str(path.relative_to(root) if path.is_relative_to(root) else path)


def _write_sot_review_report(
    *,
    repo_root: Path,
    study: str,
    form: str,
    reason: str,
    issues: list[dict[str, str]],
    resolved_pdf: Path | None = None,
    resolved_dataset: Path | None = None,
    action_taken: str = (
        "no Source Truth policy, dataset schema, joined view, or source pack was generated"
    ),
    required_next_step: str = "resolve the missing or ambiguous source pair, then rerun Stage 0",
) -> Path:
    """Write a human-review audit report for a SoT pair that cannot be authored."""

    report_path = _sot_review_report_path(repo_root, study, form)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Source Truth Human Review",
        "",
        "Boundary: this report uses file paths and source-availability metadata only. "
        "No dataset row values were read or written.",
        "",
        "## Decision",
        "",
        "- status: `human_review_required`",
        f"- action_taken: {action_taken}",
        f"- reason: {reason}",
        f"- required_next_step: {required_next_step}",
        "",
        "## Scope",
        "",
        f"- study: `{study}`",
        f"- form: `{form}`",
        f"- generated_utc: `{datetime.now(UTC).replace(microsecond=0).isoformat()}`",
        "",
        "## Source Resolution",
        "",
        f"- annotated_pdf: `{_rel_or_str(resolved_pdf, repo_root)}`",
        f"- dataset: `{_rel_or_str(resolved_dataset, repo_root)}`",
        "",
        "## Issues",
        "",
    ]
    for issue in issues:
        lines.append(f"- classification: `{issue['classification']}`")
        lines.append(f"  detail: {issue['detail']}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _finish_sot_review(report_path: Path) -> int:
    print("status=human_review_required")
    print(f"sot_review_report={report_path}")
    return 0


# ---------------------------------------------------------------------------
# Importable shims used by plugins/report-ai-study-pipeline/skills/sot-lean-generator/scripts/extract_sources.py
# ---------------------------------------------------------------------------


def read_headers_only(dataset: Path) -> list[str]:
    """Read only the first-row headers from an xlsx or csv dataset.

    Row-2+ bytes are never read — the file handle is closed immediately after
    the first row.  This is the sole PHI/row-data isolation point for header
    extraction.
    """
    import csv

    suffix = dataset.suffix.lower()
    if suffix == ".csv":
        with dataset.open(newline="", encoding="utf-8-sig") as fh:
            return next(csv.reader(fh))
    if suffix in {".xlsx", ".xlsm"}:
        return _xlsx_first_row_headers(dataset)
    raise ValueError(f"Unsupported dataset format: {dataset.suffix}")


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    value = 0
    for letter in letters:
        value = value * 26 + (ord(letter) - ord("A") + 1)
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xlsx_first_row_headers(dataset: Path) -> list[str]:
    import zipfile

    from defusedxml import ElementTree

    def _rel_target(base: str, target: str) -> str:
        if target.startswith("/"):
            return target.lstrip("/")
        return str((Path(base).parent / target).as_posix())

    with zipfile.ZipFile(dataset) as zf:
        wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
        sheet = next(e for e in wb.iter() if _local_name(e.tag) == "sheet")
        rid = sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        sheet_path = next(
            _rel_target("xl/workbook.xml", r.attrib["Target"])
            for r in rels
            if r.attrib.get("Id") == rid
        )

        cells: list[tuple[int, str, str | None]] = []
        shared_indices: set[int] = set()
        with zf.open(sheet_path) as fh:
            for _ev, elem in ElementTree.iterparse(fh, events=("end",)):
                if _local_name(elem.tag) != "row":
                    continue
                if elem.attrib.get("r") != "1":
                    elem.clear()
                    continue
                for cell in elem:
                    if _local_name(cell.tag) != "c":
                        continue
                    ref = cell.attrib.get("r", "")
                    ctype = cell.attrib.get("t")
                    raw = next(
                        (c.text for c in cell.iter() if _local_name(c.tag) == "v"),
                        None,
                    )
                    if ctype == "inlineStr":
                        raw = next(
                            (c.text for c in cell.iter() if _local_name(c.tag) == "t"),
                            None,
                        )
                    if raw is None:
                        continue
                    if ctype == "s":
                        shared_indices.add(int(raw))
                        cells.append((_column_index(ref), "s", raw))
                    else:
                        cells.append((_column_index(ref), "raw", raw.strip()))
                elem.clear()
                break

        # Read only the shared-string entries we need
        shared: dict[int, str] = {}
        if shared_indices:
            max_needed = max(shared_indices)
            idx = -1
            with zf.open("xl/sharedStrings.xml") as fh2:
                for _ev, elem in ElementTree.iterparse(fh2, events=("end",)):
                    if _local_name(elem.tag) != "si":
                        continue
                    idx += 1
                    if idx in shared_indices:
                        shared[idx] = "".join(elem.itertext()).strip()
                    if idx >= max_needed and len(shared) == len(shared_indices):
                        break
                    elem.clear()

        headers = []
        for _col, ctype, val in sorted(cells, key=lambda x: x[0]):
            h = shared.get(int(val or "0"), "") if ctype == "s" else (val or "")
            if h:
                headers.append(h)
        return headers


def _read_form_pdf_pages(pdf: Path) -> list[dict]:
    """Extract text, annotations, and page metadata from a PDF using pdfplumber.

    Returns a list of page dicts with keys: page, lines, annotations,
    annotation_details.
    """
    import pdfplumber

    def _annot_text(annot: dict) -> str | None:
        for key in ("contents", "Content", "title", "T", "subject", "Subj"):
            v = annot.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        data = annot.get("data")
        if isinstance(data, dict):
            for key in ("Contents", "T", "Subj"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    pages = []
    with pdfplumber.open(pdf) as doc:
        for i, page in enumerate(doc.pages, start=1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            annotations = []
            annotation_details = []
            for annot in getattr(page, "annots", None) or []:
                if not isinstance(annot, dict):
                    continue
                t = _annot_text(annot)
                if t:
                    annotations.append(t)
                annotation_details.append(
                    {
                        "text": t,
                        "page": i,
                        "x0": annot.get("x0"),
                        "top": annot.get("top"),
                        "x1": annot.get("x1"),
                        "bottom": annot.get("bottom"),
                        "width": annot.get("width"),
                        "height": annot.get("height"),
                        "rotate": (annot.get("data") or {}).get("Rotate"),
                    }
                )
            pages.append(
                {
                    "page": i,
                    "lines": lines,
                    "annotations": annotations,
                    "annotation_details": annotation_details,
                }
            )
    return pages


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.source_truth.study_intake",
        description=(
            "Stage-0 SoT intake: resolve PDF + dataset paths and produce a "
            "source pack JSON + per-page render PNGs.  Stages 1-3 (LLM lean-YAML "
            "authoring) are NOT run here — they live in the skill pipeline."
        ),
    )
    p.add_argument("--study", required=True, help="Study folder name (e.g. Indo-VAP)")
    p.add_argument("--form", required=True, help="Form identifier (e.g. 6_HIV)")
    p.add_argument(
        "--repo-root",
        default=".",
        type=Path,
        help="Repository root directory (default: current directory)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    study_dir = repo_root / "data" / "raw" / args.study

    if not study_dir.is_dir():
        report_path = _write_sot_review_report(
            repo_root=repo_root,
            study=args.study,
            form=args.form,
            reason="study_directory_missing",
            issues=[
                {
                    "classification": "missing_study_directory",
                    "detail": f"Study directory was not found: {study_dir}",
                }
            ],
        )
        return _finish_sot_review(report_path)

    issues: list[dict[str, str]] = []

    # Resolve PDF
    pdf = _find_pdf(study_dir, args.form)
    if pdf is None:
        issues.append(
            {
                "classification": "missing_pdf",
                "detail": (
                    f"No annotated PDF found for form '{args.form}' "
                    f"under {study_dir / 'annotated_pdfs'}"
                ),
            }
        )

    # Resolve dataset
    dataset_error: str | None = None
    try:
        dataset = _find_dataset(study_dir, args.form)
    except ValueError as exc:
        dataset = None
        dataset_error = str(exc)
        issues.append(
            {
                "classification": "ambiguous_dataset",
                "detail": dataset_error,
            }
        )
    if dataset is None and dataset_error is None:
        issues.append(
            {
                "classification": "missing_dataset",
                "detail": (
                    f"No dataset file (.xlsx/.csv/.xlsm) found for form '{args.form}' "
                    f"under {study_dir / 'datasets'}"
                ),
            }
        )

    if issues:
        report_path = _write_sot_review_report(
            repo_root=repo_root,
            study=args.study,
            form=args.form,
            reason="; ".join(issue["classification"] for issue in issues),
            issues=issues,
            resolved_pdf=pdf,
            resolved_dataset=dataset,
        )
        return _finish_sot_review(report_path)

    out_pack = Path(f"/tmp/sot_source_pack_{args.form}.json")
    render_dir = Path(f"/tmp/sot_render_{args.form}")

    extract_script = (
        repo_root
        / "plugins"
        / "report-ai-study-pipeline"
        / "skills"
        / "sot-lean-generator"
        / "scripts"
        / "extract_sources.py"
    )
    if not extract_script.exists():
        print(
            f"error: extract_sources.py not found at {extract_script}",
            file=sys.stderr,
        )
        return 1

    cmd = [
        sys.executable,
        str(extract_script),
        "--repo-root",
        str(repo_root),
        "--pdf",
        str(pdf),
        "--dataset",
        str(dataset),
        "--out",
        str(out_pack),
        "--render-dir",
        str(render_dir),
    ]

    result = subprocess.run(cmd, cwd=str(repo_root))  # noqa: S603
    if result.returncode != 0:
        print(
            f"error: extract_sources.py exited with code {result.returncode}. "
            "Check that ghostscript (gs) is installed and the PDF/dataset are readable.",
            file=sys.stderr,
        )
        return result.returncode

    # Print parseable output
    print(f"source_pack={out_pack}")
    pack = json.loads(out_pack.read_text(encoding="utf-8"))
    for render in pack.get("renders", []):
        print(f"render={render}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
