from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import phi_engine.config.config as config
import phi_engine.pipeline.run as pipeline_run
from phi_engine.pipeline.dependencies import (
    DatasetDependency,
    DependencyDecision,
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    Sensitivity,
    load_dependency_recommendations,
    support_role_sha256,
)
from phi_engine.security.phi_review import (
    Action,
    FormReviewApproval,
    HeaderClassification,
)
from scripts.extraction.forms_manifest import (
    DependencyRelation,
    DependencyRelationState,
)


_DATASET_ONE = "a_" + "1" * 32
_DATASET_TWO = "a_" + "2" * 32
_SUPPORT_ONE = "a_" + "3" * 32
_RECOMMENDATION = "dr_" + "4" * 32
_DECISION = "dd_" + "5" * 32
_RULEBOOK_SHA = "a" * 64
_SCRUB_SHA = "b" * 64
_OLD_SHA = "c" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_0600_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _organized_two_dataset_fixture(
    tmp_path: Path,
    *,
    include_support: bool,
    support_source: bytes = b"current-support-bytes",
) -> tuple[Path, dict[str, object], dict[str, str]]:
    study = "Study"
    organized = tmp_path / "organized" / study
    datasets_dir = organized / "datasets"
    verified_dir = organized / ".verified_sources"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    verified_dir.mkdir(parents=True, exist_ok=True)

    hashes: dict[str, str] = {}
    public_datasets: list[dict[str, object]] = []
    for artifact_id, marker, output, source_path, raw_name in (
        (_DATASET_ONE, "1", "alpha.jsonl", "datasets/alpha.csv", "PRIVATE-ALPHA"),
        (_DATASET_TWO, "2", "beta.jsonl", "datasets/beta.csv", "PRIVATE-BETA"),
    ):
        source_bytes = f"dataset-{marker}-source".encode()
        source_sha = _sha(source_bytes)
        normalized = (json.dumps({"h_" + marker * 24: "row-value"}) + "\n").encode()
        hashes[artifact_id] = source_sha
        verified_dir.joinpath(artifact_id).write_bytes(source_bytes)
        datasets_dir.joinpath(output).write_bytes(normalized)
        _write_0600_json(
            organized / ".protected" / "headers" / f"{artifact_id}.json",
            {
                "artifact_id": artifact_id,
                "source_sha256": source_sha,
                "headers": [
                    {
                        "header_id": "h_" + marker * 24,
                        "column_index": 0,
                        "raw_name": raw_name,
                        "normalized_name": raw_name.lower(),
                    }
                ],
                "source_relative_path": source_path,
            },
        )
        public_datasets.append(
            {
                "artifact_id": artifact_id,
                "source_sha256": source_sha,
                "output": output,
                "normalized_rows_sha256": _sha(normalized),
                "row_count": 1,
                "headers": [
                    {
                        "header_id": "h_" + marker * 24,
                        "column_index": 0,
                        "normalized_name": raw_name.lower(),
                    }
                ],
            }
        )

    support_artifacts: list[dict[str, object]] = []
    if include_support:
        source_sha = _sha(support_source)
        normalized = (
            json.dumps(
                {
                    "support_artifact_id": _SUPPORT_ONE,
                    "source_sha256": source_sha,
                    "sheet_index": 0,
                    "table_index": 0,
                    "row_index": 0,
                    "cells": [{"column_index": 0, "value": "PRIVATE-ALPHA"}],
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        normalized_path = organized / "support" / "dictionary" / "one.jsonl"
        normalized_path.parent.mkdir(parents=True)
        normalized_path.write_bytes(normalized)
        verified_dir.joinpath(_SUPPORT_ONE).write_bytes(support_source)
        support_public = {
            "artifact_id": _SUPPORT_ONE,
            "source_sha256": source_sha,
            "kind": "dictionary",
            "format": "csv",
            "parse_status": "parsed",
            "normalized_rows_sha256": _sha(normalized),
            "failure_code": None,
        }
        _write_0600_json(
            organized / ".protected" / "support" / f"{_SUPPORT_ONE}.json",
            {
                **support_public,
                "normalized_rows_path": str(normalized_path),
                "source_relative_path": "data_dictionary/one.csv",
                "normalized_source_stem": "one",
            },
        )
        support_artifacts.append(support_public)
        hashes[_SUPPORT_ONE] = source_sha
        hashes["normalized_support"] = _sha(normalized)

    manifest: dict[str, object] = {
        "study": study,
        "datasets": public_datasets,
        "support_artifacts": support_artifacts,
        "pdf_roles": {},
        "review_bucket": [],
        "intake_manifest_sha": "f" * 64,
    }
    return organized, manifest, hashes


def _basis(*, support_id: str | None, role_sha: str | None = None) -> DependencyDecisionBasis:
    return DependencyDecisionBasis(
        rulebook_sha256=_RULEBOOK_SHA,
        scrub_config_sha256=_SCRUB_SHA,
        support_role_sha256=role_sha
        or support_role_sha256(
            recommendation_id=_RECOMMENDATION,
            dataset_artifact_id=_DATASET_ONE,
            support_artifact_id=support_id,
            kind=DependencyKind.DICTIONARY,
            role_source=pipeline_run.RoleSource.MANIFEST,
            organizer_role_version=1,
        ),
    )


def _recommendation(
    hashes: dict[str, str],
    *,
    level: DependencyLevel,
) -> DependencyRecommendation:
    return DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=_RECOMMENDATION,
        dataset_artifact_id=_DATASET_ONE,
        dataset_sha256=hashes[_DATASET_ONE],
        support_artifact_id=_SUPPORT_ONE,
        support_sha256=hashes[_SUPPORT_ONE],
        normalized_support_sha256=hashes["normalized_support"],
        kind=DependencyKind.DICTIONARY,
        suggested_level=level,
        default_sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        header_ids=(),
        matched_rule_ids=(),
        transform_requirement_ids=(),
        basis=_basis(support_id=_SUPPORT_ONE),
    )


def _decision(
    recommendation: DependencyRecommendation,
    *,
    level: DependencyLevel,
    stale_basis: bool = False,
    stale_support_bytes: bool = False,
) -> DependencyDecision:
    basis = recommendation.basis
    if stale_basis:
        basis = DependencyDecisionBasis(
            rulebook_sha256="d" * 64,
            scrub_config_sha256=basis.scrub_config_sha256,
            support_role_sha256=basis.support_role_sha256,
        )
    return DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=_DECISION,
        recommendation_id=recommendation.recommendation_id,
        dataset_artifact_id=recommendation.dataset_artifact_id,
        dataset_sha256=recommendation.dataset_sha256,
        support_artifact_id=recommendation.support_artifact_id,
        support_sha256=_OLD_SHA if stale_support_bytes else recommendation.support_sha256,
        normalized_support_sha256=recommendation.normalized_support_sha256,
        kind=recommendation.kind,
        level=level,
        sensitivity=recommendation.default_sensitivity,
        reason_code=recommendation.reason_code,
        basis=basis,
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )


def _relation(
    hashes: dict[str, str],
    *,
    level: DependencyLevel,
    support_state: DependencyRelationState,
    declared_support_missing: bool = False,
) -> DependencyRelation:
    declared_support_id = None if declared_support_missing else _SUPPORT_ONE
    dependency = DatasetDependency(
        dataset_path="datasets/alpha.csv",
        dataset_artifact_id=_DATASET_ONE,
        dataset_source_sha256=hashes[_DATASET_ONE],
        support="data_dictionary/one.csv",
        support_artifact_id=declared_support_id,
        support_source_sha256=(
            None
            if declared_support_missing
            else hashes.get(_SUPPORT_ONE, _OLD_SHA)
            if support_state is DependencyRelationState.CURRENT
            else _OLD_SHA
        ),
        kind=DependencyKind.DICTIONARY,
        level=level,
        sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        recommendation_id=_RECOMMENDATION,
        basis=DependencyDecisionBasis(
            rulebook_sha256="d" * 64,
            scrub_config_sha256="e" * 64,
            support_role_sha256="f" * 64,
        ),
        confirmed_by="reviewer",
        confirmed_at="2026-07-14T10:00:00Z",
    )
    return DependencyRelation(
        dependency=dependency,
        dataset_state=DependencyRelationState.CURRENT,
        support_state=support_state,
    )


class _EffectiveConfig:
    def field_is_keep(self, _name: str) -> bool:
        return True

    def field_is_date(self, _name: str) -> bool:
        return False

    def field_is_birthdate(self, _name: str) -> bool:
        return False

    def field_is_id(self, _name: str) -> bool:
        return False

    def field_is_drop(self, _name: str) -> bool:
        return False

    def cap_rule_for(self, _name: str) -> None:
        return None

    def generalize_rule_for(self, _name: str) -> None:
        return None

    def band_rule_for(self, _name: str) -> None:
        return None

    def field_is_suppress_small_cell(self, _name: str) -> bool:
        return False


class _RuleBundle:
    rules_sha256 = _RULEBOOK_SHA
    source_mode = "pinned"

    def to_json(self) -> dict[str, object]:
        return {"rules_sha256": self.rules_sha256, "rules": []}


def _run_two_dataset_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    recommendations: tuple[DependencyRecommendation, ...] | None,
    decisions: tuple[DependencyDecision, ...],
    relations: tuple[DependencyRelation, ...],
    organize_manifest: dict[str, object],
    scrub_exception: Exception | None = None,
) -> tuple[pipeline_run.PipelineResult, Path, tuple[str, ...]]:
    study = "Study"
    output = tmp_path / "output" / study
    staging = tmp_path / "staging" / study
    lock_dir = tmp_path / "tmp"
    lock_dir.mkdir(mode=0o700, exist_ok=True)

    for name, value in {
        "TMP_DIR": lock_dir,
        "RAW_DATA_DIR": tmp_path / "raw",
        "ORGANIZED_DIR": tmp_path / "organized",
        "STUDY_OUTPUT_DIR": output,
        "STUDY_AUDIT_DIR": output / "audit",
        "LLM_SOURCE_SOT_DIR": tmp_path / "sot",
        "ANNOTATED_PDFS_DIR": tmp_path / "annotated",
        "STAGING_DATASETS_DIR": staging / "datasets",
        "STUDY_STAGING_DIR": staging,
        "STUDY_LLM_SOURCE_DIR": output / "llm_source",
    }.items():
        monkeypatch.setattr(config, name, value)
    source_root = tmp_path / "source"
    source_root.mkdir(exist_ok=True)

    monkeypatch.setattr(pipeline_run, "bootstrap_study_privacy", lambda *_: None)
    monkeypatch.setattr(
        pipeline_run,
        "load_study_privacy_config",
        lambda *_: SimpleNamespace(rule_refresh="pinned_only"),
    )
    monkeypatch.setattr(
        pipeline_run,
        "resolve_rulebook",
        lambda *_a, **_k: SimpleNamespace(
            bundle=_RuleBundle(), protection_weakened=False, cache_status="cache_hit"
        ),
    )
    monkeypatch.setattr(
        pipeline_run,
        "load_intake_manifest",
        lambda *_: {"source_root": str(source_root), "status": "ready"},
    )
    # dependency_relations is reused from _organize_locked's own result
    # (see run.py's removal of _load_manifest_dependency_relations and its
    # Path.resolve()/check_forms_manifest second source-root read) --
    # never a separate check_forms_manifest mock here.
    monkeypatch.setattr(
        pipeline_run,
        "_organize_locked",
        lambda *_: {**organize_manifest, "dependency_relations": {"datasets/alpha.csv": relations}},
    )
    monkeypatch.setattr(pipeline_run, "load_review_decisions", lambda *_: {})
    monkeypatch.setattr(pipeline_run, "load_study_dependency_decisions", lambda *_: decisions)
    monkeypatch.setattr(pipeline_run, "load_sot_variable_signals", lambda *_: {})
    monkeypatch.setattr(pipeline_run, "synthesize_study_config", lambda *_: None)
    if recommendations is not None:
        monkeypatch.setattr(
            pipeline_run,
            "recommend_dependencies",
            lambda **_: recommendations,
        )
    monkeypatch.setattr(pipeline_run.phi_scrub, "load_scrub_config", lambda **_: _EffectiveConfig())
    monkeypatch.setattr(
        pipeline_run.phi_scrub,
        "effective_scrub_config_hash",
        lambda *_, **__: _SCRUB_SHA,
    )

    def approve(*, form_name: str, headers: list[str], **_kwargs: object) -> FormReviewApproval:
        classifications = tuple(
            HeaderClassification(
                header=header,
                action=Action.KEEP,
                matched_rules=(),
                jurisdictions=("USA",),
                reasons=("test",),
            )
            for header in headers
        )
        return FormReviewApproval(
            form_name=form_name,
            status="approved",
            attempts=1,
            actions={header: Action.KEEP.value for header in headers},
            classifications=classifications,
            reasons=(),
            rule_bundle_sha256=_RULEBOOK_SHA,
            source_mode="pinned",
        )

    monkeypatch.setattr(pipeline_run, "review_form_headers", approve)
    staged_at_scrub: list[tuple[str, ...]] = []

    def run_scrub(*_args: object, **_kwargs: object) -> None:
        staged_at_scrub.append(
            tuple(
                path.name
                for path in sorted(Path(config.STAGING_DATASETS_DIR).glob("*.jsonl"))
            )
        )
        if scrub_exception is not None:
            raise scrub_exception

    monkeypatch.setattr(pipeline_run.phi_scrub, "run_scrub", run_scrub)
    monkeypatch.setattr(
        pipeline_run,
        "run_phi_guard_gate",
        lambda *_: SimpleNamespace(ok=True),
    )

    result = pipeline_run.run_pipeline(study, "us")
    run_dirs = sorted((output / "runs").iterdir())
    assert run_dirs
    staged = staged_at_scrub[0] if staged_at_scrub else ()
    return result, run_dirs[-1], staged


