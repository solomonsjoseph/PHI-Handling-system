from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    TransformRequirementOrigin,
    SupportFailureCode,
    SupportParseStatus,
    support_role_sha256,
    write_dependency_recommendations,
)
from phi_engine.security.phi_review import Action, HeaderClassification


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _id(char: str) -> str:
    return "a_" + char * 32


def _header(char: str) -> str:
    return "h_" + char * 24


def _recommendation_id(char: str) -> str:
    return "dr_" + char * 32


def _decision_id(char: str) -> str:
    return "dd_" + char * 32


def _write_0600_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _organized_alias_fixture(tmp_path: Path) -> tuple[Path, dict[str, object], str, str, str]:
    organized = tmp_path / "organized"
    datasets_dir = organized / "datasets"
    datasets_dir.mkdir(parents=True)
    source_bytes = b"same source bytes"
    source_sha = _sha(source_bytes)
    first_id = _id("1")
    second_id = _id("2")
    support_id = _id("3")
    first_header = _header("a")
    second_header = _header("b")

    entries: list[dict[str, object]] = []
    for artifact_id, output, header_id, raw_name, source_path in (
        (first_id, "alpha.jsonl", first_header, "PRIVATE-ALPHA", "datasets/alpha.csv"),
        (second_id, "beta.jsonl", second_header, "PRIVATE-BETA", "datasets/beta.csv"),
    ):
        normalized = (json.dumps({header_id: "row-value"}, sort_keys=True) + "\n").encode()
        (datasets_dir / output).write_bytes(normalized)
        (organized / ".verified_sources").mkdir(parents=True, exist_ok=True)
        (organized / ".verified_sources" / artifact_id).write_bytes(source_bytes)
        _write_0600_json(
            organized / ".protected" / "headers" / f"{artifact_id}.json",
            {
                "artifact_id": artifact_id,
                "source_sha256": source_sha,
                "headers": [
                    {
                        "header_id": header_id,
                        "column_index": 0,
                        "raw_name": raw_name,
                        "normalized_name": raw_name.lower(),
                    }
                ],
                "source_relative_path": source_path,
            },
        )
        entries.append(
            {
                "artifact_id": artifact_id,
                "source_sha256": source_sha,
                "output": output,
                "normalized_rows_sha256": _sha(normalized),
                "row_count": 1,
                "headers": [
                    {
                        "header_id": header_id,
                        "column_index": 0,
                        "normalized_name": raw_name.lower(),
                    }
                ],
            }
        )

    support_source = b"support source bytes"
    support_source_sha = _sha(support_source)
    support_rows = b'{"cells":[{"column_index":0,"value":"PRIVATE-ALPHA"}],"row_index":0,"sheet_index":0,"support_artifact_id":"a_33333333333333333333333333333333","source_sha256":"' + support_source_sha.encode() + b'","table_index":0}\n'
    support_rows_path = organized / "support" / "dictionary" / "dictionary.jsonl"
    support_rows_path.parent.mkdir(parents=True)
    support_rows_path.write_bytes(support_rows)
    (organized / ".verified_sources" / support_id).write_bytes(support_source)
    support_public = {
        "artifact_id": support_id,
        "source_sha256": support_source_sha,
        "kind": "dictionary",
        "format": "csv",
        "parse_status": "parsed",
        "normalized_rows_sha256": _sha(support_rows),
        "failure_code": None,
    }
    _write_0600_json(
        organized / ".protected" / "support" / f"{support_id}.json",
        {
            **support_public,
            "normalized_rows_path": str(support_rows_path),
            "source_relative_path": "data_dictionary/private.csv",
            "normalized_source_stem": "private",
        },
    )
    manifest: dict[str, object] = {
        "study": "Study",
        "datasets": entries,
        "support_artifacts": [support_public],
        "pdf_roles": {},
        "review_bucket": [],
        "intake_manifest_sha": "f" * 64,
    }
    return organized, manifest, first_id, second_id, support_id


def _basis(
    recommendation_id: str,
    dataset_id: str,
    support_id: str,
    *,
    role_source: RoleSource = RoleSource.INFERRED,
) -> DependencyDecisionBasis:
    return DependencyDecisionBasis(
        rulebook_sha256="a" * 64,
        scrub_config_sha256="b" * 64,
        support_role_sha256=support_role_sha256(
            recommendation_id=recommendation_id,
            dataset_artifact_id=dataset_id,
            support_artifact_id=support_id,
            kind=DependencyKind.DICTIONARY,
            role_source=role_source,
            organizer_role_version=1,
        ),
    )


