from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import yaml


TEST_PHI_KEY_HEX = "0" * 64


def _drop_phi_runtime_modules() -> None:
    keep = {"phi_engine", "phi_engine.utils", "phi_engine.utils.pipeline_lock"}
    for name in list(sys.modules):
        if name in keep:
            continue
        if name.startswith("phi_engine.") or name.startswith("scripts.extraction.forms_manifest"):
            del sys.modules[name]


@contextmanager
def _workspace(tmp_path: Path, study: str = "Phase2Study") -> Iterator[Path]:
    old_workspace = os.environ.get("PHI_WORKSPACE")
    old_study = os.environ.get("STUDY_NAME")
    old_key = os.environ.get("PHI_KEY_PATH")
    key = tmp_path / "phi_key"
    key.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key.chmod(0o600)
    try:
        os.environ["PHI_WORKSPACE"] = str(tmp_path / "workspace")
        os.environ["STUDY_NAME"] = study
        os.environ["PHI_KEY_PATH"] = str(key)
        _drop_phi_runtime_modules()
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
        _drop_phi_runtime_modules()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_intake_rescans_stable_ids_aliases_and_removals(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir()
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "data_dictionary" / "labs_copy.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        first = intake_add(source, "Phase2Study")
        entries_by_rel = {entry["relative_path"]: entry for entry in first["entries"].values()}
        assert set(entries_by_rel) == {"datasets/labs.csv", "data_dictionary/labs_copy.csv"}
        assert entries_by_rel["datasets/labs.csv"]["artifact_id"] != entries_by_rel["data_dictionary/labs_copy.csv"]["artifact_id"]
        assert entries_by_rel["datasets/labs.csv"]["sha256"] == entries_by_rel["data_dictionary/labs_copy.csv"]["sha256"]

        kept_id = entries_by_rel["datasets/labs.csv"]["artifact_id"]
        (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,41\n", encoding="utf-8")
        (source / "data_dictionary" / "labs_copy.csv").unlink()
        second = intake_add(source, "Phase2Study")
        second_by_rel = {entry["relative_path"]: entry for entry in second["entries"].values()}
        assert second_by_rel["datasets/labs.csv"]["artifact_id"] == kept_id
        assert second_by_rel["datasets/labs.csv"]["sha256"] != entries_by_rel["datasets/labs.csv"]["sha256"]
        assert second["removals"][-1]["relative_path"] == "data_dictionary/labs_copy.csv"

        manifest_path = workspace / "intake" / "Phase2Study" / "intake_manifest.json"
        tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
        tampered["unexpected"] = True
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown intake manifest keys"):
            load_intake_manifest("Phase2Study")


def test_organize_uses_verified_descriptor_copy_and_header_ids(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("Subject ID,Age\n001,40\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake = intake_add(source, "Phase2Study")
        artifact = next(iter(intake["entries"].values()))
        manifest = organize("Phase2Study")

        dataset = manifest["datasets"][0]
        assert dataset["artifact_id"] == artifact["artifact_id"]
        assert dataset["source_sha256"] == artifact["sha256"]
        verified = workspace / "organized" / "Phase2Study" / ".verified_sources" / artifact["artifact_id"]
        assert verified.read_bytes() == (source / "datasets" / "labs.csv").read_bytes()
        assert stat.S_IMODE(verified.stat().st_mode) == 0o600
        assert all(header["header_id"].startswith("h_") for header in dataset["headers"])
        protected_map = workspace / "organized" / "Phase2Study" / ".protected" / "headers" / f"{artifact['artifact_id']}.json"
        assert stat.S_IMODE(protected_map.stat().st_mode) == 0o600
        dataset_path = workspace / "organized" / "Phase2Study" / "datasets" / dataset["output"]
        assert stat.S_IMODE(dataset_path.stat().st_mode) == 0o600
        organize_manifest = json.loads((workspace / "organized" / "Phase2Study" / "organize_manifest.json").read_text(encoding="utf-8"))
        manifest_text = json.dumps(organize_manifest)
        assert "Subject ID" not in manifest_text
        assert "datasets/labs.csv" not in manifest_text
        assert "source_original" not in manifest_text
        assert "source_relative_path" not in manifest_text
        assert "source_manifest_sha" not in organize_manifest
        assert "intake_manifest_sha" in organize_manifest
        row = _read_jsonl(workspace / "organized" / "Phase2Study" / "datasets" / dataset["output"])[0]
        assert set(row) == {header["header_id"] for header in dataset["headers"]}
        assert "Subject ID" not in row


def test_organize_rejects_source_mutation_during_verified_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    data_file = source / "datasets" / "labs.csv"
    data_file.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module

        intake_add(source, "Phase2Study")
        real_copy = organize_module._copy_descriptor_to_verified

        def mutate_then_copy(fd: int, dest: Path) -> str:
            data_file.write_text("SUBJID,AGE\n1,99\n", encoding="utf-8")
            return real_copy(fd, dest)

        monkeypatch.setattr(organize_module, "_copy_descriptor_to_verified", mutate_then_copy)
        manifest = organize_module.organize("Phase2Study")
        assert manifest["datasets"] == []
        assert manifest["review_bucket"][0]["reason"] == "source-mutated-during-copy"


def test_forms_manifest_dataset_dependencies_validate_ids_hashes_and_paths(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir()
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "data_dictionary" / "labs.xlsx").write_text("not actually parsed here", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from scripts.extraction.forms_manifest import ManifestMismatchError, check_forms_manifest

        intake = intake_add(source, "Phase2Study")
        by_rel = {entry["relative_path"]: entry for entry in intake["entries"].values()}
        config_dir = workspace / "config" / "Phase2Study"
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
                                "dataset_artifact_id": by_rel["datasets/labs.csv"]["artifact_id"],
                                "dataset_source_sha256": by_rel["datasets/labs.csv"]["sha256"],
                                "support": "data_dictionary/labs.xlsx",
                                "support_artifact_id": by_rel["data_dictionary/labs.xlsx"]["artifact_id"],
                                "support_source_sha256": by_rel["data_dictionary/labs.xlsx"]["sha256"],
                                "kind": "dictionary",
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
        result = check_forms_manifest(source / "datasets")
        assert result.dataset_dependencies["datasets/labs.csv"][0].support == "data_dictionary/labs.xlsx"

        bad = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        bad["dataset_dependencies"]["../escape.csv"] = []
        manifest_path.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(ManifestMismatchError):
            check_forms_manifest(source / "datasets")


def test_support_files_parse_normalized_rows_and_limits(tmp_path: Path) -> None:
    from phi_engine.pipeline.dependencies import DependencyKind, SupportFailureCode, SupportParseStatus
    from phi_engine.pipeline.support_files import parse_support_artifact

    support = tmp_path / "dict.csv"
    support.write_text("variable,label\nSUBJID,Subject ID\n", encoding="utf-8")
    parsed = parse_support_artifact(
        artifact_id="a_" + "a" * 32,
        source_sha256="b" * 64,
        kind=DependencyKind.DICTIONARY,
        source_path=support,
        output_dir=tmp_path / "out",
    )
    assert parsed.parse_status is SupportParseStatus.PARSED
    rows = _read_jsonl(parsed.normalized_rows_path)
    assert rows == [
        {
            "support_artifact_id": "a_" + "a" * 32,
            "source_sha256": "b" * 64,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": 0,
            "cells": [
                {"column_index": 0, "value": "SUBJID"},
                {"column_index": 1, "value": "Subject ID"},
            ],
        }
    ]

    too_large = parse_support_artifact(
        artifact_id="a_" + "c" * 32,
        source_sha256="d" * 64,
        kind=DependencyKind.DICTIONARY,
        source_path=support,
        output_dir=tmp_path / "out2",
        limits={"max_rows": 0},
    )
    assert too_large.parse_status is SupportParseStatus.FAILED
    assert too_large.failure_code is SupportFailureCode.ROW_LIMIT
    assert too_large.normalized_rows_path is None


def test_intake_load_rejects_duplicate_paths_broken_links_and_outside_targets(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        manifest = intake_add(source, "Phase2Study")
        manifest_path = workspace / "intake" / "Phase2Study" / "intake_manifest.json"
        link_name, entry = next(iter(manifest["entries"].items()))

        dup = json.loads(manifest_path.read_text(encoding="utf-8"))
        other = dict(entry)
        other["link_name"] = "duplicate__labs.csv"
        other["artifact_id"] = "a_" + "2" * 32
        (manifest_path.parent / other["link_name"]).symlink_to(source / "datasets" / "labs.csv")
        dup["entries"][other["link_name"]] = other
        manifest_path.write_text(json.dumps(dup), encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate relative_path"):
            load_intake_manifest("Phase2Study")
        (manifest_path.parent / other["link_name"]).unlink()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        intake_add(source, "Phase2Study")
        (manifest_path.parent / link_name).unlink()
        with pytest.raises(ValueError, match="broken intake link"):
            load_intake_manifest("Phase2Study")

        intake_add(source, "Phase2Study")
        outside = tmp_path / "outside.csv"
        outside.write_text("x\n", encoding="utf-8")
        (manifest_path.parent / link_name).unlink()
        (manifest_path.parent / link_name).symlink_to(outside)
        with pytest.raises(ValueError, match="outside source_root"):
            load_intake_manifest("Phase2Study")


def test_aliases_are_independently_organized_and_role_resolved(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir()
    content = "variable,label\nSUBJID,Subject ID\n"
    (source / "datasets" / "labs.csv").write_text(content, encoding="utf-8")
    (source / "data_dictionary" / "labs.csv").write_text(content, encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake = intake_add(source, "Phase2Study")
        assert len(intake["entries"]) == 2
        manifest = organize("Phase2Study")
        assert len(manifest["datasets"]) == 1
        assert len(manifest["support_artifacts"]) == 1
        assert manifest["datasets"][0]["artifact_id"] != manifest["support_artifacts"][0]["artifact_id"]


def test_nested_unrecognized_directories_do_not_use_root_suffix_or_pdf_companion_fallback(tmp_path: Path) -> None:
    source = tmp_path / "src"
    misc = source / "misc"
    misc.mkdir(parents=True)
    (misc / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (misc / "labs.pdf").write_bytes(b"%PDF-1.4\n% nested misc fixture\n")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake_add(source, "Phase2Study")
        manifest = organize("Phase2Study")
        assert manifest["datasets"] == []
        assert manifest["pdf_roles"] == {}
        review_by_file = {entry["file"]: entry for entry in manifest["review_bucket"]}
        assert review_by_file["labs.csv"]["reason"] == "unrecognized-format"
        assert review_by_file["labs.pdf"]["reason"] == "unrecognized-format"


def test_nested_canonical_directory_names_do_not_trigger_directory_role_fallback(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "misc" / "datasets").mkdir(parents=True)
    (source / "uploads" / "data_dictionary").mkdir(parents=True)
    (source / "misc" / "forms").mkdir(parents=True)
    (source / "misc" / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "uploads" / "data_dictionary" / "foo.csv").write_text("variable,label\nSUBJID,Subject ID\n", encoding="utf-8")
    (source / "misc" / "forms" / "labs.pdf").write_bytes(b"%PDF-1.4\n% nested forms fixture\n")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake_add(source, "Phase2Study")
        manifest = organize("Phase2Study")
        assert manifest["datasets"] == []
        assert manifest["support_artifacts"] == []
        assert manifest["pdf_roles"] == {}
        review_by_file = {entry["file"]: entry for entry in manifest["review_bucket"]}
        assert review_by_file["labs.csv"]["reason"] == "unrecognized-format"
        assert review_by_file["foo.csv"]["reason"] == "unrecognized-format"
        assert review_by_file["labs.pdf"]["reason"] == "unrecognized-format"


def test_manifest_confirmed_role_overrides_nested_root_anchored_fallback(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "misc" / "datasets").mkdir(parents=True)
    (source / "misc" / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake = intake_add(source, "Phase2Study")
        by_rel = {entry["relative_path"]: entry for entry in intake["entries"].values()}
        config_dir = workspace / "config" / "Phase2Study"
        config_dir.mkdir(parents=True)
        (config_dir / "_forms_manifest.yaml").write_text(
            yaml.safe_dump(
                {
                    "dataset_dependencies_schema": "dataset-dependencies/v1",
                    "dataset_dependencies_code_table_version": 1,
                    "dataset_dependencies": {"misc/datasets/labs.csv": []},
                }
            ),
            encoding="utf-8",
        )
        manifest = organize("Phase2Study")
        assert len(manifest["datasets"]) == 1
        assert manifest["datasets"][0]["artifact_id"] == by_rel["misc/datasets/labs.csv"]["artifact_id"]
        assert manifest["review_bucket"] == []


def test_organize_rejects_source_hash_mismatch_and_in_root_symlink_swap(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    data_file = source / "datasets" / "labs.csv"
    data_file.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module

        intake = intake_add(source, "Phase2Study")
        manifest_path = Path(os.environ["PHI_WORKSPACE"]) / "intake" / "Phase2Study" / "intake_manifest.json"
        tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
        first_key = next(iter(tampered["entries"]))
        tampered["entries"][first_key]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        manifest = organize_module.organize("Phase2Study")
        assert manifest["datasets"] == []
        assert manifest["review_bucket"][0]["reason"] == "source-hash-mismatch"

        intake_add(source, "Phase2Study")
        decoy = source / "datasets" / "decoy.csv"
        decoy.write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
        data_file.unlink()
        data_file.symlink_to(decoy)
        manifest = organize_module.organize("Phase2Study")
        assert manifest["datasets"] == []
        assert manifest["review_bucket"][0]["reason"] == "source-symlink-not-allowed"


def test_organize_snapshot_temp_file_is_created_private_before_any_bytes_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reproduces the exact prior vulnerability: under a permissive umask, the
    # snapshot's private-content temp file must never be observably 0644 at
    # any point, including at the moment of creation before content is
    # written -- not merely "eventually chmod'd to 0600 after the fact".
    import phi_engine.pipeline.organize as organize_module

    old_umask = os.umask(0o022)
    try:
        src = tmp_path / "source.csv"
        src.write_bytes(b"PHI-bearing raw content" * 200)
        fd = os.open(src, os.O_RDONLY)

        observed_modes: list[int] = []
        real_open = os.open

        def recording_open(path, flags, mode=0o777, *args, **kwargs):
            result = real_open(path, flags, mode, *args, **kwargs)
            if (flags & os.O_CREAT) and (flags & os.O_EXCL):
                observed_modes.append(stat.S_IMODE(os.fstat(result).st_mode))
            return result

        monkeypatch.setattr(organize_module.os, "open", recording_open)
        dest = tmp_path / "verified" / "artifact123"
        try:
            organize_module._copy_descriptor_to_verified(fd, dest)
        finally:
            os.close(fd)

        assert observed_modes, "private temp file creation was never observed by the recording hook"
        assert all(mode == 0o600 for mode in observed_modes), observed_modes
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600
        assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700
        leftover = [p.name for p in dest.parent.iterdir() if p != dest]
        assert leftover == []
    finally:
        os.umask(old_umask)


def test_organize_normalizes_injected_snapshot_copy_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A filesystem-level failure during the snapshot copy itself (distinct
    # from anything open_verified_source already normalizes) must never
    # leak as a raw OSError -- it must become the fixed source-unreadable
    # review reason, with no destination artifact left behind.
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module

        intake_add(source, "Phase2Study")

        def broken_copy(fd: int, dest: Path) -> str:
            raise OSError(5, "SENTINEL_COPY_EIO")

        monkeypatch.setattr(organize_module, "_copy_descriptor_to_verified", broken_copy)
        manifest = organize_module.organize("Phase2Study")

        assert manifest["datasets"] == []
        assert manifest["review_bucket"][0]["reason"] == "source-unreadable"
        serialized = json.dumps(manifest)
        assert "SENTINEL_COPY_EIO" not in serialized
        verified_dir = Path(os.environ["PHI_WORKSPACE"]) / "organized" / "Phase2Study" / ".verified_sources"
        leftover = list(verified_dir.iterdir()) if verified_dir.exists() else []
        assert leftover == []


def test_verified_snapshot_removes_leftover_artifact_on_race_between_own_postcheck_and_context_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a source mutation landing in the narrow window AFTER
    # _verified_snapshot's own pre/post fstat comparison already passed
    # (so its explicit "source-mutated-during-copy" unlink branch never
    # fires) but BEFORE open_verified_source's own context-exit identity
    # re-check fires (which then raises VerifiedSourceError from a point
    # this function never explicitly checks) must still remove whatever
    # _copy_descriptor_to_verified already wrote -- not just the two
    # explicit unlink() branches this function had before this fix.
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    target = source / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module

        intake_add(source, "Phase2Study")

        real_same_stat = organize_module._same_stat

        def racing_same_stat(left: object, right: object) -> bool:
            # The organizer's own pre/post comparison reflects the
            # pre-mutation state (both stats were already captured before
            # this call) and legitimately passes; mutate the source right
            # after, landing squarely in the window before
            # open_verified_source's own context-exit re-check fires.
            result = real_same_stat(left, right)
            target.write_text("SUBJID,AGE\n1,40\nRACE,ROW\n", encoding="utf-8")
            return result

        monkeypatch.setattr(organize_module, "_same_stat", racing_same_stat)
        manifest = organize_module.organize("Phase2Study")

        assert manifest["datasets"] == []
        assert manifest["review_bucket"][0]["reason"] == "source-unreadable"
        verified_dir = Path(os.environ["PHI_WORKSPACE"]) / "organized" / "Phase2Study" / ".verified_sources"
        leftover = list(verified_dir.iterdir()) if verified_dir.exists() else []
        assert leftover == []


def test_organize_rejects_same_byte_different_inode_source_replacement(tmp_path: Path) -> None:
    # Organizer snapshots must be bound to the exact manifest-recorded
    # identity (device/inode/size/mtime_ns), not merely to a matching
    # content hash: swapping the source path to a DIFFERENT inode with the
    # SAME bytes is a provenance/hardlink-policy violation, not a benign
    # no-op, and must fail closed even though the eventual copied hash would
    # still match.
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    data_file = source / "datasets" / "labs.csv"
    data_file.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        import phi_engine.pipeline.organize as organize_module

        intake_add(source, "Phase2Study")
        replacement = source / "datasets" / "replacement.csv"
        replacement.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
        data_file.unlink()
        replacement.rename(data_file)
        manifest = organize_module.organize("Phase2Study")
        assert manifest["datasets"] == []
        assert manifest["review_bucket"][0]["reason"] == "source-unreadable"


def test_organize_fails_closed_for_malformed_stale_forms_manifest(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from scripts.extraction.forms_manifest import ManifestMismatchError

        intake_add(source, "Phase2Study")
        config_dir = workspace / "config" / "Phase2Study"
        config_dir.mkdir(parents=True)
        (config_dir / "_forms_manifest.yaml").write_text("unknown_key: true\n", encoding="utf-8")
        with pytest.raises(ManifestMismatchError, match="unknown forms manifest keys"):
            organize("Phase2Study")


def test_intake_removes_existing_path_that_becomes_broken_or_outside_symlink(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    file_path = source / "datasets" / "labs.csv"
    file_path.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "Phase2Study")
        first_id = next(iter(first["entries"].values()))["artifact_id"]
        file_path.unlink()
        os.symlink(source / "missing.csv", file_path)
        second = intake_add(source, "Phase2Study")
        assert second["entries"] == {}
        assert second["removals"][-1]["artifact_id"] == first_id
        assert second["removals"][-1]["relative_path"] == "datasets/labs.csv"

        file_path.unlink()
        outside = tmp_path / "outside.csv"
        outside.write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
        file_path.symlink_to(outside)
        third = intake_add(source, "Phase2Study")
        assert third["entries"] == {}
        assert third["errors"][0]["reason"] == "source-target-outside-root"


def test_organize_rejects_real_symlink_escape_after_intake(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    file_path = source / "datasets" / "labs.csv"
    file_path.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake_add(source, "Phase2Study")
        file_path.unlink()
        outside = tmp_path / "outside.csv"
        outside.write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
        file_path.symlink_to(outside)
        with pytest.raises(ValueError, match="intake link target outside source_root"):
            organize("Phase2Study")


def test_organize_parses_support_from_extensionless_verified_snapshots_and_hides_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir()
    (source / "forms").mkdir()
    (source / "datasets" / "labs.csv").write_text("Subject ID,Age\nA1,40\n", encoding="utf-8")
    (source / "data_dictionary" / "labs.csv").write_text("variable,label\nSubject ID,Subject identifier\n", encoding="utf-8")

    pd = pytest.importorskip("pandas")
    pd.DataFrame([["variable", "label"], ["Age", "Age in years"]]).to_excel(
        source / "data_dictionary" / "labs.xlsx", index=False, header=False
    )

    canvas_mod = pytest.importorskip("reportlab.pdfgen.canvas")
    canvas = canvas_mod.Canvas(str(source / "forms" / "labs.pdf"))
    canvas.drawString(72, 720, "Subject ID annotated form code")
    canvas.save()

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize

        intake_manifest = intake_add(source, "Phase2Study")
        by_rel = {entry["relative_path"]: entry for entry in intake_manifest["entries"].values()}
        manifest = organize("Phase2Study")
        support_artifacts = manifest["support_artifacts"]
        assert len(support_artifacts) == 3
        assert {item["parse_status"] for item in support_artifacts} == {"parsed"}
        assert {item["format"] for item in support_artifacts} == {"csv", "xlsx", "pdf"}

        ordinary = json.dumps(support_artifacts, sort_keys=True)
        assert "normalized_rows_path" not in ordinary
        assert "source_relative_path" not in ordinary
        assert "normalized_source_stem" not in ordinary
        assert "labs.csv" not in ordinary
        assert "labs.xlsx" not in ordinary
        assert "labs.pdf" not in ordinary

        organized_root = workspace / "organized" / "Phase2Study"
        for rel in ("data_dictionary/labs.csv", "data_dictionary/labs.xlsx", "forms/labs.pdf"):
            snapshot = organized_root / ".verified_sources" / by_rel[rel]["artifact_id"]
            assert snapshot.is_file()
            assert snapshot.suffix == ""

        protected_support_dir = organized_root / ".protected" / "support"
        protected = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(protected_support_dir.glob("*.json"))]
        assert len(protected) == 3
        for item in protected:
            normalized_path = Path(item["normalized_rows_path"])
            assert normalized_path.name == f"labs__{item['artifact_id']}.jsonl"
            assert normalized_path.is_file()
            assert stat.S_IMODE(normalized_path.stat().st_mode) == 0o600
            assert stat.S_IMODE((protected_support_dir / f"{item['artifact_id']}.json").stat().st_mode) == 0o600

        import phi_engine.pipeline.dependencies as depmod
        from phi_engine.pipeline.dependencies import (
            OrganizedDataset,
            OrganizedHeader,
            load_protected_support_artifacts,
            recommend_dependencies,
        )

        monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: "4" * 64)
        dataset_entry = manifest["datasets"][0]
        header_payload = json.loads(
            (organized_root / ".protected" / "headers" / f"{dataset_entry['artifact_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        dataset = OrganizedDataset(
            artifact_id=dataset_entry["artifact_id"],
            source_sha256=dataset_entry["source_sha256"],
            normalized_rows_path=organized_root / "datasets" / dataset_entry["output"],
            normalized_rows_sha256=dataset_entry["normalized_rows_sha256"],
            headers=tuple(OrganizedHeader(**header) for header in header_payload["headers"]),
        )
        recs = recommend_dependencies(
            datasets=(dataset,),
            support_artifacts=load_protected_support_artifacts(organized_root),
            published_raw_headers_by_dataset={dataset.artifact_id: frozenset()},
            transform_requirements_by_dataset={},
            confirmed_links=(),
            rule_bundle=type("RuleBundleStub", (), {"rules_sha256": "5" * 64})(),
        )
        pdf_recs = [rec for rec in recs if rec.kind.value == "pdf" and rec.reason_code.value == "same_stem_companion"]
        assert len(pdf_recs) == 1
        assert pdf_recs[0].support_artifact_id == by_rel["forms/labs.pdf"]["artifact_id"]
