"""Step-4 focused tests: organize/run/review trust intake-manifest/v4
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
    (source / "dictionary_mapping").mkdir()
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "dictionary_mapping" / "labs_dict.csv").write_text(
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
        -1,
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
    from phi_engine.pipeline.verified_source import _open_pinned_root

    root_fd = _open_pinned_root(source_root)
    router = organize_module._Router(
        "Study",
        tmp_path / "intake",
        organized_root / "datasets",
        organized_root,
        root_fd,
    )
    for path, relative in ((json_file, "datasets/labs.json"), (jsonl_file, "datasets/labs.jsonl")):
        entry = _make_entry(source_root, relative, "datasets", "a_" + "1" * 32)
        router.route_dataset("link-" + path.name, entry)

    assert router.datasets == []
    assert {item["reason"] for item in router.review_bucket} == {"unrecognized-format"}
    os.close(root_fd)


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
        from phi_engine.pipeline.verified_source import _open_pinned_root

        root_fd = _open_pinned_root(source_root)
        router = organize_module._Router(
            "GateStudy",
            workspace / "intake" / "GateStudy",
            organized_root / "datasets",
            organized_root,
            root_fd,
        )
        entry = _make_entry(source_root, "forms/labs.pdf", "forms", "a_" + "2" * 32)
        router.route_pdf("link", entry)
        os.close(root_fd)

        assert not Path(config.ANNOTATED_PDFS_DIR).exists()
        assert router.pdf_roles["link"]["role"] != "annotated_pdf_companion"
        assert "matched_dataset_stem" not in router.pdf_roles["link"]


def test_organize_ignores_confirmed_forms_manifest_dependency_component_is_sole_authority(
    tmp_path: Path,
) -> None:
    """Regression: a non-empty _forms_manifest.yaml dataset_dependencies
    entry declaring forms/consent.pdf as a confirmed PDF support dependency
    of datasets/labs.csv must NOT override the v4 'forms' component's 'pdf'
    role. _Router no longer accepts/consults confirmed_dependencies at all
    -- v4 component is the sole role authority."""
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
        # ready v4 manifest in practice), stays _unclassified -- component
        # is consulted with no forms_manifest override in play.
        import phi_engine.pipeline.organize as organize_module

        router = organize_module._Router(
            "GateStudy",
            workspace / "intake" / "GateStudy",
            workspace / "organized" / "GateStudy" / "datasets",
            workspace / "organized" / "GateStudy",
            -1,
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
        # organize.py's own _COMPONENT_ROLES table (out of scope for this
        # phase -- a later Naming Boundary/Downstream V2 phase renames it
        # to DependencyKind.DICTIONARY_MAPPING per the approved plan) does
        # not yet recognize the "dictionary_mapping" component intake now
        # assigns, so a dictionary_mapping entry currently falls through
        # to organize's existing "_unclassified" role and is never parsed
        # as support content -- an expected, deferred inconsistency this
        # phase does not introduce and does not fix.
        assert result["support_artifacts"] == []
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
    monkeypatch.setattr(pipeline_run, "_organize_locked", fail)

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
    monkeypatch.setattr(pipeline_run, "_organize_locked", fail)

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
    monkeypatch.setattr(pipeline_run, "_organize_locked", fail)

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
        # forms/ and dictionary_mapping/ are both missing, and
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


# --------------------------------------------------------------------------
# organize(): source_root ancestry is verified no-follow, never resolved,
# and the per-study lock is held for the entire operation.
# --------------------------------------------------------------------------


def test_organize_rejects_post_intake_source_root_symlink_swap_before_any_write(
    tmp_path: Path,
) -> None:
    """Security regression: renaming the intake source root and replacing
    its original path with a symlink to the new location must fail closed
    at organize()'s entry -- a no-follow ancestry check on the manifest's
    LEXICALLY configured source_root -- before check_forms_manifest's
    ordinary (symlink-following) metadata read ever touches it, and before
    organized/ is deleted/recreated or anything else is written.
    Path.resolve() would silently follow the swap and erase this evidence;
    organize() must never call it on source_root."""
    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.pipeline.verified_source import VerifiedSourceError

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"
        assert intake_manifest["source_root"] == str(source)

        # Rename the real source tree elsewhere, then replace its
        # original path with a directory symlink to the new location.
        # The manifest still lexically records the ORIGINAL path, which
        # is now a symlinked alias.
        renamed = tmp_path / "src-renamed"
        source.rename(renamed)
        source.symlink_to(renamed, target_is_directory=True)

        organized_root = workspace / "organized" / "GateStudy"

        with pytest.raises(VerifiedSourceError) as exc_info:
            organize_module.organize("GateStudy")
        assert exc_info.value.reason == "source-symlink-not-allowed"
        assert not organized_root.exists()

        # The lock releases even on this failure -- a subsequent
        # operation is never left blocked by the rejected attempt.
        from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock

        acquire_pipeline_lock("GateStudy")
        release_pipeline_lock("GateStudy")


def test_organize_rejects_source_root_swap_immediately_after_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security regression: a namespace actor who swaps source_root the
    INSTANT after _open_pinned_root's ancestry walk completes -- a window
    the walk itself cannot observe, since it has already finished -- must
    still fail closed with the same fixed VerifiedSourceError before
    check_forms_manifest, before organized/ is deleted/recreated, and
    before anything else is written. Simulated by wrapping
    _open_pinned_root: call the REAL implementation to obtain a
    legitimately pinned descriptor, THEN perform the swap, THEN return
    that descriptor unchanged -- exactly the interleaving a real race
    would produce, and exactly the reproduction technique that found the
    prior gap (the earlier fix only rejected a swap completed BEFORE
    organize() was ever entered)."""
    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module
        import phi_engine.pipeline.verified_source as verified_source_module
        from phi_engine.pipeline.verified_source import VerifiedSourceError

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"

        real_open_pinned_root = verified_source_module._open_pinned_root
        renamed = tmp_path / "src-renamed-post-pin"

        def swap_after_pin(path: Path) -> int:
            fd = real_open_pinned_root(path)
            source.rename(renamed)
            source.symlink_to(renamed, target_is_directory=True)
            return fd

        monkeypatch.setattr(organize_module, "_open_pinned_root", swap_after_pin)

        organized_root = workspace / "organized" / "GateStudy"

        with pytest.raises(VerifiedSourceError) as exc_info:
            organize_module.organize("GateStudy")
        assert exc_info.value.reason == "source-unreadable"
        assert not organized_root.exists()

        from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock

        acquire_pipeline_lock("GateStudy")
        release_pipeline_lock("GateStudy")


