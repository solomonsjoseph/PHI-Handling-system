from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from phi_engine.pipeline.dependencies import (
    DependencyDecision,
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    OrganizedDataset,
    OrganizedHeader,
    ParsedSupportArtifact,
    PrivateDependencyRecommendation,
    RoleSource,
    Sensitivity,
    StructuredTransformKind,
    SupportFailureCode,
    SupportParseStatus,
    TransformRequirement,
    TransformRequirementOrigin,
    load_dependency_decisions,
    load_dependency_recommendations,
    recommend_dependencies,
    write_dependency_recommendations,
)
from phi_engine.security.phi_review import Action

A = "a_" + "1" * 32
B = "a_" + "2" * 32
C = "a_" + "7" * 32
D = "a_" + "8" * 32
DR = "dr_" + "3" * 32
DD = "dd_" + "4" * 32
H = "h_" + "5" * 24
TR = "tr_" + "6" * 32
SHA1 = "1" * 64
SHA2 = "2" * 64
SHA3 = "3" * 64
SHA4 = "4" * 64
SHA5 = "5" * 64


def basis() -> DependencyDecisionBasis:
    return DependencyDecisionBasis(rulebook_sha256=SHA1, scrub_config_sha256=SHA2, support_role_sha256=SHA3)


def recommendation() -> DependencyRecommendation:
    return DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=B,
        support_sha256=SHA2,
        normalized_support_sha256=SHA3,
        kind=DependencyKind.DICTIONARY,
        suggested_level=DependencyLevel.REQUIRED,
        default_sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.ONLY_INTERPRETATION,
        header_ids=(H,),
        matched_rule_ids=("rule-1",),
        transform_requirement_ids=(TR,),
        basis=basis(),
    )


def decision() -> DependencyDecision:
    return DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=DD,
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=B,
        support_sha256=SHA2,
        normalized_support_sha256=SHA3,
        kind=DependencyKind.DICTIONARY,
        level=DependencyLevel.REQUIRED,
        sensitivity=Sensitivity.NON_CONFIDENTIAL,
        reason_code=DependencyReasonCode.ONLY_INTERPRETATION,
        basis=basis(),
        decided_by="reviewer-1",
        decided_at="2026-07-14T10:00:00Z",
    )


def test_authoritative_enum_tokens_are_exact() -> None:
    assert {x.value for x in DependencyKind} == {"pdf", "dictionary", "mapping"}
    assert {x.value for x in DependencyLevel} == {"required", "helpful", "ignored"}
    assert {x.value for x in Sensitivity} == {"confidential", "non_confidential"}
    assert {x.value for x in RoleSource} == {"manifest", "directory", "inferred"}
    assert {x.value for x in DependencyReasonCode} == {
        "manifest_declared",
        "same_stem_companion",
        "exact_header_match",
        "only_interpretation",
        "transform_parameters_missing",
    }
    assert {x.value for x in SupportFailureCode} == {
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
    }


def test_recommendation_json_shape_is_exact_and_round_trips() -> None:
    payload = recommendation().to_json()
    assert list(payload) == [
        "schema_version",
        "recommendation_id",
        "dataset_artifact_id",
        "dataset_sha256",
        "support_artifact_id",
        "support_sha256",
        "normalized_support_sha256",
        "kind",
        "suggested_level",
        "default_sensitivity",
        "reason_code",
        "header_ids",
        "matched_rule_ids",
        "transform_requirement_ids",
        "basis",
        "code_table_version",
    ]
    assert payload["code_table_version"] == 1
    assert payload["kind"] == "dictionary"
    assert payload["suggested_level"] == "required"
    assert payload["default_sensitivity"] == "confidential"
    assert payload["header_ids"] == [H]
    assert payload["basis"] == {
        "rulebook_sha256": SHA1,
        "scrub_config_sha256": SHA2,
        "support_role_sha256": SHA3,
    }
    assert DependencyRecommendation.from_json(payload) == recommendation()


def test_decision_json_shape_is_exact_and_round_trips() -> None:
    payload = decision().to_json()
    assert list(payload) == [
        "schema_version",
        "decision_id",
        "recommendation_id",
        "dataset_artifact_id",
        "dataset_sha256",
        "support_artifact_id",
        "support_sha256",
        "normalized_support_sha256",
        "kind",
        "level",
        "sensitivity",
        "reason_code",
        "basis",
        "decided_by",
        "decided_at",
        "code_table_version",
    ]
    assert payload["code_table_version"] == 1
    assert payload["level"] == "required"
    assert payload["sensitivity"] == "non_confidential"
    assert DependencyDecision.from_json(payload) == decision()


