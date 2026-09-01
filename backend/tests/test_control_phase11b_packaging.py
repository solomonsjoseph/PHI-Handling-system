"""Phase 11b wave 2 (Packaging and Integration): ``ZIPBuilder`` (docs
#58/#61), ``IntegrityService`` (docs #62), and the real
``report_package_complete`` producer (``control/report_artifacts.py``).

Covers: the section-61 canonical ZIP structure (with and without
``04_Human_Review/``), the reporting-safety refusal path (docs #60, a
genuine planted-finding case, not a vacuous empty-content pass), the
section-61 exclusion invariant (a genuine positive-detection case: forbidden
files planted right next to the legitimate inputs are never pulled in), and
the section-62 exact-output binding (both the all-agree PASS case and a
genuine cross-run mismatch REFUSAL).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import openpyxl
import pytest
from phi_core.control.integrity_service import (
    BindingKey,
    ExactOutputBindingViolation,
    IntegrityService,
)
from phi_core.control.records import ExecutionResult, VerificationResult, VerifiedClassificationManifest
from phi_core.control.report_artifacts import ReportArtifacts, is_report_package_complete
from phi_core.control.zip_builder import ReportingSafetyRefused, ZIPBuilder
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# ---- fixtures ---------------------------------------------------------------


def _pdf(path: Path, text: str) -> Path:
    """A real, on-disk PDF with a genuine digital text layer (well past
    ``file_readers.OCR_TEXT_THRESHOLD`` so extraction never falls through
    to the OCR path)."""
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


def _json_file(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _dataset_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _artifacts(tmp_path: Path, *, with_human_review: bool = False,
               ledger_rows: list[list[str]] | None = None) -> ReportArtifacts:
    ledger_rows = ledger_rows or [["Column", "Action"], ["visit_date", "generalize"]]
    return ReportArtifacts(
        audit_report_pdf=_pdf(
            tmp_path / "audit.pdf",
            "PHI handling audit report body for this run. Nothing sensitive is written here.",
        ),
        column_ledger_xlsx=_xlsx(tmp_path / "column_ledger.xlsx", ledger_rows),
        technical_appendix_pdf=_pdf(
            tmp_path / "appendix.pdf",
            "Technical appendix body describing the deterministic methods applied to each column.",
        ),
        human_review_summary_pdf=(
            _pdf(tmp_path / "human_review.pdf", "Human review summary body for this run.")
            if with_human_review else None
        ),
        evidence_manifest_json=_json_file(tmp_path / "evidence.json", {"evidence": []}),
        verification_manifest_json=_json_file(tmp_path / "verification.json", {"passed": True}),
        run_manifest_json=_json_file(tmp_path / "run.json", {"run_id": "run-a"}),
        checksums_sha256=_dataset_csv(tmp_path / "upstream_checksums.sha256", "placeholder\n"),
    )


def _manifest(**overrides) -> VerifiedClassificationManifest:
    base = dict(run_id="run-a", preview_review_id="", unresolved_items=0, status="verified_for_execution")
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def _execution_result(manifest: VerifiedClassificationManifest, **overrides) -> ExecutionResult:
    base = dict(
        task_id=f"execution:{manifest.manifest_id}", run_id=manifest.run_id,
        manifest_id=manifest.manifest_id, manifest_version=str(manifest.schema_version), success=True,
    )
    base.update(overrides)
    return ExecutionResult(**base)


def _verification_result(manifest: VerifiedClassificationManifest, **overrides) -> VerificationResult:
    base = dict(
        run_id=manifest.run_id, manifest_id=manifest.manifest_id,
        manifest_version=str(manifest.schema_version), passed=True, failed_checks=[],
        manifest_coverage_percent=100,
    )
    base.update(overrides)
    return VerificationResult(**base)


# ---- is_report_package_complete ---------------------------------------------


def test_is_report_package_complete_true_with_no_human_review(tmp_path: Path):
    artifacts = _artifacts(tmp_path, with_human_review=False)
    assert is_report_package_complete(artifacts, human_review_occurred=False) is True


def test_is_report_package_complete_false_when_a_required_field_missing(tmp_path: Path):
    artifacts = _artifacts(tmp_path, with_human_review=False)
    incomplete = ReportArtifacts(**{**artifacts.__dict__, "run_manifest_json": None})
    assert is_report_package_complete(incomplete, human_review_occurred=False) is False


def test_is_report_package_complete_requires_summary_when_review_occurred(tmp_path: Path):
    artifacts = _artifacts(tmp_path, with_human_review=False)
    assert is_report_package_complete(artifacts, human_review_occurred=True) is False


def test_is_report_package_complete_true_when_review_occurred_and_summary_present(tmp_path: Path):
    artifacts = _artifacts(tmp_path, with_human_review=True)
    assert is_report_package_complete(artifacts, human_review_occurred=True) is True


# ---- ZIPBuilder: canonical structure ----------------------------------------


def test_zip_builder_builds_canonical_structure_without_human_review(tmp_path: Path):
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    artifacts = _artifacts(tmp_path / "artifacts", with_human_review=False)
    dataset_exports = {
        "f1": _dataset_csv(tmp_path / "export_f1.csv", "col_a,col_b\n1,2\n"),
        "f2": _dataset_csv(tmp_path / "export_f2.csv", "col_a\n3\n"),
    }
    manifest = _manifest()
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = ZIPBuilder.build(
        manifest=manifest, output_dir=output_dir, artifacts=artifacts,
        dataset_exports=dataset_exports, human_review_occurred=False,
    )

    assert result.zip_path == output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip"
    assert result.zip_path.exists()
    assert result.human_review_included is False
    assert set(result.included_dataset_file_ids) == {"f1", "f2"}

    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert names == set(result.members)
    assert any(n.startswith("01_Processed_Datasets/export_f1") for n in names)
    assert any(n.startswith("01_Processed_Datasets/export_f2") for n in names)
    assert "02_Audit_Report/PHI_Handling_Audit_Report.pdf" in names
    assert "02_Audit_Report/What_Happened_to_Each_Column.xlsx" in names
    assert "03_Technical_Appendix/Technical_Appendix.pdf" in names
    assert "03_Technical_Appendix/Evidence_Manifest.json" in names
    assert "03_Technical_Appendix/Verification_Manifest.json" in names
    assert "03_Technical_Appendix/Run_Manifest.json" in names
    assert "05_Integrity/CHECKSUMS.sha256" in names
    # docs #61: no human review occurred -> the whole folder is absent,
    # never an empty placeholder.
    assert not any(n.startswith("04_Human_Review/") for n in names)


def test_zip_builder_includes_human_review_folder_when_occurred_and_present(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = _artifacts(artifacts_dir, with_human_review=True)
    dataset_exports = {"f1": _dataset_csv(tmp_path / "export_f1.csv", "col_a\n1\n")}
    manifest = _manifest(run_id="run-b")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = ZIPBuilder.build(
        manifest=manifest, output_dir=output_dir, artifacts=artifacts,
        dataset_exports=dataset_exports, human_review_occurred=True,
    )

    assert result.human_review_included is True
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "04_Human_Review/Human_Review_Summary.pdf" in names


def test_zip_builder_omits_human_review_folder_when_occurred_but_artifact_missing(tmp_path: Path):
    """A run where human review occurred but ReportGenerator has not (yet)
    produced the summary must never crash or fabricate an empty folder --
    it is honestly omitted, same as the "no review" case."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = _artifacts(artifacts_dir, with_human_review=False)
    dataset_exports = {"f1": _dataset_csv(tmp_path / "export_f1.csv", "col_a\n1\n")}
    manifest = _manifest(run_id="run-c")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = ZIPBuilder.build(
        manifest=manifest, output_dir=output_dir, artifacts=artifacts,
        dataset_exports=dataset_exports, human_review_occurred=True,
    )

    assert result.human_review_included is False
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert not any(n.startswith("04_Human_Review/") for n in names)