def test_run_pipeline_locked_rejects_source_root_swap_immediately_after_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security regression: run.py performs no separate/second source-root
    read of its own -- dependency_relations is reused from organize's own
    already-verified in-memory result (see run.py's removal of
    _load_manifest_dependency_relations and its Path.resolve() call) --
    so the SAME post-pin swap window closed for standalone organize()
    above must also be closed when reached through _run_pipeline_locked's
    real (unmocked) call into _organize_locked."""
    with _workspace(tmp_path) as workspace:
        from types import SimpleNamespace

        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module
        import phi_engine.pipeline.verified_source as verified_source_module
        from phi_engine.pipeline.verified_source import VerifiedSourceError
        from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"

        real_open_pinned_root = verified_source_module._open_pinned_root
        renamed = tmp_path / "src-renamed-run-post-pin"

        def swap_after_pin(path: Path) -> int:
            fd = real_open_pinned_root(path)
            source.rename(renamed)
            source.symlink_to(renamed, target_is_directory=True)
            return fd

        monkeypatch.setattr(organize_module, "_open_pinned_root", swap_after_pin)
        monkeypatch.setattr(pipeline_run, "bootstrap_study_privacy", lambda *_a: None)
        monkeypatch.setattr(
            pipeline_run,
            "load_study_privacy_config",
            lambda *_a: SimpleNamespace(rule_refresh="pinned_only"),
        )
        monkeypatch.setattr(
            pipeline_run,
            "resolve_rulebook",
            lambda *_a, **_k: SimpleNamespace(
                bundle=SimpleNamespace(rules_sha256="0" * 64, source_mode="pinned"),
                protection_weakened=False,
                cache_status="cache_hit",
            ),
        )

        organized_root = workspace / "organized" / "GateStudy"

        acquire_pipeline_lock("GateStudy")
        try:
            with pytest.raises(VerifiedSourceError) as exc_info:
                pipeline_run._run_pipeline_locked("GateStudy", "us")
        finally:
            release_pipeline_lock("GateStudy")
        assert exc_info.value.reason == "source-unreadable"
        assert not organized_root.exists()


def test_organize_locked_rejects_direct_call_without_held_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security regression: _organize_locked is lock-required -- an
    unlocked direct call (bypassing organize()'s pipeline_lock(study)
    wrapper, and bypassing _run_pipeline_locked's caller-owned-lock
    contract) must fail closed with the fixed OrganizerLockNotHeldError
    BEFORE load_intake_manifest or any other read/write, never silently
    proceed as if some caller held the lock."""
    with _workspace(tmp_path):
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.utils.pipeline_lock import held_lock_path

        assert held_lock_path() is None

        def fail_load(_study: str) -> dict[str, Any]:
            pytest.fail(
                "_organize_locked must not read the intake manifest before its "
                "lock-ownership check"
            )

        monkeypatch.setattr(organize_module, "load_intake_manifest", fail_load)

        organized_root = Path(config.ORGANIZED_DIR) / "GateStudy"

        with pytest.raises(organize_module.OrganizerLockNotHeldError) as exc_info:
            organize_module._organize_locked("GateStudy")
        assert exc_info.value.code == "organizer_lock_not_held"
        assert not organized_root.exists()