def test_constructor_validation_rejects_bad_ids_hashes_timestamps_and_support_nulls() -> None:
    with pytest.raises(ValueError, match="recommendation_id"):
        DependencyRecommendation(**{**recommendation().__dict__, "recommendation_id": "bad"})
    with pytest.raises(ValueError, match="dataset_sha256"):
        DependencyRecommendation(**{**recommendation().__dict__, "dataset_sha256": "f" * 63})
    with pytest.raises(ValueError, match="support fields"):
        DependencyDecision(**{**decision().__dict__, "support_artifact_id": None})
    with pytest.raises(ValueError, match="decided_at"):
        DependencyDecision(**{**decision().__dict__, "decided_at": "2026-07-14T10:00:00+00:00"})
    with pytest.raises(ValueError, match="decided_by"):
        DependencyDecision(**{**decision().__dict__, "decided_by": "reviewer private detail"})
    with pytest.raises(ValueError, match="non_confidential"):
        DependencyDecision(
            **{
                **decision().__dict__,
                "support_artifact_id": None,
                "support_sha256": None,
                "normalized_support_sha256": None,
            }
        )
    with pytest.raises(ValueError, match="non_confidential"):
        DependencyDecision(
            **{
                **decision().__dict__,
                "normalized_support_sha256": None,
            }
        )
    with pytest.raises(ValueError, match="header_ids"):
        DependencyRecommendation(**{**recommendation().__dict__, "header_ids": ("Subject ID",)})


def test_from_json_rejects_missing_unknown_duplicate_code_version_and_unknown_codes() -> None:
    payload = recommendation().to_json()
    bad_missing = dict(payload)
    bad_missing.pop("support_sha256")
    with pytest.raises(ValueError, match="keys mismatch"):
        DependencyRecommendation.from_json(bad_missing)
    with pytest.raises(ValueError, match="keys mismatch"):
        DependencyRecommendation.from_json({**payload, "metadata": {}})
    with pytest.raises(ValueError, match="unsupported code_table_version"):
        DependencyRecommendation.from_json({**payload, "code_table_version": 2})
    with pytest.raises(ValueError):
        DependencyRecommendation.from_json({**payload, "kind": "spreadsheet"})

    duplicate_json = '{"schema_version":"dependency-recommendation/v1",' + json.dumps(payload)[1:]
    with pytest.raises(ValueError, match="duplicate key"):
        DependencyRecommendation.from_json(duplicate_json)


def test_missing_support_recommendation_shape_keeps_explicit_nulls() -> None:
    rec = DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=None,
        support_sha256=None,
        normalized_support_sha256=None,
        kind=DependencyKind.DICTIONARY,
        suggested_level=DependencyLevel.REQUIRED,
        default_sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING,
        header_ids=(H,),
        matched_rule_ids=(),
        transform_requirement_ids=(TR,),
        basis=basis(),
    )
    payload = rec.to_json()
    assert payload["support_artifact_id"] is None
    assert payload["support_sha256"] is None
    assert payload["normalized_support_sha256"] is None
    assert DependencyRecommendation.from_json(payload) == rec


def test_support_parse_status_requires_exact_parsed_or_failed_identity(tmp_path: Path) -> None:
    failed = ParsedSupportArtifact(
        artifact_id=B,
        source_sha256=SHA2,
        kind=DependencyKind.DICTIONARY,
        format="csv",
        parse_status=SupportParseStatus.FAILED,
        normalized_rows_path=None,
        normalized_rows_sha256=None,
        failure_code=SupportFailureCode.PARSE_ERROR,
    )
    assert failed.normalized_rows_path is None
    assert failed.normalized_rows_sha256 is None

    with pytest.raises(ValueError, match="failed support"):
        ParsedSupportArtifact(
            **{
                **failed.__dict__,
                "normalized_rows_path": tmp_path / "failed.jsonl",
                "normalized_rows_sha256": SHA3,
            }
        )
    with pytest.raises(ValueError, match="failure_code"):
        ParsedSupportArtifact(**{**failed.__dict__, "failure_code": None})
    with pytest.raises(ValueError, match="parsed support"):
        ParsedSupportArtifact(
            **{
                **failed.__dict__,
                "parse_status": SupportParseStatus.PARSED,
                "failure_code": None,
            }
        )


def _write_support_rows(path: Path, *values: str) -> None:
    rows = [
        {
            "support_artifact_id": B,
            "source_sha256": SHA2,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": index,
            "cells": [{"column_index": 0, "value": value}],
        }
        for index, value in enumerate(values)
    ]
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _dataset(path: Path, header_name: str = "Subject ID") -> OrganizedDataset:
    return OrganizedDataset(
        artifact_id=A,
        source_sha256=SHA1,
        normalized_rows_path=path / "labs.jsonl",
        normalized_rows_sha256=SHA4,
        headers=(OrganizedHeader(header_id=H, column_index=0, raw_name=header_name, normalized_name=header_name),),
    )