def _recommendation(
    *,
    recommendation_id: str,
    dataset_id: str,
    dataset_sha: str,
    support_id: str,
    support_sha: str,
    normalized_sha: str,
    level: DependencyLevel,
    sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL,
) -> DependencyRecommendation:
    return DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=recommendation_id,
        dataset_artifact_id=dataset_id,
        dataset_sha256=dataset_sha,
        support_artifact_id=support_id,
        support_sha256=support_sha,
        normalized_support_sha256=normalized_sha,
        kind=DependencyKind.DICTIONARY,
        suggested_level=level,
        default_sensitivity=sensitivity,
        reason_code=DependencyReasonCode.EXACT_HEADER_MATCH,
        header_ids=(),
        matched_rule_ids=(),
        transform_requirement_ids=(),
        basis=_basis(recommendation_id, dataset_id, support_id),
    )


def _decision(rec: DependencyRecommendation, *, level: DependencyLevel | None = None) -> DependencyDecision:
    return DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=_decision_id(rec.recommendation_id[-1]),
        recommendation_id=rec.recommendation_id,
        dataset_artifact_id=rec.dataset_artifact_id,
        dataset_sha256=rec.dataset_sha256,
        support_artifact_id=rec.support_artifact_id,
        support_sha256=rec.support_sha256,
        normalized_support_sha256=rec.normalized_support_sha256,
        kind=rec.kind,
        level=level or rec.suggested_level,
        sensitivity=rec.default_sensitivity,
        reason_code=rec.reason_code,
        basis=rec.basis,
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )


def test_hydrates_exact_alias_datasets_and_support_only_from_protected_0600_metadata(tmp_path: Path) -> None:
    from phi_engine.pipeline.run import _hydrate_dependency_inputs

    organized, manifest, first_id, second_id, support_id = _organized_alias_fixture(tmp_path)
    hydrated = _hydrate_dependency_inputs(organized, manifest)

    assert [dataset.artifact_id for dataset in hydrated.datasets] == [first_id, second_id]
    assert hydrated.datasets[0].source_sha256 == hydrated.datasets[1].source_sha256
    assert hydrated.datasets[0].headers[0].raw_name == "PRIVATE-ALPHA"
    assert hydrated.datasets[1].headers[0].raw_name == "PRIVATE-BETA"
    assert [support.artifact_id for support in hydrated.support_artifacts] == [support_id]
    assert hydrated.dataset_paths_by_id == {
        first_id: "datasets/alpha.csv",
        second_id: "datasets/beta.csv",
    }
    assert hydrated.support_paths_by_id == {support_id: "data_dictionary/private.csv"}

    protected = organized / ".protected" / "headers" / f"{first_id}.json"
    protected.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        _hydrate_dependency_inputs(organized, manifest)


@pytest.mark.parametrize(
    "header_payload",
    [
        {
            "header_id": _header("a"),
            "column_index": 0,
            "raw_name": "PRIVATE-ALPHA",
            "normalized_name": "private-alpha",
            "unexpected": "metadata",
        },
        {
            "header_id": _header("a"),
            "column_index": 0,
            "normalized_name": "private-alpha",
        },
        {
            "header_id": _header("a"),
            "column_index": "0",
            "raw_name": "PRIVATE-ALPHA",
            "normalized_name": "private-alpha",
        },
        {
            "header_id": _header("a"),
            "column_index": True,
            "raw_name": "PRIVATE-ALPHA",
            "normalized_name": "private-alpha",
        },
        {
            "header_id": 123,
            "column_index": 0,
            "raw_name": "PRIVATE-ALPHA",
            "normalized_name": "private-alpha",
        },
        {
            "header_id": _header("a"),
            "column_index": 0,
            "raw_name": 123,
            "normalized_name": "private-alpha",
        },
        {
            "header_id": _header("a"),
            "column_index": 0,
            "raw_name": "PRIVATE-ALPHA",
            "normalized_name": None,
        },
    ],
)
def test_hydration_rejects_non_exact_or_coerced_protected_header_mappings(
    tmp_path: Path,
    header_payload: dict[str, object],
) -> None:
    from phi_engine.pipeline.run import _hydrate_dependency_inputs

    organized, manifest, first_id, _second_id, _support_id = (
        _organized_alias_fixture(tmp_path)
    )
    protected_path = (
        organized / ".protected" / "headers" / f"{first_id}.json"
    )
    protected = json.loads(protected_path.read_text(encoding="utf-8"))
    protected["headers"] = [header_payload]
    _write_0600_json(protected_path, protected)

    with pytest.raises(ValueError, match="protected dataset header"):
        _hydrate_dependency_inputs(organized, manifest)


