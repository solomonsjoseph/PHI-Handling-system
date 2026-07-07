from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator

import pytest

from harness.make_stress_fixtures import build_stress_fixtures


TEST_PHI_KEY_HEX = "0" * 64


def _drop_phi_runtime_modules() -> None:
    """Force workspace/study-derived phi_engine paths to resolve fresh per test.

    phi_engine.config.config binds PHI_WORKSPACE, STUDY_NAME, and PHI_KEY_PATH at
    import time, and most pipeline modules hold that config module. A prefix sweep
    before and after each hermetic workspace prevents cross-test module identity
    and path churn, matching tests/test_run_phi_system.py's pattern.
    """
    for name in list(sys.modules):
        if name == "phi_engine" or name.startswith("phi_engine."):
            del sys.modules[name]


@contextmanager
def _hermetic_phi_workspace(tmp_path: Path, study_prefix: str) -> Iterator[tuple[Path, str]]:
    original_workspace = os.environ.get("PHI_WORKSPACE")
    original_study = os.environ.get("STUDY_NAME")
    original_phi_key_path = os.environ.get("PHI_KEY_PATH")

    workspace = tmp_path / "workspace"
    study = f"{study_prefix}{uuid.uuid4().hex[:8]}"
    key_path = tmp_path / "phi_key"
    key_path.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key_path.chmod(0o600)

    try:
        os.environ["PHI_WORKSPACE"] = str(workspace)
        os.environ["STUDY_NAME"] = study
        os.environ["PHI_KEY_PATH"] = str(key_path)
        _drop_phi_runtime_modules()
        yield workspace, study
    finally:
        if original_workspace is None:
            os.environ.pop("PHI_WORKSPACE", None)
        else:
            os.environ["PHI_WORKSPACE"] = original_workspace
        if original_study is None:
            os.environ.pop("STUDY_NAME", None)
        else:
            os.environ["STUDY_NAME"] = original_study
        if original_phi_key_path is None:
            os.environ.pop("PHI_KEY_PATH", None)
        else:
            os.environ["PHI_KEY_PATH"] = original_phi_key_path
        _drop_phi_runtime_modules()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_source_hashes_unchanged(manifest: dict) -> None:
    source_root = Path(manifest["source_root"])
    assert {
        rel_path: _sha256_file(source_root / rel_path)
        for rel_path in manifest["files"]
    } == manifest["files"]


def _published_dataset_dir(workspace: Path, study: str) -> Path:
    return workspace / "output" / study / "llm_source" / "datasets"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _all_published_text(workspace: Path, study: str) -> str:
    dataset_dir = _published_dataset_dir(workspace, study)
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(dataset_dir.glob("*.jsonl"))
    )


def _prepare_stress_source(tmp_path: Path) -> tuple[Path, dict]:
    source = tmp_path / "src"
    return source, build_stress_fixtures(source, seed=42)


def _intake_organize_run(tmp_path: Path, study_prefix: str = "Stress"):
    source, manifest = _prepare_stress_source(tmp_path)
    ctx = _hermetic_phi_workspace(tmp_path, study_prefix)
    workspace, study = ctx.__enter__()
    try:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline

        intake_manifest = intake_add(source, study)
        organize_manifest = organize(study)
        result = run_pipeline(study, "us")
        return ctx, workspace, study, source, manifest, intake_manifest, organize_manifest, result
    except Exception:
        ctx.__exit__(*sys.exc_info())
        raise


def test_intake_links_everything_and_preserves_source_bytes(tmp_path: Path):
    source, fixture_manifest = _prepare_stress_source(tmp_path)

    with _hermetic_phi_workspace(tmp_path, "StressIntake") as (workspace, study):
        from phi_engine.pipeline.intake import intake_add

        intake_manifest = intake_add(source, study)

        linked_or_duplicate_count = len(intake_manifest["entries"]) + len(intake_manifest["duplicates"])
        assert linked_or_duplicate_count == len(fixture_manifest["files"])
        assert any(
            Path(error["path"]).name == "vanished_file.jsonl"
            and error["reason"] == "broken-symlink-in-source"
            for error in intake_manifest["errors"]
        )

        intake_study_dir = workspace / "intake" / study
        assert (intake_study_dir / "intake_manifest.json").is_file()
        for entry in intake_study_dir.rglob("*"):
            if entry.is_dir():
                continue
            if entry.name == "intake_manifest.json":
                assert entry.is_file()
                assert not entry.is_symlink()
            else:
                assert entry.is_symlink(), f"{entry} should be an intake symlink"

        _assert_source_hashes_unchanged(fixture_manifest)


