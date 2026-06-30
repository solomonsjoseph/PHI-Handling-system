#!/usr/bin/env python3
"""Skill entrypoint: header-extraction (Phase 2b, after dedup).

Reads ONLY the first-row headers (column NAMES — metadata, never a row value)
from each manifest-kept dataset and writes them to ``header_extraction.json`` in
the run dir, so the PHI-classification phase can classify headers before any row
value is opened. Honours the forms manifest ``reject:`` list. Invoked by the
orchestrator after ``dataset-deduplication`` (P2) as a file-path subprocess (D3).
Emits a value-free SkillResult.
"""

from __future__ import annotations

import argparse
import json
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

_DATASET_SUFFIXES = {".xlsx", ".xlsm", ".csv"}


def _active_sheet_name(path: Path) -> str | None:
    """Best-effort active-sheet title for an xlsx (None for csv / on any error).

    Count-only metadata — never reads a cell value below row 1.
    """
    if path.suffix.lower() == ".csv":
        return None
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            return workbook.active.title if workbook.active is not None else None
        finally:
            workbook.close()
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract first-row headers from study datasets.")
    add_common_skill_args(parser)
    args = parser.parse_args(argv)

    import config
    from scripts.extraction.forms_manifest import check_forms_manifest
    from scripts.extraction.raw_file_dedup import count_data_rows_only
    from scripts.source_truth.study_intake import read_headers_only

    datasets_dir = Path(config.RAW_DATA_DIR) / args.study / "datasets"
    if not datasets_dir.is_dir():
        emit_skill_result(
            SkillResult(
                skill="header-extraction",
                ok=False,
                exit_code=2,
                summary=f"datasets dir not found: {datasets_dir}",
                data={"study": args.study},
            )
        )
        return 2

    rejected = {name.lower() for name in check_forms_manifest(datasets_dir).rejected_files}

    # Structured per-form store (Note 6): the single row-1-only access point that
    # dedup / PHI-classification / SOT read instead of re-opening raw files.
    # Per form: headers (row-1 only), header_count, row_count (count-only),
    # source_file, sheet_name. Never a row value.
    forms: dict[str, dict] = {}
    errored: list[str] = []
    for path in sorted(datasets_dir.iterdir()):
        if path.suffix.lower() not in _DATASET_SUFFIXES:
            continue
        if path.name.startswith(("~$", "_")) or path.name.lower() in rejected:
            continue
        try:
            headers = read_headers_only(path)
            row_count = count_data_rows_only(path)
        except (OSError, ValueError, StopIteration):
            errored.append(path.stem)
            continue
        forms[path.stem] = {
            "source_file": path.name,
            "sheet_name": _active_sheet_name(path),
            "headers": headers,
            "header_count": len(headers),
            "row_count": row_count,
        }

    out_dir = Path(args.run_dir) if args.run_dir else Path(config.STUDY_OUTPUT_DIR) / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "header_extraction.json"
    out_path.write_text(
        json.dumps(
            {"study": args.study, "forms": dict(sorted(forms.items()))},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    emit_skill_result(
        SkillResult(
            skill="header-extraction",
            ok=not errored,
            exit_code=0 if not errored else 1,
            summary=f"{len(forms)} form(s) read"
            + (f", {len(errored)} unreadable" if errored else ""),
            data={
                "study": args.study,
                "forms_read": len(forms),
                "column_counts": {k: v["header_count"] for k, v in sorted(forms.items())},
                "errored_forms": sorted(errored),
            },
        )
    )
    return 0 if not errored else 1


if __name__ == "__main__":
    raise SystemExit(main())