def test_raw_row_header_mapping_rejects_types_instead_of_coercing(
    tmp_path: Path,
) -> None:
    from phi_engine.pipeline.run import _raw_rows_from_organized

    organized, manifest, first_id, _second_id, _support_id = (
        _organized_alias_fixture(tmp_path)
    )
    protected_path = (
        organized / ".protected" / "headers" / f"{first_id}.json"
    )
    protected = json.loads(protected_path.read_text(encoding="utf-8"))
    protected["headers"][0]["column_index"] = "0"
    _write_0600_json(protected_path, protected)
    entry = manifest["datasets"][0]

    with pytest.raises(ValueError, match="protected dataset header"):
        _raw_rows_from_organized(
            organized,
            entry,
            [{_header("a"): "value"}],
        )


class _EffectiveConfig:
    def field_is_keep(self, name: str) -> bool:
        return name == "KEEP"

    def field_is_date(self, _name: str) -> bool:
        return False

    def field_is_birthdate(self, _name: str) -> bool:
        return False

    def field_is_id(self, _name: str) -> bool:
        return False

    def field_is_drop(self, _name: str) -> bool:
        return False

    def cap_rule_for(self, name: str) -> object | None:
        return object() if name == "CAP_READY" else None

    def generalize_rule_for(self, name: str) -> object | None:
        return object() if name == "GENERALIZE_READY" else None

    def band_rule_for(self, name: str) -> object | None:
        return object() if name == "BAND_ONLY" else None

    def field_is_suppress_small_cell(self, name: str) -> bool:
        return name == "SUPPRESS_READY"


def test_preliminary_inputs_use_opaque_header_ids_and_explicit_transforms_without_banding(
    tmp_path: Path,
) -> None:
    from phi_engine.pipeline.run import (
        _build_preliminary_dependency_inputs,
        _header_protected_by_effective_config,
    )

    organized, manifest, first_id, second_id, _support_id = _organized_alias_fixture(tmp_path)
    hydrated = _hydrate_dependency_inputs_for_test(organized, manifest)
    first = hydrated.datasets[0]
    second = hydrated.datasets[1]
    classifications = {
        first_id: (
            HeaderClassification(
                header=first.headers[0].raw_name,
                action=Action.CAP,
                matched_rules=("cap-rule",),
                jurisdictions=("USA",),
                reasons=("cap",),
            ),
        ),
        second_id: (
            HeaderClassification(
                header=second.headers[0].raw_name,
                action=Action.GENERALIZE,
                matched_rules=("generalize-rule",),
                jurisdictions=("USA",),
                reasons=("generalize",),
            ),
        ),
    }
    published, requirements = _build_preliminary_dependency_inputs(
        hydrated.datasets,
        classifications,
        _EffectiveConfig(),
    )

    assert not _header_protected_by_effective_config(
        _EffectiveConfig(),
        "KEEP",
    )
    assert published == {
        first_id: frozenset({first.headers[0].header_id}),
        second_id: frozenset({second.headers[0].header_id}),
    }
    first_requirement = requirements[first_id][0]
    second_requirement = requirements[second_id][0]
    assert first_requirement.kind is StructuredTransformKind.CAP
    assert first_requirement.origin is TransformRequirementOrigin.RULE_CLASSIFICATION
    assert first_requirement.required_support_kind is DependencyKind.DICTIONARY
    assert second_requirement.kind is StructuredTransformKind.GENERALIZE
    assert second_requirement.origin is TransformRequirementOrigin.RULE_CLASSIFICATION
    assert second_requirement.required_support_kind is DependencyKind.MAPPING
    assert all(req.kind.value != "band" for values in requirements.values() for req in values)
    assert _build_preliminary_dependency_inputs(
        hydrated.datasets,
        classifications,
        _EffectiveConfig(),
    ) == (published, requirements)


