from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def _drop_phi_runtime_modules() -> None:
    """Force PHI_WORKSPACE/STUDY_NAME-derived runtime paths to resolve fresh."""

    for name in list(sys.modules):
        if name == "phi_engine" or name.startswith("phi_engine."):
            del sys.modules[name]


def test_joined_query_view_feeds_phi_review_sot_signals(tmp_path: Path) -> None:
    from phi_engine.security.phi_review import load_sot_variable_signals
    from phi_engine.sot.sot_joined_view import (
        build_joined_query_view,
        resolve_sot_joined_view_path,
        write_joined_query_view_yaml,
    )

    study = "UnitStudy"
    form_stem = "demographics"
    construction_root = tmp_path / "output" / study / "audit" / "SoT_construction" / form_stem
    policy_path = construction_root / "pdf" / f"{form_stem}_policy.yaml"
    schema_path = construction_root / "dataset" / f"{form_stem}_schema.json"
    sot_root = tmp_path / "output" / study / "llm_source" / "SoT"
    joined_path = sot_root / form_stem / "joined" / f"{form_stem}_joined_query_view.yaml"

    policy_path.parent.mkdir(parents=True)
    schema_path.parent.mkdir(parents=True)
    joined_path.parent.mkdir(parents=True)
    policy_path.write_text(
        yaml.safe_dump(
            {
                "study": study,
                "form": {
                    "number": "Form 1",
                    "title": "Demographics",
                    "version": "v1.0",
                    "revision_date": "2026-07-07",
                    "page_count": 1,
                },
                "sections": {
                    "header": {"label": None, "note": "form-header band"},
                    "unmatched_dataset": {
                        "label": None,
                        "note": "dataset headers without visible printed PDF widgets",
                    },
                },
                "variables": {
                    "SUBJID": {
                        "section": "header",
                        "pdf_question": "Subject ID:",
                        "widget": "single-line text field",
                        "phi": "drop",
                    },
                    "NOTES": {
                        "section": "unmatched_dataset",
                        "pdf_question": None,
                        "widget": (
                            "no visible printed widget found on rendered PDF; "
                            "dataset row-1 header retained for binding only"
                        ),
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    schema_path.write_text(
        json.dumps(
            {
                "study": study,
                "form": form_stem,
                "source_dataset": f"data/raw/{study}/datasets/{form_stem}.csv",
                "columns": [
                    {"name": "SUBJID", "source_order": 1, "phi_action": "drop"},
                    {"name": "NOTES", "source_order": 2},
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    view = build_joined_query_view(policy_path, schema_path)
    write_joined_query_view_yaml(joined_path, view)

    assert resolve_sot_joined_view_path(sot_root, form_stem) == joined_path
    assert load_sot_variable_signals(sot_root, form_stem) == {
        "SUBJID": {"has_pdf_question": True, "sot_phi": "drop", "is_phi": True},
        "NOTES": {"has_pdf_question": False, "sot_phi": None, "is_phi": False},
    }


def test_generate_sot_smoke_writes_joined_view_or_review_hold(tmp_path: Path, monkeypatch) -> None:
    from reportlab.pdfgen import canvas

    workspace = tmp_path / "workspace"
    study = "PytestSoTProducer"
    form_stem = "demographics"
    raw_study = workspace / "data" / "raw" / study
    annotated_pdfs = raw_study / "annotated_pdfs"
    datasets = raw_study / "datasets"
    annotated_pdfs.mkdir(parents=True)
    datasets.mkdir(parents=True)

    pdf_path = annotated_pdfs / f"{form_stem}.pdf"
    pdf = canvas.Canvas(str(pdf_path))
    pdf.drawString(72, 720, "Minimal SoT smoke form with no PDF annotations")
    pdf.save()
    (datasets / f"{form_stem}.csv").write_text("SUBJID,NOTES\nS001,baseline\n", encoding="utf-8")

    monkeypatch.setenv("PHI_WORKSPACE", str(workspace))
    monkeypatch.setenv("STUDY_NAME", study)
    _drop_phi_runtime_modules()

    try:
        from phi_engine.sot import generate_sot

        rc = generate_sot(study)
    finally:
        _drop_phi_runtime_modules()

    output_root = workspace / "output" / study
    joined_views = list(
        (output_root / "llm_source" / "SoT").glob("*/joined/*_joined_query_view.yaml")
    )
    review_reports = list((output_root / "audit").rglob("review_report.md"))

    assert rc == 0
    assert joined_views or review_reports