def _support(path: Path, *, artifact_id: str = B, kind: DependencyKind = DependencyKind.DICTIONARY) -> ParsedSupportArtifact:
    support_path = path / f"{artifact_id}.jsonl"
    _write_support_rows(support_path, "Subject ID")
    return ParsedSupportArtifact(
        artifact_id=artifact_id,
        source_sha256=SHA2,
        kind=kind,
        format="csv",
        parse_status=SupportParseStatus.PARSED,
        normalized_rows_path=support_path,
        normalized_rows_sha256=SHA3,
        failure_code=None,
    )


def _rule_bundle() -> SimpleNamespace:
    return SimpleNamespace(rules_sha256=SHA5)


def test_structured_transform_requirement_contract_is_phase5_exact() -> None:
    req = TransformRequirement(
        requirement_id=TR,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        header_id=H,
        classification_action=Action.GENERALIZE,
        kind=StructuredTransformKind.BAND,
        origin=TransformRequirementOrigin.RULE_CLASSIFICATION,
        origin_rule_id="rule-1",
        required_support_kind=DependencyKind.DICTIONARY,
    )
    assert req.classification_action is Action.GENERALIZE
    assert req.kind.value == "band"
    assert req.origin.value == "rule_classification"
    with pytest.raises(ValueError, match="dataset_sha256"):
        TransformRequirement(**{**req.__dict__, "dataset_sha256": "0" * 63})
    with pytest.raises(ValueError, match="classification_action"):
        TransformRequirement(**{**req.__dict__, "classification_action": "generalize"})


def test_recommender_exact_header_occurrence_is_helpful_and_never_reads_dataset_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    dataset.normalized_rows_path.write_text("SHOULD NOT BE READ", encoding="utf-8")
    support = _support(tmp_path)

    original_read_text = Path.read_text

    def guarded_read_text(self: Path, *args, **kwargs):
        if self == dataset.normalized_rows_path:
            raise AssertionError("dataset rows must not be read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    recs = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={A: frozenset({H})},
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )
    assert len(recs) == 1
    assert recs[0].suggested_level is DependencyLevel.HELPFUL
    assert recs[0].reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
    assert recs[0].header_ids == (H,)
    assert recs[0].basis.rulebook_sha256 == SHA5
    assert recs[0].basis.scrub_config_sha256 == SHA4


def test_recommender_helpful_for_safe_header_and_basis_changes_with_scrub_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import phi_engine.pipeline.dependencies as depmod

    dataset = _dataset(tmp_path)
    support = _support(tmp_path)
    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA1)
    first = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )
    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA2)
    second = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )
    assert first[0].suggested_level is DependencyLevel.HELPFUL
    assert first[0].reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
    assert first[0].basis.scrub_config_sha256 == SHA1
    assert second[0].basis.scrub_config_sha256 == SHA2
    assert first[0].basis.support_role_sha256 == second[0].basis.support_role_sha256


def test_recommender_same_stem_pdf_and_confirmed_links_and_ignored_suppression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    pdf = _support(tmp_path, kind=DependencyKind.PDF)
    object.__setattr__(pdf, "normalized_rows_path", tmp_path / "labs__pdf.jsonl")
    _write_support_rows(pdf.normalized_rows_path, "Other")
    ignored_recommendation_id = depmod.recommendation_identity(
        dataset_artifact_id=A,
        support_artifact_id=B,
        kind=DependencyKind.PDF,
        reason_code=DependencyReasonCode.SAME_STEM_COMPANION,
        header_ids=(),
        transform_requirement_ids=(),
    )
    basis_for_decision = DependencyDecisionBasis(
        rulebook_sha256=SHA5,
        scrub_config_sha256=SHA4,
        support_role_sha256=depmod.support_role_sha256(
            recommendation_id=ignored_recommendation_id,
            dataset_artifact_id=A,
            support_artifact_id=B,
            kind=DependencyKind.PDF,
            role_source=RoleSource.INFERRED,
            organizer_role_version=1,
        ),
    )
    ignored = DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=DD,
        recommendation_id=ignored_recommendation_id,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=B,
        support_sha256=SHA2,
        normalized_support_sha256=SHA3,
        kind=DependencyKind.PDF,
        level=DependencyLevel.IGNORED,
        sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.SAME_STEM_COMPANION,
        basis=basis_for_decision,
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )
    ignored_recommendations = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(pdf,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(ignored,),
        rule_bundle=_rule_bundle(),
    )
    assert ignored_recommendations == ()

    required = DependencyDecision(**{**ignored.__dict__, "level": DependencyLevel.REQUIRED})
    recs = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(pdf,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(required,),
        rule_bundle=_rule_bundle(),
    )
    assert any(rec.reason_code is DependencyReasonCode.MANIFEST_DECLARED and rec.suggested_level is DependencyLevel.REQUIRED for rec in recs)