class _ConfiguredTransforms(_EffectiveConfig):
    def field_is_keep(self, _name: str) -> bool:
        return False

    def cap_rule_for(self, name: str) -> object | None:
        if name == "PRIVATE-ALPHA":
            return SimpleNamespace(threshold=90, label="90+")
        return None

    def generalize_rule_for(self, name: str) -> object | None:
        if name == "PRIVATE-BETA":
            return SimpleNamespace(mapping={"x": "group"})
        return None

    def field_is_suppress_small_cell(self, name: str) -> bool:
        return name == "PRIVATE-GAMMA"

class _MissingGeneralization(_ConfiguredTransforms):
    def generalize_rule_for(self, name: str) -> object | None:
        if name == "PRIVATE-BETA":
            return SimpleNamespace(mapping={})
        return None


def test_effective_config_transforms_are_explicit_even_when_rules_classify_keep(
    tmp_path: Path,
) -> None:
    from phi_engine.pipeline.run import _build_preliminary_dependency_inputs

    organized, manifest, first_id, second_id, _support_id = _organized_alias_fixture(tmp_path)
    hydrated = _hydrate_dependency_inputs_for_test(organized, manifest)
    third_id = _id("4")
    third = OrganizedDataset(
        artifact_id=third_id,
        source_sha256="4" * 64,
        normalized_rows_path=Path("gamma.jsonl"),
        normalized_rows_sha256="5" * 64,
        headers=(
            OrganizedHeader(
                header_id=_header("c"),
                column_index=0,
                raw_name="PRIVATE-GAMMA",
                normalized_name="private-gamma",
            ),
        ),
    )
    datasets = (*hydrated.datasets, third)
    classifications = {
        dataset.artifact_id: tuple(
            HeaderClassification(
                header=header.raw_name,
                action=Action.KEEP,
                matched_rules=(),
                jurisdictions=("USA",),
                reasons=("keep",),
            )
            for header in dataset.headers
        )
        for dataset in datasets
    }
    published, requirements = _build_preliminary_dependency_inputs(
        datasets,
        classifications,
        _ConfiguredTransforms(),
    )

    assert published == {
        first_id: frozenset(),
        second_id: frozenset(),
        third_id: frozenset(),
    }
    assert requirements[first_id][0].kind is StructuredTransformKind.CAP
    assert requirements[second_id][0].kind is StructuredTransformKind.GENERALIZE
    assert requirements[third_id][0].kind is StructuredTransformKind.SUPPRESS_SMALL_CELL
    assert all(
        requirement.origin is TransformRequirementOrigin.EFFECTIVE_CONFIG
        and requirement.required_support_kind is None
        for values in requirements.values()
        for requirement in values
    )
    _, missing_requirements = _build_preliminary_dependency_inputs(
        datasets,
        classifications,
        _MissingGeneralization(),
    )
    assert (
        missing_requirements[second_id][0].origin
        is TransformRequirementOrigin.EFFECTIVE_CONFIG
    )
    assert (
        missing_requirements[second_id][0].required_support_kind
        is DependencyKind.MAPPING
    )


def _hydrate_dependency_inputs_for_test(
    organized: Path,
    manifest: dict[str, object],
):
    from phi_engine.pipeline.run import _hydrate_dependency_inputs

    return _hydrate_dependency_inputs(organized, manifest)


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("dataset_artifact_id", _id("9")),
        ("dataset_sha256", "9" * 64),
        ("support_artifact_id", _id("8")),
        ("support_sha256", "8" * 64),
        ("normalized_support_sha256", "7" * 64),
        ("normalized_support_sha256", None),
        ("kind", DependencyKind.MAPPING),
        ("sensitivity", Sensitivity.NON_CONFIDENTIAL),
        ("rulebook_sha256", "6" * 64),
        ("scrub_config_sha256", "5" * 64),
        ("support_role_sha256", "4" * 64),
    ],
)
def test_every_declared_decision_basis_and_identity_field_is_stale(
    field: str,
    replacement: object,
) -> None:
    from phi_engine.pipeline.dependencies import dependency_decision_is_current

    rec = _recommendation(
        recommendation_id=_recommendation_id("1"),
        dataset_id=_id("1"),
        dataset_sha="1" * 64,
        support_id=_id("2"),
        support_sha="2" * 64,
        normalized_sha="3" * 64,
        level=DependencyLevel.REQUIRED,
    )
    dec = _decision(rec)
    values = dict(dec.__dict__)
    if field in {"rulebook_sha256", "scrub_config_sha256", "support_role_sha256"}:
        basis_values = dict(dec.basis.__dict__)
        basis_values[field] = replacement
        values["basis"] = DependencyDecisionBasis(**basis_values)
    else:
        values[field] = replacement
    stale = DependencyDecision(**values)

    assert dependency_decision_is_current(dec, rec)
    assert not dependency_decision_is_current(stale, rec)