def test_zip_builder_checksums_cover_every_packaged_member(tmp_path: Path):
    """When no upstream CHECKSUMS.sha256 is supplied, ZIPBuilder writes its
    own -- and it must genuinely cover every member packaged before it,
    not a subset."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = _artifacts(artifacts_dir, with_human_review=False)
    artifacts = ReportArtifacts(**{**artifacts.__dict__, "checksums_sha256": None})
    dataset_exports = {"f1": _dataset_csv(tmp_path / "export_f1.csv", "col_a\n1\n")}
    manifest = _manifest(run_id="run-d")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = ZIPBuilder.build(
        manifest=manifest, output_dir=output_dir, artifacts=artifacts,
        dataset_exports=dataset_exports, human_review_occurred=False,
    )

    with zipfile.ZipFile(result.zip_path) as zf:
        checksums_text = zf.read("05_Integrity/CHECKSUMS.sha256").decode("utf-8")
    packaged_members = [m for m in result.members if m != "05_Integrity/CHECKSUMS.sha256"]
    for member in packaged_members:
        assert member in checksums_text, f"{member} missing from CHECKSUMS.sha256"


# ---- ZIPBuilder: reporting-safety refusal (docs #60) -----------------------


def test_zip_builder_refuses_to_package_on_reporting_safety_fail(tmp_path: Path):
    """Genuine positive-detection case: a real SSN planted in the packaged
    workbook must be caught and must block packaging -- not a vacuous
    all-clean pass."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = _artifacts(
        artifacts_dir, with_human_review=False,
        ledger_rows=[["Column", "Action", "Note"], ["ssn", "drop", "123-45-6789"]],
    )
    dataset_exports = {"f1": _dataset_csv(tmp_path / "export_f1.csv", "col_a\n1\n")}
    manifest = _manifest(run_id="run-e")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ReportingSafetyRefused) as excinfo:
        ZIPBuilder.build(
            manifest=manifest, output_dir=output_dir, artifacts=artifacts,
            dataset_exports=dataset_exports, human_review_occurred=False,
        )
    assert excinfo.value.result.verdict == "FAIL"
    assert len(excinfo.value.result.findings) >= 1
    # Refused before any zip was ever written to disk.
    assert not (output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip").exists()


# ---- ZIPBuilder: section-61 exclusion invariant ----------------------------


def test_zip_builder_never_includes_forbidden_categories(tmp_path: Path):
    """Plants one file per forbidden category (docs #61) in the exact same
    directories ZIPBuilder's real inputs live in, then proves none of them
    -- by name or by byte content -- ever reach the archive. A genuine
    positive-detection case: the forbidden files are real, readable, and
    sitting right where a careless implementation could have globbed them
    in; ZIPBuilder only ever touches the two paths explicitly handed to it.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = _artifacts(artifacts_dir, with_human_review=False)
    exports_dir = tmp_path
    dataset_exports = {"f1": _dataset_csv(exports_dir / "export_f1.csv", "col_a\n1\n")}

    secret_marker = "AKIA1234567890EXAMPLE"
    forbidden = {
        "raw_dataset.csv": exports_dir / "raw_dataset.csv",
        "original_dictionary.csv": exports_dir / "original_dictionary.csv",
        "original_form.pdf": exports_dir / "original_form.pdf",
        "original_crf.pdf": exports_dir / "original_crf.pdf",
        "study_protocol.docx": exports_dir / "study_protocol.docx",
        "raw_prompt.txt": exports_dir / "raw_prompt.txt",
        "raw_completion.txt": exports_dir / "raw_completion.txt",
        "sandbox_tmp.bin": exports_dir / "sandbox_tmp.bin",
        "transform_code.py": exports_dir / "transform_code.py",
        "secrets.env": exports_dir / "secrets.env",
    }
    for _label, path in forbidden.items():
        path.write_text(f"forbidden content for {path.name} secret={secret_marker}", encoding="utf-8")
    # Also plant one directly inside the artifacts directory alongside the
    # legitimate report files, and one inside the output directory itself.
    (artifacts_dir / "raw_source_next_to_report.csv").write_text("raw phi values here", encoding="utf-8")

    manifest = _manifest(run_id="run-f")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "leftover_sandbox_temp.tmp").write_text("should never be swept in", encoding="utf-8")

    result = ZIPBuilder.build(
        manifest=manifest, output_dir=output_dir, artifacts=artifacts,
        dataset_exports=dataset_exports, human_review_occurred=False,
    )

    with zipfile.ZipFile(result.zip_path) as zf:
        names = zf.namelist()
        all_bytes = b"".join(zf.read(n) for n in names)

    # Exactly the expected members, nothing extra pulled in.
    assert set(names) == set(result.members)
    for label, path in forbidden.items():
        assert path.name not in "".join(names), f"{label} name leaked into archive member list"
    assert secret_marker.encode("utf-8") not in all_bytes, "forbidden secret content leaked into archive bytes"
    assert b"raw phi values here" not in all_bytes
    assert b"should never be swept in" not in all_bytes


# ---- IntegrityService: exact-output binding (docs #62) ---------------------


def _built_zip_manifest(tmp_path: Path, manifest: VerifiedClassificationManifest, suffix: str):
    artifacts_dir = tmp_path / f"artifacts_{suffix}"
    artifacts_dir.mkdir()
    artifacts = _artifacts(artifacts_dir, with_human_review=False)
    dataset_exports = {f"f_{suffix}": _dataset_csv(tmp_path / f"export_{suffix}.csv", "col_a\n1\n")}
    output_dir = tmp_path / f"out_{suffix}"
    output_dir.mkdir()
    return ZIPBuilder.build(
        manifest=manifest, output_dir=output_dir, artifacts=artifacts,
        dataset_exports=dataset_exports, human_review_occurred=False,
    )


def test_integrity_service_passes_when_every_piece_shares_one_binding(tmp_path: Path):
    manifest = _manifest(run_id="run-x")
    execution_result = _execution_result(manifest)
    verification_result = _verification_result(manifest)
    zip_manifest = _built_zip_manifest(tmp_path, manifest, "x")

    key = IntegrityService.verify_exact_output_binding(
        manifest=manifest, execution_result=execution_result, verification_result=verification_result,
        reviewer_final_run_id=manifest.run_id, reviewer_final_manifest_id=manifest.manifest_id,
        reviewer_final_manifest_version=str(manifest.schema_version), zip_manifest=zip_manifest,
    )

    assert key == BindingKey(
        run_id=manifest.run_id, manifest_id=manifest.manifest_id, manifest_version=str(manifest.schema_version),
    )


def test_integrity_service_refuses_when_execution_result_is_from_a_different_manifest(tmp_path: Path):
    manifest = _manifest(run_id="run-y")
    other_manifest = _manifest(run_id="run-y")  # different manifest_id, same run_id
    execution_result = _execution_result(other_manifest)
    verification_result = _verification_result(manifest)
    zip_manifest = _built_zip_manifest(tmp_path, manifest, "y")

    with pytest.raises(ExactOutputBindingViolation) as excinfo:
        IntegrityService.verify_exact_output_binding(
            manifest=manifest, execution_result=execution_result, verification_result=verification_result,
            reviewer_final_run_id=manifest.run_id, reviewer_final_manifest_id=manifest.manifest_id,
            reviewer_final_manifest_version=str(manifest.schema_version), zip_manifest=zip_manifest,
        )
    assert "ExecutionResult" in str(excinfo.value)


def test_integrity_service_refuses_a_report_package_built_from_a_different_run(tmp_path: Path):
    """The exact scenario the assignment names: a report built from run
    A's real records must never be silently certified alongside run B's
    packaged export files -- the mismatch is refused, not swallowed."""
    manifest_a = _manifest(run_id="run-A")
    manifest_b = _manifest(run_id="run-B")
    execution_result_a = _execution_result(manifest_a)
    verification_result_a = _verification_result(manifest_a)
    # zip_manifest genuinely built from run B's own manifest/exports --
    # simulating a packaging bug that fed the wrong run's files in.
    zip_manifest_b = _built_zip_manifest(tmp_path, manifest_b, "b")

    with pytest.raises(ExactOutputBindingViolation) as excinfo:
        IntegrityService.verify_exact_output_binding(
            manifest=manifest_a, execution_result=execution_result_a, verification_result=verification_result_a,
            reviewer_final_run_id=manifest_a.run_id, reviewer_final_manifest_id=manifest_a.manifest_id,
            reviewer_final_manifest_version=str(manifest_a.schema_version), zip_manifest=zip_manifest_b,
        )
    message = str(excinfo.value)
    assert "ReportPackage" in message
    assert "Checksums" in message
    assert manifest_b.run_id in message and manifest_a.run_id in message


def test_integrity_service_refuses_when_reviewer_final_binding_declared_wrong(tmp_path: Path):
    manifest = _manifest(run_id="run-z")
    execution_result = _execution_result(manifest)
    verification_result = _verification_result(manifest)
    zip_manifest = _built_zip_manifest(tmp_path, manifest, "z")

    with pytest.raises(ExactOutputBindingViolation) as excinfo:
        IntegrityService.verify_exact_output_binding(
            manifest=manifest, execution_result=execution_result, verification_result=verification_result,
            reviewer_final_run_id="run-not-z", reviewer_final_manifest_id=manifest.manifest_id,
            reviewer_final_manifest_version=str(manifest.schema_version), zip_manifest=zip_manifest,
        )
    assert "ReviewerFinalResult" in str(excinfo.value)


def test_integrity_service_lists_every_mismatch_not_just_the_first(tmp_path: Path):
    manifest = _manifest(run_id="run-w")
    other_manifest = _manifest(run_id="run-w")
    execution_result = _execution_result(other_manifest)
    verification_result = _verification_result(other_manifest)
    zip_manifest = _built_zip_manifest(tmp_path, manifest, "w")

    with pytest.raises(ExactOutputBindingViolation) as excinfo:
        IntegrityService.verify_exact_output_binding(
            manifest=manifest, execution_result=execution_result, verification_result=verification_result,
            reviewer_final_run_id="wrong-run", reviewer_final_manifest_id="wrong-manifest",
            reviewer_final_manifest_version="9", zip_manifest=zip_manifest,
        )
    assert len(excinfo.value.mismatches) == 3
