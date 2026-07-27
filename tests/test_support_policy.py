"""Support -> scrub-parameter continuity (the dictionary/mapping product gap).

Covers the deterministic map builder (eligibility, code->label extraction,
fail-closed on ambiguity/conflict, floor preservation), the strengthen-only
signal application, the fail-soft signal producer, and the end-to-end proof that
an overlaid map reaches the synthesized scrub config and actually rewrites values.

Support fixtures are produced through the REAL ``parse_support_artifact`` (on a
temp CSV) so the normalized-row shape can never drift from what the pipeline
actually feeds the builder — a data dictionary is a ``variable, code, label``
table whose ``variable`` column names the dataset column (that value match is
what yields the EXACT_HEADER_MATCH link); the parser strips the CSV header row.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import phi_engine.config.config as config
from phi_engine.pipeline.dependencies import (
    ORGANIZER_ROLE_VERSION,
    DependencyDecision,
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    OrganizedDataset,
    OrganizedHeader,
    ParsedSupportArtifact,
    RoleSource,
    Sensitivity,
    recommendation_identity,
    support_role_sha256,
)
from phi_engine.pipeline.support_files import parse_support_artifact
from phi_engine.pipeline.support_policy import (
    apply_support_signal_actions,
    build_transform_maps_from_support,
    extract_support_signals,
)
from phi_engine.pipeline.synthesize_config import synthesize_study_config
from phi_engine.security import phi_scrub
from phi_engine.security.phi_review import Action, HeaderClassification

_DS = "a_" + "1" * 32
_SUP = "a_" + "2" * 32
_HID = "h_" + "a" * 24
_DS_SHA = "a" * 64
_NORM_SHA = "d" * 64
_RULE_SHA = "e" * 64
_SCRUB_SHA = "f" * 64


def _header(raw: str = "CODE") -> OrganizedHeader:
    return OrganizedHeader(
        header_id=_HID, column_index=0, raw_name=raw, normalized_name=raw.lower()
    )


def _dataset() -> OrganizedDataset:
    return OrganizedDataset(
        artifact_id=_DS,
        source_sha256=_DS_SHA,
        normalized_rows_path=Path("/dev/null"),
        normalized_rows_sha256=_NORM_SHA,
        headers=(_header(),),
    )


def _parsed_support(
    tmp_path: Path, table, *, variable: str = "CODE", artifact_id: str = _SUP
) -> ParsedSupportArtifact:
    lines = ["variable,code,label"] + [f"{variable},{code},{label}" for code, label in table]
    src = tmp_path / f"dict_{artifact_id[-4:]}.csv"
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    parsed = parse_support_artifact(
        artifact_id=artifact_id,
        source_sha256=sha,
        kind=DependencyKind.DICTIONARY_MAPPING,
        source_path=src,
        output_dir=tmp_path / f"out_{artifact_id[-4:]}",
        limits=None,
    )
    return parsed


def _recommendation(
    support: ParsedSupportArtifact,
    *,
    level: DependencyLevel = DependencyLevel.HELPFUL,
    reason: DependencyReasonCode = DependencyReasonCode.EXACT_HEADER_MATCH,
    header_id: str = _HID,
) -> DependencyRecommendation:
    recommendation_id = recommendation_identity(
        dataset_artifact_id=_DS,
        support_artifact_id=support.artifact_id,
        kind=DependencyKind.DICTIONARY_MAPPING,
        reason_code=reason,
        header_ids=(header_id,),
        transform_requirement_ids=(),
    )
    basis = DependencyDecisionBasis(
        rulebook_sha256=_RULE_SHA,
        scrub_config_sha256=_SCRUB_SHA,
        support_role_sha256=support_role_sha256(
            recommendation_id=recommendation_id,
            dataset_artifact_id=_DS,
            support_artifact_id=support.artifact_id,
            kind=DependencyKind.DICTIONARY_MAPPING,
            role_source=RoleSource.INFERRED,
            organizer_role_version=ORGANIZER_ROLE_VERSION,
        ),
    )
    return DependencyRecommendation(
        schema_version="dependency-recommendation/v2",
        recommendation_id=recommendation_id,
        dataset_artifact_id=_DS,
        dataset_sha256=_DS_SHA,
        support_artifact_id=support.artifact_id,
        support_sha256=support.source_sha256,
        normalized_support_sha256=support.normalized_rows_sha256,
        kind=DependencyKind.DICTIONARY_MAPPING,
        suggested_level=level,
        default_sensitivity=Sensitivity.CONFIDENTIAL,
        reason_code=reason,
        header_ids=(header_id,),
        matched_rule_ids=(),
        transform_requirement_ids=(),
        basis=basis,
    )


def _decision(rec: DependencyRecommendation, level: DependencyLevel) -> DependencyDecision:
    return DependencyDecision(
        schema_version="dependency-decision/v2",
        decision_id="dd_" + "3" * 32,
        recommendation_id=rec.recommendation_id,
        dataset_artifact_id=rec.dataset_artifact_id,
        dataset_sha256=rec.dataset_sha256,
        support_artifact_id=rec.support_artifact_id,
        support_sha256=rec.support_sha256,
        normalized_support_sha256=rec.normalized_support_sha256,
        kind=rec.kind,
        level=level,
        sensitivity=rec.default_sensitivity,
        reason_code=rec.reason_code,
        basis=rec.basis,
        decided_by="reviewer",
        decided_at="2026-07-14T10:00:00Z",
    )


def _generalize_cls(header: str = "CODE") -> tuple[HeaderClassification, ...]:
    return (
        HeaderClassification(
            header=header,
            action=Action.GENERALIZE,
            matched_rules=(),
            jurisdictions=("USA",),
            reasons=("test",),
        ),
    )


def _build(support, rec, *, decisions=(), classifications=None):
    return build_transform_maps_from_support(
        datasets=(_dataset(),),
        support_artifacts=(support,),
        recommendations=(rec,),
        decisions=decisions,
        classifications_by_dataset={
            _DS: _generalize_cls() if classifications is None else classifications
        },
    )


def test_auto_helpful_exact_header_builds_map(tmp_path):
    support = _parsed_support(tmp_path, [("A", "Low"), ("B", "Mid"), ("C", "High")])
    result = _build(support, _recommendation(support, level=DependencyLevel.HELPFUL))
    assert result.generalization_maps == {
        "_synth_generalize_code": {"A": "Low", "B": "Mid", "C": "High"}
    }
    assert _HID in result.applied_header_ids
    assert result.provenance and result.provenance[0].entry_count == 3


def test_required_without_decision_not_applied(tmp_path):
    support = _parsed_support(tmp_path, [("A", "Low")])
    result = _build(support, _recommendation(support, level=DependencyLevel.REQUIRED))
    assert result.generalization_maps == {}
    assert not result.applied_header_ids


def test_required_with_current_decision_applied(tmp_path):
    support = _parsed_support(tmp_path, [("A", "Low"), ("B", "Mid")])
    rec = _recommendation(support, level=DependencyLevel.REQUIRED)
    result = _build(support, rec, decisions=(_decision(rec, DependencyLevel.REQUIRED),))
    assert result.generalization_maps == {"_synth_generalize_code": {"A": "Low", "B": "Mid"}}


def test_support_cannot_fill_non_generalize_header(tmp_path):
    # A DROP-classified header is never given a generalize map — support strengthens
    # existing GENERALIZE columns only, it never promotes or weakens the floor.
    support = _parsed_support(tmp_path, [("A", "Low")])
    drop_cls = (
        HeaderClassification(
            header="CODE", action=Action.DROP, matched_rules=(), jurisdictions=("USA",), reasons=(),
        ),
    )
    result = _build(
        support, _recommendation(support, level=DependencyLevel.HELPFUL), classifications=drop_cls
    )
    assert result.generalization_maps == {}


def test_unlinkable_table_is_skipped(tmp_path):
    # The dictionary's variable column names OTHER, not CODE -> no linking column
    # for CODE -> fail-closed (empty, quarantine).
    support = _parsed_support(tmp_path, [("A", "Low")], variable="OTHER")
    result = _build(support, _recommendation(support, level=DependencyLevel.HELPFUL))
    assert result.generalization_maps == {}


def test_inconsistent_taxonomy_fails_closed(tmp_path):
    # Same code A maps to two different labels within the dictionary -> fail-closed.
    support = _parsed_support(tmp_path, [("A", "Low"), ("A", "High")])
    result = _build(support, _recommendation(support, level=DependencyLevel.HELPFUL))
    assert result.generalization_maps == {}


def test_conflicting_tables_drop_to_empty(tmp_path):
    # Two eligible links fill the same synth map name with DIFFERENT tables ->
    # the map is left empty (fail-closed) rather than a silent last-wins.
    support_a = _parsed_support(tmp_path, [("A", "Low")], artifact_id="a_" + "2" * 32)
    support_b = _parsed_support(tmp_path, [("A", "DIFFERENT")], artifact_id="a_" + "7" * 32)
    result = build_transform_maps_from_support(
        datasets=(_dataset(),),
        support_artifacts=(support_a, support_b),
        recommendations=(
            _recommendation(support_a, level=DependencyLevel.HELPFUL),
            _recommendation(support_b, level=DependencyLevel.HELPFUL),
        ),
        decisions=(),
        classifications_by_dataset={_DS: _generalize_cls()},
    )
    assert result.generalization_maps == {}


class _Signal:
    def __init__(self, header_id, action, matched_rule_id):
        self.header_id = header_id
        self.action = action
        self.matched_rule_id = matched_rule_id


def test_signal_strengthens_only_with_known_rule():
    cls = (
        HeaderClassification(header="X", action=Action.KEEP, matched_rules=(), jurisdictions=("USA",), reasons=()),
    )
    upgraded = apply_support_signal_actions(
        cls,
        (_Signal(_HID, Action.DROP, "rule1"),),
        header_name_by_id={_HID: "X"},
        candidate_rule_ids=frozenset({"rule1"}),
    )
    assert upgraded[0].action is Action.DROP


def test_signal_never_weakens():
    cls = (
        HeaderClassification(header="X", action=Action.DROP, matched_rules=(), jurisdictions=("USA",), reasons=()),
    )
    unchanged = apply_support_signal_actions(
        cls,
        (_Signal(_HID, Action.KEEP, "rule1"),),
        header_name_by_id={_HID: "X"},
        candidate_rule_ids=frozenset({"rule1"}),
    )
    assert unchanged[0].action is Action.DROP


def test_signal_with_unknown_rule_ignored():
    cls = (
        HeaderClassification(header="X", action=Action.KEEP, matched_rules=(), jurisdictions=("USA",), reasons=()),
    )
    unchanged = apply_support_signal_actions(
        cls,
        (_Signal(_HID, Action.DROP, "rogue"),),
        header_name_by_id={_HID: "X"},
        candidate_rule_ids=frozenset({"rule1"}),
    )
    assert unchanged[0].action is Action.KEEP


def test_producer_fails_soft_without_local_model(tmp_path):
    # A current CONFIDENTIAL exact-header decision exists, but the default local
    # model is offline -> the producer returns no signals and never raises.
    support = _parsed_support(tmp_path, [("A", "Low")])
    rec = _recommendation(support, level=DependencyLevel.HELPFUL)

    class _Bundle:
        rules = ()

    signals = extract_support_signals(
        datasets=(_dataset(),),
        support_artifacts=(support,),
        recommendations=(rec,),
        decisions=(_decision(rec, DependencyLevel.HELPFUL),),
        rule_bundle=_Bundle(),
    )
    assert signals == ()


def test_overlay_map_reaches_scrub_config_and_rewrites_values(tmp_path, monkeypatch):
    study = "SupportPolicyStudy"
    monkeypatch.setattr(config, "_STUDY_CONFIG_ROOT", tmp_path / "config")
    overlay = {"_synth_generalize_code": {"A": "Low", "B": "Mid", "C": "High"}}
    out_path = synthesize_study_config(
        study, "USA", _generalize_cls(), generalization_map_overlay=overlay
    )
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["generalization_maps"]["_synth_generalize_code"] == overlay["_synth_generalize_code"]

    cfg = phi_scrub.load_scrub_config(study=study)
    rule = cfg.generalize_rule_for("CODE")
    assert rule is not None
    assert phi_scrub.generalize_value("A", mapping=rule.mapping) == ("Low", True)
    assert phi_scrub.generalize_value("Z", mapping=rule.mapping) == ("Z", False)