def test_required_holds_only_its_dataset_helpful_reviews_and_exact_ignored_suppresses() -> None:
    from phi_engine.pipeline.run import _evaluate_dependency_state

    required = _recommendation(
        recommendation_id=_recommendation_id("1"),
        dataset_id=_id("1"),
        dataset_sha="1" * 64,
        support_id=_id("3"),
        support_sha="3" * 64,
        normalized_sha="4" * 64,
        level=DependencyLevel.REQUIRED,
    )
    helpful = _recommendation(
        recommendation_id=_recommendation_id("2"),
        dataset_id=_id("2"),
        dataset_sha="2" * 64,
        support_id=_id("3"),
        support_sha="3" * 64,
        normalized_sha="4" * 64,
        level=DependencyLevel.HELPFUL,
    )
    support = ParsedSupportArtifact(
        artifact_id=_id("3"),
        source_sha256="3" * 64,
        kind=DependencyKind.DICTIONARY,
        format="csv",
        parse_status=SupportParseStatus.PARSED,
        normalized_rows_path=Path("support.jsonl"),
        normalized_rows_sha256="4" * 64,
        failure_code=None,
    )

    pending = _evaluate_dependency_state((required, helpful), (), (support,))
    assert pending.held_dataset_ids == frozenset({_id("1")})
    assert pending.review_recommendation_ids == frozenset(
        {_recommendation_id("1"), _recommendation_id("2")}
    )

    stale_helpful = DependencyDecision(
        **{
            **_decision(helpful).__dict__,
            "support_sha256": "9" * 64,
        }
    )
    ignored_required = _decision(required, level=DependencyLevel.IGNORED)
    resolved = _evaluate_dependency_state(
        (required, helpful),
        (ignored_required, stale_helpful),
        (support,),
    )
    assert resolved.held_dataset_ids == frozenset()
    assert resolved.review_recommendation_ids == frozenset({_recommendation_id("2")})

    swapped_support = ParsedSupportArtifact(
        **{**support.__dict__, "source_sha256": "8" * 64}
    )
    byte_swapped = _evaluate_dependency_state(
        (required,),
        (ignored_required,),
        (swapped_support,),
    )
    assert byte_swapped.held_dataset_ids == frozenset({_id("1")})
    assert byte_swapped.review_recommendation_ids == frozenset({_recommendation_id("1")})

def test_exact_required_decision_with_expected_support_still_missing_holds() -> None:
    from phi_engine.pipeline.run import _evaluate_dependency_state

    recommendation = DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=_recommendation_id("8"),
        dataset_artifact_id=_id("1"),
        dataset_sha256="1" * 64,
        support_artifact_id=None,
        support_sha256=None,
        normalized_support_sha256=None,
        kind=DependencyKind.MAPPING,
        suggested_level=DependencyLevel.REQUIRED,
        default_sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING,
        header_ids=(_header("a"),),
        matched_rule_ids=(),
        transform_requirement_ids=("tr_" + "8" * 32,),
        basis=DependencyDecisionBasis(
            rulebook_sha256="a" * 64,
            scrub_config_sha256="b" * 64,
            support_role_sha256=support_role_sha256(
                recommendation_id=_recommendation_id("8"),
                dataset_artifact_id=_id("1"),
                support_artifact_id=None,
                kind=DependencyKind.MAPPING,
                role_source=RoleSource.INFERRED,
                organizer_role_version=1,
            ),
        ),
    )
    state = _evaluate_dependency_state(
        (recommendation,),
        (_decision(recommendation, level=DependencyLevel.REQUIRED),),
        (),
    )

    assert state.held_dataset_ids == frozenset({_id("1")})
    assert state.review_recommendation_ids == frozenset(
        {recommendation.recommendation_id}
    )



