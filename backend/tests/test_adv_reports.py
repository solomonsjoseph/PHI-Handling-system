"""Phase 15b category 8: reports (docs section 98).

Positive-detection adversarial tests: a sensitive column header, a
sensitive dataset export filename, an unsafe human review comment, and
raw source content accidentally embedded in the audit report itself must
each be caught by ReportingSafetyGate (docs #60) and block packaging --
driven through the real ZIPBuilder.build() end-to-end pipeline (Phase
11b), not merely run_reporting_safety_gate's own unit tests.
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from phi_core.control.records import VerifiedClassificationManifest
from phi_core.control.report_artifacts import ReportArtifacts
from phi_core.control.zip_builder import ReportingSafetyRefused, ZIPBuilder
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def _pdf(path: Path, text: str) -> Path:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawString(72, 720, text)
    c.save()
    return path


def _xlsx(path: Path, rows: list[list[str]]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def _json_file(path: Path, data: str) -> Path:
    path.write_text(data, encoding="utf-8")
    return path


def _manifest(**overrides) -> VerifiedClassificationManifest:
    base = dict(run_id="run-adv-reports", preview_review_id="", unresolved_items=0, status="verified_for_execution")
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def _base_artifacts(tmp_path: Path, *, audit_text: str, human_review_pdf: Path | None = None,
                     ledger_rows: list[list[str]] | None = None) -> ReportArtifacts:
    ledger_rows = ledger_rows or [["Column", "Action"], ["visit_date", "generalize"]]
    return ReportArtifacts(
        audit_report_pdf=_pdf(tmp_path / "audit.pdf", audit_text),
        column_ledger_xlsx=_xlsx(tmp_path / "column_ledger.xlsx", ledger_rows),
        technical_appendix_pdf=_pdf(tmp_path / "appendix.pdf", "Technical appendix body, nothing sensitive here."),
        human_review_summary_pdf=human_review_pdf,
        evidence_manifest_json=_json_file(tmp_path / "evidence.json", '{"evidence": []}'),
        verification_manifest_json=_json_file(tmp_path / "verification.json", '{"passed": true}'),
        run_manifest_json=_json_file(tmp_path / "run.json", '{"run_id": "run-adv-reports"}'),
        checksums_sha256=_json_file(tmp_path / "upstream_checksums.sha256", "placeholder\n"),
    )


# ---------------------------------------------------------------------------
# 1. sensitive header -- the column ledger's own header/column-name cell
#    (not a value/note cell, extending the existing SSN-in-note-column
#    coverage) carries a raw identifier and must block packaging.
# ---------------------------------------------------------------------------


def test_sensitive_column_header_blocks_packaging(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    planted_ssn = "246-81-3579"
    artifacts = _base_artifacts(
        artifacts_dir, audit_text="Audit body, nothing sensitive.",
        ledger_rows=[["Column", "Action"], [f"patient ssn {planted_ssn} raw", "drop"]],
    )
    dataset_exports = {"f1": tmp_path.joinpath("export_f1.csv")}
    dataset_exports["f1"].write_text("col_a\n1\n", encoding="utf-8")
    manifest = _manifest(run_id="run-header")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ReportingSafetyRefused) as excinfo:
        ZIPBuilder.build(
            manifest=manifest, output_dir=output_dir, artifacts=artifacts,
            dataset_exports=dataset_exports, human_review_occurred=False,
        )

    assert excinfo.value.result.verdict == "FAIL"
    assert not (output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip").exists()


# ---------------------------------------------------------------------------
# 2. sensitive filename -- the dataset export's own on-disk filename
#    (scanned via the "filenames" surface) carries a raw identifier.
# ---------------------------------------------------------------------------

def test_sensitive_dataset_export_filename_blocks_packaging(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = _base_artifacts(artifacts_dir, audit_text="Audit body, nothing sensitive.")
    planted_name = "Marcus Whitfield DOB 1958-03-14 export.csv"
    export_path = tmp_path / planted_name
    export_path.write_text("col_a\n1\n", encoding="utf-8")
    dataset_exports = {"f1": export_path}
    manifest = _manifest(run_id="run-filename")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ReportingSafetyRefused) as excinfo:
        ZIPBuilder.build(
            manifest=manifest, output_dir=output_dir, artifacts=artifacts,
            dataset_exports=dataset_exports, human_review_occurred=False,
        )

    assert excinfo.value.result.verdict == "FAIL"
    findings_surfaces = {f.surface for f in excinfo.value.result.findings}
    assert "filenames" in findings_surfaces
    assert not (output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip").exists()


# ---------------------------------------------------------------------------
# 3. unsafe human comment -- the packaged Human Review Summary PDF itself
#    carries a raw reviewer comment quoting an identifier.
# ---------------------------------------------------------------------------


def test_unsafe_human_review_comment_blocks_packaging(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    planted_name = "Sofia Delgado-Reyes"
    human_review_pdf = _pdf(
        artifacts_dir / "human_review.pdf",
        f"Reviewer note: confirmed with {planted_name} directly by phone before resolving.",
    )
    artifacts = _base_artifacts(
        artifacts_dir, audit_text="Audit body, nothing sensitive.", human_review_pdf=human_review_pdf,
    )
    dataset_exports = {"f1": tmp_path / "export_f1.csv"}
    dataset_exports["f1"].write_text("col_a\n1\n", encoding="utf-8")
    manifest = _manifest(run_id="run-comment")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ReportingSafetyRefused) as excinfo:
        ZIPBuilder.build(
            manifest=manifest, output_dir=output_dir, artifacts=artifacts,
            dataset_exports=dataset_exports, human_review_occurred=True,
        )

    assert excinfo.value.result.verdict == "FAIL"
    findings_surfaces = {f.surface for f in excinfo.value.result.findings}
    assert "human_review_summaries" in findings_surfaces
    assert not (output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip").exists()


# ---------------------------------------------------------------------------
# 4. raw source accidentally included -- the audit report's own narrative
#    text (not a sibling forbidden file, extending
#    test_zip_builder_never_includes_forbidden_categories's sibling-file
#    coverage) accidentally quotes a raw dataset value verbatim instead
#    of an alias/summary.
# ---------------------------------------------------------------------------

def test_raw_source_value_accidentally_quoted_in_audit_report_blocks_packaging(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    planted_phone = "617-555-0142"
    audit_text = (
        f"Column 'patient_id' was generalized. Example original value: patient phone {planted_phone} "
        "(should have been aliased)."
    )
    artifacts = _base_artifacts(artifacts_dir, audit_text=audit_text)
    dataset_exports = {"f1": tmp_path / "export_f1.csv"}
    dataset_exports["f1"].write_text("col_a\n1\n", encoding="utf-8")
    manifest = _manifest(run_id="run-rawsource")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ReportingSafetyRefused) as excinfo:
        ZIPBuilder.build(
            manifest=manifest, output_dir=output_dir, artifacts=artifacts,
            dataset_exports=dataset_exports, human_review_occurred=False,
        )

    assert excinfo.value.result.verdict == "FAIL"
    findings_surfaces = {f.surface for f in excinfo.value.result.findings}
    assert "report_text" in findings_surfaces
    assert not (output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip").exists()