def test_recommender_exact_manifest_non_confidential_suppresses_inferred_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    pdf = _support(tmp_path, kind=DependencyKind.PDF)
    object.__setattr__(pdf, "normalized_rows_path", tmp_path / "labs__pdf.jsonl")
    _write_support_rows(pdf.normalized_rows_path, "Other")
    recommendation_id = depmod.recommendation_identity(
        dataset_artifact_id=A,
        support_artifact_id=B,
        kind=DependencyKind.PDF,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        header_ids=(),
        transform_requirement_ids=(),
    )
    manifest_decision = DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=DD,
        recommendation_id=recommendation_id,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=B,
        support_sha256=SHA2,
        normalized_support_sha256=SHA3,
        kind=DependencyKind.PDF,
        level=DependencyLevel.HELPFUL,
        sensitivity=Sensitivity.NON_CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        basis=DependencyDecisionBasis(
            rulebook_sha256=SHA5,
            scrub_config_sha256=SHA4,
            support_role_sha256=depmod.support_role_sha256(
                recommendation_id=recommendation_id,
                dataset_artifact_id=A,
                support_artifact_id=B,
                kind=DependencyKind.PDF,
                role_source=RoleSource.MANIFEST,
                organizer_role_version=1,
            ),
        ),
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )

    recs = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(pdf,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(manifest_decision,),
        rule_bundle=_rule_bundle(),
    )

    assert len(recs) == 1
    assert recs[0].reason_code is DependencyReasonCode.MANIFEST_DECLARED
    assert recs[0].default_sensitivity is Sensitivity.NON_CONFIDENTIAL


def test_recommender_conservatively_merges_duplicate_manifest_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    support = _support(tmp_path)

    def prior_decision(
        recommendation_id: str,
        decision_id: str,
        level: DependencyLevel,
    ) -> DependencyDecision:
        return DependencyDecision(
            schema_version="dependency-decision/v1",
            decision_id=decision_id,
            recommendation_id=recommendation_id,
            dataset_artifact_id=A,
            dataset_sha256=SHA1,
            support_artifact_id=B,
            support_sha256=SHA2,
            normalized_support_sha256=SHA3,
            kind=DependencyKind.DICTIONARY,
            level=level,
            sensitivity=Sensitivity.CONFIDENTIAL,
            reason_code=DependencyReasonCode.EXACT_HEADER_MATCH,
            basis=DependencyDecisionBasis(
                rulebook_sha256=SHA5,
                scrub_config_sha256=SHA4,
                support_role_sha256=depmod.support_role_sha256(
                    recommendation_id=recommendation_id,
                    dataset_artifact_id=A,
                    support_artifact_id=B,
                    kind=DependencyKind.DICTIONARY,
                    role_source=RoleSource.INFERRED,
                    organizer_role_version=1,
                ),
            ),
            decided_by="reviewer",
            decided_at="2026-07-14T10:00:00Z",
        )

    helpful = prior_decision("dr_" + "8" * 32, "dd_" + "8" * 32, DependencyLevel.HELPFUL)
    required = prior_decision("dr_" + "9" * 32, "dd_" + "9" * 32, DependencyLevel.REQUIRED)
    for decisions in ((helpful, required), (required, helpful)):
        recs = recommend_dependencies(
            datasets=(dataset,),
            support_artifacts=(support,),
            published_raw_headers_by_dataset={A: frozenset()},
            transform_requirements_by_dataset={},
            confirmed_links=decisions,
            rule_bundle=_rule_bundle(),
        )
        manifest = [
            rec
            for rec in recs
            if rec.reason_code is DependencyReasonCode.MANIFEST_DECLARED
        ]
        assert len(manifest) == 1
        assert manifest[0].suggested_level is DependencyLevel.REQUIRED


def test_recommender_pdf_matching_prefers_exact_stem_then_unambiguous_form_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    object.__setattr__(dataset, "normalized_rows_path", tmp_path / "12a labs.jsonl")
    exact = _support(tmp_path, artifact_id=B, kind=DependencyKind.PDF)
    object.__setattr__(exact, "normalized_rows_path", tmp_path / "12a labs__pdf.jsonl")
    _write_support_rows(exact.normalized_rows_path, "Other")
    same_code = _support(tmp_path, artifact_id=C, kind=DependencyKind.PDF)
    object.__setattr__(same_code, "normalized_rows_path", tmp_path / "12A Follow-up__pdf.jsonl")
    _write_support_rows(same_code.normalized_rows_path, "Other")

    exact_recs = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(same_code, exact),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )
    assert [
        rec.support_artifact_id
        for rec in exact_recs
        if rec.reason_code is DependencyReasonCode.SAME_STEM_COMPANION
    ] == [B]

    form_code_recs = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(same_code,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )
    assert [
        rec.support_artifact_id
        for rec in form_code_recs
        if rec.reason_code is DependencyReasonCode.SAME_STEM_COMPANION
    ] == [C]