def test_organize_locked_succeeds_when_run_pipeline_already_holds_the_lock(
    tmp_path: Path,
) -> None:
    """Companion to the rejection above: _run_pipeline_locked's real
    calling convention -- caller already holds pipeline_lock(study) --
    must still succeed through _organize_locked's new lock-ownership
    assertion, never a false-positive rejection of the legitimate,
    already-locked caller."""
    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"

        acquire_pipeline_lock("GateStudy")
        try:
            result = organize_module._organize_locked("GateStudy")
        finally:
            release_pipeline_lock("GateStudy")
        assert result["datasets"]


def test_organize_public_surface_excludes_lock_required_body() -> None:
    """_organize_locked must never be part of the module's declared public
    API: `from phi_engine.pipeline.organize import *` must expose
    organize() but never the lock-required, ownership-unenforced-by-import
    body a caller using only the supported export surface could otherwise
    invoke unlocked."""
    namespace: dict[str, Any] = {}
    exec("from phi_engine.pipeline.organize import *", namespace)
    assert "organize" in namespace
    assert "_organize_locked" not in namespace


def test_organize_holds_study_lock_for_entire_operation_blocking_concurrent_intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security regression: organize() must hold pipeline_lock(study) for
    its complete operation -- manifest load/status gate through every
    write -- so a concurrent re-intake attempt on the SAME study can
    never persist a non-ready status while organize is mid-flight.
    Previously organize() acquired no lock at all, so a race could flip
    status to review_required after organize's gate check but before its
    writes completed; here the concurrent intake_add is proven to fail
    closed immediately (non-blocking advisory-lock contention) instead."""
    with _workspace(tmp_path) as workspace:
        import threading

        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.intake import load_intake_manifest as real_load_intake_manifest
        import phi_engine.pipeline.organize as organize_module
        from phi_engine.utils.pipeline_lock import PipelineBusyError

        source = _ready_source(tmp_path)
        intake_manifest = intake_add(source, "GateStudy")
        assert intake_manifest["status"] == "ready"
        original_entry_count = len(intake_manifest["entries"])

        # Add an unsupported-format file to the SAME source. If a
        # concurrent re-intake were ever allowed to reconcile this while
        # organize is running, the study's status would flip to
        # review_required underneath it.
        (source / "datasets" / "unsupported.dat").write_bytes(b"binary junk")

        real_load = organize_module.load_intake_manifest
        entered = threading.Event()
        release = threading.Event()

        def paused_load(study: str) -> dict[str, Any]:
            result = real_load(study)
            entered.set()
            assert release.wait(timeout=10), "test deadlocked waiting for release"
            return result

        monkeypatch.setattr(organize_module, "load_intake_manifest", paused_load)

        organize_result: dict[str, Any] = {}
        organize_errors: list[BaseException] = []

        def run_organize() -> None:
            try:
                organize_result["value"] = organize_module.organize("GateStudy")
            except BaseException as exc:  # pragma: no cover - surfaced via assertion below
                organize_errors.append(exc)

        thread = threading.Thread(target=run_organize)
        thread.start()
        try:
            assert entered.wait(timeout=10), "organize did not reach its status gate"

            # organize is now mid-operation, still holding
            # pipeline_lock("GateStudy"). A concurrent re-intake attempt
            # on the same study must fail closed IMMEDIATELY (non-blocking
            # advisory-lock contention), never persist a status change,
            # and never be silently queued behind organize.
            with pytest.raises(PipelineBusyError):
                intake_add(source, "GateStudy")
        finally:
            release.set()
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert not organize_errors, organize_errors
        assert organize_result["value"] is not None

        # The rejected concurrent attempt wrote nothing: the manifest
        # organize just routed is still the original, untouched ready
        # package -- never review_required, never a new entry count.
        final_manifest = real_load_intake_manifest("GateStudy")
        assert final_manifest["status"] == "ready"
        assert len(final_manifest["entries"]) == original_entry_count

        # organize itself completed successfully against that untouched
        # ready manifest and released the lock on exit.
        assert organize_result["value"]["datasets"]
        from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock

        acquire_pipeline_lock("GateStudy")
        release_pipeline_lock("GateStudy")
