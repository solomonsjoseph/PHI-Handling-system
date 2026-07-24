from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._workspace_harness import hermetic_phi_workspace, write_csv, write_pdf_table


def _workspace(tmp_path: Path, study: str = "ManifestStudy"):
    return hermetic_phi_workspace(tmp_path, study)


def _write_source(tmp_path: Path) -> tuple[Path, dict[str, dict[str, Any]]]:
    source = tmp_path / "src"
    write_csv(source / "datasets" / "labs.csv", ["SUBJID", "AGE"], [["1", "40"]])
    write_csv(source / "datasets" / "extra.csv", ["SUBJID", "AGE"], [["2", "41"]])
    write_csv(source / "data_dictionary" / "labs.csv", ["variable", "label"], [["SUBJID", "Subject"]])
    write_pdf_table(source / "forms" / "consent.pdf", ["FIELD", "VALUE"], [["consent", "signed"]])
    from phi_engine.pipeline.intake import intake_add

    intake = intake_add(source, "ManifestStudy")
    assert intake["status"] == "ready", intake["review_items"]
    by_rel = {entry["relative_path"]: entry for entry in intake["entries"].values()}
    return source, by_rel


def _dep(by_rel: dict[str, dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    value = {
        "dataset_artifact_id": by_rel["datasets/labs.csv"]["artifact_id"],
        "dataset_source_sha256": by_rel["datasets/labs.csv"]["sha256"],
        "support": "data_dictionary/labs.csv",
        "support_artifact_id": by_rel["data_dictionary/labs.csv"]["artifact_id"],
        "support_source_sha256": by_rel["data_dictionary/labs.csv"]["sha256"],
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
    value.update(overrides)
    return value


def _write_manifest(workspace: Path, payload: dict[str, Any]) -> Path:
    config_dir = workspace / "config" / "ManifestStudy"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "_forms_manifest.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_forms_manifest_preserves_additive_typed_schema(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        source, by_rel = _write_source(tmp_path)
        _write_manifest(
            workspace,
            {
                "required": ["labs.csv"],
                "optional": ["missing.csv"],
                "reject": ["extra.csv"],
                "date_locales": {"visit_dt": "DMY"},
                "dataset_dependencies_schema": "dataset-dependencies/v1",
                "dataset_dependencies_code_table_version": 1,
                "dataset_dependencies": {"datasets/labs.csv": [_dep(by_rel)]},
            },
        )
        from scripts.extraction.forms_manifest import check_forms_manifest

        result = check_forms_manifest(source / "datasets")

        assert result.required == ("labs.csv",)
        assert result.optional == ("missing.csv",)
        assert result.reject == ("extra.csv",)
        assert result.date_locales == {"VISIT_DT": "DMY"}
        assert result.rejected_files == frozenset({"extra.csv"})
        dep = result.dataset_dependencies["datasets/labs.csv"][0]
        relation = result.dependency_relations["datasets/labs.csv"][0]
        assert dep.dataset_artifact_id == by_rel["datasets/labs.csv"]["artifact_id"]
        assert dep.support_artifact_id == by_rel["data_dictionary/labs.csv"]["artifact_id"]
        assert dep.kind.value == "dictionary"
        assert relation.dependency == dep
        assert relation.dataset_state.value == "current"
        assert relation.support_state.value == "current"


def test_forms_manifest_rejects_unknown_keys_enums_paths_ids_hashes_timestamps_and_root_mismatches(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        source, by_rel = _write_source(tmp_path)
        base = {
            "required": ["labs.csv"],
            "optional": ["extra.csv"],
            "reject": [],
            "dataset_dependencies_schema": "dataset-dependencies/v1",
            "dataset_dependencies_code_table_version": 1,
            "dataset_dependencies": {"datasets/labs.csv": [_dep(by_rel)]},
        }
        _write_manifest(workspace, dict(base, surprise=True))
        from scripts.extraction.forms_manifest import ManifestMismatchError, check_forms_manifest

        with pytest.raises(ManifestMismatchError, match="unknown forms manifest keys"):
            check_forms_manifest(source / "datasets")

        bad_cases: list[tuple[str, dict[str, Any], str]] = [
            ("bad enum", {"kind": "spreadsheet"}, "invalid kind"),
            ("old sensitivity enum", {"sensitivity": "restricted"}, "invalid sensitivity"),
            ("old reason enum", {"reason_code": "irrelevant"}, "invalid reason_code"),
            ("bad support path", {"support": "../escape.csv"}, "unsafe support"),
            ("bad dataset id", {"dataset_artifact_id": "a_BAD"}, "invalid dataset artifact"),
            ("bad support hash", {"support_source_sha256": "f" * 63}, "invalid support artifact"),
            ("bad timestamp", {"confirmed_at": "2026-07-14T00:00:00+00:00"}, "confirmed_at"),
        ]
        for _name, override, pattern in bad_cases:
            payload = dict(base)
            payload["dataset_dependencies"] = {"datasets/labs.csv": [_dep(by_rel, **override)]}
            _write_manifest(workspace, payload)
            with pytest.raises(ManifestMismatchError, match=pattern):
                check_forms_manifest(source / "datasets")

        payload = dict(base)
        payload["dataset_dependencies"] = {"../labs.csv": []}
        _write_manifest(workspace, payload)
        with pytest.raises(ManifestMismatchError, match="unsafe dataset dependency key"):
            check_forms_manifest(source / "datasets")

        for field, value in (
            ("required", "labs.csv"),
            ("optional", [1]),
            ("reject", {"*.csv": True}),
            ("date_locales", {1: "DMY"}),
        ):
            payload = dict(base)
            payload[field] = value
            _write_manifest(workspace, payload)
            with pytest.raises(ManifestMismatchError, match=field):
                check_forms_manifest(source / "datasets")


def test_forms_manifest_absent_dependency_section_is_dataset_only_compatible(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        source, _by_rel = _write_source(tmp_path)
        _write_manifest(workspace, {"required": ["labs.csv"], "optional": ["extra.csv"], "reject": [], "date_locales": {"dob": "MDY"}})
        from scripts.extraction.forms_manifest import check_forms_manifest

        result = check_forms_manifest(source / "datasets")
        assert result.required == ("labs.csv",)
        assert result.optional == ("extra.csv",)
        assert result.reject == ()
        assert result.date_locales == {"DOB": "MDY"}
        assert result.dataset_dependencies == {}


def test_forms_manifest_exposes_missing_and_stale_dependency_currency_without_global_abort(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        source, by_rel = _write_source(tmp_path)
        from scripts.extraction.forms_manifest import DependencyRelationState, check_forms_manifest

        base = {
            "required": ["labs.csv"],
            "optional": ["extra.csv"],
            "reject": [],
            "dataset_dependencies_schema": "dataset-dependencies/v1",
            "dataset_dependencies_code_table_version": 1,
        }

        missing_dataset = dict(base)
        missing_dataset["dataset_dependencies"] = {"datasets/missing.csv": [_dep(by_rel)]}
        _write_manifest(workspace, missing_dataset)
        result = check_forms_manifest(source / "datasets")
        relation = result.dependency_relations["datasets/missing.csv"][0]
        assert relation.dataset_state is DependencyRelationState.MISSING
        assert relation.support_state is DependencyRelationState.CURRENT

        missing_support = dict(base)
        missing_support["dataset_dependencies"] = {
            "datasets/labs.csv": [_dep(by_rel, support="data_dictionary/missing.csv")]
        }
        _write_manifest(workspace, missing_support)
        result = check_forms_manifest(source / "datasets")
        relation = result.dependency_relations["datasets/labs.csv"][0]
        assert relation.dataset_state is DependencyRelationState.CURRENT
        assert relation.support_state is DependencyRelationState.MISSING

        stale_support = dict(base)
        stale_support["dataset_dependencies"] = {
            "datasets/labs.csv": [
                _dep(
                    by_rel,
                    support_artifact_id=by_rel["datasets/extra.csv"]["artifact_id"],
                )
            ]
        }
        _write_manifest(workspace, stale_support)
        result = check_forms_manifest(source / "datasets")
        assert (
            result.dependency_relations["datasets/labs.csv"][0].support_state
            is DependencyRelationState.STALE
        )

        null_required = dict(base)
        dep = _dep(by_rel, support="data_dictionary/not_yet.csv", support_artifact_id=None, support_source_sha256=None)
        null_required["dataset_dependencies"] = {"datasets/labs.csv": [dep]}
        _write_manifest(workspace, null_required)
        result = check_forms_manifest(source / "datasets")
        relation = result.dependency_relations["datasets/labs.csv"][0]
        assert relation.dependency.support_artifact_id is None
        assert relation.support_state is DependencyRelationState.MISSING

        ignored_missing = dict(base)
        dep = _dep(
            by_rel,
            support="data_dictionary/not_yet.csv",
            support_artifact_id=None,
            support_source_sha256=None,
            level="ignored",
        )
        ignored_missing["dataset_dependencies"] = {"datasets/labs.csv": [dep]}
        _write_manifest(workspace, ignored_missing)
        from scripts.extraction.forms_manifest import ManifestMismatchError

        with pytest.raises(ManifestMismatchError, match="ignored dependencies require concrete"):
            check_forms_manifest(source / "datasets")


def test_organizer_validates_manifest_structure_before_deleting_old_tree(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        source, by_rel = _write_source(tmp_path)
        payload = {
            "required": ["labs.csv"],
            "optional": ["extra.csv"],
            "reject": [],
            "dataset_dependencies_schema": "dataset-dependencies/v1",
            "dataset_dependencies_code_table_version": 1,
            "dataset_dependencies": {
                "../escape.csv": [_dep(by_rel)],
            },
        }
        _write_manifest(workspace, payload)
        old_tree = workspace / "organized" / "ManifestStudy"
        old_tree.mkdir(parents=True)
        marker = old_tree / "must-survive.txt"
        marker.write_text("old organized data", encoding="utf-8")

        from scripts.extraction.forms_manifest import ManifestMismatchError
        from phi_engine.pipeline.organize import organize

        with pytest.raises(ManifestMismatchError, match="unsafe dataset dependency key"):
            organize("ManifestStudy")

        assert marker.read_text(encoding="utf-8") == "old organized data"


def test_dependency_shared_enums_are_exact_authoritative_tokens() -> None:
    from phi_engine.pipeline.dependencies import DependencyKind, DependencyReasonCode, RoleSource, Sensitivity, SupportFailureCode

    assert {item.value for item in DependencyKind} == {"pdf", "dictionary", "mapping", "dictionary_mapping"}
    assert {item.value for item in Sensitivity} == {"confidential", "non_confidential"}
    assert {item.value for item in DependencyReasonCode} == {
        "manifest_declared",
        "same_stem_companion",
        "exact_header_match",
        "only_interpretation",
        "transform_parameters_missing",
    }
    assert {item.value for item in RoleSource} == {"manifest", "directory", "inferred"}
    assert {item.value for item in SupportFailureCode} == {
        "missing",
        "hash_mismatch",
        "source_changed_during_read",
        "unsupported_format",
        "source_size_limit",
        "expanded_size_limit",
        "decompression_ratio_limit",
        "sheet_limit",
        "table_limit",
        "row_limit",
        "column_limit",
        "cell_size_limit",
        "json_depth_limit",
        "parse_error",
        "normalized_schema_invalid",
        "model_unavailable",
        "model_invalid",
        "signal_conflict",
        "stale_decision",
        "residual_gate_failed",
        "reader_unavailable",
        "resource_limit",
    }