def test_recommender_pdf_form_code_ambiguous_or_no_match_emits_no_companion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    object.__setattr__(dataset, "normalized_rows_path", tmp_path / "12a labs.jsonl")
    first = _support(tmp_path, artifact_id=B, kind=DependencyKind.PDF)
    object.__setattr__(first, "normalized_rows_path", tmp_path / "12A Follow-up__pdf.jsonl")
    _write_support_rows(first.normalized_rows_path, "Other")
    second = _support(tmp_path, artifact_id=C, kind=DependencyKind.PDF)
    object.__setattr__(second, "normalized_rows_path", tmp_path / "12a Baseline__pdf.jsonl")
    _write_support_rows(second.normalized_rows_path, "Other")
    wrong_code = _support(tmp_path, artifact_id=D, kind=DependencyKind.PDF)
    object.__setattr__(wrong_code, "normalized_rows_path", tmp_path / "13 Follow-up__pdf.jsonl")
    _write_support_rows(wrong_code.normalized_rows_path, "Other")

    for supports in ((first, second), (wrong_code,), ()):
        recs = recommend_dependencies(
            datasets=(dataset,),
            support_artifacts=supports,
            published_raw_headers_by_dataset={A: frozenset()},
            transform_requirements_by_dataset={},
            confirmed_links=(),
            rule_bundle=_rule_bundle(),
        )
        assert not any(
            rec.reason_code is DependencyReasonCode.SAME_STEM_COMPANION
            for rec in recs
        )


def test_recommender_conflicting_header_tokens_remain_helpful_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    first = _support(tmp_path, artifact_id=B)
    second = _support(tmp_path, artifact_id=C)
    recs = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(first, second),
        published_raw_headers_by_dataset={A: frozenset({H})},
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )

    header_recs = [rec for rec in recs if rec.header_ids == (H,)]
    assert {rec.support_artifact_id for rec in header_recs} == {B, C}
    assert all(
        rec.suggested_level is DependencyLevel.HELPFUL
        and rec.reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
        for rec in header_recs
    )


def test_recommender_transform_requirement_missing_then_confirmed_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import phi_engine.pipeline.dependencies as depmod

    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA4)
    dataset = _dataset(tmp_path)
    support = _support(tmp_path)
    req = TransformRequirement(
        requirement_id=TR,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        header_id=H,
        classification_action=Action.GENERALIZE,
        kind=StructuredTransformKind.GENERALIZE,
        origin=TransformRequirementOrigin.EFFECTIVE_CONFIG,
        origin_rule_id="rule-transform",
        required_support_kind=DependencyKind.DICTIONARY,
    )
    missing = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={A: (req,)},
        confirmed_links=(),
        rule_bundle=_rule_bundle(),
    )
    transform_rec = [rec for rec in missing if rec.transform_requirement_ids == (TR,)][0]
    assert transform_rec.suggested_level is DependencyLevel.REQUIRED
    assert transform_rec.support_artifact_id is None
    assert transform_rec.reason_code is DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING
    ignored_missing = DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id="dd_" + "a" * 32,
        recommendation_id=transform_rec.recommendation_id,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=None,
        support_sha256=None,
        normalized_support_sha256=None,
        kind=DependencyKind.DICTIONARY,
        level=DependencyLevel.IGNORED,
        sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING,
        basis=transform_rec.basis,
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )
    ignored_attempt = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={A: (req,)},
        confirmed_links=(ignored_missing,),
        rule_bundle=_rule_bundle(),
    )
    still_required = [
        rec for rec in ignored_attempt if rec.transform_requirement_ids == (TR,)
    ][0]
    assert still_required.suggested_level is DependencyLevel.REQUIRED
    assert still_required.reason_code is DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING

    current_decision = DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=DD,
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_sha256=SHA1,
        support_artifact_id=B,
        support_sha256=SHA2,
        normalized_support_sha256=SHA3,
        kind=DependencyKind.DICTIONARY,
        level=DependencyLevel.REQUIRED,
        sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        basis=DependencyDecisionBasis(
            rulebook_sha256=SHA5,
            scrub_config_sha256=SHA4,
            support_role_sha256=depmod.support_role_sha256(
                recommendation_id=DR,
                dataset_artifact_id=A,
                support_artifact_id=B,
                kind=DependencyKind.DICTIONARY,
                role_source=RoleSource.MANIFEST,
                organizer_role_version=1,
            ),
        ),
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )
    confirmed = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={A: frozenset()},
        transform_requirements_by_dataset={A: (req,)},
        confirmed_links=(current_decision,),
        rule_bundle=_rule_bundle(),
    )
    unresolved = [rec for rec in confirmed if rec.transform_requirement_ids == (TR,)][0]
    assert unresolved.suggested_level is DependencyLevel.REQUIRED
    assert unresolved.support_artifact_id is None
    assert unresolved.reason_code is DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING


def _decision_workflow_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing_support: bool = False,
) -> tuple[Path, DependencyRecommendation, PrivateDependencyRecommendation]:
    import phi_engine.pipeline.dependencies as depmod
    import phi_engine.pipeline.review as reviewmod

    workspace = tmp_path / "workspace"
    study = "DependencyStudy"
    output_dir = workspace / "output" / study
    run_dir = output_dir / "runs" / "20260714T120000Z"
    organized_root = workspace / "organized" / study
    config_dir = workspace / "config" / study
    dataset_bytes = b"Subject ID,Age\nA1,40\n"
    support_bytes = b"variable,label\nSubject ID,Identifier\n"
    normalized_bytes = b'{"normalized_cells":["subject id","identifier"]}\n'
    dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
    support_sha = hashlib.sha256(support_bytes).hexdigest()
    normalized_sha = hashlib.sha256(normalized_bytes).hexdigest()
    support_id = None if missing_support else B
    role_hash = depmod.support_role_sha256(
        recommendation_id=DR,
        dataset_artifact_id=A,
        support_artifact_id=support_id,
        kind=DependencyKind.DICTIONARY,
        role_source=RoleSource.INFERRED,
        organizer_role_version=1,
    )
    current_basis = DependencyDecisionBasis(
        rulebook_sha256=SHA1,
        scrub_config_sha256=SHA2,
        support_role_sha256=role_hash,
    )
    rec = DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_sha256=dataset_sha,
        support_artifact_id=support_id,
        support_sha256=None if missing_support else support_sha,
        normalized_support_sha256=None if missing_support else normalized_sha,
        kind=DependencyKind.DICTIONARY,
        suggested_level=DependencyLevel.REQUIRED,
        default_sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.ONLY_INTERPRETATION,
        header_ids=(H,),
        matched_rule_ids=("rule-1",),
        transform_requirement_ids=(),
        basis=current_basis,
    )
    private = PrivateDependencyRecommendation(
        schema_version="dependency-recommendation-private/v1",
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_path="datasets/labs.csv",
        support_artifact_id=support_id,
        support_path="data_dictionary/not_yet.csv" if missing_support else "data_dictionary/labs.csv",
        raw_header_names=("Subject ID",),
        role_source=RoleSource.INFERRED,
        organizer_role_version=1,
        basis=current_basis,
    )
    write_dependency_recommendations(
        run_dir=run_dir,
        recommendations=(rec,),
        private_records=(private,),
    )

    verified = organized_root / ".verified_sources"
    protected_headers = organized_root / ".protected" / "headers"
    protected_support = organized_root / ".protected" / "support"
    verified.mkdir(parents=True)
    protected_headers.mkdir(parents=True)
    protected_support.mkdir(parents=True)
    (verified / A).write_bytes(dataset_bytes)
    header_path = protected_headers / f"{A}.json"
    header_path.write_text(
        json.dumps(
            {
                "artifact_id": A,
                "source_sha256": dataset_sha,
                "source_relative_path": "datasets/labs.csv",
                "headers": [],
            }
        ),
        encoding="utf-8",
    )
    header_path.chmod(0o600)
    if not missing_support:
        (verified / B).write_bytes(support_bytes)
        normalized_path = organized_root / "support" / "dictionary" / f"labs__{B}.jsonl"
        normalized_path.parent.mkdir(parents=True)
        normalized_path.write_bytes(normalized_bytes)
        normalized_path.chmod(0o600)
        support_path = protected_support / f"{B}.json"
        support_path.write_text(
            json.dumps(
                {
                    "artifact_id": B,
                    "source_sha256": support_sha,
                    "kind": "dictionary",
                    "format": "csv",
                    "parse_status": "parsed",
                    "normalized_rows_sha256": normalized_sha,
                    "failure_code": None,
                    "normalized_rows_path": str(normalized_path),
                    "source_relative_path": "data_dictionary/labs.csv",
                    "normalized_source_stem": "labs",
                }
            ),
            encoding="utf-8",
        )
        support_path.chmod(0o600)

    config_dir.mkdir(parents=True)
    manifest_path = config_dir / "_forms_manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "required": ["labs.csv"],
                "optional": ["extra.csv"],
                "reject": [],
                "date_locales": {"VISIT_DT": "DMY"},
                "dataset_dependencies_schema": "dataset-dependencies/v1",
                "dataset_dependencies_code_table_version": 1,
                "dataset_dependencies": {"datasets/other.csv": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(reviewmod.config, "STUDY_OUTPUT_DIR", output_dir)
    monkeypatch.setattr(reviewmod.config, "ORGANIZED_DIR", workspace / "organized")
    monkeypatch.setattr(reviewmod.config, "study_config_dir", lambda selected=None: workspace / "config" / (selected or study))
    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA2)
    return manifest_path, rec, private