@pytest.mark.parametrize(
    "level",
    [DependencyLevel.REQUIRED, DependencyLevel.HELPFUL],
)
def test_manifest_missing_support_reconciles_to_exact_hydrated_path(
    tmp_path: Path,
    level: DependencyLevel,
) -> None:
    organized, manifest, hashes = _organized_two_dataset_fixture(
        tmp_path,
        include_support=True,
    )
    hydrated = pipeline_run._hydrate_dependency_inputs(organized, manifest)
    relation = _relation(
        hashes,
        level=level,
        support_state=DependencyRelationState.STALE,
        declared_support_missing=True,
    )

    recommendations = pipeline_run._build_unavailable_manifest_recommendations(
        hydrated,
        {"datasets/alpha.csv": (relation,)},
        rulebook_sha256=_RULEBOOK_SHA,
        scrub_config_sha256=_SCRUB_SHA,
    )

    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation.support_artifact_id == _SUPPORT_ONE
    assert recommendation.support_sha256 == hashes[_SUPPORT_ONE]
    assert (
        recommendation.normalized_support_sha256
        == hashes["normalized_support"]
    )
    assert recommendation.suggested_level is level
    assert recommendation.default_sensitivity is Sensitivity.CONFIDENTIAL


@pytest.mark.parametrize(
    "level,expected_held,expected_staged",
    [
        (DependencyLevel.REQUIRED, [_DATASET_ONE], ("beta.jsonl",)),
        (
            DependencyLevel.HELPFUL,
            [],
            ("alpha.jsonl", "beta.jsonl"),
        ),
    ],
)
def test_real_run_and_decision_flow_refreshes_declared_missing_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: DependencyLevel,
    expected_held: list[str],
    expected_staged: tuple[str, ...],
) -> None:
    import phi_engine.pipeline.dependencies as dependency_contracts
    import phi_engine.pipeline.review as dependency_review

    _organized, missing_manifest, hashes = _organized_two_dataset_fixture(
        tmp_path,
        include_support=False,
    )
    missing_relation = _relation(
        hashes,
        level=level,
        support_state=DependencyRelationState.MISSING,
        declared_support_missing=True,
    )
    first_result, first_run_dir, first_staged = _run_two_dataset_scenario(
        tmp_path,
        monkeypatch,
        recommendations=None,
        decisions=(),
        relations=(missing_relation,),
        organize_manifest=missing_manifest,
    )
    missing_recommendations = load_dependency_recommendations(
        first_run_dir / "dependency_recommendations.jsonl"
    )
    assert len(missing_recommendations) == 1
    missing_recommendation = missing_recommendations[0]
    assert missing_recommendation.support_artifact_id is None
    assert first_result.dependency_held_dataset_ids == expected_held
    assert first_staged == expected_staged

    config_dir = tmp_path / "config" / "Study"
    config_dir.mkdir(parents=True)
    manifest_path = config_dir / "_forms_manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "required": ["dataset.csv"],
                "optional": [],
                "reject": [],
                "date_locales": {},
                "dataset_dependencies_schema": "dataset-dependencies/v1",
                "dataset_dependencies_code_table_version": 1,
                "dataset_dependencies": {
                    missing_relation.dependency.dataset_path: [
                        {
                            "dataset_artifact_id": _DATASET_ONE,
                            "dataset_source_sha256": hashes[_DATASET_ONE],
                            "support": missing_relation.dependency.support,
                            "support_artifact_id": None,
                            "support_source_sha256": None,
                            "kind": DependencyKind.DICTIONARY.value,
                            "level": level.value,
                            "sensitivity": Sensitivity.CONFIDENTIAL.value,
                            "reason_code": (
                                DependencyReasonCode.MANIFEST_DECLARED.value
                            ),
                            "recommendation_id": (
                                missing_recommendation.recommendation_id
                            ),
                            "basis": missing_recommendation.basis.to_json(),
                            "confirmed_by": "prior-reviewer",
                            "confirmed_at": "2026-07-14T10:00:00Z",
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    monkeypatch.setattr(
        config,
        "study_config_dir",
        lambda selected=None: config_dir,
    )
    monkeypatch.setattr(
        dependency_review,
        "_current_rulebook_sha256",
        lambda _study: _RULEBOOK_SHA,
    )
    monkeypatch.setattr(
        dependency_contracts,
        "_effective_scrub_config_sha256",
        lambda: _SCRUB_SHA,
    )

    missing_decision = dependency_review.decide_dependency(
        "Study",
        dataset=_DATASET_ONE,
        recommendation=missing_recommendation.recommendation_id,
        support=None,
        level=level,
        sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        detail_file=None,
        decided_by="reviewer",
    )

    _organized, refreshed_manifest, refreshed_hashes = (
        _organized_two_dataset_fixture(
            tmp_path,
            include_support=True,
        )
    )
    refreshed_relation = _relation(
        refreshed_hashes,
        level=level,
        support_state=DependencyRelationState.STALE,
        declared_support_missing=True,
    )
    refreshed_result, refreshed_run_dir, refreshed_staged = (
        _run_two_dataset_scenario(
            tmp_path,
            monkeypatch,
            recommendations=None,
            decisions=(missing_decision,),
            relations=(refreshed_relation,),
            organize_manifest=refreshed_manifest,
        )
    )
    refreshed_recommendations = load_dependency_recommendations(
        refreshed_run_dir / "dependency_recommendations.jsonl"
    )
    assert len(refreshed_recommendations) == 1
    refreshed = refreshed_recommendations[0]
    assert refreshed.support_artifact_id == _SUPPORT_ONE
    assert refreshed.support_sha256 == refreshed_hashes[_SUPPORT_ONE]
    assert (
        refreshed.normalized_support_sha256
        == refreshed_hashes["normalized_support"]
    )
    assert refreshed_result.dependency_held_dataset_ids == expected_held
    assert refreshed_staged == expected_staged

    with pytest.raises(ValueError, match="support identity mismatch"):
        dependency_review.decide_dependency(
            "Study",
            dataset=_DATASET_ONE,
            recommendation=refreshed.recommendation_id,
            support=None,
            level=level,
            sensitivity=Sensitivity.CONFIDENTIAL,
            reason_code=DependencyReasonCode.MANIFEST_DECLARED,
            detail_file=None,
            decided_by="reviewer",
        )

    present_decision = dependency_review.decide_dependency(
        "Study",
        dataset=_DATASET_ONE,
        recommendation=refreshed.recommendation_id,
        support=_SUPPORT_ONE,
        level=level,
        sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        detail_file=None,
        decided_by="reviewer",
    )
    assert present_decision.support_artifact_id == _SUPPORT_ONE
    stored_manifest = yaml.safe_load(
        manifest_path.read_text(encoding="utf-8")
    )
    stored_dependencies = stored_manifest["dataset_dependencies"][
        missing_relation.dependency.dataset_path
    ]
    assert len(stored_dependencies) == 1
    assert stored_dependencies[0]["recommendation_id"] == refreshed.recommendation_id
    assert stored_dependencies[0]["support_artifact_id"] == _SUPPORT_ONE
    assert (
        dependency_review.load_study_dependency_decisions("Study")
        == (missing_decision, present_decision)
    )

    current_relation = _relation(
        refreshed_hashes,
        level=level,
        support_state=DependencyRelationState.CURRENT,
    )
    converged_result, converged_run_dir, converged_staged = (
        _run_two_dataset_scenario(
            tmp_path,
            monkeypatch,
            recommendations=None,
            decisions=(missing_decision, present_decision),
            relations=(current_relation,),
            organize_manifest=refreshed_manifest,
        )
    )
    converged_recommendations = load_dependency_recommendations(
        converged_run_dir / "dependency_recommendations.jsonl"
    )
    assert len(converged_recommendations) == 1
    assert (
        converged_recommendations[0].recommendation_id
        == refreshed.recommendation_id
    )
    assert converged_result.exit_code == 0
    assert converged_result.dependency_review_count == 0
    assert converged_result.dependency_held_dataset_ids == []
    assert converged_staged == ("alpha.jsonl", "beta.jsonl")


@pytest.mark.parametrize(
    "condition,level,expected_hold",
    [
        ("pending", DependencyLevel.REQUIRED, True),
        ("pending", DependencyLevel.HELPFUL, False),
        ("stale", DependencyLevel.REQUIRED, True),
        ("stale", DependencyLevel.HELPFUL, False),
        ("stale", DependencyLevel.IGNORED, False),
        ("removed", DependencyLevel.REQUIRED, True),
        ("removed", DependencyLevel.HELPFUL, False),
        ("removed", DependencyLevel.IGNORED, False),
        ("byte_swap", DependencyLevel.REQUIRED, True),
        ("byte_swap", DependencyLevel.HELPFUL, False),
        ("byte_swap", DependencyLevel.IGNORED, False),
    ],
)
def test_real_run_pipeline_scopes_dependency_pending_stale_removed_and_byte_swaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    level: DependencyLevel,
    expected_hold: bool,
) -> None:
    include_support = condition != "removed"
    _organized, manifest, hashes = _organized_two_dataset_fixture(
        tmp_path,
        include_support=include_support,
        support_source=b"byte-swapped-support" if condition == "byte_swap" else b"current-support-bytes",
    )
    recommendation_level = (
        DependencyLevel.REQUIRED
        if level is DependencyLevel.REQUIRED
        else DependencyLevel.HELPFUL
    )
    recs: tuple[DependencyRecommendation, ...] = ()
    decisions: tuple[DependencyDecision, ...] = ()
    relations: tuple[DependencyRelation, ...] = ()
    if include_support:
        recommendation = _recommendation(hashes, level=recommendation_level)
        recs = (recommendation,)
        if condition == "stale":
            decisions = (
                _decision(recommendation, level=level, stale_basis=True),
            )
        elif condition == "byte_swap":
            decisions = (
                _decision(recommendation, level=level, stale_support_bytes=True),
            )
        relations = (
            _relation(
                hashes,
                level=level,
                support_state=(
                    DependencyRelationState.STALE
                    if condition == "byte_swap"
                    else DependencyRelationState.CURRENT
                ),
            ),
        )
    else:
        relations = (
            _relation(
                hashes,
                level=level,
                support_state=DependencyRelationState.MISSING,
            ),
        )
        missing_placeholder = DependencyRecommendation(
            schema_version="dependency-recommendation/v1",
            recommendation_id=_RECOMMENDATION,
            dataset_artifact_id=_DATASET_ONE,
            dataset_sha256=hashes[_DATASET_ONE],
            support_artifact_id=None,
            support_sha256=None,
            normalized_support_sha256=None,
            kind=DependencyKind.DICTIONARY,
            suggested_level=recommendation_level,
            default_sensitivity=Sensitivity.CONFIDENTIAL,
            reason_code=DependencyReasonCode.MANIFEST_DECLARED,
            header_ids=(),
            matched_rule_ids=(),
            transform_requirement_ids=(),
            basis=_basis(support_id=None),
        )
        decisions = (
            DependencyDecision(
                **{
                    **_decision(
                        DependencyRecommendation(
                            **{
                                **missing_placeholder.__dict__,
                                "support_artifact_id": _SUPPORT_ONE,
                                "support_sha256": _OLD_SHA,
                                "normalized_support_sha256": "9" * 64,
                                "basis": _basis(support_id=_SUPPORT_ONE),
                            }
                        ),
                        level=level,
                    ).__dict__,
                }
            ),
        )

    result, run_dir, staged = _run_two_dataset_scenario(
        tmp_path,
        monkeypatch,
        recommendations=recs,
        decisions=decisions,
        relations=relations,
        organize_manifest=manifest,
    )

    assert result.exit_code == 8
    assert result.dependency_review_count == 1
    assert result.dependency_held_dataset_ids == ([_DATASET_ONE] if expected_hold else [])
    assert staged == (("beta.jsonl",) if expected_hold else ("alpha.jsonl", "beta.jsonl"))
    assert set(path.name for path in (Path(config.STUDY_LLM_SOURCE_DIR) / "datasets").glob("*.jsonl")) == set(staged)

    pending_path = run_dir / "pending_dependency_recommendations.jsonl"
    assert stat.S_IMODE(pending_path.stat().st_mode) == 0o600
    pending = load_dependency_recommendations(pending_path)
    assert tuple(item.recommendation_id for item in pending) == (_RECOMMENDATION,)
    pending_text = pending_path.read_text(encoding="utf-8")
    assert "PRIVATE-ALPHA" not in pending_text
    assert "datasets/alpha.csv" not in pending_text
    assert "data_dictionary/one.csv" not in pending_text
    if condition == "removed":
        private_text = (
            run_dir / ".protected" / "dependency_recommendations.jsonl"
        ).read_text(encoding="utf-8")
        assert "data_dictionary/one.csv" in private_text


def test_real_run_pipeline_suppresses_only_an_exact_current_ignored_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _organized, manifest, hashes = _organized_two_dataset_fixture(
        tmp_path,
        include_support=True,
    )
    recommendation = _recommendation(hashes, level=DependencyLevel.HELPFUL)
    ignored = _decision(recommendation, level=DependencyLevel.IGNORED)
    relation = _relation(
        hashes,
        level=DependencyLevel.IGNORED,
        support_state=DependencyRelationState.CURRENT,
    )

    result, run_dir, staged = _run_two_dataset_scenario(
        tmp_path,
        monkeypatch,
        recommendations=(recommendation,),
        decisions=(ignored,),
        relations=(relation,),
        organize_manifest=manifest,
    )

    assert result.exit_code == 0
    assert result.dependency_review_count == 0
    assert result.dependency_held_dataset_ids == []
    assert staged == ("alpha.jsonl", "beta.jsonl")
    assert load_dependency_recommendations(
        run_dir / "pending_dependency_recommendations.jsonl"
    ) == ()


def test_pipeline_result_uses_controlled_scrub_error_codes_without_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _organized, manifest, _hashes = _organized_two_dataset_fixture(
        tmp_path,
        include_support=False,
    )
    result, run_dir, _staged = _run_two_dataset_scenario(
        tmp_path,
        monkeypatch,
        recommendations=(),
        decisions=(),
        relations=(),
        organize_manifest=manifest,
        scrub_exception=RuntimeError("PROTECTED-SCRUB-DETAIL"),
    )

    assert result.exit_code == 1
    assert result.scrub_raised == "scrub_exception"
    assert "sot_generation_error" not in result.to_json()
    serialized = (run_dir / "pipeline_result.json").read_text(encoding="utf-8")
    assert "PROTECTED-SCRUB-DETAIL" not in serialized
    assert "sot_generation_error" not in json.loads(serialized)
