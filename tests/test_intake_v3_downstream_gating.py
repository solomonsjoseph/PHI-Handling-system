"""Step-4 focused tests: organize/run/review trust intake-manifest/v3
``status``/``component`` and perform no work for a non-ready intake.

Covers:
 - organize(study) gates on manifest status BEFORE check_forms_manifest,
   deleting/recreating organized/, snapshots, or audit writes.
 - _Router routes solely from entry["component"]; _unclassified is never
   parsed; JSON/JSONL dataset dispatch and the annotated_pdfs alias are gone.
 - _run_pipeline_locked checks intake first (after lock, before privacy
   bootstrap/rulebook resolution) and returns exact 8/intake_review_required
   or 2/intake_failed with zero downstream writes.
 - review.list_review_items redacts intake review items to artifact_id
   (when present)/reason/blocking/detail (when present)/source, and
   tolerates a missing/invalid intake manifest ONLY for this key.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from reportlab.pdfgen import canvas

import phi_engine.config.config as config
import phi_engine.pipeline.run as pipeline_run

TEST_PHI_KEY_HEX = "0" * 64




@contextmanager
def _workspace(tmp_path: Path, study: str = "GateStudy") -> Iterator[Path]:
    old_workspace = os.environ.get("PHI_WORKSPACE")
    old_study = os.environ.get("STUDY_NAME")
    old_key = os.environ.get("PHI_KEY_PATH")
    key = tmp_path / "phi_key"
    key.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key.chmod(0o600)
    import phi_engine.utils.pipeline_lock as pipeline_lock_module

    original_pipeline_lock_config = pipeline_lock_module.config
    try:
        os.environ["PHI_WORKSPACE"] = str(tmp_path / "workspace")
        os.environ["STUDY_NAME"] = study
        os.environ["PHI_KEY_PATH"] = str(key)
        importlib.reload(config)
        pipeline_lock_module.config = config
        yield Path(os.environ["PHI_WORKSPACE"])
    finally:
        if old_workspace is None:
            os.environ.pop("PHI_WORKSPACE", None)
        else:
            os.environ["PHI_WORKSPACE"] = old_workspace
        if old_study is None:
            os.environ.pop("STUDY_NAME", None)
        else:
            os.environ["STUDY_NAME"] = old_study
        if old_key is None:
            os.environ.pop("PHI_KEY_PATH", None)
        else:
            os.environ["PHI_KEY_PATH"] = old_key
        importlib.reload(config)
        pipeline_lock_module.config = original_pipeline_lock_config


def _ready_source(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir()
    (source / "data_dictionary").mkdir()
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "data_dictionary" / "labs_dict.csv").write_text(
        "VAR,DESC\nAGE,Age in years\n", encoding="utf-8"
    )
    pdf_path = source / "forms" / "consent.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(72, 720, "Consent form placeholder text, no tables")
    c.save()
    return source


def _make_entry(source_root: Path, relative_path: str, component: str, artifact_id: str) -> dict[str, Any]:
    full = source_root / relative_path
    st = full.stat()
    return {
        "artifact_id": artifact_id,
        "intake_path": f"{component}/{Path(relative_path).name}",
        "component": component,
        "relative_path": relative_path,
        "original_path": str(full),
        "sha256": hashlib.sha256(full.read_bytes()).hexdigest(),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "device": st.st_dev,
        "inode": st.st_ino,
        "mode": stat.S_IMODE(st.st_mode),
    }


# --------------------------------------------------------------------------
# organize(): status gate runs before everything else, no writes when blocked
# --------------------------------------------------------------------------


def test_organize_raises_before_forms_check_and_writes_nothing_when_review_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.pipeline.intake import IntakeNotReadyError

        organized_root = workspace / "organized" / "GateStudy"
        organized_root.mkdir(parents=True)
        marker = organized_root / "marker.txt"
        marker.write_text("old organized data", encoding="utf-8")

        source = tmp_path / "src"
        source.mkdir()

        def fake_load(_study: str) -> dict[str, Any]:
            return {
                "status": "review_required",
                "entries": {},
                "source_root": str(source),
                "review_items": [
                    {"path": "forms", "reason": "missing-component-directory", "blocking": True}
                ],
            }

        monkeypatch.setattr(organize_module, "load_intake_manifest", fake_load)

        def fail_forms_manifest(*_a: object, **_k: object) -> None:
            pytest.fail("check_forms_manifest must not run before the intake-ready gate")

        monkeypatch.setattr(
            "scripts.extraction.forms_manifest.check_forms_manifest", fail_forms_manifest
        )

        with pytest.raises(IntakeNotReadyError) as exc_info:
            organize_module.organize("GateStudy")

        assert exc_info.value.status == "review_required"
        assert marker.read_text(encoding="utf-8") == "old organized data"
        assert list(organized_root.iterdir()) == [marker]


@pytest.mark.parametrize("status", ["failed", "review_required"])
def test_organize_raises_not_ready_for_every_non_ready_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    with _workspace(tmp_path):
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.pipeline.intake import IntakeNotReadyError

        monkeypatch.setattr(
            organize_module,
            "load_intake_manifest",
            lambda _s: {"status": status, "entries": {}, "source_root": str(tmp_path)},
        )
        with pytest.raises(IntakeNotReadyError) as exc_info:
            organize_module.organize("GateStudy")
        assert exc_info.value.status == status


def test_organize_propagates_missing_manifest_without_forms_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _workspace(tmp_path):
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.pipeline.intake import IntakeManifestError

        def fake_load(_study: str) -> dict[str, Any]:
            raise IntakeManifestError("intake_manifest_missing")

        monkeypatch.setattr(organize_module, "load_intake_manifest", fake_load)

        def fail_forms_manifest(*_a: object, **_k: object) -> None:
            pytest.fail("check_forms_manifest must not run for a missing manifest")

        monkeypatch.setattr(
            "scripts.extraction.forms_manifest.check_forms_manifest", fail_forms_manifest
        )

        with pytest.raises(IntakeManifestError) as exc_info:
            organize_module.organize("GateStudy")
        assert exc_info.value.code == "intake_manifest_missing"


# --------------------------------------------------------------------------
# _Router: component-only routing, no path/suffix guessing, no aliasing
# --------------------------------------------------------------------------


def test_role_for_routes_solely_from_component_and_never_guesses_from_path(tmp_path: Path) -> None:
    import phi_engine.pipeline.organize as organize_module

    router = organize_module._Router(
        "Study",
        tmp_path / "intake",
        tmp_path / "organized" / "datasets",
        tmp_path / "organized",
        {"source_root": str(tmp_path)},
    )
    assert router._role_for({"relative_path": "datasets/x.csv", "component": "datasets"}) == "dataset"
    assert (
        router._role_for({"relative_path": "data_dictionary/x.csv", "component": "data_dictionary"})
        == "dictionary"
    )
    assert router._role_for({"relative_path": "mappings/x.csv", "component": "mappings"}) == "mapping"
    assert router._role_for({"relative_path": "forms/x.pdf", "component": "forms"}) == "pdf"
    assert (
        router._role_for({"relative_path": "weird/x.dat", "component": "_unclassified"}) == "_unclassified"
    )
    # The path still says "datasets/..." but the manifest's component says
    # "forms" -- routing follows component alone, never the path/suffix.
    assert router._role_for({"relative_path": "datasets/x.pdf", "component": "forms"}) == "pdf"


def test_route_dataset_no_longer_dispatches_json_or_jsonl(tmp_path: Path) -> None:
    import phi_engine.pipeline.organize as organize_module

    source_root = tmp_path / "src"
    (source_root / "datasets").mkdir(parents=True)
    json_file = source_root / "datasets" / "labs.json"
    json_file.write_text(json.dumps([{"SUBJID": "1"}]), encoding="utf-8")
    jsonl_file = source_root / "datasets" / "labs.jsonl"
    jsonl_file.write_text(json.dumps({"SUBJID": "1"}) + "\n", encoding="utf-8")

    organized_root = tmp_path / "organized"
    router = organize_module._Router(
        "Study",
        tmp_path / "intake",
        organized_root / "datasets",
        organized_root,
        {"source_root": str(source_root)},
    )
    for path, relative in ((json_file, "datasets/labs.json"), (jsonl_file, "datasets/labs.jsonl")):
        entry = _make_entry(source_root, relative, "datasets", "a_" + "1" * 32)
        router.route_dataset("link-" + path.name, entry)

    assert router.datasets == []
    assert {item["reason"] for item in router.review_bucket} == {"unrecognized-format"}


def test_route_pdf_creates_no_annotated_pdfs_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.organize as organize_module

        source_root = tmp_path / "src"
        (source_root / "forms").mkdir(parents=True)
        pdf_path = source_root / "forms" / "labs.pdf"
        c = canvas.Canvas(str(pdf_path))
        c.drawString(72, 720, "blank consent form, no extractable tables")
        c.save()

        organized_root = workspace / "organized" / "GateStudy"
        router = organize_module._Router(
            "GateStudy",
            workspace / "intake" / "GateStudy",
            organized_root / "datasets",
            organized_root,
            {"source_root": str(source_root)},
        )
        entry = _make_entry(source_root, "forms/labs.pdf", "forms", "a_" + "2" * 32)
        router.route_pdf("link", entry)

        assert not Path(config.ANNOTATED_PDFS_DIR).exists()
        assert router.pdf_roles["link"]["role"] != "annotated_pdf_companion"
        assert "matched_dataset_stem" not in router.pdf_roles["link"]


def test_organize_ignores_confirmed_forms_manifest_dependency_component_is_sole_authority(
    tmp_path: Path,
) -> None:
    """Regression: a non-empty _forms_manifest.yaml dataset_dependencies
    entry declaring forms/consent.pdf as a confirmed PDF support dependency
    of datasets/labs.csv must NOT override the v3 'forms' component's 'pdf'
    role. _Router no longer accepts/consults confirmed_dependencies at all
    -- v3 component is the sole role authority."""
    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"

        by_rel = {entry["relative_path"]: entry for entry in intake_manifest["entries"].values()}
        dataset_entry = by_rel["datasets/labs.csv"]
        pdf_entry = by_rel["forms/consent.pdf"]

        config_dir = workspace / "config" / "GateStudy"
        config_dir.mkdir(parents=True)
        manifest_path = config_dir / "_forms_manifest.yaml"
        manifest_path.write_text(
            yaml.safe_dump(
                {
                    "dataset_dependencies_schema": "dataset-dependencies/v1",
                    "dataset_dependencies_code_table_version": 1,
                    "dataset_dependencies": {
                        "datasets/labs.csv": [
                            {
                                "dataset_artifact_id": dataset_entry["artifact_id"],
                                "dataset_source_sha256": dataset_entry["sha256"],
                                "support": "forms/consent.pdf",
                                "support_artifact_id": pdf_entry["artifact_id"],
                                "support_source_sha256": pdf_entry["sha256"],
                                "kind": "pdf",
                                "level": "required",
                                "sensitivity": "confidential",
                                "reason_code": "only_interpretation",
                                "recommendation_id": "dr_" + "1" * 32,
                                "basis": {
                                    "rulebook_sha256": "2" * 64,
                                    "scrub_config_sha256": "3" * 64,
                                    "support_role_sha256": "4" * 64,
                                },
                                "confirmed_by": "reviewer-id",
                                "confirmed_at": "2026-07-14T00:00:00Z",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        result = organize("GateStudy")

        # forms/consent.pdf still routes via route_pdf (component-derived
        # "pdf" role) despite the confirmed dependency above -- never via
        # route_support(DependencyKind.PDF), which the old self.roles
        # override would have selected.
        support_ids = {item["artifact_id"] for item in result["support_artifacts"]}
        assert pdf_entry["artifact_id"] not in support_ids
        pdf_link_names = [
            link
            for link, entry in intake_manifest["entries"].items()
            if entry["relative_path"] == "forms/consent.pdf"
        ]
        assert pdf_link_names
        assert pdf_link_names[0] in result["pdf_roles"]

        # An _unclassified entry, injected directly (never produced by a
        # ready v3 manifest in practice), stays _unclassified -- component
        # is consulted with no forms_manifest override in play.
        import phi_engine.pipeline.organize as organize_module

        router = organize_module._Router(
            "GateStudy",
            workspace / "intake" / "GateStudy",
            workspace / "organized" / "GateStudy" / "datasets",
            workspace / "organized" / "GateStudy",
            {"source_root": str(source)},
        )
        assert (
            router._role_for({"relative_path": "misc/oddfile.dat", "component": "_unclassified"})
            == "_unclassified"
        )


def test_organize_end_to_end_ready_manifest_routes_solely_by_component(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"

        result = organize("GateStudy")

        assert len(result["datasets"]) == 1
        assert result["datasets"][0]["output"] == "labs.jsonl"
        assert len(result["support_artifacts"]) == 1
        assert result["pdf_roles"]
        assert not Path(config.ANNOTATED_PDFS_DIR).exists()


# --------------------------------------------------------------------------
# _run_pipeline_locked: intake checked first, exact exit codes, no writes
# --------------------------------------------------------------------------


def test_run_pipeline_locked_maps_intake_review_required_with_no_downstream_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_run, "load_intake_manifest", lambda _s: {"status": "review_required"}
    )

    def fail(*_a: object, **_k: object) -> None:
        pytest.fail("no privacy/rulebook/organize work may run for a non-ready intake")

    monkeypatch.setattr(pipeline_run, "bootstrap_study_privacy", fail)
    monkeypatch.setattr(pipeline_run, "load_study_privacy_config", fail)
    monkeypatch.setattr(pipeline_run, "resolve_rulebook", fail)
    monkeypatch.setattr(pipeline_run, "organize", fail)

    result = pipeline_run._run_pipeline_locked("GateStudy", "us")
    assert result.exit_code == 8
    assert result.message == "intake_review_required"


def test_run_pipeline_locked_maps_intake_failed_status_with_no_downstream_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_run, "load_intake_manifest", lambda _s: {"status": "failed"})

    def fail(*_a: object, **_k: object) -> None:
        pytest.fail("no privacy/rulebook/organize work may run for a non-ready intake")

    monkeypatch.setattr(pipeline_run, "bootstrap_study_privacy", fail)
    monkeypatch.setattr(pipeline_run, "load_study_privacy_config", fail)
    monkeypatch.setattr(pipeline_run, "resolve_rulebook", fail)
    monkeypatch.setattr(pipeline_run, "organize", fail)

    result = pipeline_run._run_pipeline_locked("GateStudy", "us")
    assert result.exit_code == 2
    assert result.message == "intake_failed"


def test_run_pipeline_locked_maps_missing_or_invalid_manifest_to_intake_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(_s: str) -> dict[str, Any]:
        raise pipeline_run.IntakeManifestError("intake_manifest_invalid")

    monkeypatch.setattr(pipeline_run, "load_intake_manifest", fake_load)

    def fail(*_a: object, **_k: object) -> None:
        pytest.fail("no privacy/rulebook/organize work may run for a missing/invalid intake")

    monkeypatch.setattr(pipeline_run, "bootstrap_study_privacy", fail)
    monkeypatch.setattr(pipeline_run, "load_study_privacy_config", fail)
    monkeypatch.setattr(pipeline_run, "resolve_rulebook", fail)
    monkeypatch.setattr(pipeline_run, "organize", fail)

    result = pipeline_run._run_pipeline_locked("GateStudy", "us")
    assert result.exit_code == 2
    assert result.message == "intake_failed"


# --------------------------------------------------------------------------
# review.list_review_items: redacted intake items, tolerant only here
# --------------------------------------------------------------------------


def test_list_review_items_redacts_intake_review_items(tmp_path: Path) -> None:
    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.review import list_review_items

        source = tmp_path / "src"
        (source / "datasets").mkdir(parents=True)
        (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
        (source / "datasets" / "notes.txt").write_text("free text", encoding="utf-8")
        # forms/ and a data_dictionary-or-mappings dir are both missing, and
        # notes.txt is an unsupported dataset-directory format -- all
        # blocking review, so intake status is review_required.

        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "review_required"

        listed = list_review_items("GateStudy")
        items = listed["intake_review_items"]
        assert items
        for item in items:
            assert set(item).issubset({"artifact_id", "reason", "blocking", "detail", "source"})
            assert item["source"] == "intake"
            assert item["blocking"] is True

        serialized = json.dumps(listed)
        assert str(source) not in serialized
        assert "notes.txt" not in serialized
        assert "candidates" not in serialized


def test_list_review_items_tolerates_missing_intake_manifest(tmp_path: Path) -> None:
    with _workspace(tmp_path):
        from phi_engine.pipeline.review import list_review_items

        listed = list_review_items("NeverIntaken")
        assert listed["intake_review_items"] == []


def test_list_review_items_tolerates_invalid_intake_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _workspace(tmp_path):
        import phi_engine.pipeline.review as review_module
        from phi_engine.pipeline.intake import IntakeManifestError

        def fake_load(_study: str) -> dict[str, Any]:
            raise IntakeManifestError("intake_manifest_invalid")

        monkeypatch.setattr(review_module, "load_intake_manifest", fake_load)
        listed = review_module.list_review_items("GateStudy")
        assert listed["intake_review_items"] == []