def test_dependency_recommendation_writers_and_loaders_are_exact_private_and_mode_0600(
    tmp_path: Path,
) -> None:
    private = PrivateDependencyRecommendation(
        schema_version="dependency-recommendation-private/v1",
        recommendation_id=DR,
        dataset_artifact_id=A,
        dataset_path="datasets/labs.csv",
        support_artifact_id=B,
        support_path="data_dictionary/labs.csv",
        raw_header_names=("Subject ID",),
        role_source=RoleSource.INFERRED,
        organizer_role_version=1,
        basis=basis(),
    )
    ordinary_path, private_path = write_dependency_recommendations(
        run_dir=tmp_path / "run",
        recommendations=(recommendation(),),
        private_records=(private,),
    )

    assert load_dependency_recommendations(ordinary_path) == (recommendation(),)
    assert stat.S_IMODE(ordinary_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(private_path.parent.stat().st_mode) == 0o700
    assert "Subject ID" not in ordinary_path.read_text(encoding="utf-8")
    assert "Subject ID" in private_path.read_text(encoding="utf-8")


def test_decide_dependency_updates_only_manifest_dependency_fields_and_appends_exact_private_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, rec, _private = _decision_workflow_fixture(tmp_path, monkeypatch)
    import phi_engine.pipeline.review as reviewmod

    detail_path = tmp_path / "detail.txt"
    detail_path.write_text("PRIVATE-CANARY reviewer context", encoding="utf-8")
    detail_path.chmod(0o600)
    decision = reviewmod.decide_dependency(
        "DependencyStudy",
        dataset=A,
        recommendation=DR,
        support=B,
        level="helpful",
        sensitivity="non_confidential",
        reason_code="only_interpretation",
        detail_file=detail_path,
        decided_by="reviewer-id",
    )

    assert decision.dataset_sha256 == rec.dataset_sha256
    assert decision.support_sha256 == rec.support_sha256
    assert decision.normalized_support_sha256 == rec.normalized_support_sha256
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["required"] == ["labs.csv"]
    assert manifest["optional"] == ["extra.csv"]
    assert manifest["reject"] == []
    assert manifest["date_locales"] == {"VISIT_DT": "DMY"}
    assert manifest["dataset_dependencies_schema"] == "dataset-dependencies/v1"
    assert manifest["dataset_dependencies_code_table_version"] == 1
    assert manifest["dataset_dependencies"]["datasets/other.csv"] == []
    assert manifest["dataset_dependencies"]["datasets/labs.csv"] == [
        {
            "dataset_artifact_id": A,
            "dataset_source_sha256": rec.dataset_sha256,
            "support": "data_dictionary/labs.csv",
            "support_artifact_id": B,
            "support_source_sha256": rec.support_sha256,
            "kind": "dictionary",
            "level": "helpful",
            "sensitivity": "non_confidential",
            "reason_code": "only_interpretation",
            "recommendation_id": DR,
            "basis": rec.basis.to_json(),
            "confirmed_by": "reviewer-id",
            "confirmed_at": decision.decided_at,
        }
    ]
    decisions_path = manifest_path.parent / "dependency_decisions.jsonl"
    decisions = load_dependency_decisions(decisions_path)
    assert decisions == (decision,)
    assert json.loads(decisions_path.read_text(encoding="utf-8")) == decision.to_json()
    assert stat.S_IMODE(decisions_path.stat().st_mode) == 0o600
    ordinary = decisions_path.read_text(encoding="utf-8") + manifest_path.read_text(encoding="utf-8")
    assert "PRIVATE-CANARY" not in ordinary
    private_path = manifest_path.parent / "dependency_decision_details.jsonl"
    assert "PRIVATE-CANARY reviewer context" in private_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600


def test_decide_dependency_accepts_omitted_support_only_for_missing_support_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, rec, _private = _decision_workflow_fixture(tmp_path, monkeypatch, missing_support=True)
    import phi_engine.pipeline.review as reviewmod

    decision = reviewmod.decide_dependency(
        "DependencyStudy",
        dataset=A,
        recommendation=DR,
        support=None,
        level="required",
        sensitivity="confidential",
        reason_code="only_interpretation",
        detail_file=None,
        decided_by="reviewer-id",
    )
    assert decision.support_artifact_id is None
    dependency = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["dataset_dependencies"]["datasets/labs.csv"][0]
    assert dependency["support"] == "data_dictionary/not_yet.csv"
    assert dependency["support_artifact_id"] is None
    assert dependency["support_source_sha256"] is None
    assert rec.support_sha256 is None


def test_decide_dependency_rejects_composite_mismatch_and_stale_basis_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _rec, _private = _decision_workflow_fixture(tmp_path, monkeypatch)
    import phi_engine.pipeline.dependencies as depmod
    import phi_engine.pipeline.review as reviewmod

    original = manifest_path.read_bytes()
    with pytest.raises(ValueError, match="support identity mismatch"):
        reviewmod.decide_dependency(
            "DependencyStudy",
            dataset=A,
            recommendation=DR,
            support="a_" + "9" * 32,
            level="required",
            sensitivity="confidential",
            reason_code="only_interpretation",
            detail_file=None,
            decided_by="reviewer-id",
        )
    assert manifest_path.read_bytes() == original
    monkeypatch.setattr(depmod, "_effective_scrub_config_sha256", lambda: SHA5)
    with pytest.raises(ValueError, match="stale recommendation basis"):
        reviewmod.decide_dependency(
            "DependencyStudy",
            dataset=A,
            recommendation=DR,
            support=B,
            level="required",
            sensitivity="confidential",
            reason_code="only_interpretation",
            detail_file=None,
            decided_by="reviewer-id",
        )
    assert manifest_path.read_bytes() == original
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()


def test_decide_dependency_manifest_replace_failure_preserves_original_and_no_trail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _rec, _private = _decision_workflow_fixture(tmp_path, monkeypatch)
    import phi_engine.pipeline.review as reviewmod

    original = manifest_path.read_bytes()

    def fail_replace(_source: str | bytes | os.PathLike[str] | os.PathLike[bytes], _destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(reviewmod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        reviewmod.decide_dependency(
            "DependencyStudy",
            dataset=A,
            recommendation=DR,
            support=B,
            level="required",
            sensitivity="confidential",
            reason_code="only_interpretation",
            detail_file=None,
            decided_by="reviewer-id",
        )
    assert manifest_path.read_bytes() == original
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()


def test_dependency_decide_cli_help_and_service_mapping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from phi_engine.cli.main import main
    import phi_engine.pipeline.review as reviewmod

    with pytest.raises(SystemExit) as help_exit:
        main(["review", "--study", "DependencyStudy", "dependency-decide", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    for token in (
        "--dataset",
        "--recommendation",
        "--support",
        "--level",
        "--sensitivity",
        "--reason-code",
        "--detail-file",
        "--decided-by",
    ):
        assert token in help_text

    captured: dict[str, object] = {}

    def fake_decide(study: str, **kwargs: object) -> DependencyDecision:
        captured["study"] = study
        captured.update(kwargs)
        return decision()

    monkeypatch.setattr(reviewmod, "decide_dependency", fake_decide)
    detail = tmp_path / "detail.txt"
    detail.write_text("PRIVATE-CANARY", encoding="utf-8")
    detail.chmod(0o600)
    argv = [
        "review",
        "--study",
        "DependencyStudy",
        "dependency-decide",
        "--dataset",
        A,
        "--recommendation",
        DR,
        "--support",
        B,
        "--level",
        "required",
        "--sensitivity",
        "confidential",
        "--reason-code",
        "only_interpretation",
        "--detail-file",
        str(detail),
        "--decided-by",
        "reviewer-id",
    ]
    assert main(argv) == 0
    cli_output = capsys.readouterr()
    assert "PRIVATE-CANARY" not in cli_output.out
    assert "PRIVATE-CANARY" not in cli_output.err
    assert captured == {
        "study": "DependencyStudy",
        "dataset": A,
        "recommendation": DR,
        "support": B,
        "level": "required",
        "sensitivity": "confidential",
        "reason_code": "only_interpretation",
        "detail_file": detail,
        "decided_by": "reviewer-id",
    }

    def reject_without_echo(_study: str, **_kwargs: object) -> DependencyDecision:
        raise ValueError("PRIVATE-CANARY must never reach stderr")

    monkeypatch.setattr(reviewmod, "decide_dependency", reject_without_echo)
    assert main(argv) == 2
    rejected_output = capsys.readouterr()
    assert "PRIVATE-CANARY" not in rejected_output.out
    assert "PRIVATE-CANARY" not in rejected_output.err