def test_organize_routes_every_format_correctly(tmp_path: Path):
    source, _fixture_manifest = _prepare_stress_source(tmp_path)

    with _hermetic_phi_workspace(tmp_path, "StressOrg") as (_workspace, study):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake_add(source, study)
        organize_manifest = organize(study)

        # Exact counts (seed-42 fixture is deterministic): pins the format
        # matrix so a routing regression can't silently drop or add datasets.
        assert len(organize_manifest["datasets"]) == 61
        assert len(organize_manifest["review_bucket"]) == 2

        datasets_by_source = {entry["source_original"]: entry for entry in organize_manifest["datasets"]}
        assert "3_Labs.csv" in datasets_by_source
        assert "2_Demographics.jsonl" in datasets_by_source
        assert "extra_group.json" in datasets_by_source
        assert "1A_Screening.xlsx" in datasets_by_source

        review_by_file = {entry["file"]: entry for entry in organize_manifest["review_bucket"]}
        assert review_by_file["corrupted_workbook.xlsx"]["reason"] == "excel-open-error"
        assert review_by_file["mystery_export.dat"]["reason"] == "unrecognized-format"
        # Review-bucket entries carry file-level metadata only -- never a row
        # value (the whole point of routing to review instead of publishing).
        allowed_keys = {"file", "link_name", "reason", "detail", "suffix"}
        for entry in organize_manifest["review_bucket"]:
            assert set(entry.keys()) <= allowed_keys

        # The manifest's pdf_roles keys are intake link names; resolve them through the
        # intake manifest to assert on source filenames rather than sha-prefixed links.
        from phi_engine.pipeline.intake import load_intake_manifest

        intake_entries = load_intake_manifest(study)["entries"]
        pdf_roles_by_file = {
            Path(intake_entries[link_name]["original_path"]).name: role
            for link_name, role in organize_manifest["pdf_roles"].items()
        }
        lab_pdf_role = pdf_roles_by_file["lab_results_table.pdf"]
        assert lab_pdf_role["role"] == "table_extracted"
        assert lab_pdf_role["tables_extracted"] >= 1
        assert pdf_roles_by_file["1A_Screening.pdf"]["role"] == "annotated_pdf_companion"

        if "legacy_site.xls" in datasets_by_source:
            assert datasets_by_source["legacy_site.xls"]["row_count"] >= 1
        else:
            assert review_by_file["legacy_site.xls"]["reason"] in {
                "xls-reader-unavailable",
                "excel-open-error",
            }


def test_run_completes_partial_and_escalates_phi_in_unexpected_columns(tmp_path: Path):
    """The two planted PHI-in-unexpected-columns headers ('NOTES' holding
    SSN-shaped values, 'COMMENT' holding phone-shaped values) must never
    publish raw, however the pipeline arrives at that outcome (name-rule
    suppression, force_drop_headers, or the value-profiler's escalation
    rule -- profile_escalations elsewhere in the same run, e.g. the corpus
    generator's own annotated-PHI sidecar columns, is a separate concern and
    intentionally NOT asserted here as an exact count)."""
    ctx, workspace, study, _source, manifest, _intake_manifest, _organize_manifest, result = _intake_organize_run(
        tmp_path, "StressRun"
    )
    try:
        assert result.exit_code == 8
        assert result.profile_escalations >= 1

        published_text = _all_published_text(workspace, study)
        for planted_row in manifest["planted_unexpected_phi_rows"]:
            assert planted_row["NOTES"] not in published_text
            assert planted_row["COMMENT"] not in published_text

        site_notes_file = manifest["planted_unexpected_phi_file"]
        for row in _read_jsonl(_published_dataset_dir(workspace, study) / site_notes_file):
            assert "NOTES" not in row
            assert "COMMENT" not in row
    finally:
        ctx.__exit__(None, None, None)


