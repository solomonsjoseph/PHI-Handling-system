"""Standalone Source-of-Truth (SoT) producer integration."""

from __future__ import annotations

from pathlib import Path

import phi_engine.config.config as config
from phi_engine.audit.review_paths import is_sot_review_report_path

__all__ = ["generate_sot"]


def generate_sot(study: str) -> int:
    """Generate joined SoT views for annotated-PDF-backed forms in *study*.

    Normal human-review holds are successful partial outcomes: unresolved PDF /
    dataset discrepancies simply leave that form without LLM-facing SoT signals,
    and the downstream PHI review loader is intentionally fail-soft.
    """
    from phi_engine.sot.generate_lean_outputs import (
        _cleanup_sot_temps,
        discover_pdf_backed_forms_with_reviews,
        generate_form,
    )


    repo_root = Path(config.BASE_DIR).resolve()
    study_dir = Path(config.RAW_DATA_DIR) / study
    pdf_dir = Path(config.ANNOTATED_PDFS_DIR)
    if not pdf_dir.is_dir() or not any(pdf_dir.glob("*.pdf")):
        return 0

    try:
        out_dir = Path(config.LLM_SOURCE_SOT_DIR)
        forms, review_paths = discover_pdf_backed_forms_with_reviews(repo_root, study_dir, study)
        generated: list[Path] = []
        reviewed: list[Path] = [*review_paths]

        for form in forms:
            try:
                result = generate_form(repo_root, study, form, out_dir)
            finally:
                _cleanup_sot_temps(form)
            if is_sot_review_report_path(result):
                reviewed.append(result)
            else:
                generated.append(result)
    except Exception:
        return 1
    return 0