def test_helpful_failed_support_omits_evidence_without_holding_dataset() -> None:
    from phi_engine.pipeline.run import _evaluate_dependency_state

    helpful = _recommendation(
        recommendation_id=_recommendation_id("7"),
        dataset_id=_id("1"),
        dataset_sha="1" * 64,
        support_id=_id("3"),
        support_sha="3" * 64,
        normalized_sha="4" * 64,
        level=DependencyLevel.HELPFUL,
    )
    helpful = DependencyRecommendation(
        **{**helpful.__dict__, "normalized_support_sha256": None}
    )
    failed = ParsedSupportArtifact(
        artifact_id=_id("3"),
        source_sha256="3" * 64,
        kind=DependencyKind.DICTIONARY,
        format="csv",
        parse_status=SupportParseStatus.FAILED,
        normalized_rows_path=None,
        normalized_rows_sha256=None,
        failure_code=SupportFailureCode.PARSE_ERROR,
    )
    state = _evaluate_dependency_state(
        (helpful,),
        (_decision(helpful, level=DependencyLevel.HELPFUL),),
        (failed,),
    )
    assert state.held_dataset_ids == frozenset()
    assert state.review_recommendation_ids == frozenset(
        {helpful.recommendation_id}
    )

def test_support_byte_change_restores_confidential_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as dependency_contracts
    from phi_engine.pipeline.dependencies import (
        recommendation_identity,
        recommend_dependencies,
    )

    organized, manifest, first_id, _second_id, support_id = _organized_alias_fixture(tmp_path)
    hydrated = _hydrate_dependency_inputs_for_test(organized, manifest)
    dataset = hydrated.datasets[0]
    support = hydrated.support_artifacts[0]
    monkeypatch.setattr(
        dependency_contracts,
        "_effective_scrub_config_sha256",
        lambda: "b" * 64,
    )
    recommendation_id = recommendation_identity(
        dataset_artifact_id=first_id,
        support_artifact_id=support_id,
        kind=DependencyKind.DICTIONARY,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        header_ids=(),
        transform_requirement_ids=(),
    )
    decision = DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id=_decision_id("6"),
        recommendation_id=recommendation_id,
        dataset_artifact_id=first_id,
        dataset_sha256=dataset.source_sha256,
        support_artifact_id=support_id,
        support_sha256=support.source_sha256,
        normalized_support_sha256=support.normalized_rows_sha256,
        kind=DependencyKind.DICTIONARY,
        level=DependencyLevel.HELPFUL,
        sensitivity=Sensitivity.NON_CONFIDENTIAL,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        basis=DependencyDecisionBasis(
            rulebook_sha256="a" * 64,
            scrub_config_sha256="b" * 64,
            support_role_sha256=support_role_sha256(
                recommendation_id=recommendation_id,
                dataset_artifact_id=first_id,
                support_artifact_id=support_id,
                kind=DependencyKind.DICTIONARY,
                role_source=RoleSource.MANIFEST,
                organizer_role_version=1,
            ),
        ),
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )
    current = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(support,),
        published_raw_headers_by_dataset={first_id: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(decision,),
        rule_bundle=SimpleNamespace(rules_sha256="a" * 64),
    )
    current_manifest = next(
        rec
        for rec in current
        if rec.reason_code is DependencyReasonCode.MANIFEST_DECLARED
    )
    assert current_manifest.default_sensitivity is Sensitivity.NON_CONFIDENTIAL

    swapped = ParsedSupportArtifact(
        **{**support.__dict__, "source_sha256": "9" * 64}
    )
    stale = recommend_dependencies(
        datasets=(dataset,),
        support_artifacts=(swapped,),
        published_raw_headers_by_dataset={first_id: frozenset()},
        transform_requirements_by_dataset={},
        confirmed_links=(decision,),
        rule_bundle=SimpleNamespace(rules_sha256="a" * 64),
    )
    stale_manifest = next(
        rec
        for rec in stale
        if rec.reason_code is DependencyReasonCode.MANIFEST_DECLARED
    )
    assert stale_manifest.default_sensitivity is Sensitivity.CONFIDENTIAL