def test_feedback_loop_drop_decision_clears_the_flagged_columns(tmp_path: Path):
    ctx, workspace, study, _source, _manifest, _intake_manifest, _organize_manifest, first_result = _intake_organize_run(
        tmp_path, "StressFeedback"
    )
    try:
        from phi_engine.pipeline.review import decide
        from phi_engine.pipeline.run import run_pipeline

        decide(study, header="NOTES", decision="drop")
        decide(study, header="COMMENT", decision="drop")
        second_result = run_pipeline(study, "us")

        site_notes_outputs = sorted(_published_dataset_dir(workspace, study).glob("site_notes*.jsonl"))
        assert site_notes_outputs, "site_notes-derived output should be published"
        for output in site_notes_outputs:
            for row in _read_jsonl(output):
                assert "NOTES" not in row
                assert "COMMENT" not in row

        assert second_result.organizer_review_count == first_result.organizer_review_count
        assert second_result.review_queue_size <= first_result.review_queue_size
    finally:
        ctx.__exit__(None, None, None)


def test_llm_boundary_zero_prompts_in_default_run_and_egress_gate_blocks_contamination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, manifest = _prepare_stress_source(tmp_path)

    with _hermetic_phi_workspace(tmp_path, "StressLLM") as (_workspace, study):
        from phi_engine.config import config
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline

        prompts: list[str] = []

        def spy_complete(self, prompt: str) -> str:  # noqa: ANN001 - method signature mirrors production
            prompts.append(prompt)
            raise AssertionError("Default deterministic pipeline must not call LLMClient.complete")

        with monkeypatch.context() as m:
            m.setattr(config.LLMClient, "complete", spy_complete)
            intake_add(source, study)
            organize(study)
            run_pipeline(study, "us")

        assert prompts == []

        contaminated_value = manifest["planted_unexpected_phi_rows"][0]["NOTES"]
        client = config.LLMClient(provider="ollama", model="x", base_url="http://127.0.0.1:1")
        with pytest.raises(PermissionError) as exc_info:
            client.complete(f"Summarize this row note: {contaminated_value}")
        assert type(exc_info.value).__name__ == "PHIEgressBlockedError"
        assert contaminated_value not in str(exc_info.value)


def test_spec_check_passes_against_the_full_stress_run(tmp_path: Path):
    ctx, workspace, study, _source, manifest, _intake_manifest, _organize_manifest, result = _intake_organize_run(
        tmp_path, "StressSpec"
    )
    try:
        assert result.exit_code == 8

        source_manifest_path = tmp_path / "source_manifest.json"
        source_manifest_path.write_text(
            json.dumps(
                {
                    "source_root": manifest["source_root"],
                    "files": manifest["files"],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        from harness.spec_check import run_spec_check

        report = run_spec_check(
            workspace=workspace,
            study=study,
            skip_pytest=True,
            source_manifest=source_manifest_path,
        )
        assert report["all_pass"] is True, report["checks"]
    finally:
        ctx.__exit__(None, None, None)


def test_stale_staged_file_never_publishes_without_current_approval(tmp_path: Path):
    """Regression test (Phase 7 final-audit finding): a JSONL left sitting in
    tmp/<study>/datasets/ (e.g. residue from a prior run that scrubbed
    successfully but then failed the residual guard gate, so publish was
    skipped and the scrubbed files were never cleaned up) must NEVER be
    published by a LATER run unless it is part of THAT run's own approved
    forms -- publishing it would bypass the current run's
    classification/scrub/approval pipeline entirely."""
    with _hermetic_phi_workspace(tmp_path, "StaleStaging") as (workspace, study):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline
        import phi_engine.config.config as config

        source = tmp_path / "src"
        source.mkdir()
        rows = [{"SUBJID": f"S{i}", "AGE": 30 + i} for i in range(5)]
        (source / "current.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
        )
        intake_add(source, study)
        organize(study)

        # Seed a stale file directly into staging -- simulates leftover
        # residue that never went through this run's organizer/classifier.
        staging_dir = Path(config.STAGING_DATASETS_DIR)
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "stale.jsonl").write_text(
            json.dumps({"SSN": "123-45-6789"}) + "\n", encoding="utf-8"
        )

        result = run_pipeline(study, "us")

        assert result.exit_code == 0
        published = sorted(p.name for p in _published_dataset_dir(workspace, study).glob("*.jsonl"))
        assert published == ["current.jsonl"]
        assert "stale.jsonl" not in published
        assert not (staging_dir / "stale.jsonl").exists()  # staging cleared, not just unpublished