def test_ignored_decision_is_dataset_scoped_for_alias_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as dependency_contracts
    from phi_engine.pipeline.dependencies import recommend_dependencies

    organized, manifest, first_id, second_id, _support_id = _organized_alias_fixture(tmp_path)
    hydrated = _hydrate_dependency_inputs_for_test(organized, manifest)
    support = hydrated.support_artifacts[0]
    assert support.normalized_rows_path is not None
    rows = [
        {
            "support_artifact_id": support.artifact_id,
            "source_sha256": support.source_sha256,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": index,
            "cells": [{"column_index": 0, "value": value}],
        }
        for index, value in enumerate(("PRIVATE-ALPHA", "PRIVATE-BETA"))
    ]
    support.normalized_rows_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    support = ParsedSupportArtifact(
        **{
            **support.__dict__,
            "normalized_rows_sha256": _sha(
                support.normalized_rows_path.read_bytes()
            ),
        }
    )
    monkeypatch.setattr(
        dependency_contracts,
        "_effective_scrub_config_sha256",
        lambda: "b" * 64,
    )
    raw_ids = {
        dataset.artifact_id: frozenset(
            header.header_id for header in dataset.headers
        )
        for dataset in hydrated.datasets
    }
    first_pass = recommend_dependencies(
        datasets=hydrated.datasets,
        support_artifacts=(support,),
        published_raw_headers_by_dataset=raw_ids,
        transform_requirements_by_dataset={},
        confirmed_links=(),
        rule_bundle=SimpleNamespace(rules_sha256="a" * 64),
    )
    first_rec = next(
        recommendation
        for recommendation in first_pass
        if recommendation.dataset_artifact_id == first_id
        and recommendation.reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
    )
    ignored = _decision(first_rec, level=DependencyLevel.IGNORED)
    second_pass = recommend_dependencies(
        datasets=hydrated.datasets,
        support_artifacts=(support,),
        published_raw_headers_by_dataset=raw_ids,
        transform_requirements_by_dataset={},
        confirmed_links=(ignored,),
        rule_bundle=SimpleNamespace(rules_sha256="a" * 64),
    )

    assert not any(
        recommendation.dataset_artifact_id == first_id
        and recommendation.reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
        for recommendation in second_pass
    )
    assert any(
        recommendation.dataset_artifact_id == second_id
        and recommendation.reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
        for recommendation in second_pass
    )


def test_private_context_canaries_never_enter_ordinary_records_or_review_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.config.config as config
    from phi_engine.pipeline.review import list_review_items
    from phi_engine.pipeline.run import (
        _build_private_dependency_recommendations,
        _hydrate_dependency_inputs,
    )

    organized, manifest, first_id, _second_id, support_id = _organized_alias_fixture(tmp_path)
    hydrated = _hydrate_dependency_inputs(organized, manifest)
    support = hydrated.support_artifacts[0]
    assert support.normalized_rows_sha256 is not None
    rec = _recommendation(
        recommendation_id=_recommendation_id("1"),
        dataset_id=first_id,
        dataset_sha=hydrated.datasets[0].source_sha256,
        support_id=support_id,
        support_sha=support.source_sha256,
        normalized_sha=support.normalized_rows_sha256,
        level=DependencyLevel.HELPFUL,
    )
    rec = DependencyRecommendation(
        **{
            **rec.__dict__,
            "header_ids": (hydrated.datasets[0].headers[0].header_id,),
        }
    )
    private = _build_private_dependency_recommendations((rec,), hydrated)
    assert private == (
        PrivateDependencyRecommendation(
            schema_version="dependency-recommendation-private/v1",
            recommendation_id=rec.recommendation_id,
            dataset_artifact_id=first_id,
            dataset_path="datasets/alpha.csv",
            support_artifact_id=support_id,
            support_path="data_dictionary/private.csv",
            raw_header_names=("PRIVATE-ALPHA",),
            role_source=RoleSource.INFERRED,
            organizer_role_version=1,
            basis=rec.basis,
        ),
    )

    output = tmp_path / "output" / "Study"
    run_dir = output / "runs" / "20260714T100000Z"
    write_dependency_recommendations(
        run_dir=run_dir,
        recommendations=(rec,),
        private_records=private,
    )
    monkeypatch.setattr(config, "STUDY_OUTPUT_DIR", output)
    monkeypatch.setattr(config, "STUDY_AUDIT_DIR", tmp_path / "audit")
    ordinary_text = (run_dir / "dependency_recommendations.jsonl").read_text(encoding="utf-8")
    assert "PRIVATE-ALPHA" not in ordinary_text
    assert "private.csv" not in ordinary_text
    private_text = (
        run_dir / ".protected" / "dependency_recommendations.jsonl"
    ).read_text(encoding="utf-8")
    assert "PRIVATE-ALPHA" in private_text
    assert "private.csv" in private_text

    listed = list_review_items("Study")
    assert listed["dependency_recommendations"] == [rec.to_json()]
    serialized = json.dumps(listed, sort_keys=True)
    assert "PRIVATE-ALPHA" not in serialized
    assert "private.csv" not in serialized
